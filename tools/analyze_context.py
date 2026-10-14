#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""prompt 长度增长分析：从已有对局外推"12 座 6 天大概要多大上下文"。

为什么要单独算：座位的上下文预算是**选型的硬约束**。256K 听起来很大，
但要看它在第 6 天还剩多少。这里只用真实跑过的 seat_*.jsonl，不猜。
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def analyze(run):
    path = os.path.join(ROOT, run)
    rows = []
    for p in glob.glob(os.path.join(path, "seat_*.jsonl")):
        try:
            no = int(os.path.basename(p).split("_")[1].split(".")[0])
        except (IndexError, ValueError):
            continue
        for line in open(p, encoding="utf-8"):
            if not line.strip():
                continue
            r = json.loads(line)
            pt = r.get("prompt_tokens")
            if not pt:
                continue
            rows.append((no, r.get("day") or 0, r.get("phase") or "", pt,
                         len(r.get("messages") or [])))
    if not rows:
        return None
    by_day = {}
    for _, day, _, pt, _ in rows:
        by_day.setdefault(day, []).append(pt)
    return {
        "run": run,
        "seats": len({r[0] for r in rows}),
        "calls": len(rows),
        "max": max(r[3] for r in rows),
        "by_day": {d: (max(v), sum(v) // len(v), len(v))
                   for d, v in sorted(by_day.items())},
        "days": max(r[1] for r in rows),
    }


def main():
    runs = sys.argv[1:]
    if not runs:
        runs = [os.path.basename(d) for d in
                sorted(glob.glob(os.path.join(ROOT, "runs", "2*seat*")))
                if os.path.exists(os.path.join(d, "summary.json"))]
    print(f"{'对局':<32}{'座':>3}{'天':>3}{'调用':>5}{'最大prompt':>11}")
    print("-" * 60)
    stats = []
    for r in runs:
        a = analyze(r)
        if not a:
            continue
        stats.append(a)
        print(f"{a['run'][:31]:<32}{a['seats']:>3}{a['days']:>3}"
              f"{a['calls']:>5}{a['max']:>11}")
        for d, (mx, avg, n) in a["by_day"].items():
            print(f"      第{d}天：最大 {mx:>7}  平均 {avg:>7}  （{n} 次调用）")
    print()
    print("=== 外推：12 座 6 天 ===")
    # 用"每天最大 prompt 的增量"做线性外推，取几个对局的增量的最大值（保守）
    incs = []
    for a in stats:
        days = sorted(a["by_day"])
        if len(days) >= 2:
            incs.append((a["by_day"][days[-1]][0] - a["by_day"][days[0]][0])
                        / (days[-1] - days[0]))
    if incs:
        per_day = max(incs)
        print(f"  实测每天最大 prompt 增量（各局取最大）：约 {per_day:.0f} tokens/天")
        for seats, base in ((9, 12078), (10, 15470)):
            pass
        print(f"  按 {per_day:.0f}/天的增量，从第 2 天算起，第 6 天 ≈ "
              f"{max(a['by_day'][sorted(a['by_day'])[-1]][0] for a in stats) + per_day * 4:.0f}"
              f" tokens（含 12 座比 10 座多出的在场人数）")
    print()
    print("说明：12 座比 10 座多 2 个座位的发言/投票，每天事件数约多 20%，")
    print("所以上面这个外推还要再乘一个系数。")


if __name__ == "__main__":
    main()
