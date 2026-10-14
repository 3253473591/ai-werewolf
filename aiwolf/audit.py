"""信息隔离审计 —— M1 的验收标准。

对应 DESIGN.md 第 10 节：**村民座位的 prompt 里永远不能出现任何 WOLF 频道内容、
别人的私密信息或真实身份。** 整个系统的生命线就是这一条。

审计对象是 ``seat_*.jsonl`` 里**实际发出去的完整 messages**，不是重建的视图——
只有这样才能证明"真的没漏"。
"""
from __future__ import annotations

import glob
import json
import os
from typing import Any, Dict, List

WOLF_HEADER = "狼队夜间频道"


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def audit_prompts(records_dir: str, log, seats: List) -> Dict[str, Any]:
    """拿事件流里的话语反查每个座位的实际 prompt，找出任何泄漏。"""
    by_no = {s.no: s for s in seats}
    private_texts = [(ev.to_seat, ev.text) for ev in log
                     if ev.channel.value == "private" and ev.text]
    labels = [(s.no, s.label) for s in seats if s.label]

    # ★ 只有"**只**在狼队频道出现过"的话才算秘密（2026-10-06 修）。
    #
    # 老写法是"狼队频道里的每一句都当秘密"，于是一句泛化文案会变成永久误报：
    # 实测 10 座局里狼队夜间频道有一条 `本轮未发言`（某只狼整晚没开口），
    # 而公开频道第 2/3 天也有三条一模一样的 `本轮未发言` 公告 —— 那是人人都该看到的。
    # 结果 5 个好人座位被判了 15 处"泄漏"，全是假的。
    # 判据应该是"这句信息**独属于**狼队"，光看它在狼队出现过毫无意义。
    public_texts = {ev.text for ev in log
                    if ev.channel.value == "public" and ev.text}
    wolf_events = [(ev.day, ev.text) for ev in log
                   if ev.channel.value == "wolf" and ev.text
                   and ev.text not in public_texts]

    violations: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {}

    for path in sorted(glob.glob(os.path.join(records_dir, "seat_*.jsonl"))):
        no = int(os.path.basename(path).split("_")[1].split(".")[0])
        seat = by_no.get(no)
        if seat is None:
            continue

        calls = max_prompt = errors = 0
        cache_ratios: List[float] = []
        leaks: List[str] = []

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                calls += 1
                max_prompt = max(max_prompt, rec.get("prompt_tokens") or 0)
                if rec.get("error"):
                    errors += 1
                if rec.get("cache_ratio") is not None:
                    cache_ratios.append(rec["cache_ratio"])

                messages = rec.get("messages", [])
                blob = "\n".join(_flatten(m.get("content")) for m in messages)
                where = f"第{rec.get('day')}天 {rec.get('phase')}"
                rec_day = rec.get("day") or 0

                if not seat.is_wolf:
                    if WOLF_HEADER in blob:
                        leaks.append(f"{where}：出现狼队频道标题")
                    for ev_day, t in wolf_events:
                        # ★ 时间顺序也要管：**后来的**狼队发言不可能泄进**先前**的提示词。
                        # 老写法拿全场狼队台词去比对，于是第 4 天夜里的发言
                        # 会去指控第 3 天的 prompt。按天过滤是最低成本的下界。
                        if ev_day and rec_day and ev_day > rec_day:
                            continue
                        if t in blob:
                            leaks.append(f"{where}：泄漏狼队频道内容「{t[:30]}」")

                for owner, t in private_texts:
                    if owner != seat.no and t in blob:
                        leaks.append(f"{where}：泄漏 {owner}号 的私密信息「{t[:30]}」")

                # 模型身份泄漏：只查尾部块。主提示词里"如GPT、Claude、DeepSeek等"
                # 是泛指对手类型，不构成座位到模型的映射，不算泄漏。
                if messages:
                    tail = _flatten(messages[-1].get("content"))
                    for owner, label in labels:
                        if label in tail:
                            leaks.append(f"{where}：尾部块出现模型名「{label}」"
                                         f"（{owner}号）")

        summary[seat.label] = {
            "no": seat.no,
            "role": seat.role,
            "calls": calls,
            "max_prompt_tokens": max_prompt,
            "avg_cache_ratio": round(sum(cache_ratios) / len(cache_ratios), 3) if cache_ratios else 0.0,
            "errors": errors,
            "leaks": leaks,
        }
        for leak in leaks:
            violations.append({"seat": seat.no, "label": seat.label, "detail": leak})

    return {"ok": not violations, "violations": violations, "seats": summary}
