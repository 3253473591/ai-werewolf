#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""/v1/models 探测：列出通道上有哪些模型。key 只从环境变量读，不落盘、不打印。"""
import json
import os
import ssl
import sys
import urllib.request

BASE = os.environ.get("PROBE_BASE", "")
KEY = os.environ.get("PROBE_KEY", "")
if not BASE or not KEY:
    print("需要 PROBE_BASE / PROBE_KEY 环境变量")
    sys.exit(2)

req = urllib.request.Request(
    BASE.rstrip("/") + "/models",
    headers={"Authorization": "Bearer " + KEY, "Accept": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=60,
                                context=ssl.create_default_context()) as r:
        body = json.loads(r.read().decode("utf-8"))
except Exception as e:                                        # noqa: BLE001
    print(f"❌ {type(e).__name__}: {e}")
    sys.exit(1)

data = body.get("data") if isinstance(body, dict) else None
if not isinstance(data, list):
    print("返回结构不认识：", json.dumps(body, ensure_ascii=False)[:600])
    sys.exit(1)

ids = []
for m in data:
    if isinstance(m, dict):
        ids.append(m.get("id") or m.get("model") or "?")
    else:
        ids.append(str(m))
ids.sort()
print(f"共 {len(ids)} 个模型")
print()
for i in ids:
    print("  ", i)
