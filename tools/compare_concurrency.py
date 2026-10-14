#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发上限对比：同一个模型、同一套梯度，在**多条通道**上各打一遍。

    # 通道可以引用 config/endpoints.json 里的 id，也可以直接给 url|key
    python tools/compare_concurrency.py --model qwen3.7-max --levels 8,16,32 \
        --target ep3 \
        --target "中转站 B|https://https://relay-b.example/v1|sk-xxx"

为什么要一个单独的对比工具：**并发上限是"这条通道 + 这个模型"的属性**，
之前 ep3 只测到 12、另一条只测了 32，两批数据量级和模型都不同，没法比。
只有同模型同梯度并排跑，才知道换通道到底换来了多少余量。

⚠️ 会真的打并发、烧额度。key 只从命令行/配置读，不打印。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from probe_relay import t4_concurrency  # noqa: E402


def load_endpoint(eid: str):
    """从 config/endpoints.json 取一条通道（key 不打印）。"""
    path = os.path.join(ROOT, "config", "endpoints.json")
    cfg = json.load(open(path, encoding="utf-8"))
    for e in cfg.get("endpoints") or []:
        if e.get("id") == eid:
            return e.get("name") or eid, e.get("base_url", ""), e.get("api_key", "")
    raise SystemExit(f"❌ config/endpoints.json 里没有通道 {eid}")


def parse_target(spec: str):
    """`ep3` 或 `label|base_url|key` 或 `label|base_url|key|模型名`。

    最后一段是给"两条通道上模型名不一样"准备的：同一个模型在 中转站 A 上叫
    `grok-4-6`，在别家叫 `gk-4.6` —— 不写模型名就没法同模型对比。
    """
    if "|" not in spec:
        return load_endpoint(spec) + (None,)
    parts = spec.split("|")
    if len(parts) not in (3, 4):
        raise SystemExit(f"❌ 目标格式应为 label|base_url|key[|model]：{spec}")
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) == 4 else None)


def main() -> int:
    ap = argparse.ArgumentParser(description="多通道并发上限对比")
    ap.add_argument("--model", required=True, help="默认模型名（可被 target 里的覆盖）")
    ap.add_argument("--levels", default="8,16,32")
    ap.add_argument("--target", action="append", required=True,
                    help="通道：config 里的 id，或 label|base_url|key[|model]")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    targets = []
    for spec in args.target:
        label, base, key, model = parse_target(spec)
        targets.append((label, base, key, model or args.model))

    print("=" * 84)
    print(f"并发上限对比 · 梯度 {levels}")
    for label, base, _, model in targets:
        print(f"  {label:<12} {base}  模型={model}")
    print("=" * 84)

    results = {}
    for label, base, key, model in targets:
        if not key:
            print(f"⚠ {label} 没有 key，跳过")
            continue
        print(f"\n--- {label}  {base}  [{model}]")
        rows = []
        for n in levels:
            r = t4_concurrency(base, key, model, args.timeout, n)
            rows.append((n, r))
            flag = "✅" if r["success"] == n else "⚠️"
            print(f"  x{n:<3} {flag} ok={r['success']}/{n}  {r['wall_s']:>6.2f}s  "
                  f"{r['status_counts']}")
            for e in (r.get("errors") or [])[:2]:
                print(f"        {e}")
            if r["success"] < n:
                break
        results[label] = rows

    print("\n" + "=" * 84)
    print("对比")
    print("=" * 84)
    width = max((len(lab) for lab, _, _, _ in targets), default=10)
    print(f"{'通道':<{width}} " + " ".join(f"x{n:<10}" for n in levels))
    print("-" * 84)
    for label, _, _, _ in targets:
        if label not in results:
            continue
        row = []
        for n in levels:
            hit = next((r for k, r in results[label] if k == n), None)
            if hit is None:
                row.append("     -     ")
            elif hit["success"] == n:
                row.append(f"  ✅ {hit['wall_s']:>5.1f}s ")
            else:
                row.append(f"  ⚠️ {hit['success']}/{n}   ")
        print(f"{label:<{width}} " + " ".join(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
