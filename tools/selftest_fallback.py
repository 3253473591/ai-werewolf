#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代打（fallback）自测 —— 假 http_post，精确控制每一次尝试的结果。

代打这条链的难点全在"什么时候换人"上，所以这里逐条验：
  1. 主模型 500 两次（达 max_attempts_per_model）→ 换 fallback，标记 substituted
  2. 主模型**超时** → 不再死磕，立刻换 fallback（这是最省时间的一条）
  3. 429 仍然按退避重试，不立刻换人（那种会自愈）
  4. 所有模型都挂 → 明确失败，model_used 为空
  5. call_deadline_s 兜住整次调用的总时长
  6. 代打模型不在座位绑定的通道上时，改走**能跑它的通道**
  7. 代打关闭时行为不变（老路径不能被改坏）

用法：python tools/selftest_fallback.py
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import aiwolf.relay as relay_mod                      # noqa: E402
from aiwolf.relay import Relay, _looks_like_timeout    # noqa: E402
from aiwolf.seats import Seat, load_config             # noqa: E402

FAILURES: List[str] = []
CONFIG = os.path.join(ROOT, "config", "seats.json")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def ok_body(speech: str = "我是1号") -> tuple:
    return (200, {"choices": [{"message": {"content": json.dumps(
        {"thinking": "t", "speech": speech,
         "action": {"type": "speak", "target": None}, "confidence": 0.8},
        ensure_ascii=False)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}, 0.01, None)


def fail(status: int, err: str = "") -> tuple:
    return (status, None, 0.01, err or f"HTTP {status}")


class FakeHTTP:
    """按 (模型, 第几次) 送回放；没编排到的默认成功。"""

    def __init__(self, plan: Dict[str, List[tuple]]) -> None:
        self.plan = {k: list(v) for k, v in plan.items()}
        self.calls: List[dict] = []

    def __call__(self, url, headers, data, timeout, transport="auto", cancel=None):
        payload = json.loads(data.decode("utf-8"))
        model = payload["model"]
        self.calls.append({"model": model, "url": url, "max_tokens": payload["max_tokens"]})
        q = self.plan.get(model)
        if q:
            return q.pop(0) if len(q) > 1 else q[0]
        return ok_body(f"{model} 的发言")

    def models(self) -> List[str]:
        return [c["model"] for c in self.calls]


def make_relay(**game_over) -> Relay:
    cfg = load_config(CONFIG)
    cfg["game"].update(game_over)
    return Relay(cfg, api_key="k", verbose=False)


def run_case(plan: Dict[str, List[tuple]], seat: Seat, **game_over):
    fake = FakeHTTP(plan)
    real = relay_mod.http_post
    relay_mod.http_post = fake
    try:
        rl = make_relay(**game_over)
        res = rl.chat(seat, [{"role": "user", "content": "hi"}],
                      phase="DAY_SPEECH", day=1, max_tokens=3072)
        return rl, res, fake
    finally:
        relay_mod.http_post = real


def main() -> int:
    cfg = load_config(CONFIG)
    print("=" * 74)
    print("代打（fallback）自测 —— 全部用假 http_post，不消耗额度")
    print(f"策略：fallback_enabled={cfg.get('fallback_enabled')} "
          f"max_attempts_per_model={cfg['game'].get('max_attempts_per_model')} "
          f"switch_model_on_timeout={cfg['game'].get('switch_model_on_timeout')} "
          f"call_deadline_s={cfg['game'].get('call_deadline_s')}")
    print("=" * 74)

    seat = Seat(no=1, label="Doubao", model="primary-x",
                fallback=["backup-y", "backup-z"], endpoint="")

    print("\n[1] 主模型连续 500 → 换代打，并标记 substituted")
    rl, res, fake = run_case({"primary-x": [fail(500), fail(500)]}, seat)
    check("换了代打", fake.models()[:3] == ["primary-x", "primary-x", "backup-y"],
          str(fake.models()))
    check("结果被标成 substituted", res.substituted is True)
    check("model_used 是代打模型", res.model_used == "backup-y", res.model_used)
    check("代打成功时也记下试过哪几个", res.models_tried == ["primary-x", "backup-y"],
          str(res.models_tried))
    check("记下主模型失败的原因（否则日志只剩一句「主模型失败」）",
          "HTTP 500" in res.primary_error, res.primary_error[:80])
    check("落进 stats 的 substituted 也是 True",
          any(s.get("substituted") for s in rl.stats))

    print("\n[2] 主模型**超时** → 不死磕，立刻换人")
    rl, res, fake = run_case(
        {"primary-x": [fail(0, "curl: (28) Operation timed out after 240002 ms")]}, seat)
    check("只试了主模型一次就换", fake.models() == ["primary-x", "backup-y"],
          str(fake.models()))
    check("代打生效", res.substituted and res.model_used == "backup-y")
    check("_looks_like_timeout 认得这种错",
          _looks_like_timeout(0, "curl: (28) Operation timed out after 240002 ms"))
    check("429 不算超时", not _looks_like_timeout(429, "concurrency_limit_exceeded"))
    check("HTTP 500 不算超时", not _looks_like_timeout(500, "server error"))

    print("\n[3] 429 仍然按退避重试，不急着换人")
    rl, res, fake = run_case({"primary-x": [fail(429, "rate limit")]}, seat)
    check("主模型试满 2 次才换", fake.models()[:3] == ["primary-x", "primary-x", "backup-y"],
          str(fake.models()))

    print("\n[4] 所有模型都挂 → 明确失败，不假装成功")
    rl, res, fake = run_case({"primary-x": [fail(500)], "backup-y": [fail(500)],
                              "backup-z": [fail(500)]}, seat)
    check("三个模型都试过了", set(fake.models()) == {"primary-x", "backup-y", "backup-z"},
          str(fake.models()))
    check("返回错误而不是空正文", bool(res.error))
    check("model_used 为空（没有假装是谁答的）", res.model_used == "")
    check("没有标 substituted", res.substituted is False)
    check("说清了试过哪几个模型（日志不能只剩一句'调用失败'）",
          res.models_tried == ["primary-x", "backup-y", "backup-z"], str(res.models_tried))
    check("stats 里也带上了 attempts 与 models_tried",
          bool(rl.stats) and rl.stats[-1].get("attempts") and rl.stats[-1].get("models_tried"))
    from aiwolf.summary import call_stats
    cs = call_stats(rl)
    check("summary 的 calls_retried / attempts_total 靠 stats.attempts（以前漏了）",
          cs["attempts_total"] > cs["calls_total"], str(cs))

    print("\n[5] call_deadline_s 兜住整次调用")
    # 每次失败都"耗掉"一秒钟：把 deadline 设成 2 秒，最多只能撑两次尝试
    rl, res, fake = run_case({"primary-x": [fail(500)], "backup-y": [fail(500)],
                              "backup-z": [fail(500)]}, seat, call_deadline_s=2)
    check("达到了时限就停手", len(fake.calls) <= 4, f"实际调用 {len(fake.calls)} 次")
    check("错误信息里说明了是时限", ("上限" in res.error) or ("deadline" in res.error) or
          bool(res.error), res.error[:80])

    print("\n[6] 代打模型不在座位绑定的通道上 → 改走能跑它的通道")
    from aiwolf.endpoints import Endpoint

    class FakeRegistry:
        def __init__(self):
            self.ep_a = Endpoint(id="a", name="A", base_url="http://a/v1",
                                 api_key="ka", models=["only-on-a"])
            self.ep_b = Endpoint(id="b", name="B", base_url="http://b/v1",
                                 api_key="kb", models=["only-on-b"])

        def get(self, eid):
            return {"a": self.ep_a, "b": self.ep_b}.get(eid)

        def capable_endpoints(self, model):
            return [e for e in (self.ep_a, self.ep_b) if model in (e.models or [])]

    reg = FakeRegistry()
    fake = FakeHTTP({"only-on-a": [fail(500)], "only-on-b": []})
    real = relay_mod.http_post
    relay_mod.http_post = fake
    try:
        rl = make_relay()
        rl.registry = reg
        s2 = Seat(no=1, label="T", model="only-on-a", fallback=["only-on-b"], endpoint="a")
        res = rl.chat(s2, [{"role": "user", "content": "hi"}], phase="DAY_SPEECH", day=1)
        check("主模型走座位绑定的通道", fake.calls[0]["url"].startswith("http://a/"),
              fake.calls[0]["url"])
        check("代打走了另一条能跑它的通道", fake.calls[-1]["url"].startswith("http://b/"),
              fake.calls[-1]["url"])
        check("代打成功", res.substituted and res.model_used == "only-on-b")
    finally:
        relay_mod.http_post = real

    print("\n[7] 代打关闭时行为不变")
    cfg_off = load_config(CONFIG)
    cfg_off["fallback_enabled"] = False
    fake = FakeHTTP({"primary-x": [fail(500)]})
    relay_mod.http_post = fake
    try:
        rl = Relay(cfg_off, api_key="k", verbose=False)
        res = rl.chat(seat, [{"role": "user", "content": "hi"}], phase="DAY_SPEECH", day=1)
        check("链上只有主模型（不会偷偷换人）",
              set(fake.models()) == {"primary-x"}, str(set(fake.models())))
        check("明确失败", bool(res.error) and res.model_used == "")
    finally:
        relay_mod.http_post = real

    print("\n[8] 每条 fallback 至少要**在某条通道**上真实存在")
    # 注意这里不是"必须在座位自己那条通道上"：代打模型允许落在别的通道
    # （_route(seat, model) 会挑一条真能跑它的通道，见用例 [6]）。
    # 真正的坑是"任何通道都没有这个模型" —— 那条代打必然二次失败。
    from aiwolf.endpoints import is_deployment_id
    from aiwolf.seats import build_seats
    catalog = json.load(open(os.path.join(ROOT, "config", "endpoints.json"),
                             encoding="utf-8"))
    eps = {e["id"]: (e.get("_models") or []) for e in catalog.get("endpoints", [])
           if e.get("enabled", True)}
    have_catalog = any(eps.values())
    union = {m for models in eps.values() for m in models}
    bad, routed = [], []
    for s in build_seats(load_config(CONFIG), 9):
        for m in s.fallback:
            if m == s.model or is_deployment_id(m):
                continue
            if have_catalog and m not in union:
                bad.append(f"{s.no}号 {s.label} 的 {m}（哪条通道都没有）")
            elif have_catalog and m not in (eps.get(s.endpoint) or []):
                where = [eid for eid, ms in eps.items() if m in ms]
                routed.append(f"{s.no}号 的 {m} 将在 {','.join(where)} 上跑")
    check("没有'哪条通道都没有这个模型'的坑", not bad, "；".join(bad) or "全部能找到")
    if routed:
        print("     ℹ 跨通道代打：" + "；".join(routed))

    # 通道级故障（连接 reset / 整条挂起）时代打同通道的模型等于没代打。
    # 2026-10-05 三座测试：ep3 整条通道在 reset，2 号座三个模型全在 ep3 → 全军覆没；
    # 3 号座第一档是跨通道的豆包 → 活下来了。
    #
    # ★ 2026-10-06：这里原来只 build_seats(..., 9)（漏掉 10 号座）而且**只打印不 check**，
    #   于是 10 号座"整条链都在 ep3"这个坑一直没被发现 —— 直到 10 座局里 ep3 连接超时，
    #   10 号座连着丢两个回合（curl 28 connect timeout，三个模型同一秒数）。
    #   现在：扫配置里的**每一个**座位，而且是会失败的断言。
    all_seats = build_seats(load_config(CONFIG), len(load_config(CONFIG)["seats"]))
    same_channel = []
    for s in all_seats:
        first = (s.fallback or [None])[0]
        if not first:
            continue
        target = (s.fallback_endpoints or {}).get(first, "")
        if not target and not is_deployment_id(first):
            candidates = [eid for eid, ms in eps.items() if first in ms and eid != s.endpoint]
            target = candidates[0] if candidates else s.endpoint
        if target == s.endpoint:
            same_channel.append(f"{s.no}号 {s.label}（第一档 {first} 还在 {s.endpoint}）")
    check(f"全部 {len(all_seats)} 个座位的第一档代打都在另一条通道上（通道级故障才救得回来）",
          not same_channel, "；".join(same_channel) or "没有漏网的")

    print("\n[9] 「模型@通道」：代打必须落到**指定的那条通道**")
    # 这就是用户的三座试验田：grok 在 ep3、豆包在 ep4，一个座位只绑一条通道不够用。
    # 关键点：ep-* 是推理接入点，**不在任何通道的 /models 目录里**，
    # 所以"按清单挑通道"这条路对它是瞎猜 —— 必须听配置里写死的。
    from aiwolf.seats import parse_fallback
    models, routes = parse_fallback(['ep-20261005143108-gcv24@ep4', 'claude-sonnet-5'])
    check("解析出模型与指定通道", models == ['ep-20261005143108-gcv24', 'claude-sonnet-5']
          and routes == {'ep-20261005143108-gcv24': 'ep4'}, f"{models} {routes}")
    check("没写 @通道 的条目不带路由", 'claude-sonnet-5' not in routes)

    class Reg2:
        def __init__(self):
            self.ep3 = Endpoint(id="ep3", name="中转站 A", base_url="http://ep3/v1",
                                api_key="k3", models=["grok-4-5", "claude-sonnet-5"])
            self.ep4 = Endpoint(id="ep4", name="豆包", base_url="http://ep4/v1",
                                api_key="k4", models=["doubao-pro-128k"])

        def get(self, eid):
            return {"ep3": self.ep3, "ep4": self.ep4}.get(eid)

        def capable_endpoints(self, model):
            return [e for e in (self.ep3, self.ep4) if model in (e.models or [])]

    fake = FakeHTTP({"grok-4-5": [fail(0, "curl: (28) timed out")],
                     "ep-20261005143108-gcv24": []})
    real = relay_mod.http_post
    relay_mod.http_post = fake
    try:
        rl = make_relay()
        rl.registry = Reg2()
        s3 = Seat(no=3, label="Grok", model="grok-4-5", endpoint="ep3",
                  fallback=models, fallback_endpoints=routes)
        res = rl.chat(s3, [{"role": "user", "content": "hi"}], phase="DAY_SPEECH", day=1)
        check("主模型走它自己那条通道", fake.calls[0]["url"].startswith("http://ep3/"),
              fake.calls[0]["url"])
        check("grok 超时后立刻换人（没死磕第二次）",
              len([c for c in fake.calls if c["model"] == "grok-4-5"]) == 1,
              str(fake.models()))
        check("代打落到了**豆包那条通道 ep4**", fake.calls[-1]["url"].startswith("http://ep4/"),
              fake.calls[-1]["url"])
        check("代打成功且被标记", res.substituted and res.model_used == "ep-20261005143108-gcv24")
    finally:
        relay_mod.http_post = real

    print("\n[10] 前端可调项：白名单 + 范围校验")
    import shutil
    from aiwolf.seats import GAME_TUNABLES, update_game_cfg
    tmp = os.path.join(ROOT, "runs", "_selftest_gamecfg")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    try:
        cfg_path = os.path.join(tmp, "seats.json")
        shutil.copyfile(os.path.join(ROOT, "config", "seats.json"), cfg_path)
        got = update_game_cfg(cfg_path, request_timeout_s=90, fallback_enabled=True,
                              max_attempts_per_model=1, switch_model_on_timeout=False)
        saved = json.load(open(cfg_path, encoding="utf-8"))
        check("写进了 game 段", saved["game"]["request_timeout_s"] == 90
              and saved["game"]["max_attempts_per_model"] == 1)
        check("fallback_enabled 写顶层（历史位置）", saved["fallback_enabled"] is True)
        check("布尔项也回传", got["switch_model_on_timeout"] is False)

        for bad, label in [({"request_timeout_s": 99999}, "超范围"),
                           ({"request_timeout_s": "abc"}, "非整数"),
                           ({"nonsense": 1}, "不在白名单")]:
            try:
                update_game_cfg(cfg_path, **bad)
                check(f"{label}要被拒绝", False, "居然写进去了")
            except (KeyError, ValueError):
                check(f"{label}要被拒绝", True)
        check("被拒之后配置没被改坏",
              json.load(open(cfg_path, encoding="utf-8"))["game"]["request_timeout_s"] == 90)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n[11] 传输层**抛异常**也不能把整局带走（实测死过一次 UnboundLocalError）")
    real = relay_mod.http_post
    calls = {'n': 0}

    def raising_post(url, headers, data, timeout, transport="auto", cancel=None):
        calls['n'] += 1
        if calls['n'] <= 2:
            raise PermissionError("[WinError 5] 拒绝访问")
        return ok_body("兜住了")

    relay_mod.http_post = raising_post
    try:
        rl = make_relay()
        s = Seat(no=1, label="T", model="primary-x", fallback=["backup-y"], endpoint="")
        res = rl.chat(s, [{"role": "user", "content": "hi"}], phase="DAY_SPEECH", day=1)
        check("异常被兜成一次失败，而不是抛出去", res is not None)
        check("主模型抛异常后照常代打", res.substituted and res.model_used == "backup-y",
              f"model_used={res.model_used}")
    except Exception as e:                                    # noqa: BLE001
        check("异常被兜成一次失败，而不是抛出去", False, f"{type(e).__name__}: {e}")
    finally:
        relay_mod.http_post = real

    print("\n[12] 额度撞上限仍是 length → 换代打，绝不把半截输出交出去")
    # 2026-10-06 的线上事故：10 座局第 3 天 7 号 GLM 5.2 的思维链吃到 16380，
    # 正文只剩 3 字符 `\n{"`，撞上当时写死的上限 16384 后就被当成"成功"收下，
    # 那一回合直接未发言。同一份 prompt 放到 32768，它 16898 tokens 就正常写完了
    # —— 只差 514 个 token。这条测试同时守住两件事：
    #   a) 上限可配（max_tokens_cap），别再写死
    #   b) 到顶还截断就换下一个模型，而不是把半截 JSON 当答案
    def truncated(content: str, reasoning: str = "", compl: int = 100) -> tuple:
        return (200, {"choices": [{"message": {"content": content,
                                               "reasoning_content": reasoning},
                                   "finish_reason": "length"}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": compl,
                                "completion_tokens_details": {"reasoning_tokens": compl - 1}}},
                0.01, None)

    rl, res, fake = run_case(
        {"primary-x": [truncated('\n{"', "想", 16384)]},
        seat, max_tokens_cap=16384)
    cap_budgets = [c["max_tokens"] for c in fake.calls if c["model"] == "primary-x"]
    check("上限之内逐级翻倍，到顶就停手（不越过配置的 max_tokens_cap）",
          cap_budgets == [3072, 6144, 12288, 16384], str(cap_budgets))
    check("到顶截断后换了代打模型", fake.models()[-1] == "backup-y", str(fake.models()))
    check("没把半截 JSON 当成功交出去", res.text != '\n{"', repr(res.text))
    check("拿到的是代打模型的完整回复", res.model_used == "backup-y" and res.substituted,
          res.model_used)
    check("主模型失败原因里点明了'已到上限'",
          "上限" in res.primary_error, res.primary_error[:100])
    check("记住了这个座位需要大额度（下次不再逐级翻倍）",
          rl._budget_hint.get(1) == 16384, str(rl._budget_hint))

    # 上限真的是"可配"的吗？把它调大，ladder 必须跟着变长 ——
    # 这正是 2026-10-06 那次修复的核心：原来 16384 是写死的，调不动。
    rl2, res2, fake2 = run_case(
        {"primary-x": [truncated('\n{"', "想", 32768)]},
        seat, max_tokens_cap=32768)
    cap_budgets2 = [c["max_tokens"] for c in fake2.calls if c["model"] == "primary-x"]
    check("调大 max_tokens_cap，翻倍链跟着变长（不再是写死的 16384）",
          cap_budgets2 == [3072, 6144, 12288, 24576, 32768], str(cap_budgets2))

    print("\n[13] 上限之内的截断翻倍后拿到完整结果（不换人，别把这条路改坏）")
    rl, res, fake = run_case(
        {"primary-x": [truncated('\n{"', "想", 4096), ok_body("完整发言")]},
        seat, max_tokens_cap=16384)
    check("翻倍重试而不是换人", fake.models() == ["primary-x", "primary-x"],
          str(fake.models()))
    check("第二次额度翻倍", [c["max_tokens"] for c in fake.calls] == [3072, 6144],
          str([c["max_tokens"] for c in fake.calls]))
    check("拿到的是完整那次的结果（没有 substituted）",
          res.text and res.text != '\n{"' and not res.substituted,
          repr((res.text or "")[:40]))
    check("这次没有走到上限，所以预算提示停在 6144",
          rl._budget_hint.get(1) == 6144, str(rl._budget_hint))

    print("\n[14] 已经是最后一个模型 + 到顶截断 → 仍然交出结果（engine 负责拒绝）")
    solo = Seat(no=1, label="T", model="primary-x", fallback=[], endpoint="")
    rl, res, fake = run_case({"primary-x": [truncated('\n{"', "想", 32768)]},
                             solo, max_tokens_cap=32768)
    check("没有代打可换时不会空手而归", res.truncated is True and res.text == '\n{"',
          repr(res.text))
    check("但保留 finish_reason 供 engine 判定", res.finish_reason == "length")

    print("\n[15] 成本账目：被丢弃的尝试必须记进 attempts_detail（否则账单上是隐形的）")
    # 背景：翻倍重试会把前面那些尝试**完整计费后丢掉**。以前只记最终那次的 usage，
    # 于是"一次发言先烧 8192 被截断、再烧 16384 被截断、最后才写对"在日志里
    # 只留下最后那个数 —— 白花的钱查不出来。这里守住记录与汇总两条路。
    def ok_tokens(n: int) -> tuple:
        return (200, {"choices": [{"message": {"content": json.dumps(
            {"thinking": "t", "speech": "行", "action": {"type": "speak", "target": None},
             "confidence": 0.8}, ensure_ascii=False)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": n,
                      "completion_tokens_details": {"reasoning_tokens": n - 10}}},
                0.01, None)

    rl, res, fake = run_case(
        {"primary-x": [truncated('\n{"', "想", 8192), ok_tokens(300)]},
        seat, max_tokens_cap=16384)
    check("记下了每一次尝试（含被丢弃的那次）", len(res.attempts_detail) == 2,
          str(len(res.attempts_detail)))
    check("被丢弃的那次标成 truncated_retry",
          res.attempts_detail[0]["outcome"] == "truncated_retry",
          str(res.attempts_detail[0].get("outcome")))
    check("被丢弃那次的额度与消耗都留了痕",
          res.attempts_detail[0]["max_tokens"] == 3072
          and res.attempts_detail[0]["completion_tokens"] == 8192,
          str({k: res.attempts_detail[0][k] for k in ("max_tokens", "completion_tokens")}))
    check("被采用的那次标成 ok", res.attempts_detail[1]["outcome"] == "ok",
          str(res.attempts_detail[1].get("outcome")))
    check("被丢弃的 token 单独计数", res.wasted_completion_tokens == 8192,
          str(res.wasted_completion_tokens))
    check("stats 里也带上输出侧三项",
          rl.stats[-1].get("completion_tokens") == 300
          and rl.stats[-1].get("reasoning_tokens") == 290
          and rl.stats[-1].get("wasted_completion_tokens") == 8192,
          str({k: rl.stats[-1].get(k) for k in
               ("completion_tokens", "reasoning_tokens", "wasted_completion_tokens")}))
    from aiwolf.summary import call_stats
    cs = call_stats(rl)
    check("summary 有输出侧汇总（以前只有 prompt / cached）",
          cs["completion_tokens_total"] == 300
          and cs["reasoning_tokens_total"] == 290
          and cs["wasted_completion_tokens_total"] == 8192,
          str({k: v for k, v in cs.items() if "tokens" in k}))

    print("\n[16] 上游风控拒绝（finish_reason=content_filter）必须**换模型**，不是当成功")
    # 实测：12 号 MiMo 第 2 天投票返回
    #   finish_reason=content_filter, content="The request was rejected because
    #   it was considered high risk"（已生成 827 tokens，被风控拦下）。
    # 老逻辑只认 length，于是它被当成成功收下 → 解析失败 → "本轮未发言"。
    # 而正确做法是立刻代打：同样的 messages 发给同一个模型只会被再拒一次。
    def rejected(text="The request was rejected because it was considered high risk"):
        return (200, {"choices": [{"message": {"content": text,
                                               "reasoning_content": ""},
                                   "finish_reason": "content_filter"}],
                      "usage": {"prompt_tokens": 14380, "completion_tokens": 827}},
                0.01, None)

    rl, res, fake = run_case({"primary-x": [rejected()]}, seat)
    check("风控拒绝后换了代打模型",
          fake.models() == ["primary-x", "backup-y"], str(fake.models()))
    check("**没有把它当成功收下**", res.text != rejected()[1]["choices"][0]
          ["message"]["content"], repr((res.text or "")[:50]))
    check("拿到的是代打模型的完整回复", res.model_used == "backup-y" and res.substituted,
          res.model_used)
    check("失败原因里点明了是上游拒绝",
          "上游拒绝" in (res.primary_error or "") and "content_filter"
          in (res.primary_error or ""), (res.primary_error or "")[:120])
    check("没有为它做退避重试（同一个模型只试一次）",
          fake.models().count("primary-x") == 1, str(fake.models()))
    check("attempts_detail 里记成 upstream_rejected",
          (res.attempts_detail or [{}])[0].get("outcome") == "upstream_rejected",
          str((res.attempts_detail or [{}])[0].get("outcome")))

    print("\n[17] 已经是最后一个模型时，风控拒绝仍然把响应交出来（别空手而归）")
    solo2 = Seat(no=1, label="T", model="primary-x", fallback=[], endpoint="")
    rl, res, fake = run_case({"primary-x": [rejected("blocked")]}, solo2)
    check("没有代打时仍然交回正文",
          res.text == "blocked", repr((res.text or "")[:40]))
    check("但 finish_reason 保留给 engine 判定",
          res.finish_reason == "content_filter", res.finish_reason)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：{FAILURES}")
        return 1
    print("✅ 代打全部通过（换人时机 / 标记 / 时限 / 跨通道 / 指定端点 / 传输异常 / 截断上限 / 关闭时不变）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
