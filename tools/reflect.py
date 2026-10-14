#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""赛后采访：让每个座位对刚打完的那局发表一段局后感言。**会调 API、花额度。**

    # 先看看这一局能不能读回来（不花钱）
    python tools/reflect.py runs/20261006_011857_10seat_web --dry-run

    # 真跑（会在已有基础上续跑：已经说过话的座位跳过）
    python tools/reflect.py runs/20261006_011857_10seat_web

    # 只让 3 号、7 号、10 号说（省钱先看效果）
    python tools/reflect.py runs/xxx --seats 3,7,10

    # 不花额度，验证管路（用 stub 假模型）
    python tools/reflect.py runs/xxx --stub --force

    # 全部重说（覆盖已有感言）
    python tools/reflect.py runs/xxx --force

    # 用**压缩记录**代替全量事件流（12 座 × 几万 token → 一份一万字简报）
    python tools/brief.py runs/xxx -o runs/xxx/brief.md   # 先看产物长什么样
    python tools/reflect.py runs/xxx --brief

产物：`runs/<局>/reflections.json`；调用日志另外落在 `runs/<局>/reflect_logs/`
——**绝不写 events.jsonl / seat_*.jsonl**，那两个是权威对局记录。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description="局后感言（会花额度）")
    ap.add_argument("run", help="已结束对局的目录，例如 runs/20261006_011857_10seat_web")
    ap.add_argument("--seats", default="", help="只让这些座位说，例如 3,7,10")
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--stub", action="store_true", help="用假模型（不联网、不花钱）")
    ap.add_argument("--force", action="store_true", help="已有感言也重说")
    ap.add_argument("--dry-run", action="store_true", help="只检查能不能读回来，不调模型")
    ap.add_argument("--brief", action="store_true",
                    help="用压缩记录代替全量事件流（口径见 aiwolf/brief.py，"
                         "先用 tools/brief.py 看一眼产物）")
    ap.add_argument("--quiet", action="store_true")
    # ---- 追加采访（第二轮）：让指定的人**按指定顺序、看着彼此的原话**互相回应 ----
    ap.add_argument("--order", default="",
                    help="发言顺序，例如 6,1,2,4。后说的人能看到先说的原话。")
    ap.add_argument("--followup", default="",
                    help="追加提问。给了就换成'指名回应'口径，而不是各自发表感想。")
    ap.add_argument("--seed-round1", action="store_true",
                    help="把 reflections.json 里第一轮的原话喂进上下文（掐架必需）")
    ap.add_argument("--seed-round2", default="",
                    help="再叠一份上一轮的原话，例如 interview_round2.json。"
                         "第三轮要同时看到第一轮和第二轮说过什么时必须给。")
    ap.add_argument("--out", default="",
                    help="另存文件名，例如 interview_round2.json（别盖掉第一轮）")
    args = ap.parse_args()

    from aiwolf.reflect import REFLECT_FILE, load_finished_run, reflect

    run = args.run if os.path.isabs(args.run) else os.path.join(ROOT, args.run)
    try:
        log, seats, summary = load_finished_run(run)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 2

    print("=" * 78)
    print(f"对局：{os.path.basename(run)}")
    print(f"胜方：{summary.get('winner')} · {summary.get('days')} 天 · {len(seats)} 座")
    print("身份：" + "  ".join(f"{s.no}号={s.role_cn}" for s in seats))
    print(f"事件：{len(list(log))} 条")
    out = os.path.join(run, REFLECT_FILE)
    if os.path.exists(out):
        try:
            prev = json.load(open(out, encoding="utf-8"))
            print(f"已有感言：{prev.get('seats_done')}/{prev.get('seats_total')} 座"
                  f"（{', '.join(str(t['seat']) for t in prev.get('transcript', []))}）")
        except (OSError, ValueError):
            print("已有感言文件读不出来，会重写")
    print("=" * 78)

    if args.dry_run:
        from aiwolf.brief import build_brief
        from aiwolf.reflect import build_reflection_messages
        brief_text = build_brief(log, summary, seats) if args.brief else ""
        msgs = build_reflection_messages(seats[0], seats, log, summary, [],
                                         brief_text=brief_text)
        print(f"\n干跑：给 {seats[0].no}号 组装的 prompt 共 {len(msgs)} 条消息，"
              f"{sum(len(m['content']) for m in msgs)} 字"
              + (f"（压缩记录 {len(brief_text)} 字）" if brief_text else ""))
        print("-" * 78)
        body = msgs[1]["content"]
        print(body[:1200] + ("…（只显示前 1200 字）" if len(body) > 1200 else ""))
        print("-" * 78)
        print(msgs[-1]["content"])
        print("-" * 78)
        print("（--dry-run 不调模型、不花钱）")
        return 0

    only = [int(x) for x in args.seats.replace("，", ",").split(",") if x.strip()] \
        if args.seats else None
    order = [int(x) for x in args.order.replace("，", ",").split(",") if x.strip()] \
        if args.order else None

    # 追加采访：先把第一轮原话读进来当上下文（不落盘，只是"他们听过彼此说过什么"）
    seed = None
    if args.seed_round1:
        prev = os.path.join(run, REFLECT_FILE)
        if not os.path.exists(prev):
            print(f"❌ --seed-round1 需要 {REFLECT_FILE}，但没有：{prev}")
            return 2
        seed = json.load(open(prev, encoding="utf-8")).get("transcript") or []
        # 只喂**要上桌的那几位**的原话：把 12 个人的话全塞进去，
        # 会让被点名的人淹没在无关内容里，掐不起来。
        if only:
            seed = [t for t in seed if int(t["seat"]) in only]
        print(f"已载入第一轮原话 {len(seed)} 条作为上下文："
              f"{', '.join(str(t['seat']) for t in seed)}")

    # 再叠上一轮（第二轮）的原话。**不过滤座位** —— 第三轮上桌的人
    # （比如 7号、8号）自己在第二轮没说过话，但他们必须看到
    # 1号 是怎么在第二轮点评他们的，否则接不上话。
    if args.seed_round2:
        r2 = os.path.join(run, args.seed_round2)
        if not os.path.exists(r2):
            print(f"❌ --seed-round2 找不到：{r2}")
            return 2
        extra = json.load(open(r2, encoding="utf-8")).get("transcript") or []
        seed = (seed or []) + extra
        print(f"再叠上一轮原话 {len(extra)} 条："
              f"{', '.join(str(t['seat']) for t in extra)}")

    payload = reflect(run, only_seats=only, force=args.force,
                      stub=args.stub, verbose=not args.quiet,
                      max_tokens=args.max_tokens,
                      order=order,
                      out_name=args.out or REFLECT_FILE,
                      seed_transcript=seed,
                      brief=args.brief,
                      followup=args.followup)
    print("\n" + "=" * 78)
    print("局后感言")
    print("=" * 78)
    for t in payload["transcript"]:
        print(f"\n【{t['seat']}号 {t['label']} · {t['role_cn']}】"
              + (f"  ⚠ {t['error']}" if t["error"] else ""))
        print(t["text"] or "（没有说出话来）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
