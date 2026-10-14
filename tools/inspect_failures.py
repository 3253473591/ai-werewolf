#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""列出某局里失败的调用（模型为空 / 有 error / latency 为 0）。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
run = sys.argv[1]
for no in range(1, 13):
    p = os.path.join(ROOT, run, f"seat_{no}.jsonl")
    if not os.path.exists(p):
        continue
    for line in open(p, encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("model_used") or r.get("error"):
            if r.get("error") or not r.get("model_used"):
                print(f"--- {no}号 day={r.get('day')} {r.get('phase')} "
                      f"lat={r.get('latency_s')} attempts={r.get('attempts')}")
                print(f"    models_tried   : {r.get('models_tried')}")
                print(f"    primary_error  : {str(r.get('primary_error'))[:300]}")
                print(f"    error          : {str(r.get('error'))[:400]}")
                print(f"    finish_reason  : {r.get('finish_reason')}")
                print(f"    raw            : {str(r.get('raw'))[:200]!r}")
