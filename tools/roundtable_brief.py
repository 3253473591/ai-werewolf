#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给「赛后复盘圆桌」拼一份简报。

为什么要单独拼：圆桌的每个参与者是独立 agent，**看不到这场对话**。
他们要知道三件事才能吵起来：
  1. 这局到底发生了什么（大桌公开记录，按天）；
  2. 全员底牌（赛后已摊开）；
  3. **其他人赛后说了什么** —— 这是"掐架"的燃料。没有这一条，
     他们只会各自复述自己的感言，互相不接话。

边界（跟真故障隔离一脉相承）：
  · 只取 `channel == "public"` 的事件。上帝视角、狼队小桌、私密通道都不进；
  · **不带真实故障原文**。12 号那三次调用失败在现场就是"本轮未发言" /
    "选择不开枪"，所以简报里也只写这个 —— 让 2 号去问它为什么，
    而不是让所有人直接知道"是 API 挂了"。这既保住了元层面那张牌，
    也让圆桌的质疑**基于场上真实可见的信息**。

用法：
    python tools/roundtable_brief.py runs/20261006_220858_12seat_web
    python tools/roundtable_brief.py runs/xxx --seats 1,6,2,4 -o brief.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--seats", default="", help="只列这些座位的感言，如 1,6,2,4")
    ap.add_argument("-o", "--out", default="", help="写进文件（默认打印）")
    args = ap.parse_args()

    run = args.run if os.path.isabs(args.run) else os.path.join(ROOT, args.run)
    summary = json.load(open(os.path.join(run, "summary.json"), encoding="utf-8"))
    roles = {int(k): v for k, v in summary["roles_cn"].items()}
    evs = [json.loads(l) for l in open(os.path.join(run, "events.jsonl"),
                                      encoding="utf-8") if l.strip()]
    refl = {}
    rp = os.path.join(run, "reflections.json")
    if os.path.exists(rp):
        refl = {t["seat"]: t for t in json.load(open(rp, encoding="utf-8"))["transcript"]}

    only = [int(x) for x in args.seats.replace("，", ",").split(",") if x.strip()] \
        if args.seats else sorted(roles)

    L = []
    L.append(f"# 赛后复盘圆桌 · {os.path.basename(run)}")
    L.append("")
    L.append(f"**结果**：{'狼人阵营' if summary.get('winner') == 'wolf' else '好人阵营'}"
             f"获胜，共 {summary.get('days')} 天。")
    L.append("")
    L.append("## 全员真实身份（赛后已摊开）")
    L.append("")
    L.append(" ｜ ".join(f"{no}号 {roles[no]}" for no in sorted(roles)))
    L.append("")

    # ---- 出局时间线 ----
    L.append("## 出局时间线")
    L.append("")
    for e in evs:
        t = e.get("text") or ""
        d = e.get("data") or {}
        if e["channel"] != "public":
            continue
        if "被放逐" in t and d.get("target"):
            L.append(f"- 第{e['day']}天 放逐 **{d['target']}号"
                     f"（{roles.get(int(d['target']), '?')}）**")
        elif t.startswith("昨晚死亡的是"):
            L.append(f"- 第{e['day']}天 夜里死 {t.replace('昨晚死亡的是：', '')}")
        elif t.startswith("昨晚是平安夜"):
            L.append(f"- 第{e['day']}天 平安夜（无人死亡）")
        elif "警徽移交给" in t:
            L.append(f"- 第{e['day']}天 {t[:60]}")
        elif "翻开白痴身份" in t:
            L.append(f"- 第{e['day']}天 {t[:60]}")
    L.append("")

    # ---- 大桌公开记录（掐架的原始素材）----
    L.append("## 大桌公开记录")
    L.append("")
    L.append("> 只有公开频道。席位沉默时现场看到的就是「本轮未发言」，")
    L.append("> 别的一概不知道 —— 不要假设任何场外信息。")
    L.append("")
    day = None
    for e in evs:
        if e["channel"] != "public":
            continue
        if e["day"] != day:
            day = e["day"]
            L.append(f"\n### 第 {day} 天\n")
        who = f"**{e['actor']}号**" if e.get("actor") else "公告"
        txt = (e.get("text") or "").replace("\n", " ").strip()
        if len(txt) > 700:
            txt = txt[:700] + "…（截断）"
        L.append(f"- `{e['phase']}` {who}：{txt}")

    # ---- 各座位赛后原话（燃料）----
    L.append("")
    L.append("## 各座位赛后原话")
    L.append("")
    for no in only:
        t = refl.get(no)
        if not t:
            continue
        L.append(f"### {no}号 {t['label']}（{t['role_cn']}）")
        L.append("")
        body = (t.get("text") or "").strip()
        L.append(body if body else "（没有说出话来）")
        L.append("")

    text = "\n".join(L)
    if args.out:
        out = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"已写入 {out}（{len(text)} 字）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
