#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一局的事件流压成一份简报。**不联网、不花钱、不改任何对局记录。**

    # 打印到屏幕
    python tools/brief.py runs/20261009_005829_12seat_web

    # 写进对局目录（推荐：简报本身也是复盘产物）
    python tools/brief.py runs/20261009_005829_12seat_web -o runs/20261009_005829_12seat_web/brief.md

    # 压得更狠 / 更松
    python tools/brief.py runs/xxx --max-chars 120
    python tools/brief.py runs/xxx --max-chars 600

口径见 `aiwolf/brief.py` 的模块文档：**只取公开频道**（狼队小桌、私密、上帝账本
都不进），裁决与票型全留，单条发言截到 `--max-chars` 字并留下「…（后略）」标记。

简报的两种用法：
  · 给人看 —— 12 座 5 天的一局压完约一万字，比翻 10 万字节的 events.jsonl 快得多；
  · 喂给赛后采访 —— `python tools/reflect.py runs/xxx --brief`，12 个座位各带一份
    精简记录，而不是各带一份全量。
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description="赛后简报（不联网、不花钱）")
    ap.add_argument("run", help="已结束对局的目录，例如 runs/20261009_005829_12seat_web")
    ap.add_argument("-o", "--out", default="", help="写进文件（默认打印到屏幕）")
    ap.add_argument("--max-chars", type=int, default=200,
                    help="单条发言截断字数（默认 200）")
    args = ap.parse_args()

    from aiwolf.brief import build_brief
    from aiwolf.reflect import load_finished_run

    run = args.run if os.path.isabs(args.run) else os.path.join(ROOT, args.run)
    try:
        log, seats, summary = load_finished_run(run)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 2

    events_total = sum(1 for _ in log)
    text = build_brief(log, summary, seats, max_chars=args.max_chars)

    if args.out:
        out = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
        from aiwolf.brief import write_brief
        write_brief(out, text)
        print(f"已写入 {out}")
    else:
        print(text)

    print(f"-- {os.path.basename(run)}：事件 {events_total} 条 → 简报 {len(text)} 字"
          f"（单条截 {args.max_chars} 字）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
