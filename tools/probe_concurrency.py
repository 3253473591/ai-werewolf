#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发上限梯度测试：8 → 16 → 32 → 64，看从哪一档开始出现 429 / 非 200。

    python tools/probe_concurrency.py --base-url https://api.example/v1 \
        --api-key sk-xxx --model glm-5.2 --levels 8,16,32,64

为什么要单独一个脚本：`probe_relay.py` 的 T4 只测一档。而"并发上限是多少"
是个**阶梯问题** —— 只知道 x8 全过没有意义，要知道拐点在哪，才知道
`concurrency.global` 该配几。

⚠️ 会真的打并发、烧额度。默认请求很小（一句话），但并发数会打到通道上。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from probe_relay import t4_concurrency  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="并发梯度测试（会打并发）")
    ap.add_argument("--base-url", default=os.environ.get("RELAY_BASE_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("RELAY_API_KEY", ""))
    ap.add_argument("--model", required=True)
    ap.add_argument("--levels", default="8,16,32,64")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    if not args.base_url or not args.api_key:
        print("错误：缺少 base-url / api-key")
        return 2
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    print("=" * 74)
    print(f"并发梯度测试 · {args.model}")
    print(f"  base_url: {args.base_url}")
    print(f"  梯度    : {levels}")
    print("=" * 74)

    rows = []
    for n in levels:
        r = t4_concurrency(args.base_url, args.api_key, args.model, args.timeout, n)
        rows.append((n, r))
        good = r["success"]
        err = r.get("sample_error") or ""
        print(f"  x{n:<3} ok={good}/{n}  墙钟 {r['wall_s']:>6.2f}s  "
              f"{r['status_counts']}  {err[:110]}")
        # 出现非 200 就停：再往上打只是继续挨 429
        if good < n:
            print("  ↑ 这一档已经掉队，停止加码")
            break

    print()
    print("结论：")
    healthy = [n for n, r in rows if r["success"] == n]
    broke = [n for n, r in rows if r["success"] < n]
    if healthy:
        msg = f"  x{max(healthy)} 全部 200"
        if broke:
            msg += f"；x{min(broke)} 开始掉队（{rows[[x[0] for x in rows].index(min(broke))][1]['status_counts']}）"
        else:
            msg += "，梯度内没打到上限 —— 可以再往上加"
        print(msg)
        # 打 429 时给个具体的错误样本，便于对号入座
        for n, r in rows:
            if r["success"] < n and r.get("sample_error"):
                print(f"  x{n} 的报错样本：{r['sample_error'][:220]}")
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
