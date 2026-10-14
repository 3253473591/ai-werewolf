#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 狼人杀 Web 服务 —— 浏览器里看对局。

只用标准库（http.server + SSE），不需要装任何依赖。

    python server.py                 # 默认 http://127.0.0.1:8099
    python server.py --port 9000
    python server.py --api-key sk-xxx

页面功能：
  · 牌桌视图：每个座位一张卡，上帝视角显示真实身份与存活状态
  · 发言流：大桌 / 小桌 / 私密 / 上帝 四种通道分色
  · 实时推流：SSE，模型一边说一边出
  · 谁来思考：正在调用的座位会亮起

注意：本服务与 DSH 的 Web GUI（127.0.0.1:3080）是两个独立进程，互不影响。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from aiwolf.engine import Game  # noqa: E402
from aiwolf.endpoints import EndpointRegistry  # noqa: E402
from aiwolf.gate import GameAborted, GameGate  # noqa: E402
from aiwolf.relay import Relay  # noqa: E402
from aiwolf.render import phase_cn  # noqa: E402
from aiwolf.audit import audit_prompts  # noqa: E402
from aiwolf.summary import build_summary, count_parse_failures, write_summary  # noqa: E402
from aiwolf.seats import (  # noqa: E402
    GAME_TUNABLES,
    PLAYER_COUNTS,
    build_seats,
    load_config,
    resolve_seed,
    role_summary,
    update_game_cfg,
    update_seat_def,
    wants_shuffle,
)

DEFAULT_CONFIG = os.path.join(ROOT, "config", "seats.json")
ENDPOINTS_PATH = os.path.join(ROOT, "config", "endpoints.json")
STATIC_DIR = os.path.join(ROOT, "static")

# 多通道登记表。落盘在 config/endpoints.json，重启后自动读回 ——
# 不用每次运行都重填 base_url + key。
try:
    REGISTRY = EndpointRegistry(ENDPOINTS_PATH, fallback=load_config(DEFAULT_CONFIG))
except Exception as _e:                       # 配置坏了也不能起不来
    print(f"⚠️ 通道配置载入失败（{_e}），退化成单通道")
    REGISTRY = EndpointRegistry(ENDPOINTS_PATH, fallback=None)


# --------------------------------------------------------------------------- #
# 推流中枢
# --------------------------------------------------------------------------- #

class Hub:
    """极简发布订阅。每个浏览器连接一个 Queue。"""

    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, payload: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # 浏览器跟不上的时候丢弃，不能拖慢对局


HUB = Hub()

# 对局闸门：暂停 / 继续 / 单步 / 强制结束。
# **每局一个独立实例** —— 共用的话，旧局被 abort 后闸门会被新局重置成未中止，
# 那个卡在 HTTP 里的旧线程返回后会继续往下跑，变成僵尸对局。
GATE_STATE: dict = {"ref": None}


def new_gate() -> GameGate:
    gate = GameGate(on_state=lambda payload: HUB.publish(payload))
    GATE_STATE["ref"] = gate
    return gate


def current_gate() -> GameGate:
    if GATE_STATE["ref"] is None:
        new_gate()
    return GATE_STATE["ref"]


current_gate()   # 启动时先建一个，供空转时查询

# 每局一个"取消"事件。置位后，在途的 curl 子进程会被立刻杀掉——
# 这是"强制结束"能马上生效的关键（否则要等那个请求自己超时）。
CANCEL_STATE: dict = {"ref": threading.Event()}


def new_cancel() -> threading.Event:
    ev = threading.Event()
    CANCEL_STATE["ref"] = ev
    return ev


def abort_everything() -> None:
    """强制结束当前对局：置位取消事件（杀掉在途请求）+ 让闸门抛异常。"""
    CANCEL_STATE["ref"].set()
    current_gate().abort()


def refresh_models_async(eid: Optional[str] = None) -> None:
    """后台拉模型清单，别把 HTTP 响应堵住。

    启动时和新增/修改通道后都调一次，用户不用手点「拉取模型」。
    启动时只刷"清单为空或太旧"的通道 —— 已经落过盘的直接用手上的，
    免得每次重启都等一遍网络。
    """
    def work():
        try:
            if eid:
                REGISTRY.refresh_models(eid)
            else:
                stale = [e.id for e in REGISTRY.stale_endpoints()]
                if stale:
                    print(f"  刷新 {len(stale)} 条通道的模型清单：{', '.join(stale)}")
                    for s in stale:
                        REGISTRY.refresh_models(s)
        except Exception as e:
            print(f"⚠️ 拉取模型失败：{e}")
        finally:
            HUB.publish({"type": "endpoints", "endpoints": REGISTRY.snapshot()})
    threading.Thread(target=work, daemon=True).start()

STATE_LOCK = threading.Lock()
STATE: dict = {
    "status": "idle",          # idle | running | finished
    "seats": [],
    "events": [],
    "result": None,
    "started_at": None,
    "settings": {},
}


def snapshot() -> dict:
    with STATE_LOCK:
        data = json.loads(json.dumps(STATE, ensure_ascii=False))
    data["gate"] = current_gate().snapshot()
    data["relay"] = RELAY_STATE["ref"].inflight_snapshot() if RELAY_STATE.get("ref") else {
        "inflight": 0, "label": ""}
    data["endpoints"] = REGISTRY.snapshot()
    return data


# 当前对局的 Relay 实例（用于暴露飞行中请求状态）
RELAY_STATE: dict = {"ref": None}


def _set(**kw) -> None:
    with STATE_LOCK:
        STATE.update(kw)


def _gamecfg() -> dict:
    """前端要显示/编辑的「代打 / 超时」参数。"""
    cfg = load_config(DEFAULT_CONFIG)
    game = cfg.get("game", {}) or {}
    out = {"fallback_enabled": bool(cfg.get("fallback_enabled", False))}
    for key in ("request_timeout_s", "max_attempts_per_model",
                "call_deadline_s", "switch_model_on_timeout"):
        out[key] = game.get(key)
    return out


def _push_event(ev) -> None:
    payload = {
        "type": "event",
        "seq": ev.seq,
        "day": ev.day,
        "phase": ev.phase,
        "phase_cn": phase_cn(ev.phase),
        "channel": ev.channel.value,
        "kind": ev.kind.value,
        "actor": ev.actor,
        "to_seat": ev.to_seat,
        "text": ev.text,
        "data": ev.data,
    }
    with STATE_LOCK:
        STATE["events"].append(payload)
    HUB.publish(payload)


def _refresh_alive(game: Game) -> None:
    """把存活状态同步到 STATE，供牌桌视图使用。"""
    with STATE_LOCK:
        by_no = {s["no"]: s for s in STATE["seats"]}
        for seat in game.seats:
            if seat.no in by_no:
                by_no[seat.no]["alive"] = seat.alive
        seats = json.loads(json.dumps(STATE["seats"], ensure_ascii=False))
    HUB.publish({"type": "seats", "seats": seats})


# --------------------------------------------------------------------------- #
# 对局线程
# --------------------------------------------------------------------------- #

class GameRunner(threading.Thread):
    def __init__(self, seats, stub: bool, max_days: int, api_key: str,
                 win_condition: str, gate: GameGate, cancel: threading.Event,
                 seed: int, shuffle: bool) -> None:
        super().__init__(daemon=True)
        self.seats = seats
        self.stub = stub
        self.max_days = max_days
        self.api_key = api_key
        self.win_condition = win_condition
        self.gate = gate
        self.cancel = cancel
        self.seed = seed
        self.shuffle = shuffle
        self.count = len(seats)
        # 落盘用：正常结束**和**被强制结束都要留下数据
        self.run_dir = ""
        self.game = None
        self.relay = None

    def _dump(self, winner: str) -> None:
        """写 events.jsonl + summary.json。

        以前这两份产物是在 `game.run()` **返回之后**才写的，于是「⏹ 结束」
        （抛 GameAborted）那一局什么都不留 —— 而那恰恰是最想复盘的时候。
        """
        if not self.game or not self.run_dir:
            return
        try:
            self.game.log.to_jsonl(os.path.join(self.run_dir, "events.jsonl"))
            write_summary(self.run_dir, build_summary(
                seats=self.seats, relay=self.relay, winner=winner,
                days=self.game.state.day, run_dir=self.run_dir, stub=self.stub,
                seed=self.seed, shuffled=self.shuffle,
                audit=audit_prompts(self.run_dir, self.game.log, self.seats),
                extra={"max_days": self.max_days, "win_condition": self.win_condition,
                       "aborted": winner == "aborted",
                       "parse_failures": count_parse_failures(self.game.log)},
            ))
        except Exception as e:                        # noqa: BLE001
            print(f"  ⚠ 落盘产物失败：{type(e).__name__}: {e}")

    def run(self) -> None:
        try:
            self._run()
        except GameAborted:
            calls = len(self.relay.stats) if self.relay else 0
            result = {"winner": "aborted", "winner_cn": "已强制结束",
                      "days": (self.game.state.day if self.game else 0),
                      "calls": calls,
                      "failed": sum(1 for s in (self.relay.stats if self.relay else [])
                                    if s.get("error")),
                      "run_dir": self.run_dir}
            _set(status="finished", result=result)
            HUB.publish({"type": "result", "result": result})
            self._dump("aborted")                     # 半局也要留证据
        except Exception as e:  # 任何异常都要让页面看到，不能静默卡在 running
            _set(status="finished", result={"winner": "error", "error": f"{type(e).__name__}: {e}"})
            HUB.publish({"type": "error", "message": f"{type(e).__name__}: {e}"})
            self._dump("error")

    def _run(self) -> None:
        cfg = load_config(DEFAULT_CONFIG)
        game_cfg = cfg.get("game", {})
        seats = self.seats
        seed = self.seed
        shuffle = self.shuffle
        win_condition = game_cfg.get("win_condition", "parity")

        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.join(ROOT, "runs", f"{stamp}_{self.count}seat_web")
        os.makedirs(run_dir, exist_ok=True)
        self.run_dir = run_dir          # 提前记下：被强制结束时也要能找到它

        relay = Relay(cfg, api_key=self.api_key or None, stub=self.stub,
                      log_dir=run_dir, verbose=False, registry=REGISTRY)
        relay.cancel_event = self.cancel      # 强制结束时可杀掉在途请求
        RELAY_STATE["ref"] = relay
        self.relay = relay

        seat_view = [{
            "no": s.no, "label": s.label, "model": s.model,
            "role": s.role, "role_cn": s.role_cn, "alive": True,
            "is_wolf": s.is_wolf, "cache_style": s.cache_style,
            "endpoint": s.endpoint,
        } for s in seats]

        _set(status="running", seats=seat_view, events=[], result=None,
             started_at=time.time(),
             settings={"seats": self.count, "stub": self.stub,
                       "win_condition": win_condition,
                       "roles": role_summary(self.count),
                       "role_seed": seed, "shuffled": shuffle,
                       "run_dir": run_dir})
        # ★ 新局的第一条消息必须把**顶栏要用的字段**一起带上：`started_at`（用时）
        #   与 `relay.calls`（API 调用数）。
        #
        # 原来只发 seats + settings，于是：
        #   · 用时 —— `started_at` 只在**页面加载时**那条 snapshot 里出现过一次。
        #     开了页面再点「开始」，中间的 start 消息永远不带它，
        #     `elapsedText(null, now)` 直接返回 '00:00'，整局都是 00:00。
        #   · 调用数 —— 同理，之后能刷新 relay 的只有 thinking 事件，
        #     而它只带 inflight/label（见 static/app/sse.js）。
        # 现在把 snapshot() 里的这两项直接带上；snapshot() 只调一次。
        snap = snapshot()
        HUB.publish({"type": "start", "seats": seat_view,
                     "settings": snap["settings"],
                     "started_at": snap["started_at"],
                     "relay": snap["relay"],
                     "gate": snap["gate"]})

        # ★ 「谁在生成」必须推给前端。
        # 原来 Game() 没传 on_status，于是 engine 的 thinking 回调永远是 None：
        # 页面在两次发言之间（一次调用 30~240 秒）拿不到任何反馈，
        # 看起来就像卡死了 —— 实测有一局 4 号卡了 6.5 分钟，前端一个字节都没收到。
        def on_status(payload) -> None:
            if isinstance(payload, dict) and payload.get("type"):
                HUB.publish(payload)

        game = Game(seats, relay, max_days=self.max_days, verbose=False,
                    win_condition=win_condition, seed=seed, gate=self.gate,
                    on_status=on_status,
                    sheriff=cfg.get("game", {}).get("sheriff_enabled", True))
        self.game = game
        self.win_condition = win_condition

        ALIVE_REFRESH_KINDS = {"resolution"}

        def on_event(ev) -> None:
            _push_event(ev)
            if ev.kind.value in ALIVE_REFRESH_KINDS:
                _refresh_alive(game)

        game.log.on_append = on_event

        winner = game.run()
        _refresh_alive(game)

        result = {
            "winner": winner,
            "winner_cn": {"wolf": "狼人阵营", "good": "好人阵营",
                          "draw": "平局"}.get(winner, winner),
            "days": game.state.day,
            "calls": len(relay.stats),
            "failed": sum(1 for s in relay.stats if s.get("error")),
            "roles": {s.no: s.role_cn for s in seats},
            "run_dir": run_dir,
        }
        _set(status="finished", result=result)
        HUB.publish({"type": "result", "result": result})
        # ★ Web 路径以前只写 events.jsonl，不写 summary.json ——
        # 而 M2 的验收标准是"每座的缓存命中率被记录进 summary"。
        # 现在两条路径共用 aiwolf/summary.py，CLI 与浏览器出的报告字段一致。
        self._dump(winner)
        if self.gate is current_gate():
            self.gate.resume()   # 对局正常结束，别让闸门卡着


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """客户端半路断开时**不要打一整页 traceback**。

    实测：页面一刷新 / 关标签，控制台就刷出

        ConnectionAbortedError: [WinError 10053] 你的主机中的软件中止了一个已建立的连接。

    那是 socketserver 在 `handle_one_request()` 读请求行时对方已经走了 ——
    **既不是服务端出错，也不是崩溃**，而且 `log_message` 早就被静音了，
    偏偏这条是从 `handle_error()` 走的，绕过了静音。

    危害不是"多几行字"：真正的问题（比如某个端点连不上）会被这堆红字淹掉。
    这和引擎里那条"静默缺人必须留痕"是同一个道理的反面 ——
    **无关紧要的噪声不该长得像事故**。
    """

    daemon_threads = True
    # 这些异常都是"对方先走了"，属于正常网络事件
    _CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError,
                    BrokenPipeError, ConnectionError)

    def handle_error(self, request, client_address) -> None:
        import sys
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, self._CLIENT_GONE):
            return                      # 对方挂了，不用报
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AIWolf/1.0"

    def log_message(self, fmt, *args) -> None:  # 静音，别刷屏
        pass

    def handle_one_request(self) -> None:
        # 双保险：HTTP/1.1 keep-alive 下，浏览器刷新会让这条连接在
        # 读请求行时被 RST。在最近的边界上兜住，免得一路冒到 socketserver。
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True
        except OSError as e:
            # WinError 10053/10054 在 Python 里可能以裸 OSError 出来
            if getattr(e, "winerror", None) in (10053, 10054, 10058):
                self.close_connection = True
                return
            raise

    # ---------------- 工具 ----------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ---------------- GET ----------------

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path.startswith("/app/") or path.startswith("/vendor/"):
            return self._serve_static(path.lstrip("/"))
        if path == "/app.css":
            return self._serve_static("app.css")
        if path == "/api/state":
            return self._json(snapshot())
        if path == "/api/endpoints":
            return self._json({"endpoints": REGISTRY.snapshot(),
                               "assignment": REGISTRY.assignment})
        if path == "/api/catalog":
            return self._json({"catalog": REGISTRY.model_catalog()})
        if path == "/api/seats":
            cfg = load_config(DEFAULT_CONFIG)
            # player_counts 交给前端渲染"人数"分段选择器 —— 以前前端把 [3..10]
            # 写死在 drawer.js 里，加上 11/12 人板之后就选不出来了。
            # 事实源是 aiwolf/seats.py 的 ROLE_SETS。
            return self._json({"seats": cfg.get("seats", []), "gamecfg": _gamecfg(),
                               "player_counts": PLAYER_COUNTS,
                               "seat_defs": len(cfg.get("seats", []))})
        if path == "/events":
            return self._sse()
        return self._json({"error": "not found"}, 404)

    # 静态资源：只允许 static/ 下的白名单扩展名，且路径必须落在 static/ 内
    STATIC_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".webp": "image/webp",
        ".woff2": "font/woff2",
    }

    def _serve_static(self, name: str) -> None:
        rel = name.replace("\\", "/").lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        # 目录穿越：/app/../../config/endpoints.json 之类必须挡住
        if os.path.commonpath([full, STATIC_DIR]) != STATIC_DIR or not os.path.isfile(full):
            return self._json({"error": f"缺少 {rel}"}, 404)
        ctype = self.STATIC_TYPES.get(os.path.splitext(full)[1].lower())
        if ctype is None:
            return self._json({"error": f"不支持的静态类型：{rel}"}, 404)
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = HUB.subscribe()
        try:
            # 先补一份当前快照，页面刷新后不会丢历史
            self._write_sse({"type": "snapshot", **snapshot()})
            while True:
                try:
                    payload = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._write_sse(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)

    def _write_sse(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # ---------------- POST ----------------

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)

        if path == "/api/start":
            return self._start(body)
        if path == "/api/pause":
            return self._json({"ok": True, "gate": current_gate().pause()})
        if path == "/api/resume":
            return self._json({"ok": True, "gate": current_gate().resume()})
        if path == "/api/step":
            return self._json({"ok": True, "gate": current_gate().step(int(body.get("n") or 1))})
        if path == "/api/abort":
            abort_everything()
            return self._json({"ok": True, "gate": current_gate().snapshot()})

        # ---------------- 多通道管理（全部落盘，重启不丢） ----------------

        if path == "/api/endpoints":
            try:
                ep = REGISTRY.upsert(body)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 400)
            # 填完 base_url + key 就自动去拉这个通道的模型清单
            if ep.base_url and not ep.models:
                refresh_models_async(ep.id)
            HUB.publish({"type": "endpoints", "endpoints": REGISTRY.snapshot()})
            return self._json({"ok": True, "endpoint": ep.mask(),
                               "endpoints": REGISTRY.snapshot()})

        if path == "/api/endpoints/delete":
            eid = str(body.get("id") or "")
            ok = REGISTRY.remove(eid)
            HUB.publish({"type": "endpoints", "endpoints": REGISTRY.snapshot()})
            return self._json({"ok": ok, "endpoints": REGISTRY.snapshot()})

        if path == "/api/endpoints/refresh":
            eid = str(body.get("id") or "")
            if eid:
                r = REGISTRY.refresh_models(eid)
            else:
                r = {"ok": True, "results": REGISTRY.refresh_all(),
                     "count": sum(len(e.models) for e in REGISTRY.list())}
            HUB.publish({"type": "endpoints", "endpoints": REGISTRY.snapshot()})
            r["endpoints"] = REGISTRY.snapshot()
            return self._json(r)

        if path == "/api/seats/model":
            no = int(body.get("no") or 0)
            model = body.get("model")
            endpoint = body.get("endpoint")
            if not no or not model:
                return self._json({"error": "需要 no 与 model"}, 400)
            try:
                definition = update_seat_def(DEFAULT_CONFIG, no, model=model,
                                             endpoint=endpoint or None)
            except KeyError as e:
                return self._json({"error": str(e)}, 404)
            return self._json({"ok": True, "seat": definition})

        if path == "/api/seats/update":
            no = int(body.get("no") or 0)
            if not no:
                return self._json({"error": "需要 no"}, 400)
            fields = {}
            if "label" in body and str(body["label"]).strip():
                fields["label"] = str(body["label"]).strip()
            if "endpoint" in body:
                # 空串 = 清掉绑定，回到"自动轮转"
                fields["endpoint"] = str(body["endpoint"]).strip() or None
            if "fallback" in body:
                # 前端给一个字符串或字符串数组；`模型@通道` 由 seats.parse_fallback 解释
                raw = body["fallback"]
                items = raw if isinstance(raw, list) else str(raw).split(",")
                fields["fallback"] = [str(x).strip() for x in items if str(x).strip()]
            if not fields:
                return self._json({"error": "没有要改的字段"}, 400)
            try:
                definition = update_seat_def(DEFAULT_CONFIG, no, **fields)
            except KeyError as e:
                return self._json({"error": str(e)}, 404)
            return self._json({"ok": True, "seat": definition})

        if path == "/api/gamecfg":
            # 代打 / 超时这几项，改完**下一局生效**（config 在开局时才读）
            try:
                changed = update_game_cfg(DEFAULT_CONFIG, **{
                    k: v for k, v in body.items() if k in GAME_TUNABLES})
            except (KeyError, ValueError) as e:
                return self._json({"error": str(e)}, 400)
            if not changed:
                return self._json({"error": "没有可写的字段"}, 400)
            return self._json({"ok": True, "changed": changed, "gamecfg": _gamecfg()})

        return self._json({"error": "not found"}, 404)

    def _start(self, body: dict) -> None:
        with STATE_LOCK:
            running = STATE["status"] == "running"

        # 上一局还活着就先强制结束它，否则暂停中的局会把新局永久挡住
        if running:
            abort_everything()
            for _ in range(100):                      # 最多等 10 秒
                with STATE_LOCK:
                    if STATE["status"] != "running":
                        break
                time.sleep(0.1)
            with STATE_LOCK:
                if STATE["status"] == "running":
                    return self._json({
                        "error": "上一局没能停下来，请稍后再试或重启服务"
                    }, 409)

        cfg = load_config(DEFAULT_CONFIG)
        count = int(body.get("seats") or 5)
        stub = bool(body.get("stub", False))
        max_days = int(body.get("max_days") or 4)
        api_key = self.server.api_key  # type: ignore[attr-defined]

        if count not in PLAYER_COUNTS:
            return self._json({"error": f"座位数只能是 {'/'.join(map(str, PLAYER_COUNTS))}"}, 400)

        # key 现在主要存在 endpoints.json 里（每条通道一把），
        # 只有退化成单通道（seats.json 的 relay 段）时才依赖 --api-key / 环境变量
        has_endpoint_key = any(e.resolve_key() for e in REGISTRY.enabled())
        if not stub and not api_key and not has_endpoint_key:
            return self._json({
                "error": "没有可用的 API Key。到「⚙ 配置 → 端点池」给通道填 key，"
                         "或用 --api-key 启动服务，或勾上离线模式。"
            }, 400)

        # 座位在这里就建好，开局前先体检 —— 宁可现在报错，
        # 也不要跑起来才发现某个座位整局哑火
        shuffle = wants_shuffle(cfg)
        seed = resolve_seed(cfg)
        try:
            seats = build_seats(cfg, count, shuffle=shuffle, seed=seed, registry=REGISTRY)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)

        if not stub:
            problems = REGISTRY.check_bindings(seats)
            if problems:
                detail = "；".join(
                    f"{p['no']}号 {p['model']} → {p['endpoint']}（{p['reason']}）"
                    for p in problems)
                return self._json({
                    "error": "有座位的模型和它绑定的通道对不上，开局会整局哑火：" + detail
                             + "。到「座位分配」里改端点或模型，或把端点改成「（自动轮转）」。",
                    "problems": problems,
                }, 400)

        gate = new_gate()          # ★ 新局用全新的闸门
        cancel = new_cancel()      # ★ 新局用全新的取消事件
        GameRunner(seats, stub, max_days, api_key,
                   cfg.get("game", {}).get("win_condition", "parity"),
                   gate, cancel, seed, shuffle).start()
        return self._json({"ok": True, "seats": [{"no": s.no, "label": s.label,
                                                  "model": s.model, "endpoint": s.endpoint}
                                                 for s in seats]})


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 狼人杀 Web 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--api-key", default=os.environ.get("RELAY_API_KEY", ""))
    args = ap.parse_args()

    httpd = QuietThreadingHTTPServer((args.host, args.port), Handler)
    httpd.api_key = args.api_key  # type: ignore[attr-defined]

    url = f"http://{args.host}:{args.port}"
    print("=" * 66)
    print("AI 狼人杀 Web 服务已启动")
    print(f"  打开：{url}")
    eps = REGISTRY.list()
    if eps:
        print(f"  通道（{len(eps)} 条，已从 config/endpoints.json 读回）：")
        for e in eps:
            key = e.resolve_key()
            masked = (key[:4] + "***" + key[-3:]) if len(key) > 6 else ("已配置" if key else "无 key")
            print(f"    · {e.id:<6} {e.name or '':<12} {e.base_url}  并发={e.concurrency}  key={masked}")
    else:
        print("  通道：无（请到页面的「⚙ 通道设置」里填 base_url + api_key）")
    usable = sum(1 for e in eps if e.enabled and e.resolve_key())
    if not usable and not args.api_key:
        print("  ⚠ 没有任何通道带 key —— 只能跑离线模式")
    print("  Ctrl+C 停止")
    print("=" * 66)

    refresh_models_async()      # 启动就把模型清单拉回来，不用手点
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
