#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抽卡（角色分配）单测。

起因：最初 build_seats 是按座位顺序发角色的，1-3 号永远是狼，
而 1、2 号恰好是 Gemini 和 GPT —— 几局下来就会被看出"某某总是狼"。

这里用大样本卡方检验证明抽卡是均匀的，并验证同一 seed 可复现。

用法：python tools/selftest_draw.py
"""
from __future__ import annotations

import collections
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiwolf.seats import (  # noqa: E402
    PLAYER_COUNTS,
    ROLE_SETS,
    build_seats,
    load_config,
    resolve_seed,
)

FAILURES = []
CONFIG = os.path.join(ROOT, "config", "seats.json")

# 卡方临界值（自由度 4，p=0.05）。5 人局 5 个座位 -> 自由度 = 5-1 = 4
CHI2_CRITICAL = 9.49


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    cfg = load_config(CONFIG)

    print("\n[1] 5 人局：每座当狼的频率是否均匀（卡方检验）")
    N = 8000
    wolf = collections.Counter()
    for _ in range(N):
        seed = random.SystemRandom().randrange(1, 2 ** 31)
        for s in build_seats(cfg, 5, shuffle=True, seed=seed):
            if s.is_wolf:
                wolf[s.no] += 1

    expected = N * ROLE_SETS[5].count("Werewolf") / 5
    chi2 = sum((wolf[no] - expected) ** 2 / expected for no in range(1, 6))
    print(f"     期望每座当狼 {expected:.0f} 次，实际 {dict(sorted(wolf.items()))}")
    check(f"卡方 {chi2:.2f} < {CHI2_CRITICAL}（分布均匀）", chi2 < CHI2_CRITICAL)

    print("\n[2] 不抽卡时确实固定在 1-3 号（反面对照）")
    seats = build_seats(cfg, 5, shuffle=False)
    fixed = [s.no for s in seats if s.is_wolf]
    check("shuffle=False -> 狼固定在前几个座位", fixed == [1, 2], f"实际 {fixed}")

    print("\n[3] 同一 seed 必须可复现")
    a = [s.role for s in build_seats(cfg, 7, shuffle=True, seed=12345)]
    b = [s.role for s in build_seats(cfg, 7, shuffle=True, seed=12345)]
    check("同 seed 结果一致", a == b, f"{a}")

    print("\n[4] 不同 seed 应当会变（连抽 20 次，至少出现 3 种不同排布）")
    seen = set()
    for i in range(20):
        seen.add(tuple(s.role for s in build_seats(cfg, 7, shuffle=True, seed=i * 7919)))
    check("不同 seed 能抽出不同排布", len(seen) >= 3, f"出现 {len(seen)} 种")

    print("\n[5] 抽卡不改变角色构成（只是换位置）")
    ok_all = True
    for count in PLAYER_COUNTS:
        want = sorted(ROLE_SETS[count])
        got = sorted(s.role for s in build_seats(cfg, count, shuffle=True, seed=999))
        if want != got:
            ok_all = False
            print(f"     ❌ {count} 人：期望 {want}，实际 {got}")
    check("3-10 人抽卡后角色构成不变", ok_all)

    print("\n[6] resolve_seed：config 里为 null 时每局不同")
    seeds = {resolve_seed(cfg) for _ in range(200)}
    check("连续取 200 次种子不重复", len(seeds) >= 199, f"实际 {len(seeds)} 个不同值")

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：{FAILURES}")
        return 1
    print("✅ 抽卡机制全部通过（分布均匀、可复现、构成不变）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
