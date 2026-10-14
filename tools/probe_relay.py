#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中转站探针 — AI 狼人杀项目的前置可行性验证

设计文档见 ../DESIGN.md 第 9 节「开放问题」。本脚本回答其中 4 个：

  T1  基本 OpenAI 兼容性          —— 中转站能不能用
  T2  ★ 前缀缓存是否生效          —— 决定"全量原文 + 长上下文"的成本与延迟能否压住
  T3  JSON 输出模式               —— 动作解析走 json_object 还是降级到 tool call / 提示词
  T4  并发上限 / 429 行为         —— 决定 M2 的限流参数
  T5  上下文上限（可选，耗额度）  —— 决定大桌能全量喂到多少轮

用法（Windows）:
    set RELAY_BASE_URL=https://your-relay/v1
    set RELAY_API_KEY=sk-xxx
    python probe_relay.py

用法（Linux/macOS）:
    export RELAY_BASE_URL=https://your-relay/v1
    export RELAY_API_KEY=sk-xxx
    python probe_relay.py

常用参数:
    --models a,b,c            指定要测的模型（默认取 /models 或内置的九座清单）
    --prefix-tokens 8000      T2 的公共前缀长度（估算值）
    --concurrency 8           T4 的并发数
    --timeout 120             单请求超时秒数
    --context-probe           打开 T5（会真实消耗额度，默认关闭）
    --context-tokens 100000   T5 的目标提示长度
    --out report.json         报告落盘路径

网络说明：
    默认 RELAY_HTTP=auto —— 先直连，连接层失败自动回退到 curl。
    实测某些沙箱/代理环境下 Python 的 SSL 握手会被拦，而 curl 能通，
    所以脚本会把请求体写临时文件、用 curl --data-binary 发出去。
    强制指定：set RELAY_HTTP=curl  或  set RELAY_HTTP=urllib

只依赖标准库。
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

# 视频里的九座清单，仅作为默认探测目标（实际以你的中转站 /models 为准）
DEFAULT_MODELS = [
    "gpt-6-sol",
    "claude-opus-5.5",
    "gemini-3.8-flash",
    "deepseek-v4.1-flash",
    "qwen-3.8-max",
    "kimi-k3",
    "glm-5.3",
    "grok-4.3",
    "minimax-m3",
]

# 兜底：名字不对时从 /models 里挑几个来测
FALLBACK_PROBE_LIMIT = 3


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #

_CURL = shutil.which("curl") or shutil.which("curl.exe")


def _post_json_urllib(url, headers, data, timeout) -> Tuple[int, Any, float, Optional[str]]:
    """直连。连接层失败返回 status=0。"""
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    for k, v in headers.items():
        req.add_header(k, v)

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            latency = time.monotonic() - started
            try:
                return resp.status, json.loads(raw), latency, None
            except json.JSONDecodeError:
                return resp.status, raw, latency, "响应不是合法 JSON"
    except urllib.error.HTTPError as e:
        latency = time.monotonic() - started
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
        return e.code, body, latency, None
    except Exception as e:  # 超时、TLS、DNS……
        return 0, None, time.monotonic() - started, "{}: {}".format(type(e).__name__, e)


def _mk_tmpdir() -> str:
    """建一个**真的能写**的临时目录。

    ★ 不要用 `tempfile.mkdtemp()`。实测本项目的沙箱里 mkdtemp 建的目录
    **任何文件都写不进去**（PermissionError），而手动 `os.makedirs` 建的
    同样路径却完全正常：

        手动 os.makedirs 建子目录  -> ✅ 能写
        tempfile.mkdtemp 建子目录  -> ❌ EACCES
        直接在临时根下写文件        -> ✅ 能写

    后果很隐蔽：curl 回退要往临时目录写请求体，"建目录成功"让人以为路径没问题，
    真正写 body 时才炸 —— 表现出来就是"curl 回退不可用：写临时文件失败"，
    于是 urllib 一挂（中转站 A 的 TLS 就是）整次调用就没了。
    `aiwolf/relay.py` 的 `_post_curl` 是同一个写法，一起修了。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for base in (os.path.join(root, "runs", "_curl_tmp"), tempfile.gettempdir()):
        try:
            os.makedirs(base, exist_ok=True)
            d = os.path.join(base, "relayprobe_" + uuid.uuid4().hex[:10])
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, "body.json")      # 用真名探
            with open(probe, "wb") as f:
                f.write(b'{"probe":1}')
            os.remove(probe)
            return d
        except OSError:
            continue
    raise OSError("找不到任何可写的临时目录")


def _post_json_curl(url, headers, data, timeout) -> Tuple[int, Any, float, Optional[str]]:
    """用 curl 走一遍。

    沙箱或企业代理环境下，Python 的 SSL 栈经常直接握手失败，而 curl 走系统
    证书链能通。实测本项目的沙箱就是这种情况。

    ★ **写临时文件失败时返回错误，绝不抛异常**（2026-10-06 补）：
    `aiwolf/relay.py` 早就修过这个（受限沙箱里临时目录不可写，
    抛出去会把整局带走），探针这边一直没同步。后果是并发对比跑到一半
    `PermissionError: [Errno 13] body.json` 直接把整个测试打崩 ——
    而它本来只是一次请求失败。
    """
    tmpdir = _mk_tmpdir()
    body_path = os.path.join(tmpdir, "body.json")
    out_path = os.path.join(tmpdir, "out.json")
    started = time.monotonic()
    if data is not None:
        try:
            with open(body_path, "wb") as f:
                f.write(data)
        except OSError as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return 0, None, time.monotonic() - started, \
                f"curl 回退不可用：写临时文件失败 {e}"

    cmd = [_CURL, "-sS", "-m", str(int(timeout)),
           "-o", out_path, "-w", "%{http_code}", url]
    for k, v in headers.items():
        cmd += ["-H", "{}: {}".format(k, v)]
    if data is not None:
        cmd += ["--data-binary", "@" + body_path]

    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 0, None, time.monotonic() - started, "curl 调用失败: {}".format(e)
    latency = time.monotonic() - started

    raw = ""
    try:
        if os.path.exists(out_path):
            with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
    except OSError as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 0, None, latency, f"curl 输出读不出来: {e}"

    code_txt = (proc.stdout or b"").decode("utf-8", "replace").strip()
    status = int(code_txt) if code_txt.isdigit() else 0

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = raw

    err = None
    if status == 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip() or "curl 未返回状态码"

    shutil.rmtree(tmpdir, ignore_errors=True)
    return status, body, latency, err


def _request(
    base_url: str,
    api_key: str,
    path: str,
    payload: Optional[dict],
    timeout: float,
) -> Tuple[int, Any, float, Optional[str]]:
    """发一个请求。返回 (status, body, latency_s, error)。

    status=0 表示连接层失败。绝不抛异常，一律用返回值表达。

    传输方式由 RELAY_HTTP 控制：
      auto（默认）— 先直连，连接层失败再退到 curl
      urllib      — 只用直连
      curl        — 只用 curl
    """
    url = base_url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + api_key}

    mode = os.environ.get("RELAY_HTTP", "auto").lower()

    if mode == "curl":
        if not _CURL:
            return 0, None, 0.0, "指定了 RELAY_HTTP=curl 但系统里找不到 curl"
        return _post_json_curl(url, headers, data, timeout)

    status, body, latency, err = _post_json_urllib(url, headers, data, timeout)

    if status == 0 and mode == "auto" and _CURL:
        c_status, c_body, c_latency, c_err = _post_json_curl(url, headers, data, timeout)
        if c_status != 0:
            return c_status, c_body, c_latency, c_err
        return 0, None, latency, "{}；curl 回退同样失败: {}".format(err, c_err)

    return status, body, latency, err


def _chat(base_url, api_key, model, messages, timeout, **kw) -> Tuple[int, Any, float, Optional[str]]:
    payload = {"model": model, "messages": messages, "max_tokens": kw.pop("max_tokens", 16)}
    payload.update(kw)
    return _request(base_url, api_key, "/chat/completions", payload, timeout)


def _first_text(body: Any) -> str:
    """取回复正文。

    推理型模型（glm / deepseek / kimi / minimax 等）在 max_tokens 偏小时会把额度
    全花在 reasoning_content 上，content 为空 —— 这时退回读 reasoning_content，
    否则会把可用模型误判成不可用。
    """
    try:
        msg = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content") or ""
    if content.strip():
        return content
    return msg.get("reasoning_content") or ""


def _usage(body: Any) -> dict:
    if isinstance(body, dict) and isinstance(body.get("usage"), dict):
        return body["usage"]
    return {}


def _err_text(body: Any, limit: int = 220) -> str:
    if body is None:
        return ""
    s = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    s = re.sub(r"\s+", " ", s)
    return s[:limit]


# --------------------------------------------------------------------------- #
# 缓存字段探测
# --------------------------------------------------------------------------- #

# 各家缓存统计字段的已知形态。**「读」和「写」必须分开**：
#   Anthropic 语义下第一次请求只有 cache_creation（建立缓存），第二次才有
#   cache_read（命中）。老实现"任何含 cache 且 >0 的字段都算命中"，于是
#   sn-5 第一次被报成"命中 13701"，第二次只报 692（真正的 13009 被
#   cache_creation=692 挡在后面）—— 一次 94.9% 的命中被说成了 5%。
CACHE_READ_KEYS = (
    "cached_tokens",                 # OpenAI / 多数兼容网关
    "cached",                        # 某些网关简写
    "prompt_cache_hit_tokens",       # DeepSeek
    "cache_read_input_tokens",       # Anthropic
    "cache_read_tokens",
    "cache_hit_tokens",
    "cached_content_token_count",    # Gemini
)


def _scan_cache_fields(obj: Any, path: str = "") -> List[Tuple[str, Any]]:
    """递归找出所有疑似缓存统计的字段。"""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = "{}.{}".format(path, k) if path else k
            if any(ck in k.lower() for ck in CACHE_READ_KEYS) or "cache" in k.lower():
                found.append((p, v))
            found.extend(_scan_cache_fields(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(_scan_cache_fields(v, "{}[{}]".format(path, i)))
    return found


def _is_write_field(leaf: str) -> bool:
    """这个字段说的是「建立缓存」而不是「命中缓存」。"""
    low = leaf.lower()
    return "creation" in low or "write" in low or "miss" in low


def _cache_tokens(usage: Any) -> Tuple[int, int]:
    """从 usage 里分出 (命中读到的 token, 新建缓存的 token)。"""
    read = write = 0
    for path, v in _scan_cache_fields(usage):
        if not isinstance(v, int):
            continue
        leaf = path.rsplit(".", 1)[-1]
        if _is_write_field(leaf):
            write = max(write, v)
        elif leaf.lower() in CACHE_READ_KEYS or any(
                k in leaf.lower() for k in CACHE_READ_KEYS):
            read = max(read, v)
    return read, write


# --------------------------------------------------------------------------- #
# 测试项
# --------------------------------------------------------------------------- #

def t1_basic(base_url, api_key, model, timeout) -> dict:
    # max_tokens 给足：推理型模型要先花掉一段 reasoning 才会吐正文
    status, body, latency, err = _chat(
        base_url, api_key, model,
        [{"role": "user", "content": "回复两个字：收到"}],
        timeout, max_tokens=512,
    )
    text = _first_text(body)
    try:
        finish = body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        finish = None
    ok = status == 200 and bool(text.strip())
    return {
        "ok": ok,
        "status": status,
        "latency_s": round(latency, 2),
        "reply": text[:40],
        "finish_reason": finish,
        "usage": _usage(body),
        "error": err or ("" if ok else _err_text(body)),
    }


def _build_prefix(prefix_tokens: int) -> str:
    """构造一段确定性的大前缀。同样的参数永远得到同样的字节。"""
    unit = (
        "狼人杀是一个信息不对称的推理游戏。村民需要从前一天的发言中"
        "辨别真伪，狼人则需要构造可信的伪装身份。"
    )
    # 中文约 1 token ≈ 1.5 字符，保守估计
    repeats = max(1, int(prefix_tokens * 1.5 / len(unit)))
    return "以下是历史对局记录，仅作上下文，请不要总结或复述。\n" + unit * repeats


def t2_cache(base_url, api_key, model, timeout, prefix_tokens) -> dict:
    """★ 关键测试：同一段大前缀连发两次，看第二次是否命中缓存。"""
    prefix = _build_prefix(prefix_tokens)
    est = int(len(prefix) / 1.5)

    def call(tail):
        return _chat(
            base_url, api_key, model,
            [
                {"role": "system", "content": prefix},
                {"role": "user", "content": tail},
            ],
            timeout, max_tokens=32, temperature=0.0,
        )

    s1, b1, l1, e1 = call("请回复：一")
    s2, b2, l2, e2 = call("请回复：二")

    cache_hits = _scan_cache_fields(b1) + _scan_cache_fields(b2)

    # 判定：第二次请求**读**到了 >0 的缓存。写缓存不算命中。
    read1, write1 = _cache_tokens(_usage(b1))
    read2, write2 = _cache_tokens(_usage(b2))
    has_usage = bool(_usage(b1)) or bool(_usage(b2))

    return {
        "ok": s1 == 200 and s2 == 200,
        "status": [s1, s2],
        "est_prefix_tokens": est,
        "actual_prompt_tokens": [
            _usage(b1).get("prompt_tokens"),
            _usage(b2).get("prompt_tokens"),
        ],
        "latency_s": [round(l1, 2), round(l2, 2)],
        "latency_drop": round(l1 - l2, 2),
        "cache_fields_seen": cache_hits,
        "cache_hit_tokens": [read1, read2],
        "cache_write_tokens": [write1, write2],
        "cache_effective": bool(read2),
        "usage_reported": has_usage,
        "usage_raw": [_usage(b1), _usage(b2)],
        "error": e1 or e2 or ("", _err_text(b1))[s1 != 200],
    }


def t3_json_mode(base_url, api_key, model, timeout) -> dict:
    status, body, latency, err = _chat(
        base_url, api_key, model,
        [{"role": "user", "content":
          '只输出 JSON，不要任何其他文字：{"action":"vote","target":5}'}],
        timeout, max_tokens=512, temperature=0.0,
        response_format={"type": "json_object"},
    )

    text = _first_text(body).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception:
                pass

    return {
        "ok": status == 200 and parsed is not None,
        "status": status,
        "latency_s": round(latency, 2),
        "raw_reply": text[:120],
        "parsed": parsed,
        "note": "支持 response_format" if status == 200 else "需降级为 tool call 或纯提示词",
        "error": err or ("" if status == 200 else _err_text(body)),
    }


def t4_concurrency(base_url, api_key, model, timeout, n) -> dict:
    def one(i):
        return _chat(
            base_url, api_key, model,
            [{"role": "user", "content": "回复：{}".format(i)}],
            timeout, max_tokens=8,
        )

    started = time.monotonic()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        for fut in concurrent.futures.as_completed([pool.submit(one, i) for i in range(n)]):
            try:
                results.append(fut.result())
            except Exception as e:            # noqa: BLE001
                # 一次请求炸掉不能把整个并发测试带走 —— 恰好是"掉队"的信息本身。
                results.append((0, None, 0.0, "{}: {}".format(type(e).__name__, e)))
    wall = time.monotonic() - started

    codes: Dict[str, int] = {}
    errors: List[str] = []
    for status, body, _, err in results:
        codes[str(status)] = codes.get(str(status), 0) + 1
        if status != 200:
            # status=0（连接层失败）时 body 是 None，光看 body 拿不到原因 ——
            # 必须连 err 一起收，否则并发掉队时报出来的是个空字符串。
            errors.append("status={} {}".format(
                status, (err or _err_text(body) or "(无错误信息)")[:160]))

    ok_count = codes.get("200", 0)
    return {
        "ok": ok_count == n,
        "concurrency": n,
        "wall_s": round(wall, 2),
        "status_counts": codes,
        "success": ok_count,
        "errors": errors[:4],
        "latency_s": [round(r[2], 2) for r in results],
        "sample_error": next(
            (_err_text(b) for s, b, _, _ in results if s != 200), ""
        ),
    }


def t5_context(base_url, api_key, model, timeout, target_tokens) -> dict:
    """可选：直接怼一段超长 prompt，看模型真实上下文上限。会烧额度。"""
    filler = _build_prefix(target_tokens)
    status, body, latency, err = _chat(
        base_url, api_key, model,
        [
            {"role": "system", "content": filler},
            {"role": "user", "content": "回复：收到"},
        ],
        timeout, max_tokens=8,
    )
    return {
        "ok": status == 200,
        "status": status,
        "target_tokens": target_tokens,
        "latency_s": round(latency, 2),
        "usage": _usage(body),
        "error": err or ("" if status == 200 else _err_text(body, 400)),
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def list_models(base_url, api_key, timeout) -> List[str]:
    status, body, _, err = _request(base_url, api_key, "/models", None, timeout)
    if status != 200 or not isinstance(body, dict):
        return []
    ids = []
    for m in body.get("data", []):
        mid = m.get("id") if isinstance(m, dict) else None
        if mid:
            ids.append(mid)
    return ids


def pick_models(base_url, api_key, timeout, requested: Optional[str]) -> Tuple[List[str], str]:
    if requested:
        return [m.strip() for m in requested.split(",") if m.strip()], "命令行指定"

    available = list_models(base_url, api_key, timeout)
    if not available:
        return DEFAULT_MODELS, "内置清单（/models 不可用）"

    matched = []
    for want in DEFAULT_MODELS:
        stem = want.split("-")[0].lower()
        for got in available:
            if got == want:
                matched.append(got)
                break
            if stem and stem in got.lower() and got not in matched:
                matched.append(got)
                break

    if matched:
        return matched, "/models 匹配（{} 个可选）".format(len(available))
    return available[:FALLBACK_PROBE_LIMIT], "/models 前 {} 个".format(FALLBACK_PROBE_LIMIT)


def fmt(v) -> str:
    if v is True:
        return "是"
    if v is False:
        return "否"
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser(description="中转站探针（AI 狼人杀前置验证）")
    ap.add_argument("--base-url", default=os.environ.get("RELAY_BASE_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("RELAY_API_KEY", ""))
    ap.add_argument("--models", default=os.environ.get("RELAY_MODELS", ""))
    ap.add_argument("--prefix-tokens", type=int, default=8000)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--context-probe", action="store_true")
    ap.add_argument("--context-tokens", type=int, default=100000)
    ap.add_argument("--out", default="probe_report.json")
    args = ap.parse_args()

    if not args.base_url or not args.api_key:
        print("错误：缺少 RELAY_BASE_URL / RELAY_API_KEY（或用 --base-url / --api-key）")
        return 2

    models, source = pick_models(args.base_url, args.api_key, args.timeout, args.models)
    print("=" * 74)
    print("中转站探针")
    print("  base_url : {}".format(args.base_url))
    print("  模型来源 : {}".format(source))
    print("  待测模型 : {}".format(", ".join(models)))
    print("  前缀长度 : ~{} tokens".format(args.prefix_tokens))
    print("=" * 74)

    report: Dict[str, Any] = {"base_url": args.base_url, "models": {}, "started_at": time.time()}

    for model in models:
        print("\n" + "-" * 74)
        print("模型：{}".format(model))
        print("-" * 74)
        entry: Dict[str, Any] = {}

        entry["T1_basic"] = t1_basic(args.base_url, args.api_key, model, args.timeout)
        r = entry["T1_basic"]
        print("  T1 基本兼容   : {}  status={} {:.2f}s  {}".format(
            fmt(r["ok"]), r["status"], r["latency_s"], r["reply"] or r["error"]))

        if not r["ok"]:
            print("  → 该模型不可用，跳过后续测试")
            report["models"][model] = entry
            continue

        entry["T3_json_mode"] = t3_json_mode(args.base_url, args.api_key, model, args.timeout)
        r = entry["T3_json_mode"]
        print("  T3 JSON 模式  : {}  status={}  {}  {}".format(
            fmt(r["ok"]), r["status"], r["note"], r["error"] or r["raw_reply"]))

        entry["T2_cache"] = t2_cache(
            args.base_url, args.api_key, model, args.timeout, args.prefix_tokens)
        r = entry["T2_cache"]
        print("  T2 前缀缓存   : {}  prompt_tokens={}  读到={}  写入={}  {:.2f}s → {:.2f}s (Δ{:.2f}s)".format(
            fmt(r["cache_effective"]), r["actual_prompt_tokens"],
            r["cache_hit_tokens"], r.get("cache_write_tokens"),
            r["latency_s"][0], r["latency_s"][1], r["latency_drop"]))
        if r["cache_fields_seen"]:
            for k, v in r["cache_fields_seen"][:6]:
                print("                 字段 {} = {}".format(k, v))
        elif not r.get("usage_reported", True):
            print("                 ⚠ 该模型**根本不返回 usage** —— 缓存查不到，"
                  "而且成本统计（summary 的 *_tokens_total）会全是 0")
        elif r["ok"]:
            print("                 ⚠ 有 usage 但没有任何缓存字段 —— 该网关不上报缓存")

        entry["T4_concurrency"] = t4_concurrency(
            args.base_url, args.api_key, model, args.timeout, args.concurrency)
        r = entry["T4_concurrency"]
        print("  T4 并发 x{}    : {}  耗时 {:.2f}s  {}{}".format(
            args.concurrency, fmt(r["ok"]), r["wall_s"], r["status_counts"],
            ("  " + r["sample_error"]) if r["sample_error"] else ""))

        if args.context_probe:
            entry["T5_context"] = t5_context(
                args.base_url, args.api_key, model, args.timeout, args.context_tokens)
            r = entry["T5_context"]
            print("  T5 上下文 ~{}K: {}  status={}  {}  {}".format(
                args.context_tokens // 1000, fmt(r["ok"]), r["status"],
                r["usage"] or "", r["error"]))

        report["models"][model] = entry

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 74)
    print("汇总")
    print("=" * 74)
    header = "{:<24} {:>6} {:>8} {:>8} {:>8}".format("模型", "T1", "T2缓存", "T3JSON", "T4并发")
    print(header)
    print("-" * 74)
    cache_ok = json_ok = basic_ok = 0
    for model, e in report["models"].items():
        t1 = e.get("T1_basic", {}).get("ok", False)
        t2 = e.get("T2_cache", {}).get("cache_effective", False)
        t3 = e.get("T3_json_mode", {}).get("ok", False)
        t4 = e.get("T4_concurrency", {}).get("ok", False)
        basic_ok += bool(t1)
        cache_ok += bool(t2)
        json_ok += bool(t3)
        print("{:<24} {:>6} {:>8} {:>8} {:>8}".format(
            model, fmt(t1), fmt(t2), fmt(t3), fmt(t4)))

    print("-" * 74)
    print("可用 {} / {}    缓存生效 {} / {}    JSON 模式 {} / {}".format(
        basic_ok, len(report["models"]),
        cache_ok, basic_ok or 1,
        json_ok, basic_ok or 1))

    print("\n结论：")
    if basic_ok == 0:
        print("  ✗ 没有任何模型可用，先检查 base_url / api_key / 中转站状态")
    elif cache_ok == 0:
        print("  ⚠ 未观察到缓存生效。两种可能：")
        print("      a) 网关确实不做前缀缓存 —— 那么 DESIGN.md 第 3 节的收益不成立，")
        print("         需要重新评估：缩短大桌、减少轮次、或按座位裁剪视图")
        print("      b) 网关做了缓存但不上报 —— 看 T2 的延迟差，Δ 明显（比如 >30%）")
        print("         说明实际有缓存，可以按有缓存设计")
    else:
        print("  ✓ 缓存生效，DESIGN.md 第 3 节的「共享前缀 + 尾部差异」结构可以按原计划实现")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n完整报告已写入：{}".format(os.path.abspath(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
