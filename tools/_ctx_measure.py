#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用真实 seat_*.jsonl 里的 messages 精确测量 token/字符比与上下文构成。

seat_*.jsonl 每条记录都存了**发出去的完整 messages**，所以不用猜：
  · system 主提示词多大
  · 大桌共享前缀有多少条 / 多少字符
  · 尾部块多少字符
  · 实测 prompt_tokens 对应的 token/字符比是多少
"""
from __future__ import annotations

import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
run = sys.argv[1] if len(sys.argv) > 1 else "runs/20261006_124115_12seat_web"
d = run if os.path.isabs(run) else os.path.join(ROOT, run)

recs = []
for p in glob.glob(os.path.join(d, "seat_*.jsonl")):
    no = int(os.path.basename(p).split("_")[1].split(".")[0])
    for line in open(p, encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        r["_seat"] = no
        recs.append(r)

ok = [r for r in recs if r.get("prompt_tokens") and r.get("messages")]
print(f"run={os.path.basename(d)}  记录 {len(recs)} 条，可用 {len(ok)} 条")


def parts(msgs):
    """返回 (system字符, 大桌字符, 尾部字符, 大桌条数)"""
    sysc = len(msgs[0]["content"]) if msgs else 0
    big = sum(len(m["content"]) for m in msgs[1:-1])
    tail = len(msgs[-1]["content"]) if len(msgs) > 1 else 0
    return sysc, big, tail, max(0, len(msgs) - 2)


print("\n{'天':>3} {'阶段':<16} {'座':>3} {'prompt':>8} {'字符':>8} {'tok/字':>7} "
      "| system  大桌  尾部")
rows = []
for r in ok:
    sc, bc, tc, n = parts(r["messages"])
    total = sc + bc + tc
    rows.append((r["day"], r["phase"], r["_seat"], r["prompt_tokens"], total,
                 r["prompt_tokens"] / total, sc, bc, tc, n))

rows.sort(key=lambda x: -x[3])
print(f"{'天':>3} {'阶段':<18} {'座':>3} {'prompt':>8} {'字符':>8} {'tok/字':>7} "
      f"{'system':>7} {'大桌':>7} {'尾部':>6}")
for day, ph, seat, pt, tot, ratio, sc, bc, tc, n in rows[:18]:
    print(f"{day:>3} {ph:<18} {seat:>3} {pt:>8} {tot:>8} {ratio:>7.3f} "
          f"{sc:>7} {bc:>7} {tc:>6}")

ratios = [x[5] for x in rows if x[3] > 3000]
if ratios:
    ratios.sort()
    print(f"\ntoken/字符比（prompt>3000 的 {len(ratios)} 条）："
          f"min={ratios[0]:.3f} 中位={ratios[len(ratios)//2]:.3f} max={ratios[-1]:.3f}")

# 逐天峰值 + 每天增量
print("\n=== 逐天 prompt 峰值 ===")
by = {}
for day, ph, seat, pt, *_ in rows:
    by.setdefault(day, []).append(pt)
prev = None
for day in sorted(by):
    mx = max(by[day])
    inc = "" if prev is None else f"   (+{mx - prev})"
    print(f"  第{day}天: 峰值 {mx:>7}  平均 {sum(by[day])//len(by[day]):>7}  "
          f"({len(by[day])} 次){inc}")
    prev = mx

# 峰值那条的构成
top = rows[0]
print(f"\n=== 峰值调用分解（第{top[0]}天 {top[1]} {top[2]}号）===")
print(f"  prompt_tokens 实测 = {top[3]}")
print(f"  纯字符总数 = {top[4]}   -> 实测 tok/字 = {top[5]:.3f}")
for label, val in (("system 主提示词", top[6]), ("大桌共享前缀", top[7]), ("尾部块", top[8])):
    print(f"    {label:<14} {val:>7} 字符 = {val/top[4]*100:>5.1f}%  "
          f"≈ {val*top[5]:>7.0f} tokens")

# 256K 结论
peak = max(x[3] for x in rows)
print(f"\n=== 结论 ===")
for lim, nm in ((128 * 1024, "128K"), (256 * 1024, "256K")):
    print(f"  {nm}: 峰值 {peak} = {peak/lim*100:.1f}%，余量 {lim-peak} tokens")
days = sorted(by)
if len(days) >= 3:
    incs = [max(by[days[i]]) - max(by[days[i - 1]]) for i in range(1, len(days))]
    incs = [i for i in incs if i > 0]
    if incs:
        per = sum(incs) / len(incs)
        print(f"  逐天峰值平均增长 = {per:.0f} tokens/天（有效样本 {len(incs)} 天）")
        print(f"  -> 256K 还能再跑 ≈ {(256*1024-peak)/per:.0f} 天，"
              f"合计 ≈ {len(days) + (256*1024-peak)/per:.0f} 天")

# ---------- 按各家 tokenizer 折算：同一份中文，token 数差 1.5 倍 ----------
print("\n=== 同一份中文，各 tokenizer 的成本差 ===")
print("  （用实测 ratio 聚类；ratio 直接来自各家 API 返回的 prompt_tokens）")
import statistics  # noqa: E402
fam = {}
for day, ph, seat, pt, tot, ratio, *_ in rows:
    if pt < 3000:
        continue
    key = round(ratio, 1)
    fam.setdefault(key, []).append((seat, tot, pt))
print(f"  {'tok/字':>8} {'样本':>5} {'代表座位':<20} {'该比率下的 25.3K 字符大桌':>24}")
for k in sorted(fam):
    v = fam[k]
    seats_here = sorted({s for s, _, _ in v})
    print(f"  {k:>8} {len(v):>5} {str(seats_here)[:20]:<20} "
          f"{25312*k:>20.0f} tokens")

# ---------- 终局上下文预算（chars × 各家 ratio） ----------
print("\n=== 终局上下文预算（同一局，同一份中文，只换 tokenizer）===")
SYS_CH, BIG_CH, TAIL_MAX = 1373, 25312, 3282   # 实测：system / 大桌全量 / 最大尾部块
print(f"  实测字符：system {SYS_CH} + 大桌 {BIG_CH} + 尾部 {TAIL_MAX} = "
      f"{SYS_CH + BIG_CH + TAIL_MAX} 字符")
print(f"  {'模型族':<22} {'tok/字':>7} {'需要 prompt':>12} {'占 256K':>9} {'余量':>9}")
FAM = [("DeepSeek/Qwen/豆包/GLM/Kimi/MiniMax", 0.73),
       ("Grok/StepFun/小米 mimo", 0.79),
       ("Claude", 1.07),
       ("GPT tt-6-sol", 1.15)]
for name, r in FAM:
    need = (SYS_CH + BIG_CH + TAIL_MAX) * r
    print(f"  {name:<22} {r:>7.2f} {need:>12.0f} {need/(256*1024)*100:>8.1f}% "
          f"{256*1024-need:>9.0f}")

print("\n=== 每天要重放的量 vs 每天新增的量（256K 的真实消耗口径）===")
print(f"  · 每题都要重放：system {SYS_CH} + 大桌前缀（第1天末 12012 字符 → "
      f"终局 {BIG_CH} 字符）")
print(f"  · 每天新增：第1天 12012 字符，第2天起稳态 ≈2660 字符/天")
for r in (0.73, 1.07):
    grow = 2660 * r
    print(f"    tok/字={r}: 峰值外推 +{grow:.0f} tokens/天，"
          f"256K 上限在第 {(256*1024 - (SYS_CH+BIG_CH+TAIL_MAX)*r)/grow:.0f} 天之后才触到")


