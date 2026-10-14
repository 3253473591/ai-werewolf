#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把某一局里**某一次真实调用**原样重发，用来验证"换个额度/关掉思维链会怎样"。

为什么需要它：`tools/probe_reasoning.py` 用的是手写的小 prompt，
而"思维链吃光额度"恰恰只在**真实局势**下才复现（第 3 天的盘面、
10 个座位的历史、狼队频道……）。所以这里直接从 `runs/<局>/seat_N.jsonl`
里把那次调用实际发出去的 `messages` 读出来重放 —— 变量只有一个，
就是你想试的那个旋钮。

用法：
    # 先看看有哪些调用（含被截断的）
    python tools/replay_call.py runs/20261006_011857_10seat_web --list

    # 原样重放（复现）
    python tools/replay_call.py runs/xxx 7 3 DAY_SPEECH

    # 关掉思维链（豆包/方舟那招，看 ep2 认不认）
    python tools/replay_call.py runs/xxx 7 3 DAY_SPEECH --no-thinking

    # 把额度开到 32768（试出 GLM 到底要多少）
    python tools/replay_call.py runs/xxx 7 3 DAY_SPEECH --max-tokens 32768

⚠️ 真的会调 API、花额度。默认只发 1 次，不做 relay 那套翻倍重试。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiwolf import relay as relay_mod            # noqa: E402
from aiwolf.endpoints import EndpointRegistry    # noqa: E402


def load_rows(run_dir: str, seat: int):
    path = os.path.join(run_dir, f"seat_{seat}.jsonl")
    if not os.path.exists(path):
        return []
    rows = []
    for line in open(path, "r", encoding="utf-8"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def cmd_list(run_dir: str) -> int:
    print(f"{'seat':>4} {'day':>3} {'phase':<14} {'trunc':>5} {'lat':>7} "
          f"{'compl':>7} {'reason':<8} {'raw':>6} model")
    print("-" * 100)
    for no in range(1, 13):
        for r in load_rows(run_dir, no):
            usage = r.get("usage") or {}
            raw = r.get("raw") or ""
            print(f"{no:>4} {r.get('day', ''):>3} {str(r.get('phase', '')):<14} "
                  f"{str(bool(r.get('truncated'))):>5} {r.get('latency_s', 0):>7.1f} "
                  f"{str(usage.get('completion_tokens', '')):>7} "
                  f"{str(r.get('finish_reason', '')):<8} {len(raw):>6} {r.get('model_used', '')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="原样重放某一次真实调用")
    ap.add_argument("run")
    ap.add_argument("seat", nargs="?", type=int)
    ap.add_argument("day", nargs="?", type=int)
    ap.add_argument("phase", nargs="?")
    ap.add_argument("--list", action="store_true", help="只列出这局的所有调用")
    ap.add_argument("--model", default="", help="换模型（默认用记录里的 model_used）")
    ap.add_argument("--endpoint", default="", help="换通道（默认按 seats.json 里该座位的绑定）")
    ap.add_argument("--max-tokens", type=int, default=0,
                    help="本次请求的 max_tokens（默认 3072，与发言阶段一致）")
    ap.add_argument("--no-thinking", action="store_true",
                    help='加 thinking={"type":"disabled"}')
    ap.add_argument("--keep-extra-body", action="store_true",
                    help="沿用座位配置里的 extra_body（回放工具默认不继承，便于对比）")
    ap.add_argument("--temperature", type=float, default=-1.0)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--no-json-mode", action="store_true")
    args = ap.parse_args()

    run_dir = args.run if os.path.isabs(args.run) else os.path.join(ROOT, args.run)
    if not os.path.isdir(run_dir):
        print(f"❌ 没有目录 {run_dir}")
        return 2
    if args.list or args.seat is None:
        return cmd_list(run_dir)

    rows = [r for r in load_rows(run_dir, args.seat)
            if r.get("day") == args.day and r.get("phase") == args.phase]
    if not rows:
        print(f"❌ seat_{args.seat}.jsonl 里没有 day={args.day} phase={args.phase} 这条")
        return 2
    rec = rows[-1]
    messages = rec.get("messages")
    if not messages:
        print("❌ 这条记录里没有 messages（早期格式不带），没法重放")
        return 2

    # 通道：显式 --endpoint > 记录里的 endpoint > 座位绑定
    from aiwolf.seats import build_seats, load_config
    cfg_path = os.path.join(ROOT, "config", "seats.json")
    reg = EndpointRegistry(os.path.join(ROOT, "config", "endpoints.json"))
    cfg = load_config(cfg_path)
    # 角色在这里无所谓（messages 是记录里读出来的），只要拿到座位绑定的通道/温度
    seats = build_seats(cfg, len(cfg["seats"]), registry=reg)
    seat = next((s for s in seats if s.no == args.seat), None)
    if seat is None:
        print(f"❌ seats.json 里没有 {args.seat} 号座")
        return 2

    model = args.model or rec.get("model_used") or seat.model
    ep_id = args.endpoint or rec.get("endpoint") or seat.endpoint
    ep = reg.get(ep_id) if ep_id else None
    if ep is None:
        print(f"❌ 通道 {ep_id} 不存在")
        return 2

    temperature = args.temperature if args.temperature >= 0 else seat.temperature
    max_tokens = args.max_tokens or 3072

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if not args.no_json_mode:
        body["response_format"] = {"type": "json_object"}
    extra = {}
    if args.no_thinking:
        extra["thinking"] = {"type": "disabled"}
    elif args.keep_extra_body and seat.extra_body:
        extra = dict(seat.extra_body)
    body.update(extra)

    print("=" * 78)
    print(f"重放：{run_dir}")
    print(f"记录：{args.seat}号 day={args.day} phase={args.phase}")
    print(f"原记录：model={rec.get('model_used')} endpoint={rec.get('endpoint')} "
          f"finish={rec.get('finish_reason')} truncated={rec.get('truncated')} "
          f"latency={rec.get('latency_s')}s")
    print(f"原 usage：{json.dumps(rec.get('usage') or {}, ensure_ascii=False)}")
    print(f"本次：model={model} endpoint={ep_id}({ep.name}) max_tokens={max_tokens} "
          f"temperature={temperature} extra={json.dumps(extra, ensure_ascii=False)}")
    print(f"messages：{len(messages)} 条，其中 system "
          f"{sum(1 for m in messages if m.get('role') == 'system')} 条")
    print("=" * 78)

    url = ep.base_url.rstrip("/") + "/chat/completions"
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + ep.api_key}

    started = time.monotonic()
    status, payload, latency, err = relay_mod.http_post(url, headers, data,
                                                        args.timeout, "auto")
    wall = time.monotonic() - started

    if status != 200 or not isinstance(payload, dict):
        print(f"❌ HTTP {status}  {wall:.1f}s  {err}")
        if payload is not None:
            print(json.dumps(payload, ensure_ascii=False)[:600])
        return 1

    content, reasoning = relay_mod.extract_text(payload)
    finish = relay_mod.finish_reason_of(payload)
    usage = payload.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    r_tokens = details.get("reasoning_tokens")

    print(f"finish_reason  = {finish!r}")
    print(f"墙钟 / 请求内  = {wall:.1f}s / {latency:.1f}s")
    print(f"usage          = {json.dumps(usage, ensure_ascii=False)}")
    print(f"content 长度   = {len(content)}")
    print(f"reasoning 长度 = {len(reasoning)}")
    if r_tokens is not None and usage.get("completion_tokens"):
        out = usage["completion_tokens"]
        if out > max_tokens:
            print(f"思维链 {r_tokens} tokens —— completion {out} > max_tokens {max_tokens}，"
                  f"该网关不把思维链算进额度 ✅")
        else:
            print(f"思维链占输出   = {r_tokens}/{out}（上限 {max_tokens}）")
    print("-" * 78)
    print("content 前 400：")
    print(content[:400])
    if reasoning:
        print("-" * 78)
        print(f"reasoning 前 200：{reasoning[:200]!r}")

    # 能不能解析成动作 —— 这才是真正在意的
    sys.path.insert(0, os.path.join(ROOT, "aiwolf"))
    try:
        from aiwolf.protocol import parse_decision
        d = parse_decision(content, args.phase)
        print("-" * 78)
        print(f"解析结果：issues={d.issues} speech={len(d.speech or '')}字 "
              f"action={(d.action or {}).get('type') if isinstance(d.action, dict) else d.action}")
    except Exception as exc:                                  # noqa: BLE001
        print(f"（解析预览失败：{type(exc).__name__}: {exc}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
