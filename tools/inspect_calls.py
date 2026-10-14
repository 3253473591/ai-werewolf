#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""看某一局里每次调用的额度/截断/耗时，按座位或按 day+phase 过滤。

用法：
    python tools/inspect_calls.py runs/20261006_011857_10seat_web
    python tools/inspect_calls.py runs/xxx --seat 7
    python tools/inspect_calls.py runs/xxx --truncated
    python tools/inspect_calls.py runs/xxx --seat 7 --phase DAY_SPEECH
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--seat", type=int)
    ap.add_argument("--phase")
    ap.add_argument("--truncated", action="store_true")
    ap.add_argument("--day", type=int)
    ap.add_argument("--full", action="store_true", help="打印每条的 raw 预览")
    args = ap.parse_args()

    run_dir = args.run if os.path.isabs(args.run) else os.path.join(ROOT, args.run)
    seats = [args.seat] if args.seat else list(range(1, 11))

    total = trunc = 0
    wasted = 0
    print(f"{'seat':>4} {'day':>3} {'phase':<14} {'attempts':>8} {'trunc':>5} "
          f"{'lat':>7} {'prompt':>7} {'compl':>6} {'think':>6} {'waste':>6} {'reason':<10} model")
    print("-" * 124)
    for no in seats:
        p = os.path.join(run_dir, f"seat_{no}.jsonl")
        if not os.path.exists(p):
            continue
        for r in load(p):
            if args.phase and r.get("phase") != args.phase:
                continue
            if args.day and r.get("day") != args.day:
                continue
            if args.truncated and not r.get("truncated"):
                continue
            total += 1
            usage = r.get("usage") or {}
            if r.get("truncated"):
                trunc += 1
            wasted += r.get("wasted_completion_tokens") or 0
            print(f"{no:>4} {r.get('day', ''):>3} {str(r.get('phase', '')):<14} "
                  f"{r.get('attempts', ''):>8} {str(bool(r.get('truncated'))):>5} "
                  f"{r.get('latency_s', 0):>7.1f} {r.get('prompt_tokens', 0):>7} "
                  f"{r.get('completion_tokens') or usage.get('completion_tokens', 0) or '':>6} "
                  f"{r.get('reasoning_tokens') or '':>6} "
                  f"{r.get('wasted_completion_tokens') or '':>6} "
                  f"{str(r.get('finish_reason', '')):<10} {r.get('model_used', '')}")
            if args.full:
                raw = r.get("raw") or ""
                print("      raw:", raw[:400].replace("\n", "\\n"))
                for a in (r.get("attempts_detail") or []):
                    print(f"      attempt: model={a.get('model')} ep={a.get('endpoint')} "
                          f"max_tokens={a.get('max_tokens')} status={a.get('status')} "
                          f"compl={a.get('completion_tokens')} think={a.get('reasoning_tokens')} "
                          f"finish={a.get('finish_reason')} outcome={a.get('outcome')} "
                          f"lat={a.get('latency_s')}s")
    print("-" * 124)
    print(f"共 {total} 条，截断 {trunc} 条")
    if wasted:
        print(f"被丢弃的尝试白烧了 {wasted} 个输出 token（不含最终交出去的那次）")
    elif total:
        print("没有 wasted_completion_tokens 记录：可能是 2026-10-06 之前的老对局"
              "（那时中间尝试不记账），或这局确实没有重试")


if __name__ == "__main__":
    sys.exit(main())
