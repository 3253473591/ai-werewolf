"""对局产物：`events.jsonl` / `summary.json` 的组装。

**为什么要单独抽一个模块**：CLI（`run_text.py`）和 Web（`server.py`）两条路径
本来各写各的，结果 Web 路径一直没写 `summary.json` —— 而 M2 的验收标准之一
正是"每座的缓存命中率被记录进 summary"。同一个东西写两遍，迟早只写对一遍。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .events import Kind


def call_stats(relay) -> Dict[str, Any]:
    """把 relay.stats 汇总成几个一眼能看的数。

    ⚠️ `failed` 单独看会误导：**尝试级的失败**（超时、被截断后翻倍重试）
    只要最终拿到了结果就不会记进 `error`。所以额外给出 `attempts_retried`
    和 `attempts_total`，否则"0 次失败"会被读成"一遍过"。
    """
    stats: List[dict] = list(getattr(relay, "stats", []) or [])
    retried = [s for s in stats if (s.get("attempts") or 1) > 1]
    return {
        "calls_total": len(stats),
        "calls_failed": sum(1 for s in stats if s.get("error")),
        "calls_truncated": sum(1 for s in stats if s.get("truncated")),
        "calls_substituted": sum(1 for s in stats if s.get("substituted")),
        "calls_retried": len(retried),
        "attempts_total": sum((s.get("attempts") or 1) for s in stats),
        "latency_s_total": round(sum((s.get("latency_s") or 0) for s in stats), 1),
        "prompt_tokens_total": sum((s.get("prompt_tokens") or 0) for s in stats),
        "cached_tokens_total": sum((s.get("cached_tokens") or 0) for s in stats),
        # ★ 成本口径（2026-10-06 加）。
        #
        # 以前只汇总 prompt / cached，**输出侧一个数都没有**，于是"这一局花了多少"
        # 只能靠人肉扒 seat_*.jsonl。而这里最该盯的是两项：
        #   · reasoning_tokens_total —— 实测占输出的 80% 上下，钱主要花在"想"上
        #   · wasted_completion_tokens_total —— 翻倍重试/超时丢弃的那些尝试，
        #     **同样要计费**，但只记最终 usage 的话它们在账单上是隐形的
        "completion_tokens_total": sum((s.get("completion_tokens") or 0) for s in stats),
        "reasoning_tokens_total": sum((s.get("reasoning_tokens") or 0) for s in stats),
        "wasted_completion_tokens_total": sum(
            (s.get("wasted_completion_tokens") or 0) for s in stats),
    }


def build_summary(
    *,
    seats: List,
    relay,
    winner: str,
    days: int,
    run_dir: str = "",
    stub: bool = False,
    seed: Optional[int] = None,
    shuffled: bool = True,
    shared_prefix_ok: Optional[bool] = None,
    no_model_names_ok: Optional[bool] = None,
    audit: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> Dict[str, Any]:
    """组装一份完整的 summary。两条路径共用，字段名保持向后兼容。"""
    parse_failures = int((extra or {}).get("parse_failures", 0))
    stats = call_stats(relay)
    summary: Dict[str, Any] = {
        "seats": len(seats),
        "stub": stub,
        "winner": winner,
        "days": days,
        "role_seed": seed,
        "shuffled": shuffled,
        "roles": {s.label: s.role for s in seats},
        "roles_cn": {s.no: s.role_cn for s in seats},
        "endpoints": {s.no: s.endpoint for s in seats},
        "shared_prefix_ok": shared_prefix_ok,
        "no_model_names_ok": no_model_names_ok,
        "parse_failures": parse_failures,
        "run_dir": run_dir,
        "calls": getattr(relay, "stats", []) or [],
    }
    summary.update(stats)
    if audit is not None:
        summary["audit"] = audit
    if extra:
        for k, v in extra.items():
            if k not in summary:
                summary[k] = v
    return summary


def write_summary(run_dir: str, summary: Dict[str, Any]) -> str:
    path = os.path.join(run_dir, "summary.json")
    os.makedirs(run_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def count_parse_failures(log) -> int:
    return sum(1 for ev in log if ev.kind is Kind.PARSE_FAIL)
