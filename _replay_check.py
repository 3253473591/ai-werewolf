# -*- coding: utf-8 -*-
"""用真实对局数据回放：新解析器 vs 旧行为（8号 MiniMax 的 4 次降级）。"""
from __future__ import annotations
import json, os, sys
RUN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs", "20261011_181251_12seat_web")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aiwolf.protocol import parse_decision, THINK_RE
from aiwolf.relay import extract_text

for name in sorted(os.listdir(RUN)):
    if not name.startswith("seat_"):
        continue
    for line in open(os.path.join(RUN, name), encoding="utf-8"):
        r = json.loads(line)
        if r.get("seat") != 8 or not (r.get("raw") or "").strip():
            continue
        raw = r["raw"]
        if not THINK_RE.search(raw):
            continue
        # 旧行为：整段原样解析 → 必然走 json_salvaged，speech 可能是草稿
        old = parse_decision(raw, r.get("phase") or "")
        # 新行为：先经传输层拆分，解析器只看得到答案
        content, reasoning = extract_text(
            {"choices": [{"message": {"content": raw}}]})
        new = parse_decision(content, r.get("phase") or "")
        print("=" * 78)
        print(f"第{r['day']}天 {r['phase']}")
        print(f"  旧: issues={old.issues} speech={len(old.speech):>4}字 "
              f"action={old.action.type}/{old.action.target}  {old.speech[:40]!r}")
        print(f"  新: issues={new.issues} speech={len(new.speech):>4}字 "
              f"action={new.action.type}/{new.action.target}  {new.speech[:40]!r}")
        print(f"  思维链搬进 reasoning: {len(reasoning)} 字（原来是 0）")
        if old.speech != new.speech:
            print(f"  ★ 公开内容变了：{old.speech!r} -> {new.speech[:60]!r}…")
