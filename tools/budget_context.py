#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""上下文预算模型：prompt ≈ 系统提示 + 事件数 × 每事件成本 + 尾部块。

用真实对局标定「每事件成本」和「每人每天产生多少事件」，再外推到 12 座 + 警长。
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    run = sys.argv[1]
    path = os.path.join(ROOT, run)
    ev = [json.loads(l) for l in
          open(os.path.join(path, "events.jsonl"), encoding="utf-8") if l.strip()]
    pub_by_day = {}
    for e in ev:
        if e.get("channel") == "public":
            pub_by_day[e["day"]] = pub_by_day.get(e["day"], 0) + 1
    print(f"对局 {run}")
    print(f"  事件总数 {len(ev)}，公开事件 {sum(pub_by_day.values())}")
    print(f"  每天公开事件数：{pub_by_day}")

    # 标定：每个采样点算 prompt_tokens / (公开事件数 + 2)
    samples = []
    for p in glob.glob(os.path.join(path, "seat_*.jsonl")):
        for line in open(p, encoding="utf-8"):
            if not line.strip():
                continue
            r = json.loads(line)
            pt, day = r.get("prompt_tokens"), r.get("day") or 0
            if not pt:
                continue
            n_ev = sum(v for k, v in pub_by_day.items() if k <= day)
            samples.append((n_ev, pt, day))
    if not samples:
        print("  没有可用的 prompt_tokens 样本")
        return
    samples.sort()
    print("\n  标定样本（累计公开事件数 -> prompt_tokens）：")
    for n_ev, pt, day in samples[-6:]:
        print(f"    第{day}天  事件 {n_ev:>4}  ->  {pt:>7} tokens")

    # 最小二乘拟合 prompt = a + b × 事件数
    # a = 固定开销（系统提示 + 板子公告 + 尾部块），b = 每条公开事件摊到的 token
    n = len(samples)
    xs = [s[0] for s in samples]
    ys = [s[1] for s in samples]
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    b = (sum((x - mx) * (y - my) for x, y, _ in samples) / denom) if denom else 0.0
    a = my - b * mx
    print(f"\n  拟合：prompt ≈ {a:.0f}（固定） + {b:.1f} × 公开事件数")
    print(f"        （固定部分 = 系统提示 + 板子公告 + 尾部块；"
          f"观测到最大固定开销约 {max(ys) - b * max(xs):.0f}）")

    # 外推
    days = max(pub_by_day)
    per_day = sum(pub_by_day.values()) / days
    print(f"  实测每天公开事件 ≈ {per_day:.1f} 条（{days} 天）")
    print()
    print("=== 外推（12 座）===")
    seats_now = 10
    scale = 12 / seats_now
    # 警长：首日多一轮「竞选发言 + 竞选投票 + 结果公告」≈ 座位数 + 3 条；
    #       之后每天多一条「警徽移交 / 撕警徽」之类的公告
    sheriff_day1 = 12 + 3
    sheriff_per_day = 1
    print(f"  {'天数':>4}{'累计公开事件':>14}{'prompt 估计':>14}{'占 256K':>10}{'占 1M':>9}")
    for n_days in (4, 5, 6, 7):
        total = 0.0
        for d in range(1, n_days + 1):
            ev_day = per_day * scale
            if d == 1:
                ev_day += sheriff_day1 * scale
            ev_day += sheriff_per_day
            total += ev_day
        est = a + b * total
        print(f"  {n_days:>4}{total:>14.0f}{est:>14.0f}"
              f"{100 * est / 262144:>9.0f}%{100 * est / 1048576:>8.0f}%")
    print("\n  注：尾部块随天数也会变长（历史立场块），上面用固定 a 是低估；")
    print("      但量级上 256K 仍然远远够用。")


if __name__ == "__main__":
    main()
