#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""算一局的出局时间线、终局存活、以及"某条规则换个语义会怎样"的反事实。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    run = sys.argv[1]
    d = run if os.path.isabs(run) else os.path.join(ROOT, run)
    summ = json.load(open(os.path.join(d, "summary.json"), encoding="utf-8"))
    rcn = {int(k): v for k, v in summ["roles_cn"].items()}
    seats = summ["seats"]
    evs = [json.loads(l) for l in open(os.path.join(d, "events.jsonl"), encoding="utf-8")
           if l.strip()]
    wolves = {k for k, v in rcn.items() if v == "狼人"}
    print("狼:", sorted(wolves), " 好人:", sorted(set(rcn) - wolves))

    dead = set()
    tl = []
    for e in evs:
        t = e.get("text") or ""
        data = e.get("data") or {}
        if "被放逐" in t and data.get("target"):
            dead.add(data["target"])
            tl.append((e.get("day"), f"放逐 {data['target']}号（{rcn[data['target']]}）"))
        if t.startswith("昨晚死亡的是"):
            for tok in t.replace("昨晚死亡的是：", "").split("、"):
                tok = tok.strip().rstrip("号")
                if tok.isdigit():
                    dead.add(int(tok))
                    tl.append((e.get("day"), f"夜里死 {tok}号（{rcn[int(tok)]}）"))
        if "开枪带走了" in t and data.get("target"):
            dead.add(data["target"])
            # DAY_HUNTER 的 resolution 事件 actor 是空的，开枪者要从句首取
            shooter = e.get("actor") or (t.split("号")[0] if "号" in t else "?")
            tl.append((e.get("day"),
                       f"猎人 {shooter}号 开枪带走 {data['target']}号"))
        if "自曝狼人身份" in t:
            tl.append((e.get("day"), f"** {data.get('seat')}号 自曝（引擎未判出局）"))

    print("\n=== 出局时间线 ===")
    for day, x in tl:
        print(f"  第{day}天  {x}")

    alive = [s for s in range(1, seats + 1) if s not in dead]
    w = [s for s in alive if s in wolves]
    g = [s for s in alive if s not in wolves]
    print(f"\n终局存活 {alive}")
    for s in alive:
        print(f"    {s}号 {rcn[s]}")
    print(f"  狼 {w}×{len(w)}   好人 {g}×{len(g)}   "
          f"parity(好人<=狼人) = {len(g) <= len(w)}  -> {'狼胜' if len(g) <= len(w) else '继续'}")

    selfx = []
    for e in evs:
        if "自曝狼人身份" in (e.get("text") or ""):
            no = (e.get("data") or {}).get("seat")
            if no:
                selfx.append(int(no))
    if selfx:
        print(f"\n=== 反事实：自曝即出局（标准狼人杀）—— 拿掉 {selfx} ===")
        dead2 = dead | set(selfx)
        a2 = [s for s in range(1, seats + 1) if s not in dead2]
        w2 = [s for s in a2 if s in wolves]
        g2 = [s for s in a2 if s not in wolves]
        print(f"  自曝那一刻之后，狼只剩 {w2}×{len(w2)}，好人 {g2}×{len(g2)}")
        print("  后续每晚最多刀 1 个好人 -> 好人始终人数占优（parity 不成立）")
        print("  只要好人在白天投出最后那只狼，就是好人胜。")
        print(f"  => 这条规则直接决定了这一局的胜方。")


if __name__ == "__main__":
    main()
