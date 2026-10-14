#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把某局 reflections.json 里已存的感言按当前 clean_reflection() 重洗一遍。

为什么要单独一个工具：感言的清理逻辑会随模型行为变化（例如某个模型开始吐
`<think>` 思维链）。**重跑一遍要重新花额度**，而已经说过的原文就存在
reflections.json 里 —— 那就地重洗即可，只改 text 字段。

安全边界：
  · 只动 reflections.json；
  · **绝不碰 events.jsonl / seat_*.jsonl** —— 那两份是权威对局记录；
  · 默认 dry-run，先看会改什么；加 --write 才落盘。

用法：
    python tools/reclean_reflect.py runs/20261006_220858_12seat_web           # 只报告
    python tools/reclean_reflect.py runs/20261006_220858_12seat_web --write   # 落盘
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiwolf.reflect import REFLECT_FILE, clean_reflection  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--write", action="store_true", help="真的落盘（会先备份）")
    args = ap.parse_args()

    run = args.run if os.path.isabs(args.run) else os.path.join(ROOT, args.run)
    path = os.path.join(run, REFLECT_FILE)
    if not os.path.exists(path):
        print(f"❌ 没有 {REFLECT_FILE}：{path}")
        return 2

    payload = json.load(open(path, encoding="utf-8"))
    changed = []
    for t in payload.get("transcript", []):
        old = t.get("text") or ""
        new = clean_reflection(old)
        if new != old:
            changed.append((t["seat"], len(old), len(new), old[:60], new[:60]))
            t["text"] = new

    print(f"局：{os.path.basename(run)}")
    print(f"感言 {len(payload.get('transcript', []))} 条，需要重洗 {len(changed)} 条")
    for seat, lo, ln, o, n in changed:
        print(f"\n  {seat}号：{lo} -> {ln} 字")
        print(f"    旧开头：{o!r}")
        print(f"    新开头：{n!r}")

    if not changed:
        print("没有需要改的。")
        return 0
    if not args.write:
        print("\n（dry-run，未落盘。加 --write 才写）")
        return 0

    shutil.copy2(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已落盘（备份 {os.path.basename(path)}.bak）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
