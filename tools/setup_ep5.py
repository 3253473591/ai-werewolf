#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 中转站 B 加成一档 ep5，并把 11/12 号座位接上去。

key 从环境变量 CC88_KEY 读，不落进命令行历史。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

EP_PATH = os.path.join(ROOT, "config", "endpoints.json")
SEAT_PATH = os.path.join(ROOT, "config", "seats.json")

KEY = os.environ.get("CC88_KEY", "").strip()
if not KEY:
    print("❌ 需要环境变量 CC88_KEY")
    raise SystemExit(2)

cfg = json.load(open(EP_PATH, encoding="utf-8"))
eps = cfg["endpoints"]

EP5 = {
    "id": "ep5",
    "name": "中转站 B",
    "base_url": "https://https://relay-b.example/v1",
    "api_key_env": "",
    "api_key": KEY,
    "concurrency": 4,
    "enabled": True,
    "note": "relay-b.example 的后端。OpenAI 兼容，sk- key，同时认 Authorization: Bearer 和 x-api-key。"
            "实测：前缀缓存分模型（tt-*/kimi/qwen/sn-* 命中 90~98%，gem-*/glm-* 恒 0，"
            "gk-*/MiniMax 只命中 128 的模板，deepseek-* 不返回 usage）；"
            "并发 x32 干净通过、x64 撞余额（402 billing_error，不是 429）。"
            "容量指标见公开接口 /gongkai/moxingguangchang 的 online_lines。"
            "2026-10-06 接入。",
}
old = next((e for e in eps if e.get("id") == "ep5"), None)
if old:
    old.update(EP5)
    print("ep5 已存在，就地更新")
else:
    eps.append(EP5)
    print("ep5 已加入")

# 拉一次模型清单落盘（前端下拉要用）
try:
    import ssl
    import urllib.request

    req = urllib.request.Request(
        EP5["base_url"] + "/models",
        headers={"Authorization": "Bearer " + KEY, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60,
                                context=ssl.create_default_context()) as r:
        body = json.loads(r.read().decode("utf-8"))
    ids = sorted(m["id"] for m in body.get("data", []) if isinstance(m, dict) and m.get("id"))
    if ids:
        import time
        EP5["_models"] = ids
        EP5["_models_at"] = time.time()
        print(f"模型清单已落盘：{len(ids)} 个")
except Exception as e:                                        # noqa: BLE001
    print(f"⚠ 拉模型清单失败（不影响通道本身）：{type(e).__name__}: {e}")

with open(EP_PATH, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)

# ---------------- 座位 11 / 12 ----------------
seats_cfg = json.load(open(SEAT_PATH, encoding="utf-8"))
by_no = {s["no"]: s for s in seats_cfg["seats"]}

NEW = [
    {
        "no": 11,
        "label": "Step",
        "model": "stepfun/step-3.7-flash",
        "fallback": ["glm-5.2@ep2", "gk-4.6@ep5"],
        "temperature": 0.9,
        "cache_style": "auto",
        "cache_verified": False,
        "note": "阶跃星辰 Step 3.7 Flash。256K 上下文 —— 按实测预算模型，12 座 6 天最大 prompt "
                "约 3 万 tokens，占 256K 的 11%，够用且富余很大。2026-10-06 接入（ep5 中转站 B）。",
        "endpoint": "ep5",
    },
    {
        "no": 12,
        "label": "MiMo",
        "model": "mimo-v2.5-pro",
        "fallback": ["glm-5.2@ep2", "tt-5.6-terra@ep5"],
        "temperature": 0.9,
        "cache_style": "auto",
        "cache_verified": False,
        "note": "小米 MiMo v2.5 Pro，1M 上下文。12 座 6 天实测预算约 3 万 tokens，到不了它的零头；"
                "留着当长局保险。2026-10-06 接入（ep5 中转站 B）。",
        "endpoint": "ep5",
    },
]
for s in NEW:
    if s["no"] in by_no:
        by_no[s["no"]].update(s)
        print(f"座位 {s['no']} 已存在，就地更新")
    else:
        seats_cfg["seats"].append(s)
        print(f"座位 {s['no']} 已加入：{s['label']} {s['model']}")

seats_cfg["seats"].sort(key=lambda x: x["no"])
seats_cfg["_updated"] = "2026-10-06"
seats_cfg["game"]["player_count"] = 12
with open(SEAT_PATH, "w", encoding="utf-8") as f:
    json.dump(seats_cfg, f, ensure_ascii=False, indent=2)
print(f"座位数现在 {len(seats_cfg['seats'])}")
