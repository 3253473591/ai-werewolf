#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单模型"思考 token 吃不吃光"探针。

背景：DESIGN §4.2.1 记录过一类坑 —— 重推理模型把整个 max_tokens 花在思维链上，
`finish_reason=length`、`content` 为空串，一局里那个座位就"未发言"了
（实测 `glm-5.3-flash` 在 中转站 A 中转站上单次发言 104~406 秒）。

**同一个模型名在不同通道上的行为可能完全不同**（中转站会把推理塞进自己的字段、
甚至自己做摘要）。换通道、换模型之前，先用这个探针量一次，别直接开一局九座。

用法：
    python tools/probe_reasoning.py                       # 默认测 ep2 上该座位的模型
    python tools/probe_reasoning.py glm-5.3-flash glm-5.2
    python tools/probe_reasoning.py --endpoint ep3 glm-5.3-flash
    python tools/probe_reasoning.py --max-tokens 8192
    python tools/probe_reasoning.py --phase wolf         # 用狼队夜间商讨的 prompt（更重）

看什么：
  * `finish_reason` = length 且 content 为空  -> 这个座位会烧光额度
  * `reasoning_tokens` 占 max_tokens 的比例   -> 余量够不够
  * 延迟                                      -> 会不会撞 relay 的 request_timeout_s(240s)

⚠️ 这个脚本**真的会调 API、花额度**，所以不在 tools/run_all_tests.py 里。
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENDPOINTS = os.path.join(ROOT, "config", "endpoints.json")
SEATS = os.path.join(ROOT, "config", "seats.json")

SYSTEM = """你在参加一场 AI 狼人杀，身份是村民。
只输出一个 JSON 对象，不要加解释或代码围栏：
{"thinking": "只有你自己能看到的内部推理", "speech": "你要公开说的话", "action": {"type": "speak", "target": null}, "confidence": 0.0}
thinking 是私密草稿（不进大桌），控制在 300 字以内：它和 speech 共用同一份输出额度。
要说服别人的长论证写进 speech。"""

# 白天发言：最长、最需要推理的阶段
USER_SPEECH = """# Character Prompt
## 你的身份
村民
## 当前阶段：白天
你是好人阵营。通过聆听其他选手的发言找出逻辑漏洞。
## 当前局势
第 3 天 · 白天发言
存活：1号、3号、4号、6号、7号、8号
出局：2号、5号、9号
## 你的座位号
7号
## 本阶段任务
现在是白天发言阶段。公开发言，表明立场、分析或指认。
## 输出格式
只输出一个 JSON 对象。"""

# 狼队夜间商讨：要读队友发言、定刀口、考虑悍跳，是推理最重的阶段
USER_WOLF = """# Character Prompt
## 你的身份
狼人
## 你的狼队友
3号、8号
## 当前阶段：夜晚
【夜间行动】你们需要商讨今晚的击杀目标：谁最可能是神职？谁逻辑最清晰、最具威胁？是否需要安排"悍跳"战术？
## 你掌握的私密信息
你还有 2 名狼队友，见上面「你的狼队友」。
## 狼队夜间频道（仅你和狼队友可见）
[第3天 · 狼队夜间频道] 3号：我建议刀1号，他昨天带的节奏最像预言家。
[第3天 · 狼队夜间频道] 8号：我同意，但4号一直在盯我，要不要先处理4号？
## 当前局势
第 3 天 · 狼队夜间频道
存活：1号、3号、4号、6号、7号、8号
出局：2号、5号、9号
## 你的座位号
7号
## 本阶段任务
现在是狼队夜间频道，只有你和狼队友能看到。商量今晚刀谁，说一句话。今晚的刀口由狼队多数表决决定，所以你要说服队友，而不是抢最后发言。
## 输出格式
只输出一个 JSON 对象。"""

PROMPTS = {"speech": USER_SPEECH, "wolf": USER_WOLF}


def call(base: str, key: str, model: str, system: str, user: str, *,
         max_tokens: int, temperature: float, timeout: float, json_mode: bool,
         extra_body: Optional[dict] = None):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if extra_body:
        body.update(extra_body)
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as r:
            return time.monotonic() - started, json.loads(r.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return time.monotonic() - started, None, f"HTTP {e.code} {raw[:300]}"
    except Exception as e:                                    # noqa: BLE001
        return time.monotonic() - started, None, f"{type(e).__name__}: {e}"


def probe(base: str, key: str, model: str, args) -> dict:
    extra = {"thinking": {"type": "disabled"}} if getattr(args, "no_thinking", False) else None
    tag = "  thinking=disabled" if extra else ""
    print(f"\n=== {model}   max_tokens={args.max_tokens}  阶段={args.phase}{tag}")
    lat, payload, err = call(base, key, model, SYSTEM, PROMPTS[args.phase],
                             max_tokens=args.max_tokens, temperature=args.temperature,
                             timeout=args.timeout, json_mode=not args.no_json_mode,
                             extra_body=extra)
    if err and "response_format" in err:
        print("  ⚠ 网关不认 response_format，去掉后重试")
        lat, payload, err = call(base, key, model, SYSTEM, PROMPTS[args.phase],
                                 max_tokens=args.max_tokens, temperature=args.temperature,
                                 timeout=args.timeout, json_mode=False, extra_body=extra)
    if err:
        print(f"  ❌ {lat:.1f}s  {err}")
        return {"model": model, "error": err, "latency_s": round(lat, 1)}

    choice = (payload.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    reasoning = (msg.get("reasoning_content") or msg.get("reasoning")
                 or msg.get("thinking") or msg.get("thought") or "")
    usage = payload.get("usage") or {}
    r_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    out_tokens = usage.get("completion_tokens")
    finish = choice.get("finish_reason")

    print(f"  finish_reason = {finish!r}")
    print(f"  延迟          = {lat:.1f}s")
    print(f"  content 长度  = {len(content)}    reasoning 长度 = {len(reasoning)}")
    print(f"  usage         = {json.dumps(usage, ensure_ascii=False)}")
    if r_tokens is not None and out_tokens:
        if out_tokens > args.max_tokens:
            # 方舟（豆包）这类网关**不把思维链算进 max_tokens**：
            # completion_tokens 可以远超上限，而正文照样完整。
            # 也就是说"思考把额度吃光"这个坑在它身上不存在。
            print(f"  思考 {r_tokens} tokens（{out_tokens} > max_tokens {args.max_tokens}）"
                  f"—— 这个网关不把思维链算进输出额度 ✅")
        else:
            print(f"  思考占输出    = {r_tokens}/{out_tokens}"
                  f"（额度上限 {args.max_tokens}，余量 {100 * (1 - r_tokens / args.max_tokens):.0f}%）")

    verdict = "✅ 正常出正文"
    if not content.strip():
        verdict = "❌ content 为空 —— 这个座位会烧光额度"
    elif finish == "length":
        verdict = "⚠ 撞到 max_tokens 上限，正文可能被截断"
    else:
        try:
            json.loads(content)
        except Exception:                                     # noqa: BLE001
            verdict = "⚠ 正文不是合法 JSON（engine 会走抢救链）"
    print(f"  结论          = {verdict}")
    print(f"  正文前 100 字 ：{content[:100]!r}")
    return {"model": model, "finish_reason": finish, "latency_s": round(lat, 1),
            "content_len": len(content), "reasoning_len": len(reasoning),
            "reasoning_tokens": r_tokens, "completion_tokens": out_tokens,
            "max_tokens": args.max_tokens, "verdict": verdict}


def main() -> int:
    ap = argparse.ArgumentParser(description="单模型思考 token 探针（会花额度）")
    ap.add_argument("models", nargs="*", help="要测的模型名；不给就用该通道上座位配的那个")
    ap.add_argument("--endpoint", default="ep2", help="config/endpoints.json 里的通道 id（默认 ep2）")
    ap.add_argument("--max-tokens", type=int, default=3072, help="与发言阶段一致（默认 3072）")
    ap.add_argument("--temperature", type=float, default=0.9)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--phase", choices=sorted(PROMPTS), default="speech")
    ap.add_argument("--no-json-mode", action="store_true")
    ap.add_argument("--no-thinking", action="store_true",
                    help="加 thinking={type:disabled}（方舟/豆包支持；关掉思维链能大幅降延迟）")
    ap.add_argument("--out", default="", help="把结果追加到 jsonl（可选）")
    args = ap.parse_args()

    eps = json.load(open(ENDPOINTS, encoding="utf-8"))["endpoints"]
    ep = next((e for e in eps if e["id"] == args.endpoint), None)
    if ep is None:
        print(f"❌ 没有通道 {args.endpoint}，可选：{[e['id'] for e in eps]}")
        return 2
    if not ep.get("api_key"):
        print(f"❌ 通道 {args.endpoint} 没有 key")
        return 2

    models = args.models
    if not models:
        seats = json.load(open(SEATS, encoding="utf-8"))["seats"]
        models = [s["model"] for s in seats if s.get("endpoint") == args.endpoint]
        if not models:
            print(f"❌ 没有座位绑在 {args.endpoint} 上，请显式给模型名")
            return 2

    print("=" * 70)
    print(f"通道：{ep['name']}  {ep['base_url']}")
    print(f"模型：{', '.join(models)}    （每次调用都会花额度）")
    print("=" * 70)
    rows = [probe(ep["base_url"], ep["api_key"], m, args) for m in models]

    if args.out:
        with open(args.out, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\n结果已追加到 {args.out}")
    return 0 if all("error" not in r for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
