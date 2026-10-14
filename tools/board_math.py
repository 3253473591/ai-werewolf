#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""板子难度量化：给定 (狼数, 好人数, 村民数)，算好人能承受几次误杀。

为什么要算这个而不是"看着感觉一下"：胜负条件是 parity（存活好人 ≤ 存活狼人）
**或** 屠边（平民全灭 / 神职全灭）。倍率好不好，不看狼占比，看两件事：

  1. **容错次数**：好人在 parity 触发前最多能白白损失几个好人
     （每一次夜刀、每一次白天误放逐，都算一次）
  2. **有效村民数**：走屠边规则时，初始村民是 1 个还是 4 个，直接决定
     狼队要不要"专门去屠民" —— 只有一个村民时，那一刀就是白送的胜利条件

用法：
    python tools/board_math.py                  # 对比现有板子与候选板子
    python tools/board_math.py 12:5:7           # 自己算一组 人数:狼:好人
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiwolf.seats import ROLE_SETS  # noqa: E402


def max_mistakes(good: int, wolves: int, limit: int = 12) -> int:
    """模拟报出：好人白天**最多能投错几次**还能赢。

    ★ 不要用 `优势 // 2` 这类公式去推（第一版就是这么错的）：
      狼刀是每轮必然发生的，而"狼全灭 -> 好人胜"的优先级又在 parity 之上，
      两件事叠加之后，小人数板子（如 4 人局）的边界根本不是那个比例。
      让模拟自己跑出来，然后断言"多错一次必输" —— 这样两处算法不可能各说一套。
    """
    best = -1
    for k in range(limit + 1):
        res, _ = simulate(good, wolves, mistakes=k)
        if res == "good":
            best = k
        else:
            break
    return best


def simulate(good: int, wolves: int, mistakes: int):
    """逐天推演：夜晚狼刀，白天放逐。

    ``mistakes`` = 白天总共允许投错几次（投错的那些天好人 -1，其余天狼 -1）。

    ★ 检查点只在**白天结算**处，这一点极易写错（第一版就错了）：
      parity 是"存活好人 <= 存活狼人"，它在**白天放逐之后**判定。
      如果把"夜晚狼刀之后"也判一次，就会把容错次数少算一次 ——
      夜刀后进入的是下一天白天，那时好人还有归票机会，比赛并没结束。
      所以正确的顺序是：放逐 -> 判胜 -> 入夜 -> 次日。

    mistakes=0 是**最快路径**（每天都投对）；把 mistakes 拉到最大就是**容错边界**。
    """
    g, w = good, wolves
    left = mistakes
    log = [("开局", g, w)]
    for day in range(1, 20):
        if left > 0:
            left -= 1
            g -= 1
            log.append((f"第{day}天 误杀好人", g, w))
        else:
            w -= 1
            log.append((f"第{day}天 投出狼", g, w))
        if w <= 0:
            return "good", log
        if g <= w:                      # ← 唯一的 parity 检查点：白天结算处
            return "wolf", log
        g -= 1                          # 夜晚狼刀一个好人，之后不判定
        log.append((f"第{day}夜 狼刀好人", g, w))
    return "max_days", log


def describe(name: str, roles) -> dict:
    n = len(roles)
    wolves = roles.count("Werewolf")
    villagers = roles.count("Villager")
    gods = n - wolves - villagers
    good = n - wolves
    return {"name": name, "n": n, "wolves": wolves, "good": good,
            "villagers": villagers, "gods": gods,
            "adv": good - wolves, "mm": max_mistakes(good, wolves)}


def main() -> int:
    cands = []
    for n, roles in sorted(ROLE_SETS.items()):
        cands.append(describe(f"现有 {n} 人", roles))

    # 候选板子：5 狼 / 守卫 / 1-2 村民
    # ★ 用列表字面量逐项写，别用 ["Villager"]*k 之外的花招 ——
    #   第一版这里 `["Werewolf"]*5 + [...] + ["Villager"]*2` 少写一项，
    #   于是"12 人"其实只有 11 个角色，统计出来的板子是错的。
    cands.append(describe("候选 11 人 5狼", [
        "Werewolf", "Werewolf", "Werewolf", "Werewolf", "Werewolf",
        "Seer", "Witch", "Hunter", "Guard", "Villager", "Villager"]))
    cands.append(describe("候选 12 人 5狼 2民", [
        "Werewolf", "Werewolf", "Werewolf", "Werewolf", "Werewolf",
        "Seer", "Witch", "Hunter", "Guard", "Idiot", "Villager", "Villager"]))
    cands.append(describe("候选 12 人 5狼 1民", [
        "Werewolf", "Werewolf", "Werewolf", "Werewolf", "Werewolf",
        "Seer", "Witch", "Hunter", "Guard", "Idiot", "Villager"]))

    # 统计自检：人数必须和板子名对得上，否则整张表都是错的
    for c in cands:
        assert c["wolves"] + c["good"] == c["n"], c
        assert c["villagers"] + c["gods"] == c["good"], c

    # ★ 两处算法必须自洽：max_mistakes() 说"能错 N 次"，那 N+1 次就必须输。
    for c in cands:
        mm = c["mm"]
        res_at, _ = simulate(c["good"], c["wolves"], mistakes=mm)
        res_over, _ = simulate(c["good"], c["wolves"], mistakes=mm + 1)
        assert res_at == "good", (c["name"], mm, res_at)
        assert res_over == "wolf", (c["name"], mm + 1, res_over)

    print(f"{'板子':<20}{'人':>3}{'狼':>4}{'好人':>5}{'村民':>5}{'神职':>5}"
          f"{'狼占比':>8}{'好人优势':>9}{'能投错':>7}")
    print("-" * 70)
    for c in cands:
        print(f"{c['name']:<20}{c['n']:>3}{c['wolves']:>4}{c['good']:>5}"
              f"{c['villagers']:>5}{c['gods']:>5}"
              f"{c['wolves'] / c['n'] * 100:>7.1f}%{c['adv']:>9}{c['mm']:>7}")

    print("\n【逐天推演】白天投错 k 次之后的结果")
    for name, good, wolves in (("现有 12人 4狼", 8, 4),
                               ("现有 11人 4狼", 7, 4),
                               ("候选 12人 5狼 2民", 7, 5),
                               ("候选 11人 5狼 2民", 6, 5),
                               ("候选 12人 5狼 1民", 6, 5)):
        mm = max_mistakes(good, wolves)
        print(f"\n  ■ {name}：{wolves} 狼 vs {good} 好人，"
              f"优势 +{good - wolves}，**最多能投错 {mm} 次**")
        for k in range(0, mm + 2):
            res, log = simulate(good, wolves, mistakes=k)
            body = " -> ".join(f"{t}({g}/{w})" for t, g, w in log[1:])
            mark = "✅" if res == "good" else "❌"
            print(f"    {mark} 投错 {k} 次 -> {res:<8} {body}")

    print("\n【警长票的算术】警长 1.5 票，狼队集中投票 vs 好人分散")
    for n, wolves in ((12, 4), (12, 5), (11, 5)):
        good = n - wolves
        print(f"  {n} 人 {wolves} 狼：狼队 {wolves:.1f} 票"
              f"（若拿到警长则 {wolves + 0.5:.1f} 票）"
              f"，好人 {good} 票。狼只需让好人分票即可控场 —— "
              f"好人要归票成功，需要 {wolves + 1}/{good} 的人站对边")
    return 0


if __name__ == "__main__":
    sys.exit(main())
