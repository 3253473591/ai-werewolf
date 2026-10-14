#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多通道单测 —— 重点是**持久化**。

用户要求：填一次 base_url + key 就不要每次重填。所以这里必须证明
upsert 之后能落盘、重新载入能读回、key 回显时是掩码。

用法：python tools/selftest_endpoints.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiwolf.endpoints import EndpointRegistry  # noqa: E402
from aiwolf.seats import build_seats, load_config  # noqa: E402

FAILURES = []
CONFIG = os.path.join(ROOT, "config", "seats.json")


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def _fixture_dir(name: str) -> str:
    """夹具目录放在仓库里，不用系统临时目录。

    原因：本机跑没事，但在受限沙箱/CI 里"系统临时目录"经常不可写，
    测试就会因为环境挂掉而不是因为代码挂掉 —— 那种失败最误导人。
    """
    d = os.path.join(ROOT, "runs", name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def main() -> int:
    tmpdir = _fixture_dir("_selftest_endpoints")
    path = os.path.join(tmpdir, "endpoints.json")
    cfg = load_config(CONFIG)
    KEY = "sk-abcdef1234567890"

    print("\n[1] 没有配置文件时回退到 seats.json 的默认通道")
    reg = EndpointRegistry(path, fallback=cfg)
    eps = reg.list()
    check("回退出一个通道", len(eps) == 1, f"{[e.id for e in eps]}")
    check("base_url 来自 seats.json", eps[0].base_url.startswith("https://"), eps[0].base_url)

    print("\n[2] 添加通道后必须落盘（这是用户明确要求的）")
    reg.upsert({"id": "ep1", "name": "主通道",
                "base_url": "https://relay-a.example/v1",
                "api_key": KEY, "concurrency": 6})
    check("文件已生成", os.path.exists(path))
    raw = json.load(open(path, encoding="utf-8"))
    check("文件里有 ep1", any(e["id"] == "ep1" for e in raw["endpoints"]))
    check("key 明文落盘（重启才能读回）",
          any(e.get("api_key") == KEY for e in raw["endpoints"]))
    check("自动生成的 default 通道已被摘掉",
          all(e["id"] != "default" for e in raw["endpoints"]),
          str([e["id"] for e in raw["endpoints"]]))

    print("\n[3] 重新载入 = 重启服务，配置必须还在")
    reg2 = EndpointRegistry(path)
    ep = reg2.get("ep1")
    check("重启后仍能读到 ep1", ep is not None)
    check("base_url 保留", ep and ep.base_url == "https://relay-a.example/v1", ep.base_url if ep else "")
    check("api_key 保留", ep and ep.resolve_key() == KEY)
    check("并发数保留", ep and ep.concurrency == 6, str(ep.concurrency if ep else None))

    print("\n[4] 同一 base_url + key 允许重复登记（多通道摊并发）")
    reg2.upsert({"id": "ep2", "name": "副通道",
                 "base_url": "https://relay-a.example/v1",
                 "api_key": KEY, "concurrency": 4})
    check("两个通道并存", len([e for e in reg2.list() if e.base_url.startswith("https://relay-a")]) == 2)

    print("\n[5] 回显给前端时必须掩码，不能漏 key")
    snap = reg2.snapshot()
    blob = json.dumps(snap, ensure_ascii=False)
    check("掩码串在", any("***" in e["api_key_masked"] for e in snap))
    check("原文不在", KEY not in blob, "泄漏了！" if KEY in blob else "")
    check("有 key 的通道如实上报", all(e["has_key"] for e in snap if e["base_url"].startswith("https://relay-a")))

    print("\n[6] 留空不改 key，传 \"-\" 才清空")
    reg2.upsert({"id": "ep1", "api_key": ""})
    check("留空保留原 key", reg2.get("ep1").resolve_key() == KEY)
    reg2.upsert({"id": "ep1", "api_key": "-"})
    check("传 - 清空", reg2.get("ep1").resolve_key() == "")

    print("\n[7] 座位按通道轮转分配 + 显式绑定优先")
    reg2.upsert({"id": "ep1", "api_key": KEY})
    import copy as _copy

    # (a) 没有显式绑定时：轮转摊到两条通道上
    cfg_free = _copy.deepcopy(cfg)
    for d in cfg_free["seats"]:
        d.pop("endpoint", None)
    seats = build_seats(cfg_free, 9, shuffle=False, registry=reg2)
    used = {s.endpoint for s in seats}
    check("9 个座位摊到 2 条通道上", used == {"ep1", "ep2"}, str(sorted(used)))
    counts = {e: sum(1 for s in seats if s.endpoint == e) for e in used}
    check("分配尽量均匀", abs(counts["ep1"] - counts["ep2"]) <= 1, str(counts))
    check("同一座位稳定落在同一通道",
          [s.endpoint for s in seats] ==
          [s.endpoint for s in build_seats(cfg_free, 9, shuffle=False, registry=reg2)])

    # (b) 配置里写死的绑定必须优先于轮转（界面上改过端点就是这样落盘的）
    cfg_pin = _copy.deepcopy(cfg_free)
    cfg_pin["seats"][0]["endpoint"] = "ep2"
    pinned = build_seats(cfg_pin, 9, shuffle=False, registry=reg2)
    check("显式绑定优先于轮转", pinned[0].endpoint == "ep2", pinned[0].endpoint)
    check("其余座位仍走轮转", pinned[1].endpoint in {"ep1", "ep2"}, pinned[1].endpoint)

    print("\n[8] 删除通道")
    reg2.remove("ep2")
    check("ep2 已删", reg2.get("ep2") is None)
    check("删除也落盘", not any(e["id"] == "ep2" for e in
                              json.load(open(path, encoding="utf-8"))["endpoints"]))

    print("\n[9] 模型清单不因「缓存过期」而消失（线上事故回归）")
    # 事故现场：中转站 A 的 112 个模型在缓存年龄超过 300s 后被 model_catalog 整个剔除，
    # 下拉里只剩另一条通道的 2 个 —— 看起来就像 Gemini 3.8 flash 凭空没了。
    import time as _t
    regB = EndpointRegistry(os.path.join(tmpdir, "b.json"))
    regB.upsert({"id": "old", "name": "老通道", "base_url": "https://a.example/v1",
                 "api_key": "k", "concurrency": 2})
    regB.upsert({"id": "new", "name": "新通道", "base_url": "https://b.example/v1",
                 "api_key": "k", "concurrency": 2})
    ep_old = regB.get("old")
    ep_old.models = [f"m{i}" for i in range(112)]
    ep_old.models_at = _t.time() - 99999          # 很久没刷新
    ep_new = regB.get("new")
    ep_new.models = ["deepseek-chat", "deepseek-reasoner"]
    ep_new.models_at = _t.time()

    cat = regB.model_catalog()
    check("很久没刷新的通道仍在 catalog 里", "old" in cat, str(list(cat)))
    check("它的 112 个模型一个不少", len(cat.get("old", [])) == 112,
          str(len(cat.get("old", []))))
    check("新通道也在", "new" in cat)
    check("stale_endpoints 会把它标记出来（供后台自动刷新）",
          any(e.id == "old" for e in regB.stale_endpoints()))

    print("\n[10] 模型清单要落盘（重启后下拉立刻可用）")
    regB.save()
    regC = EndpointRegistry(os.path.join(tmpdir, "b.json"))
    check("重启后读到 old 的模型数", len(regC.get("old").models) == 112,
          str(len(regC.get("old").models)))
    check("重启后 catalog 立刻有内容",
          len(regC.model_catalog().get("old", [])) == 112)

    print("\n[11] 自动分配必须看模型能力，不能纯轮转（线上事故回归）")
    # 事故现场：两条通道（一个通用中转站 + 一个 deepseek 官方），
    # 纯轮转把 gpt-5.6-sol 分到了 deepseek 官方上，
    # 开局直接报 "The supported API model names are deepseek-flash, ..."，座位整局哑火。
    regD = EndpointRegistry(os.path.join(tmpdir, "d.json"))
    regD.upsert({"id": "relay", "name": "中转站", "base_url": "https://relay.example/v1",
                 "api_key": "k", "concurrency": 4})
    regD.upsert({"id": "ds", "name": "deepseek官方", "base_url": "https://api.deepseek.com",
                 "api_key": "k", "concurrency": 4})
    regD.get("relay").models = ["gpt-5.6-sol", "grok-4-5", "claude-sonnet-4-6"]
    regD.get("relay").models_at = 1e9
    regD.get("ds").models = ["deepseek-flash", "deepseek-v4-pro"]
    regD.get("ds").models_at = 1e9

    got = {m: regD.by_seat(no, m).id
           for no, m in [(1, "gpt-5.6-sol"), (2, "grok-4-5"), (3, "deepseek-flash"),
                         (4, "claude-sonnet-4-6"), (5, "deepseek-v4-pro")]}
    check("gpt-5.6-sol 不会被分到 deepseek 官方", got["gpt-5.6-sol"] == "relay",
          got["gpt-5.6-sol"])
    check("grok-4-5 也不会", got["grok-4-5"] == "relay", got["grok-4-5"])
    check("deepseek 模型会走 deepseek 官方", got["deepseek-flash"] == "ds",
          got["deepseek-flash"])
    check("能跑该模型的通道只有一个时，老老实实选它",
          got["claude-sonnet-4-6"] == "relay", got["claude-sonnet-4-6"])

    print("\n[12] 开局体检：模型和通道对不上要能报出来")
    class _S:
        def __init__(self, no, model, endpoint):
            self.no, self.model, self.endpoint = no, model, endpoint
    ok_seats = [_S(1, "deepseek-flash", "ds"), _S(2, "gpt-5.6-sol", "relay")]
    bad_seats = [_S(1, "gpt-5.6-sol", "ds"), _S(2, "deepseek-v4-pro", "ds"),
                 _S(3, "x", "nope"), _S(4, "whatever", "")]
    check("配置正确时无告警", regD.check_bindings(ok_seats) == [])
    probs = regD.check_bindings(bad_seats)
    check("对不上的能报出来", len(probs) == 2, str(probs))
    check("通道不存在的也能报", probs[1]["reason"] == "通道不存在", str(probs[1]))
    check("没绑端点的跳过（走自动分配）", all(p["no"] != 4 for p in probs))

    # 推理接入点（火山方舟 ep-*）不在 /models 目录里，拿目录比对必然误报 ——
    # 而开局体检会因此把整局拦下来（实测踩过）
    dep_ep = regD.get("ds")
    if dep_ep is not None:
        dep_ep.models = ["deepseek-flash", "deepseek-v4-pro"]
    dep_seats = [_S(1, "ep-20261005143108-gcv24", "ds")]
    check("推理接入点 ep-* 不被误判成'通道不提供这个模型'",
          regD.check_bindings(dep_seats) == [], str(regD.check_bindings(dep_seats)))
    from aiwolf.endpoints import is_deployment_id
    check("is_deployment_id 只认接入点前缀",
          is_deployment_id("ep-abc") and not is_deployment_id("gpt-5.6-sol"))

    shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：{FAILURES}")
        return 1
    print("✅ 多通道全部通过（含落盘 / 重启读回 / key 掩码）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
