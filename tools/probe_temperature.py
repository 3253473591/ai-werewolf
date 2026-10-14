#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探针：验证某通道上某模型对 temperature 的约束。

起因 —— 2026-10-06 12 座局 9 号 Kimi（kimi-k2.6）**整局哑火**：
    {"error": {"message": "invalid temperature: only 1 is allowed for this model",
               "type": "invalid_request_error"}}
座位 temperature=0.9，而 config 的 temperature_locked_models 只登记了 "kimi-k3"，
注释还写着"k2.6 / k2.7-code 无此约束" —— 注释与上游行为不一致。

用法:
    python tools/probe_temperature.py kimi-k2.6@ep7 1 0.9
    python tools/probe_temperature.py kimi-k2.7-code@ep7 0.9 1
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiwolf.endpoints import EndpointRegistry          # noqa: E402
from aiwolf.relay import http_post                     # noqa: E402

MSG = [{"role": "user", "content": "只回一个 JSON：{\"ok\": true}"}]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="模型名[@通道id]，如 kimi-k2.6@ep7")
    ap.add_argument("temps", nargs="+", type=float, help="要试的 temperature 值")
    ap.add_argument("--timeout", type=float, default=45.0)
    args = ap.parse_args()

    model, _, ep_id = args.target.partition("@")
    reg = EndpointRegistry()
    ep = reg.get(ep_id) if ep_id else reg.by_seat(9, model)
    if ep is None:
        print(f"找不到通道 {ep_id!r}；可用：{[e.id for e in reg.list()]}")
        return 2
    base = ep.base_url.rstrip("/")
    key = ep.resolve_key()
    print(f"通道 {ep.id} ({ep.name or '-'})  base={base}  key={'有' if key else '缺失'}")
    print(f"模型 {model}\n")

    for t in args.temps:
        payload = {"model": model, "messages": MSG, "temperature": t,
                   "max_tokens": 64}
        status, body, latency, err = http_post(
            f"{base}/chat/completions",
            {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            json.dumps(payload).encode("utf-8"), args.timeout, "auto")
        verdict = ""
        if status == 200:
            u = (body or {}).get("usage") or {}
            verdict = f"OK  completion={u.get('completion_tokens')}"
        else:
            msg = ""
            if isinstance(body, dict):
                e = body.get("error")
                msg = (e.get("message") if isinstance(e, dict) else str(e)) or \
                    json.dumps(body, ensure_ascii=False)[:200]
            else:
                msg = err or str(body)[:200]
            verdict = f"FAIL {msg}"
        mark = "OK  " if status == 200 else "FAIL"
        print(f"  {mark} temperature={t:<5} -> HTTP {status}  {latency:5.1f}s  {verdict}")

    print("\n结论：把能过的那一档写进 config/seats.json 的 temperature_locked_models。")
    print('注意 _temperature() 用的是 startswith 前缀匹配，'
          '"kimi-k2" 会连 k2.7-code 一起锁死 —— 要么写全名，要么确认同族同约束。')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
