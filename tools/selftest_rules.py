#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则单测 —— 胜负判定。

覆盖率而不是硬编码：对配置表里的**每一个人数**都验一遍，
这样改了 ROLE_SETS 之后测试不会悄悄失效。

    parity（默认）：存活好人 ≤ 存活狼人 → 狼胜；狼全灭 → 好人胜
    side（屠边）  ：杀光所有神职 或 所有平民 → 狼胜

用法：python tools/selftest_rules.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiwolf.engine import GOD_ROLES, Game  # noqa: E402
from aiwolf.seats import PLAYER_COUNTS, ROLE_SETS, build_seats, load_config  # noqa: E402

FAILURES = []
CONFIG = os.path.join(ROOT, "config", "seats.json")


def check(name: str, got, want) -> None:
    ok = got == want
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + ("" if ok else f"   期望 {want!r}，实际 {got!r}"))
    if not ok:
        FAILURES.append(name)


def make_game(count: int, win_condition: str = "parity") -> Game:
    cfg = load_config(CONFIG)
    seats = build_seats(cfg, count)
    return Game(seats, relay=None, verbose=False, win_condition=win_condition)


def kill(game: Game, nos) -> None:
    for n in nos:
        game.by_no[n].alive = False


def groups(game: Game):
    roles = {s.no: s.role for s in game.seats}
    return (
        [n for n, r in roles.items() if r == "Werewolf"],
        [n for n, r in roles.items() if r in GOD_ROLES],
        [n for n, r in roles.items() if r == "Villager"],
    )


def main() -> int:
    print("\n[1] 角色配置表自检")
    for count in PLAYER_COUNTS:
        roles = ROLE_SETS[count]
        ok = len(roles) == count
        print(f"  {'✅' if ok else '❌'} {count:>2} 人：{roles}")
        if not ok:
            FAILURES.append(f"{count} 人局角色数不等于人数")

    print("\n[2] 每一个人数：开局未分胜负 + 狼全灭则好人胜")
    for count in PLAYER_COUNTS:
        game = make_game(count)
        wolves, _, _ = groups(game)
        start_ok = game.check_win() is None
        kill(game, wolves)
        good_win = game.check_win() == "good"
        mark = "✅" if (start_ok and good_win) else "❌"
        print(f"  {mark} {count:>2} 人：开局继续={start_ok}，狼全灭→好人胜={good_win}")
        if not (start_ok and good_win):
            FAILURES.append(f"{count} 人：开局/狼全灭判定")

    print("\n[3] parity：好人多一个时继续，相等时狼胜")
    for count in PLAYER_COUNTS:
        game = make_game(count)
        wolves, _, _ = groups(game)
        good = [s.no for s in game.seats if not s.is_wolf]
        gap = len(good) - len(wolves)
        if gap <= 0:
            print(f"  ⏭  {count:>2} 人：开局即狼胜（好人 {len(good)} ≤ 狼 {len(wolves)}），跳过")
            continue
        # 先杀掉 gap 个好人，此时两边相等 -> 狼胜
        kill(game, good[:gap])
        eq = game.check_win()
        # 复活一个好人，此时好人多一个 -> 继续
        game.by_no[good[gap - 1]].alive = True
        one_more = game.check_win()
        mark = "✅" if (eq == "wolf" and one_more is None) else "❌"
        print(f"  {mark} {count:>2} 人：相等→{eq}，多一个→{one_more}")
        if not (eq == "wolf" and one_more is None):
            FAILURES.append(f"{count} 人：parity 边界")

    print("\n[4] side（屠边）：神职全灭 / 平民全灭 → 狼胜")
    for count in PLAYER_COUNTS:
        game = make_game(count, "side")
        _, gods, villagers = groups(game)
        if not gods or not villagers:
            print(f"  ⏭  {count:>2} 人：神职 {len(gods)} 平民 {len(villagers)}，不适用")
            continue
        g1 = make_game(count, "side")
        _, gods1, _ = groups(g1)
        kill(g1, gods1[:-1])
        partial = g1.check_win()
        kill(g1, gods1[-1:])
        full = g1.check_win()
        mark = "✅" if (partial is None and full == "wolf") else "❌"
        print(f"  {mark} {count:>2} 人：神职剩一个→{partial}，全灭→{full}")
        if not (partial is None and full == "wolf"):
            FAILURES.append(f"{count} 人：屠神职")

        g2 = make_game(count, "side")
        _, _, vill2 = groups(g2)
        kill(g2, vill2[:-1])
        partial2 = g2.check_win()
        kill(g2, vill2[-1:])
        full2 = g2.check_win()
        mark = "✅" if (partial2 is None and full2 == "wolf") else "❌"
        print(f"  {mark} {count:>2} 人：平民剩一个→{partial2}，全灭→{full2}")
        if not (partial2 is None and full2 == "wolf"):
            FAILURES.append(f"{count} 人：屠平民")

    print("\n[5] side：空集合不能误判")
    game = make_game(9, "side")
    _, _, villagers = groups(game)
    game.setup_villagers = []
    kill(game, villagers)
    check("开局无平民时，平民全灭不触发狼胜", game.check_win(), None)

    game = make_game(9, "side")
    _, gods, _ = groups(game)
    game.setup_gods = []
    kill(game, gods)
    check("开局无神职时，神职全灭不触发狼胜", game.check_win(), None)

    print("\n[6] 两套规则的差异：屠边结束得更早")
    # 7 人局：2 狼 5 好人（预/女巫/猎人 + 2 民）。
    # 杀掉 2 个平民后剩 3 个神职 vs 2 狼：
    #   parity：2 < 3，继续
    #   side  ：平民全灭，狼胜
    game_p = make_game(7, "parity")
    game_s = make_game(7, "side")
    for g in (game_p, game_s):
        _, _, vill = groups(g)
        kill(g, vill)
    check("parity：2狼 vs 3神职，好人更多 -> 继续", game_p.check_win(), None)
    check("side  ：平民全灭 -> 狼胜（结束更早）", game_s.check_win(), "wolf")

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：")
        for f in FAILURES:
            print(f"   · {f}")
        return 1
    print("✅ 胜负判定全部通过（parity 与 side，覆盖 3-10 人）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
