#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把一局的事件流按时间顺序打成可读的战报。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CH = {"public": "大桌", "wolf": "狼队", "private": "私密"}


def main():
    run = sys.argv[1] if len(sys.argv) > 1 else ""
    d = run if os.path.isabs(run) else os.path.join(ROOT, run)
    log = [json.loads(l) for l in open(os.path.join(d, "events.jsonl"), encoding="utf-8")
           if l.strip()]
    summary = {}
    sp = os.path.join(d, "summary.json")
    if os.path.exists(sp):
        summary = json.load(open(sp, encoding="utf-8"))
    if summary:
        print("=" * 100)
        print(f"胜方: {summary.get('winner')} · 天数: {summary.get('days')} · 座位数: {summary.get('seats')}")
        print("角色:", "  ".join(f"{k}={v}" for k, v in (summary.get("roles_cn") or {}).items()))
        print("=" * 100)
    day = None
    for e in log:
        if e.get("day") != day:
            day = e.get("day")
            print(f"\n{'#' * 100}\n### 第 {day} 天\n{'#' * 100}")
        ch = CH.get(e.get("channel"), e.get("channel"))
        phase = e.get("phase") or ""
        kind = e.get("kind") or ""
        actor = e.get("actor")
        to = e.get("to_seat")
        who = f"{actor}号" if actor else "-"
        if to:
            who += f"→{to}号"
        text = (e.get("text") or "").replace("\n", "\n      ")
        data = e.get("data") or {}
        extra = ""
        if data:
            keys = {k: v for k, v in data.items()
                    if k in ("action", "target", "reason", "silent", "exploded",
                             "issues", "vote_counts", "winner", "substituted_by",
                             "truncated", "failed_seat")}
            if keys:
                extra = "  " + json.dumps(keys, ensure_ascii=False)
        print(f"[{ch}][{phase}][{kind}] {who}: {text}{extra}")


if __name__ == "__main__":
    main()
