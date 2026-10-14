#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一局的产物算成一份 M2 报告：逐座延迟 / 缓存命中 / 截断 / 重试 / 隔离审计。

    python tools/report_run.py runs/20261005_131915_9seat_web

**为什么需要它**：Web 路径（浏览器开局）只写 `events.jsonl` + `seat_*.jsonl`，
不写 `summary.json`，而 M2 的验收标准之一正是"每座的缓存命中率被记录进 summary"。
这个脚本只读那两份产物，所以 CLI 局和浏览器局都能出同样的报告。

角色不用你记 seed —— 从事件流反推：小桌里出现过的人就是狼，
预言家/女巫/猎人的私密事件里有记录。推不出来的按村民处理（脚本会标注）。
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiwolf.audit import audit_prompts          # noqa: E402
from aiwolf.events import EventLog              # noqa: E402
from aiwolf.seats import Seat, load_config      # noqa: E402

ROLE_BY_PHASE = {"NIGHT_SEER": "Seer", "NIGHT_WITCH": "Witch", "DAY_HUNTER": "Hunter"}


def load_run(run_dir: str):
    log = EventLog()
    with open(os.path.join(run_dir, "events.jsonl"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            log.append(day=d["day"], phase=d["phase"], channel=d["channel"], kind=d["kind"],
                       actor=d["actor"], to_seat=d["to_seat"], text=d["text"], **d["data"])
    return log


def infer_roles(log, cfg) -> tuple[list[Seat], list[str]]:
    """从事件流反推每个座位的角色。"""
    nos = sorted({e.actor for e in log if e.actor} | {e.to_seat for e in log if e.to_seat})
    if not nos:
        nos = list(range(1, 10))
    defs = {int(s["no"]): s for s in cfg["seats"]}
    guessed = {n: "" for n in nos}
    for e in log:
        ch = e.channel.value
        if ch == "wolf" and e.actor:
            guessed[e.actor] = "Werewolf"
        elif ch == "private" and e.to_seat and e.phase in ROLE_BY_PHASE and not guessed[e.to_seat]:
            guessed[e.to_seat] = ROLE_BY_PHASE[e.phase]
    notes = []
    for n in nos:
        if not guessed[n]:
            guessed[n] = "Villager"
            notes.append(f"{n}号 角色推不出来（这局没走过它的专属事件），按村民算")
    seats = []
    for n in nos:
        d = defs.get(n, {})
        seats.append(Seat(no=n, label=d.get("label", f"座{n}"), model=d.get("model", "?"),
                          role=guessed[n], endpoint=d.get("endpoint", "")))
    return seats, notes


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    run_dir = sys.argv[1]
    if not os.path.isabs(run_dir):
        run_dir = os.path.join(ROOT, run_dir)
    if not os.path.isdir(run_dir):
        print(f"❌ 没有这个目录：{run_dir}")
        return 2

    cfg = load_config(os.path.join(ROOT, "config", "seats.json"))
    log = load_run(run_dir)
    seats, notes = infer_roles(log, cfg)

    print("=" * 104)
    print(f"对局报告 · {os.path.basename(run_dir)}")
    print("角色（从事件流反推）：" + "  ".join(f"{s.no}={s.role_cn}" for s in seats))
    for n in notes:
        print("  ⚠ " + n)
    print("=" * 104)

    rows = []

    def out_tokens(r: dict) -> int:
        """输出 token。老对局没有顶层字段，退回读 usage（同一份数据，两条路径）。"""
        v = r.get("completion_tokens")
        if v is None:
            v = (r.get("usage") or {}).get("completion_tokens")
        return int(v or 0)

    def think_tokens(r: dict) -> int:
        v = r.get("reasoning_tokens")
        if v is None:
            v = ((r.get("usage") or {}).get("completion_tokens_details") or {}) \
                .get("reasoning_tokens")
        return int(v or 0)

    def waste_tokens(r: dict) -> int:
        # 2026-10-06 之前的对局没有这个字段：**读出来是 0，不代表真没烧**
        return int(r.get("wasted_completion_tokens") or 0)

    for s in seats:
        p = os.path.join(run_dir, f"seat_{s.no}.jsonl")
        recs = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()] if os.path.exists(p) else []
        lat = sorted(r.get("latency_s") or 0 for r in recs)
        cache = [r.get("cache_ratio") or 0 for r in recs]
        rows.append({
            "no": s.no, "label": s.label, "role": s.role_cn, "ep": s.endpoint,
            "calls": len(recs),
            "p50": st.median(lat) if lat else 0, "max": lat[-1] if lat else 0,
            "sum": sum(lat),
            "prompt": max(((r.get("prompt_tokens") or 0) for r in recs), default=0),
            "cache": (sum(cache) / len(cache)) if cache else 0,
            "trunc": sum(1 for r in recs if r.get("truncated")),
            "retry": sum(max(0, (r.get("attempts") or 1) - 1) for r in recs),
            "err": sum(1 for r in recs if r.get("error")),
            # 成本：输出侧。老对局没有 wasted 字段，读出来是 0（不是真花了 0）。
            "out": sum(out_tokens(r) for r in recs),
            "think": sum(think_tokens(r) for r in recs),
            "waste": sum(waste_tokens(r) for r in recs),
            "has_waste_field": any("wasted_completion_tokens" in r for r in recs),
        })

    print(f"\n{'座位':<9}{'角色':<6}{'通道':<6}{'调用':>4}{'中位延迟':>10}{'最慢':>9}"
          f"{'累计':>8}{'最大prompt':>11}{'缓存率':>8}{'截断':>5}{'额外重试':>9}{'失败':>5}")
    print("-" * 104)
    for r in rows:
        print(f"{r['label']:<9}{r['role']:<6}{r['ep']:<6}{r['calls']:>4}"
              f"{r['p50']:>9.1f}s{r['max']:>8.1f}s{r['sum']:>7.0f}s{r['prompt']:>11}"
              f"{r['cache'] * 100:>7.1f}%{r['trunc']:>5}{r['retry']:>9}{r['err']:>5}")
    print("-" * 104)
    total = sum(r["sum"] for r in rows)
    print(f"合计 {sum(r['calls'] for r in rows)} 次调用 · 串行等待 {total:.0f}s（{total / 60:.1f} 分钟）"
          f" · 额外重试 {sum(r['retry'] for r in rows)} 次")

    out = sum(r["out"] for r in rows)
    think = sum(r["think"] for r in rows)
    waste = sum(r["waste"] for r in rows)
    tracked = any(r["has_waste_field"] for r in rows)
    if out or think or waste:
        print(f"\n{'座位':<9}{'输出tokens':>12}{'其中思维链':>12}{'思维链占比':>11}"
              f"{'被丢弃(白烧)':>14}{'白烧占输出':>12}")
        print("-" * 104)
        for r in rows:
            prop = f"{100 * r['think'] / r['out']:.0f}%" if r["out"] else "-"
            wprop = f"{100 * r['waste'] / r['out']:.0f}%" if r["out"] else "-"
            print(f"{r['label']:<9}{r['out']:>12}{r['think']:>12}{prop:>11}"
                  f"{(r['waste'] if tracked else '?')!s:>14}{wprop:>12}")
        print("-" * 104)
        print(f"{'合计':<9}{out:>12}{think:>12}"
              f"{(f'{100 * think / out:.0f}%' if out else '-'):>11}"
              f"{(waste if tracked else '?')!s:>14}")
        if not tracked:
            print("  ⚠ 「被丢弃」显示 ? —— 这是 2026-10-06 之前的对局，"
                  "中间尝试没有记账（钱花了但查不到），不代表没烧")
        elif not waste:
            print("  这局没有发生翻倍重试/失败重试，所以没有白烧的输出")

    evs = list(log)
    pub = [e for e in evs if e.channel.value == "public"]
    votes = [e for e in pub if e.data.get("action") == "vote"]
    reasons = [e for e in pub if e.phase in ("DAY_VOTE", "DAY_VOTE_PK")
               and e.kind.value == "speech" and e.data.get("action") is None]
    print(f"\n事件 {len(evs)} 条（公开 {len(pub)}）")
    print(f"  投票 {len(votes)} 次，带理由 {len(reasons)} 条，弃票 {sum(1 for v in votes if not v.data.get('target'))} 次")
    print(f"  自曝 {sum(1 for e in evs if e.data.get('exploded'))} 次"
          f" · PK 环节 {sum(1 for e in evs if e.phase in ('DAY_PK_SPEECH', 'DAY_VOTE_PK'))} 条"
          f" · 未发言 {sum(1 for e in evs if e.data.get('silent'))} 次"
          f" · 上帝视角诊断 {sum(1 for e in evs if e.channel.value == 'private' and e.to_seat is None)} 条")

    # 每座最慢的 3 次，方便定位瓶颈
    slow = []
    for r in rows:
        p = os.path.join(run_dir, f"seat_{r['no']}.jsonl")
        for rec in ([json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
                    if os.path.exists(p) else []):
            slow.append((rec.get("latency_s") or 0, r["label"], rec.get("phase"),
                         rec.get("finish_reason"), rec.get("attempts")))
    slow.sort(reverse=True)
    print("\n最慢的 6 次调用：")
    for lat, label, phase, fin, att in slow[:6]:
        flag = "  ← 截断" if fin == "length" else ""
        print(f"  {lat:>7.1f}s  {label:<9} {phase:<16} 尝试 {att} 次{flag}")

    a = audit_prompts(run_dir, log, seats)
    print("\n信息隔离审计：" + ("✅ 零泄漏" if a["ok"] else f"❌ {len(a['violations'])} 处泄漏"))
    for v in a["violations"][:10]:
        print("   ·", v)
    return 0 if a["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
