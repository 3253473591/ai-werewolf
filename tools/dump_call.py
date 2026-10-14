#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""看某一条调用记录的全貌（正文/推理各多长、结尾是什么）。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    run, seat, day, phase = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    p = os.path.join(ROOT, run, f"seat_{seat}.jsonl")
    for line in open(p, "r", encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("day") == day and r.get("phase") == phase:
            print(f"=== seat {seat} day {day} {phase} ===")
            print("model_used   :", r.get("model_used"), "| requested:", r.get("requested_model"))
            print("finish       :", r.get("finish_reason"), "| truncated:", r.get("truncated"))
            print("attempts     :", r.get("attempts"), "| latency:", r.get("latency_s"))
            print("usage        :", json.dumps(r.get("usage") or {}, ensure_ascii=False))
            print("prompt_tokens:", r.get("prompt_tokens"), "| cached:", r.get("cached_tokens"))
            raw = r.get("raw") or ""
            rsn = r.get("reasoning") or ""
            print(f"raw len      : {len(raw)} 字符")
            print(f"reasoning len: {len(rsn)} 字符（日志里最多存 2000）")
            print("--- raw 前 300 ---")
            print(raw[:300])
            print("--- raw 后 300 ---")
            print(raw[-300:])
            print("--- reasoning 前 200 ---")
            print(rsn[:200])


if __name__ == "__main__":
    main()
