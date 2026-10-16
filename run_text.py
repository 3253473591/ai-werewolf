#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 文本版 runner —— 跑一整局并做信息隔离审计。

用法：

    # 离线自检，不烧额度（先把引擎/渲染/审计跑通）
    python run_text.py --stub --seats 5

    # 真实对局
    set RELAY_API_KEY=...
    python run_text.py --seats 5

    # 完整十二座
    python run_text.py --seats 12

M1 验收标准：审计输出里 violations 必须为空 ——
村民座位的 prompt 里从未出现任何 WOLF 频道内容或别人的私密信息。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiwolf.audit import audit_prompts
from aiwolf.endpoints import EndpointRegistry
from aiwolf.engine import TASKS, Game
from aiwolf.events import Channel, Kind
from aiwolf.gate import ConsoleGate
from aiwolf.relay import Relay
from aiwolf.render import (
    build_messages,
    check_no_model_names,
    check_shared_prefix,
    phase_cn,
)
from aiwolf.seats import (
    PLAYER_COUNTS,
    build_seats,
    load_config,
    resolve_seed,
    role_summary,
    wants_shuffle,
)
from aiwolf.summary import build_summary, write_summary

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(ROOT, "config", "seats.json")
ENDPOINTS_PATH = os.path.join(ROOT, "config", "endpoints.json")


def print_transcript(log, seats) -> None:
    name_of = {s.no: s.label for s in seats}
    print("\n" + "=" * 74)
    print("上帝视角 · 完整事件流")
    print("=" * 74)
    for ev in log:
        if ev.channel is Channel.WOLF:
            tag = "[小桌]"
        elif ev.channel is Channel.PRIVATE:
            if ev.to_seat is None:
                tag = "[上帝]"
            else:
                tag = f"[私密→{ev.to_seat}号]"
        else:
            tag = "[大桌]"
        who = f"{ev.actor}号 {name_of.get(ev.actor, '')} " if ev.actor else ""
        text = ev.text or (str(ev.data) if ev.data else "")
        if text:
            print(f"  {tag} 第{ev.day}天·{phase_cn(ev.phase):<8} {who}{text}")


def _display_width(s: str) -> int:
    """按终端显示宽度算长度（中文/emoji 占 2 格）。"""
    import unicodedata
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _center(s: str, width: int) -> str:
    pad = max(0, width - _display_width(s))
    left = pad // 2
    return " " * left + s + " " * (pad - left)


def print_result_banner(winner: str, days: int) -> None:
    """文本版的"结算动画" —— 和 Web 端的胜利弹层是同一件事。"""
    icon, title, sub = {
        "wolf": ("🐺", "狼 人 阵 营 获 胜", "黑夜吞掉了这张桌子"),
        "good": ("☀️", "好 人 阵 营 获 胜", "天亮了，狼人全部出局"),
        "draw": ("⚖️", "平 局", "谁也没能带走这一局"),
    }.get(winner, ("❓", winner, ""))
    inner = 68
    print("\n" + "█" * (inner + 2))
    print("█" + _center(f"{icon}  {title}", inner) + "█")
    if sub:
        print("█" + _center(sub, inner) + "█")
    print("█" + _center(f"共 {days} 天", inner) + "█")
    print("█" * (inner + 2))


def print_audit(result) -> None:
    print("\n" + "=" * 74)
    print("信息隔离审计")
    print("=" * 74)
    print(f"{'座位':<12}{'角色':<8}{'调用':>5}{'最大prompt':>11}{'缓存率':>9}{'失败':>6}  泄漏")
    print("-" * 74)
    for label, s in result["seats"].items():
        print(f"{label:<12}{s['role']:<8}{s['calls']:>5}{s['max_prompt_tokens']:>11}"
              f"{s['avg_cache_ratio']:>9.1%}{s['errors']:>6}  "
              f"{'无' if not s['leaks'] else len(s['leaks'])}")
    print("-" * 74)
    if result["ok"]:
        print("✅ 通过：没有任何座位看到越权信息")
    else:
        print(f"❌ 失败：{len(result['violations'])} 处泄漏")
        for v in result["violations"][:20]:
            print(f"   {v['label']}（{v['no']}号）：{v['detail']}")


def print_prefix_check(label: str, result) -> bool:
    """共享前缀一致性 —— 缓存设计成立的前提。"""
    if result["ok"]:
        print(f"  ✅ {label}：{result['seats']} 个座位的共享前缀逐字节相同"
              f"（{result['n_shared']} 条消息），尾部块各不相同")
        return True
    print(f"  ❌ {label}：{result.get('mismatch')}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 狼人杀 M1 文本版 runner")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--seats", type=int, default=5,
                    help=f"座位数（{'/'.join(map(str, PLAYER_COUNTS))}）")
    ap.add_argument("--shuffle-roles", action="store_true",
                    help="用固定 seed 打乱角色，避免狼永远坐 1-3 号")
    ap.add_argument("--stub", action="store_true", help="用离线假模型，不消耗额度")
    ap.add_argument("--step", action="store_true",
                    help="单步调试：每次调用模型前停下来等回车")
    ap.add_argument("--max-days", type=int, default=6)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--out", default=os.path.join(ROOT, "runs"))
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--dump-prompt", type=int, default=0,
                    help="只打印指定座位的完整 prompt 然后退出（不调 API）")
    ap.add_argument("--dump-phase", default="NIGHT_WOLF")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.seats not in PLAYER_COUNTS:
        print(f"错误：座位数只能是 {'/'.join(map(str, PLAYER_COUNTS))}")
        return 2
    shuffle = args.shuffle_roles or wants_shuffle(cfg)
    seed = resolve_seed(cfg)

    # ★ 多通道：CLI 也必须读 config/endpoints.json。
    # 以前 run_text 不建 registry，于是**所有座位都被塞进单条 legacy relay**
    # （config 的 relay.base_url + RELAY_API_KEY 环境变量）——
    # 多通道、每座一条通道、跨通道代打这些全都不生效，
    # 而且失败信息看起来像"每个端点都挂了"，极难定位（实测踩了很久）。
    registry = None
    try:
        registry = EndpointRegistry(ENDPOINTS_PATH, fallback=cfg)
        if not registry.enabled():
            registry = None
    except Exception as e:                                    # noqa: BLE001
        print(f"  ⚠ 读 config/endpoints.json 失败（退回单通道）：{type(e).__name__}: {e}")
        registry = None

    seats = build_seats(cfg, args.seats, shuffle=shuffle, seed=seed, registry=registry)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(args.out, f"{stamp}_{args.seats}seat{'_stub' if args.stub else ''}")
    os.makedirs(run_dir, exist_ok=True)

    print("=" * 74)
    print(f"AI 狼人杀 · M1 文本版 · {args.seats} 座" + ("  【离线 stub 模式】" if args.stub else ""))
    print(f"角色配置：{role_summary(args.seats)}" + (f"   （已抽卡，seed={seed}）" if shuffle else ""))
    print("=" * 74)
    for s in seats:
        ep = ""
        if registry is not None and s.endpoint:
            e = registry.get(s.endpoint)
            ep = f" @{s.endpoint}" + (f"({e.name})" if e and e.name else "")
        print(f"  {s.no}号 {s.label:<9} {s.model:<22} {s.role_cn:<5} 缓存={s.cache_style}{ep}")
    print(f"  输出目录：{run_dir}")

    relay = Relay(cfg, api_key=args.api_key, stub=args.stub,
                  log_dir=run_dir, verbose=not args.quiet, registry=registry)
    gate = None
    if args.step:
        gate = ConsoleGate(show=lambda label: print(f"\n  ▶ 即将调用：{label}"))
        print("\n【单步模式】每次模型调用前会停下来，按回车放行。")
    game = Game(seats, relay, max_days=args.max_days, verbose=not args.quiet,
                win_condition=cfg.get("game", {}).get("win_condition", "parity"),
                seed=seed, gate=gate,
                sheriff=cfg.get("game", {}).get("sheriff_enabled", True))

    print("\n-- 渲染不变量预检 --")
    pre_ok = print_prefix_check(
        "开局（空前缀）",
        check_shared_prefix(seats, game.log, day=1, phase="NIGHT_WOLF", task="预检"))
    no_name = check_no_model_names(seats, game.log, day=1, phase="NIGHT_WOLF", task="预检")
    if no_name["ok"]:
        print("  ✅ 尾部块不含任何模型名（队友只以座位号出现）")
    else:
        print(f"  ❌ 尾部块出现模型名：{no_name['leaks'][:3]}")

    if args.dump_prompt:
        dump_seat = next(s for s in seats if s.no == args.dump_prompt)
        msgs, n_shared = build_messages(
            dump_seat, seats, game.log, day=1, phase=args.dump_phase,
            task=TASKS.get(args.dump_phase, "请行动。"),
            state={"wolf_target": 3, "witch_heal_used": False, "witch_poison_used": False,
                   "seer_checks": {}, "last_guard_target": None, "hunter_can_shoot": True})
        print(f"\n{'=' * 74}\n{args.dump_prompt}号（{dump_seat.role_cn}）的完整 prompt"
              f"  [共享前缀 {n_shared} 条 + 尾部 1 条]\n{'=' * 74}")
        for i, m in enumerate(msgs):
            tag = "共享前缀" if i < n_shared else "★ 尾部块"
            text = m["content"] if isinstance(m["content"], str) else json.dumps(
                m["content"], ensure_ascii=False)
            print(f"\n--- [{i}] {m['role']} · {tag} ---\n{text}")
        return 0

    winner = game.run()

    post_ok = print_prefix_check(
        "终局（完整前缀）",
        check_shared_prefix(seats, game.log, day=game.state.day, phase="DAY_VOTE", task="预检"))

    print_transcript(game.log, seats)
    game.log.to_jsonl(os.path.join(run_dir, "events.jsonl"))

    audit = audit_prompts(run_dir, game.log, seats)
    print_audit(audit)

    winner_cn = {"wolf": "狼人阵营", "good": "好人阵营", "draw": "平局"}.get(winner, winner)
    print_result_banner(winner, game.state.day)
    print(f"\n结果：{winner_cn} 获胜（共 {game.state.day} 天）")

    parse_fails = [ev for ev in game.log if ev.kind is Kind.PARSE_FAIL]
    truncated = sum(1 for s in relay.stats if s.get("truncated"))
    print("\n-- 输出质量 --")
    print(f"  调用总数      : {len(relay.stats)}")
    print(f"  截断(finish=length): {truncated}")
    print(f"  解析失败需兜底: {len(parse_fails)}")
    for ev in parse_fails:
        print(f"     · {ev.text}")

    summary = build_summary(
        seats=seats, relay=relay, winner=winner, days=game.state.day,
        run_dir=run_dir, stub=args.stub, seed=seed, shuffled=shuffle,
        shared_prefix_ok=pre_ok and post_ok, no_model_names_ok=no_name["ok"],
        audit=audit, extra={"parse_failures": len(parse_fails)},
    )
    write_summary(run_dir, summary)

    failed = sum(1 for s in relay.stats if s.get("error"))
    print(f"调用 {len(relay.stats)} 次，失败 {failed} 次。summary 已写入 "
          f"{os.path.join(run_dir, 'summary.json')}")
    return 0 if (audit["ok"] and pre_ok and post_ok and no_name["ok"]) else 1


if __name__ == "__main__":
    sys.exit(main())
