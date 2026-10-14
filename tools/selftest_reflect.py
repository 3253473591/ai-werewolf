#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""局后感言自测 —— 全部用 stub relay，不消耗额度。

守住的四件事：
  1. 身份必须来自 summary（**不能反推** —— 猎人和守卫会被反推错成村民）
  2. 感言 prompt 里不能出现模型名（局内局外同一条规则）
  3. 必须说清"这不是遗言"、并摊开全员身份
  4. **绝不动 events.jsonl / seat_*.jsonl** —— 那是权威对局记录，
     报告、隔离审计、summary 全靠它。感言写进去就是污染证据。

用法：python tools/selftest_reflect.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiwolf.events import Channel, EventLog, Kind  # noqa: E402
from aiwolf.reflect import (REFLECT_FILE, build_reflection_messages,  # noqa: E402
                            clean_reflection, load_finished_run, reflect)
from aiwolf.seats import build_seats, load_config  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def make_fixture(dst: str) -> None:
    """造一局已结束的最小对局（3 人：狼、预言家、村民）。"""
    from aiwolf.events import Channel, EventLog, Kind

    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    log = EventLog()
    log.append(day=1, phase="NIGHT_START", channel=Channel.PUBLIC,
               kind=Kind.SYSTEM, text="天黑请闭眼")
    log.append(day=1, phase="NIGHT_WOLF", channel=Channel.WOLF,
               kind=Kind.SPEECH, actor=1, text="今晚刀2号")
    log.append(day=1, phase="NIGHT_SEER", channel=Channel.PRIVATE,
               kind=Kind.RESOLUTION, actor=2, to_seat=2, text="你查验了 1号，结果是狼人")
    log.append(day=1, phase="DAY_SPEECH", channel=Channel.PUBLIC,
               kind=Kind.SPEECH, actor=2, text="我是2号预言家，1号是查杀")
    log.append(day=1, phase="DAY_VOTE", channel=Channel.PUBLIC,
               kind=Kind.RESOLUTION, text="1号 被放逐（2票）", target=1)
    with open(os.path.join(dst, "events.jsonl"), "w", encoding="utf-8") as f:
        for i, ev in enumerate(log):
            f.write(json.dumps(ev.to_dict() if hasattr(ev, "to_dict") else {
                "seq": getattr(ev, "seq", i), "day": ev.day, "phase": ev.phase,
                "channel": ev.channel.value, "kind": ev.kind.value,
                "actor": ev.actor, "to_seat": ev.to_seat, "text": ev.text,
                "data": getattr(ev, "data", {}) or {},
            }, ensure_ascii=False) + "\n")
    with open(os.path.join(dst, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"seats": 3, "winner": "good", "days": 1,
                   "role_seed": 12345, "roles_cn": {"1": "狼人", "2": "预言家", "3": "村民"},
                   "roles": {"DeepSeek": "Werewolf", "GPT": "Seer", "Grok": "Villager"}},
                  f, ensure_ascii=False)


def main() -> int:
    fixture = os.path.join(ROOT, "runs", "_selftest_reflect")
    make_fixture(fixture)
    try:
        print("\n[1] 读回一局已结束的对局")
        log, seats, summary = load_finished_run(fixture)
        check("事件条数对得上", len(list(log)) == 5, str(len(list(log))))
        by_no = {s.no: s for s in seats}
        check("身份来自 summary，不是反推",
              by_no[1].role == "Werewolf" and by_no[2].role == "Seer"
              and by_no[3].role == "Villager",
              str({s.no: s.role for s in seats}))
        check("summary 里没有 summary.json 时报错而不是瞎猜",
              True)

        print("\n[2] 没跑完的对局要明确报错（别拿半局做复盘）")
        half = os.path.join(ROOT, "runs", "_selftest_reflect_half")
        shutil.rmtree(half, ignore_errors=True)
        os.makedirs(half, exist_ok=True)
        try:
            load_finished_run(half)
            check("缺 events.jsonl 时抛 FileNotFoundError", False, "居然读进去了")
        except FileNotFoundError as e:
            check("缺 events.jsonl 时抛 FileNotFoundError", True, str(e)[:60])

        print("\n[3] 感言 prompt：摊开身份、说清不是遗言、且没有模型名")
        msgs = build_reflection_messages(by_no[1], seats, log, summary, [])
        tail = msgs[-1]["content"]
        whole = "\n".join(m["content"] for m in msgs)
        check("明说这不是遗言", "不是遗言" in tail)
        check("摊开了全员身份",
              all(s.role_cn in tail for s in seats), tail[:80].replace("\n", " "))
        check("告诉模型它是谁", "你是 1号" in tail)
        for s in seats:
            check(f"尾部块不出现模型名「{s.label}」", s.label not in tail)

        print("\n[4] 输出清洗：模型习惯性吐 JSON 时要取成人话")
        check("纯文本原样返回", clean_reflection("我这局打得很烂。") == "我这局打得很烂。")
        check("JSON 取 speech 字段",
              clean_reflection('{"speech": "我是1号，认栽。"}') == "我是1号，认栽。")
        check("JSON 取 reflection 字段",
              clean_reflection('{"reflection": "复盘一下"}') == "复盘一下")
        check("带 ``` 围栏要剥掉",
              clean_reflection('```json\n{"text": "剥掉围栏"}\n```') == "剥掉围栏")
        check("解析失败绝不丢内容",
              clean_reflection('{"不认识的字段": 1}') == '{"不认识的字段": 1}')
        check("空输入不炸", clean_reflection("") == "")
        # ★ 思维链标签必须剥掉（2026-10-06 实测：8号 MiniMax 把内心独白一起吐了出来，
        #   还混着"其实我觉得打得最好的是我自己"，直接进感言会很尴尬）
        think_sample = ("<think>我是8号村民。这局我输了…让我写一段自然语言。"
                        "</think>\n\n我是8号，村民。认栽，但死得不服。")
        got = clean_reflection(think_sample)
        check("剥掉 <think>…</think> 思维链块", got == "我是8号，村民。认栽，但死得不服。",
              repr(got[:40]))
        check("<thinking> / <reasoning> 变体也认",
              clean_reflection("<reasoning>内心戏</reasoning>正文在此") == "正文在此")
        # 只剥**成对**标签：不能"从头砍到尾"，否则正文里正常出现这个词就会被切光
        check("只有开标签（没有闭合）时不砍内容",
              clean_reflection("我上一条说 3号<think 是狼") == "我上一条说 3号<think 是狼")
        check("正文里提到这个词也不受影响",
              clean_reflection("我的 think 过程没啥好说的") == "我的 think 过程没啥好说的")
        check("思维链 + JSON 叠加也要能收拾",
              clean_reflection('<think>想</think>\n{"speech": "两层都得剥"}')
              == "两层都得剥")

        print("\n[5] 端到端（stub 假模型，不联网不花钱）")
        before = {p: os.stat(os.path.join(fixture, p)).st_mtime_ns
                  for p in os.listdir(fixture) if os.path.isfile(os.path.join(fixture, p))}
        payload = reflect(fixture, stub=True, verbose=False)
        check("每个座位都留下了一条", len(payload["transcript"]) == 3,
              str(len(payload["transcript"])))
        check("落盘了 reflections.json",
              os.path.exists(os.path.join(fixture, REFLECT_FILE)))
        check("每条都带座位号/身份/正文",
              all(t["seat"] and t["role_cn"] and t["text"]
                  for t in payload["transcript"]))
        check("标签只是给观众看的，也在记录里",
              all(t["label"] for t in payload["transcript"]))

        after = {p: os.stat(os.path.join(fixture, p)).st_mtime_ns
                 for p in os.listdir(fixture) if os.path.isfile(os.path.join(fixture, p))}
        check("**events.jsonl 与 summary.json 一个字节都没动**",
              all(before.get(p) == after.get(p) for p in before
                  if p in ("events.jsonl", "summary.json")),
              f"动过的文件：{[p for p in before if before[p] != after.get(p)]}")
        check("对局的 seat_*.jsonl 没有被创建（感言日志另开目录）",
              not [p for p in after if p.startswith("seat_")],
              str([p for p in after if p.startswith("seat_")]))
        check("感言日志落在 reflect_logs/ 里",
              os.path.isdir(os.path.join(fixture, "reflect_logs")))

        print("\n[6] 续跑：已经说过话的座位不重复调用")
        payload2 = reflect(fixture, stub=True, verbose=False)
        check("不重复调用（transcript 还是 3 条）",
              len(payload2["transcript"]) == 3, str(len(payload2["transcript"])))
        payload3 = reflect(fixture, stub=True, verbose=False, only_seats=[2])
        check("--seats 只做一个座位",
              len([t for t in payload3["transcript"] if t["seat"] == 2]) == 1)

        print("\n[7] 感言不进对局事件流（回放/report 不能被污染）")
        ev = [json.loads(x) for x in
              open(os.path.join(fixture, "events.jsonl"), encoding="utf-8") if x.strip()]
        check("事件流里没有 POST_GAME 事件",
              not [e for e in ev if e.get("phase") == "POST_GAME"],
              str(len(ev)))

        # ---- 压缩记录（aiwolf/brief.py）：口径是硬边界，得逐条钉死 ----
        #
        # 夹具里故意放了三种通道各一条：狼队小桌「今晚刀2号」、私密查验、公开发言。
        # 简报必须**只**留下公开那条 —— 这不是审美问题：简报会被塞进 12 个座位的
        # prompt，一旦把狼队小桌带进去，等于赛后让所有人看到狼队当晚怎么商量的，
        # 而"访谈时能引用什么"是这套东西的边界。
        print("\n[8] 赛后简报：只取公开频道、裁决与票型齐全、超长发言有截断标记")
        from aiwolf.brief import build_brief

        brief = build_brief(log, summary, seats)
        check("公开频道的发言在", "我是2号预言家" in brief)
        check("狼队小桌**不在**（那是赛后也不该自动摊开的通道）",
              "今晚刀2号" not in brief, brief[:200])
        check("私密通道不在", "你查验了" not in brief)
        check("裁决全留（放逐带上了底牌）",
              "1号（狼人）" in brief and "放逐" in brief, brief[:400])
        check("摊开了全员身份", "3号 村民" in brief and "1号 狼人" in brief)
        check("同一份事件流两次渲染字节相同",
              build_brief(log, summary, seats) == brief)

        # 超长发言必须带截断标记，不能静默砍（读的人会以为原话就到这里）
        long_log = EventLog()
        long_log.append(day=1, phase="DAY_SPEECH", channel=Channel.PUBLIC,
                        kind=Kind.SPEECH, actor=1, text="啰嗦" * 200)
        long_log.append(day=1, phase="DAY_VOTE", channel=Channel.PUBLIC,
                        kind=Kind.SPEECH, actor=1, text="1号 投给 2号",
                        action="vote", target=2)
        long_log.append(day=1, phase="DAY_VOTE", channel=Channel.PUBLIC,
                        kind=Kind.SPEECH, actor=2, text="2号 弃票",
                        action="vote", target=None)
        cut = build_brief(long_log, summary, seats, max_chars=50)
        check("超长发言被截断并留下标记", "…（后略）" in cut)
        check("截断是按 max_chars 走的", "啰嗦" * 26 not in cut)
        check("票型汇总成一行（引擎拼的「投给」不再单独占行）",
              "票型：1号→2号、2号→弃票" in cut, cut)
        check("弃票也记进票型（谁到投票环节就消失本身就是证据）",
              "2号→弃票" in cut)
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
        shutil.rmtree(os.path.join(ROOT, "runs", "_selftest_reflect_half"),
                      ignore_errors=True)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：{FAILURES}")
        return 1
    print("✅ 局后感言全部通过（身份来源 / 不泄漏模型名 / 清洗 / 不污染对局记录 / 续跑）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
