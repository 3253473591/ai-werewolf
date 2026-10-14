#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""玩法规则自测 —— 覆盖这一轮补上的六个环节。

    1. 投票理由进大桌（不再被丢弃）
    2. 狼队多数表决（不再是"最后一只狼独裁"）
    3. 夜里被刀的猎人**天亮立刻开枪**
    4. 平票 PK 发言 + 重投（再平票才无人出局）
    5. 狼人自曝（自曝者出局、白天立即结束、当天不投票）
    6. 尾部块「你的历史立场」（防改口，且不破坏共享前缀）

用**脚本化假 relay** 给死答案，所以每个分支都能被精确触发，不依赖真实模型。
用法：python tools/selftest_gameplay.py
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiwolf.engine import Game  # noqa: E402
from aiwolf.events import Channel, Kind  # noqa: E402
from aiwolf.relay import LLMResult  # noqa: E402
from aiwolf.render import (  # noqa: E402
    build_history,
    build_messages,
    check_no_model_names,
    check_shared_prefix,
)
from aiwolf.seats import build_seats, load_config  # noqa: E402

FAILURES = []
CONFIG = os.path.join(ROOT, "config", "seats.json")

# 假 relay 的兜底动作：(speech, type, target)
DEFAULT_ACTION: Dict[str, Tuple[str, str, Optional[int]]] = {
    "NIGHT_WOLF_CHAT": ("我没有意见。", "chat", None),
    "NIGHT_WOLF": ("", "kill", None),          # target 缺失 -> 空刀
    "NIGHT_SEER": ("", "check", None),         # target 缺失 -> 查验作废
    "NIGHT_WITCH": ("", "pass", None),
    "NIGHT_GUARD": ("", "guard", None),
    "DAY_SPEECH": ("我是好人，先过。", "speak", None),
    "DAY_PK_SPEECH": ("我在 PK 台上，我是好人。", "speak", None),
    "DAY_VOTE": ("我不投。", "pass", None),     # pass 在投票阶段 = 弃票
    "DAY_VOTE_PK": ("我弃票。", "pass", None),
    "DAY_LAST_WORDS": ("我走了。", "speak", None),
    "DAY_HUNTER": ("", "pass", None),
}


class ScriptedRelay:
    """按 (座位号, 阶段) 返回写死的决策。

    没写死的走 DEFAULT_ACTION —— 保证任何阶段都有合法结构，
    测试只关心被测的那一条分支。
    """

    def __init__(self, script: Optional[Dict[tuple, tuple]] = None, *,
                 truncated: Optional[Dict[tuple, str]] = None,
                 reasoning_only: Optional[Dict[tuple, str]] = None,
                 fail: Optional[set] = None) -> None:
        self.script = script or {}
        self.calls = []
        self.stats = []
        # (座位, 阶段) -> 实际发出去的 messages：用来断言**提示词里到底写了什么**
        # （光测引擎行为不够 —— 这次的 bug 有一半出在提示词和校验对不上）
        self.messages_by_key: Dict[tuple, list] = {}
        # (座位, 阶段) -> 思维链文本：正文空 + finish_reason=length（被截断）
        self.truncated = truncated or {}
        # (座位, 阶段) -> 答案文本：正文空 + finish_reason=stop（网关把答案塞在推理字段）
        self.reasoning_only = reasoning_only or {}
        # ★ (座位, 阶段) 集合：这些调用**全部失败**（模拟真故障：超时 / 空响应）。
        # 用途是钉住"引擎不能把故障写成主动选择"这条 —— 实测 12号 猎人的
        # DAY_HUNTER 就是被写成了"猎人选择不开枪"。
        self.fail = fail or set()

    def chat(self, seat, messages, *, phase, day, ctx=None, max_tokens=1024,
             response_format=None, **kw) -> LLMResult:
        self.calls.append((seat.no, phase))
        key = (seat.no, phase)
        self.messages_by_key[key] = messages
        self.stats.append({
            "seat": seat.no, "label": seat.label, "phase": phase, "model": seat.model,
            "substituted": False, "latency_s": 0.1, "prompt_tokens": 100, "cached_tokens": 50,
            "truncated": key in self.truncated, "error": "", "attempts": 1,
        })
        if key in self.fail:
            # 与真实故障同形：正文空、推理空、带 error、试过主模型
            self.stats[-1].update({"error": "curl: (28) Connection timed out",
                                   "attempts": 2, "prompt_tokens": 0})
            return LLMResult(text="", reasoning="", model_used="",
                             error="curl: (28) Connection timed out after 15007 milliseconds",
                             models_tried=[seat.model], attempts=2)
        if key in self.truncated:
            return LLMResult(text="", reasoning=self.truncated[key],
                             finish_reason="length", model_used="script")
        if key in self.reasoning_only:
            return LLMResult(text="", reasoning=self.reasoning_only[key],
                             finish_reason="stop", model_used="script")
        speech, act, target = self.script.get(
            key, DEFAULT_ACTION.get(phase, ("", "pass", None)))
        return LLMResult(
            text=json.dumps({
                "thinking": "（脚本答案）",
                "speech": speech,
                "action": {"type": act, "target": target},
                "confidence": 0.8,
            }, ensure_ascii=False),
            model_used="script",
        )


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def make_game(count: int, script=None, max_days: int = 1, sheriff: bool = False,
              **relay_kw) -> Game:
    """默认 **关掉警长** —— 首日竞选会多出 N 次调用，会把其它组的
    "调用了哪些阶段"断言冲掉。要测警长的组显式传 sheriff=True。"""
    cfg = load_config(CONFIG)
    seats = build_seats(cfg, count)          # 不打乱：1、2 号必为狼，角色可预测
    return Game(seats, ScriptedRelay(script, **relay_kw), max_days=max_days,
                verbose=False, seed=1234, sheriff=sheriff)


def texts_of(game: Game, phase: Optional[str] = None) -> str:
    return "\n".join(e.text for e in game.log
                     if phase is None or e.phase == phase)


def index_of(game: Game, phase: str) -> int:
    for i, ev in enumerate(game.log):
        if ev.phase == phase:
            return i
    return -1


# --------------------------------------------------------------------------- #

def test_vote_reason():
    print("\n[1] 投票理由进大桌（以前被直接丢掉）")
    script = {
        (1, "DAY_VOTE"): ("我投3号，他发言最划水。", "vote", 3),
        (2, "DAY_VOTE"): ("跟1号，投3号。", "vote", 3),
        (3, "DAY_VOTE"): ("我投5号自保。", "vote", 5),
        (4, "DAY_VOTE"): ("我投3号。", "vote", 3),
        (5, "DAY_VOTE"): ("我投3号。", "vote", 3),
    }
    game = make_game(5, script)
    game.state.day = 1
    out = game.day_vote()

    reasons = [e.text for e in game.log
               if e.phase == "DAY_VOTE" and e.kind is Kind.SPEECH
               and e.data.get("action") is None]
    check("投票理由落进大桌", "我投3号，他发言最划水。" in reasons,
          f"理由条数 {len(reasons)}")
    check("理由条数 = 投票人数", len(reasons) == 5, f"实际 {len(reasons)}")
    check("放逐结果正确", out == 3, f"实际 {out}")

    votes = [e for e in game.log if e.data.get("action") == "vote"]
    check("票型带结构化数据（历史立场块要用）",
          len(votes) == 5 and all("target" in e.data for e in votes),
          f"票事件 {len(votes)} 条")


def test_wolf_majority():
    print("\n[2] 狼队多数表决（不再由最后一只狼独裁）")
    game = make_game(9, {
        (1, "NIGHT_WOLF"): ("", "kill", 3),
        (2, "NIGHT_WOLF"): ("", "kill", 3),
        (3, "NIGHT_WOLF"): ("", "kill", 4),
    })
    game.state.day = 1
    game.night()
    check("2:1 时按多数派定刀口", game.state.wolf_target == 3,
          f"实际 {game.state.wolf_target}")
    check("决议记进小桌", "狼队决议" in texts_of(game, "NIGHT_WOLF"))

    # 平票：最后一只狼提的是 4 号，但 3 号先被提出 -> 应该按 3 号
    tie = make_game(5, {
        (1, "NIGHT_WOLF"): ("", "kill", 3),
        (2, "NIGHT_WOLF"): ("", "kill", 4),
    })
    tie.state.day = 1
    tie.night()
    check("平票按'谁先提出'定，而不是最后发言的狼", tie.state.wolf_target == 3,
          f"实际 {tie.state.wolf_target}")
    check("平票在决议里标注", any(e.data.get("tied") for e in tie.log))


def test_hunter_shoots_at_dawn():
    print("\n[3] 夜里被刀的猎人，天亮立刻开枪")
    # 7 人局：5 号是猎人。两只狼都刀 5 号 -> 猎人死 -> 天亮开枪打 1 号（狼）
    game = make_game(7, {
        (1, "NIGHT_WOLF"): ("", "kill", 5),
        (2, "NIGHT_WOLF"): ("", "kill", 5),
        (5, "DAY_HUNTER"): ("", "shoot", 1),
    })
    game.run()
    hi, si = index_of(game, "DAY_HUNTER"), index_of(game, "DAY_SPEECH")
    check("猎人开过枪", hi >= 0)
    check("开枪发生在当天发言之前", 0 <= hi < si, f"hunter@{hi} speech@{si}")
    check("枪口带走了狼人", not game.by_no[1].alive)
    check("猎人本身已出局", game.by_no[5].role == "Hunter" and not game.by_no[5].alive)


def test_hunter_no_response_not_a_choice():
    """真故障不许被写成"主动选择"—— 引擎不能撒谎。

    起因（2026-10-06 实测，12 座局）：12号 猎人的 DAY_HUNTER 两次尝试
    一次 curl 超时、一次 HTTP 200 但响应体完全为空。这是基础设施故障，
    不是决策。可公开公告只写了「12号 猎人选择不开枪」，于是：
      · 全场把这句话当推理依据 —— 8号 拿它去推"12号自己也不确定
        谁是最后一张狼，说明判断链有盲区"；
      · 12号 本人也没有证据，只能凭"我没开枪"这个结果**自己编一个动机**
        （它第一轮说"我怕打错，选择了保守"）。
    也就是说：**引擎伪造的那五个字，污染了全场的推理和事后的复盘。**

    狼人的角色提示词允许**狼自己**伪造系统故障（那是合法的狼队阴招），
    但引擎替模型宣布一个它没做过的决定，性质完全不同 —— 玩家可以撒谎，
    权威记录不行。
    """
    print("\n[3.5] 真故障不许写成主动选择（引擎不能替模型认下一个决定）")
    # 7 人局：5 号是猎人。两只狼都刀 5 号 -> 猎人死 -> 天亮该开枪。
    # 但 5号 的 DAY_HUNTER **全部失败**。
    game = make_game(7,
                     {(1, "NIGHT_WOLF"): ("", "kill", 5),
                      (2, "NIGHT_WOLF"): ("", "kill", 5)},
                     fail={(5, "DAY_HUNTER")})
    game.run()
    blob = texts_of(game, "DAY_HUNTER")
    check("公告里**没有**「选择不开枪」这句伪造的话",
          "选择不开枪" not in blob, blob[:90])
    check("公告写明是未表态 / 没有回应", ("未表态" in blob or "没有回应" in blob), blob[:90])
    check("事件带 no_response 标记（复盘时能查出来是故障）",
          any(e.data.get("no_response") for e in game.log if e.phase == "DAY_HUNTER"))
    check("上帝账本里留着真故障原因（real_fault）",
          any(e.data.get("real_fault") for e in game.log))

    # 对照：**真的**主动放弃时，文案必须照旧写"选择不开枪"
    game2 = make_game(7,
                      {(1, "NIGHT_WOLF"): ("", "kill", 5),
                       (2, "NIGHT_WOLF"): ("", "kill", 5),
                       (5, "DAY_HUNTER"): ("", "pass", None)})
    game2.run()
    blob2 = texts_of(game2, "DAY_HUNTER")
    check("主动放弃时仍然写「选择不开枪」（别把正常路径改坏）",
          "选择不开枪" in blob2, blob2[:90])

    # 同一类伪造在夜间技能路径上也要挡住。
    # 7 人局不打乱时的角色：1、2 狼 / 3 预言家 / 4 女巫 / 5 猎人 / 6、7 村民。
    game3 = make_game(7, {}, fail={(3, "NIGHT_SEER")})
    game3.night()
    seer_blob = "\n".join(e.text for e in game3.log if e.phase == "NIGHT_SEER")
    check("预言家调用失败时不写「选择不查验任何人」",
          "选择不查验任何人" not in seer_blob, seer_blob[:90])
    check("预言家失败写的是未表态", "未表态" in seer_blob or "没有回应" in seer_blob,
          seer_blob[:90])


def test_pk_vote():
    print("\n[4] 平票 PK：台上发言 + 台外重投")
    script = {
        (1, "DAY_VOTE"): ("我投2号。", "vote", 2),
        (2, "DAY_VOTE"): ("我投1号。", "vote", 1),
        (3, "DAY_VOTE"): ("我投2号。", "vote", 2),
        (4, "DAY_VOTE"): ("我投1号。", "vote", 1),
        (5, "DAY_VOTE"): ("我弃票。", "pass", None),
        (1, "DAY_PK_SPEECH"): ("我是1号，台上我是好人。", "speak", None),
        (2, "DAY_PK_SPEECH"): ("我是2号，1号在骗人。", "speak", None),
        (3, "DAY_VOTE_PK"): ("我投5号。", "vote", 5),     # 台外目标 -> 应作废
        (4, "DAY_VOTE_PK"): ("我投2号。", "vote", 2),
        (5, "DAY_VOTE_PK"): ("我跟票，投2号。", "vote", 2),
    }
    game = make_game(5, script)
    game.state.day = 1
    out = game.day_vote()

    check("平票后进入 PK", "PK 发言" in texts_of(game, "DAY_VOTE"))
    check("PK 发言真的发生了", "我是2号，1号在骗人。" in texts_of(game, "DAY_PK_SPEECH"))
    check("PK 重投分出结果", out == 2, f"实际 {out}")
    check("PK 结果文案正确", "PK 重投" in texts_of(game, "DAY_VOTE_PK"))
    check("台外目标被作废（3号弃票）", "3号 弃票" in texts_of(game, "DAY_VOTE_PK"))
    pk_voters = sorted(e.actor for e in game.log
                       if e.phase == "DAY_VOTE_PK" and e.kind is Kind.SPEECH)
    check("PK 台上的人没有投票权", 1 not in pk_voters and 2 not in pk_voters,
          f"投票者 {pk_voters}")

    # ★ 2026-10-11 修：台上的人"不投票"是**规则**，不是白痴翻牌。
    #   老实现见人就写"已翻牌白痴，没有投票权"，于是 PK 重投时台上两个平票者
    #   被公开宣布成翻牌白痴 —— 12 座局第 4 天实录就是两行
    #   "1号 已翻牌白痴" / "12号 已翻牌白痴"，而本局**一张白痴牌都没翻**。
    #   8号 MiniMax 整段推理都在纠结"两个人怎么都报了白痴"，票型计算全被带歪。
    #   引擎不能撒谎：没有票的原因必须分得开。
    pk_notes = [e.text for e in game.log
                if e.phase == "DAY_VOTE_PK" and e.kind is Kind.SYSTEM]
    check("PK 台上的人**不被写成白痴**（那是规则排除，不是翻牌）",
          not any("翻牌白痴" in t for t in pk_notes), str(pk_notes))
    check("台上的人有一句准确的说明（台外重投）",
          any("在 PK 台上" in t and "1号" in t for t in pk_notes), str(pk_notes))
    check("这条说明留了可查的标记（pk_stage，不是 no_vote）",
          any(e.data.get("pk_stage") and e.data.get("seat") == 1 for e in game.log
              if e.phase == "DAY_VOTE_PK"))
    # 反面（真白痴翻牌时照旧写"已翻牌白痴"）由 [17] 的 DAY_VOTE 断言守着 ——
    # 那里期望的就是 "8号 已翻牌白痴，没有投票权（本轮不投票）"。

    # 再平票 -> 无人出局
    tie = make_game(5, {
        (1, "DAY_VOTE"): ("", "vote", 2),
        (2, "DAY_VOTE"): ("", "vote", 1),
        (3, "DAY_VOTE"): ("", "vote", 2),
        (4, "DAY_VOTE"): ("", "vote", 1),
        (5, "DAY_VOTE"): ("", "pass", None),
        (4, "DAY_VOTE_PK"): ("我投2号。", "vote", 2),
        (5, "DAY_VOTE_PK"): ("我投1号。", "vote", 1),
    })
    tie.state.day = 1
    out2 = tie.day_vote()
    check("PK 再次平票 -> 无人出局", out2 is None, f"实际 {out2}")
    check("再次平票有明确公告", "PK 再次平票" in texts_of(tie, "DAY_VOTE_PK"))


def test_explode():
    print("\n[5] 狼人自曝：**自曝者出局**、白天立即结束、当天不投票")
    game = make_game(5, {
        (1, "DAY_SPEECH"): ("我是狼，我自曝，今晚见。", "explode", None),
    })
    game.run()
    check("自曝事件被记录", any(e.data.get("exploded") for e in game.log))
    check("自曝发言仍然进大桌", "我自曝" in texts_of(game, "DAY_SPEECH"))
    check("自曝后当天没有投票", index_of(game, "DAY_VOTE") < 0)

    # ★ 2026-10-06 修：老实现只跳过投票、谁也没死，于是自曝是**纯赚**
    #   （既省掉一轮投票又活着）。10 座局里 1 号自曝后又活了整整两天，
    #   终局正好卡在 2v2 的 parity 上判狼胜。有没有代价是这个动作成不成立的关键。
    #   make_game 是脚本化 relay，跑完一整局；这里直接查最终的存活集合。
    check("**自曝的狼人出局了**（不再是白赚一轮投票）",
          1 not in {s.no for s in game.alive()},
          f"存活：{sorted(s.no for s in game.alive())}")
    check("出局原因记成 explode",
          getattr(game.by_no[1], "death_cause", "") == "explode",
          str(getattr(game.by_no[1], "death_cause", "")))
    check("公告里说清了会出局", any("出局" in (e.text or "") for e in game.log
                                    if e.data.get("exploded")))

    # 好人喊自曝不算数（explode 只对狼生效）
    good = make_game(5, {
        (3, "DAY_SPEECH"): ("我是预言家，我自曝。", "explode", None),   # 3 号是预言家
    })
    good.run()
    check("好人用 explode 不会被当成自曝",
          not any(e.data.get("exploded") for e in good.log))
    check("好人喊 explode 也不会因此出局",
          3 in {s.no for s in good.alive()},
          f"存活：{sorted(s.no for s in good.alive())}")


def test_idiot():
    print("\n[17] 白痴：被放逐翻牌不出局、失去投票权；再次被投则出局")
    # 12 人板（不打乱时角色是位置固定的）：
    #   1-4 狼 / 5 预言家 / 6 女巫 / 7 猎人 / 8 **白痴** / 9-12 村民
    everyone_votes_8 = {(n, "DAY_VOTE"): ("我投8号。", "vote", 8)
                        for n in range(1, 13)}
    g = make_game(12, everyone_votes_8, max_days=1)
    g.run()
    rev = [e for e in g.log if e.data.get("idiot_revealed")]
    check("白痴被放逐时翻牌了", len(rev) == 1, str(len(rev)))
    if rev:
        check("翻牌公告说清了不出局", "不出局" in rev[0].text, rev[0].text)
        check("翻牌公告说清了失去投票权", "投票权" in rev[0].text, rev[0].text)
    check("**白痴没有出局**",
          8 in {s.no for s in g.alive()},
          f"存活：{sorted(s.no for s in g.alive())}")
    check("出局原因没有被写成投死",
          getattr(g.by_no[8], "death_cause", "") != "vote",
          str(getattr(g.by_no[8], "death_cause", "")))
    check("引擎记住了它已翻牌", g.state.idiot_revealed is True)
    check("翻牌的白痴不在投票权列表里",
          8 not in {s.no for s in g.voters()}, str([s.no for s in g.voters()]))

    # 第二天：白痴还在场上、还能发言，但投票阶段不叫它
    g2 = make_game(12, everyone_votes_8, max_days=2)
    g2.run()
    no_vote = [e for e in g2.log if e.data.get("no_vote")]
    check("第二天留下「没有投票权」的记录", len(no_vote) >= 1,
          str([e.text for e in no_vote][:2]))
    if no_vote:
        check("那条记录指名了白痴", no_vote[0].data.get("seat") == 8,
              str(no_vote[0].data.get("seat")))
    spoke_after = [e for e in g2.log
                   if e.actor == 8 and e.phase == "DAY_SPEECH" and e.kind is Kind.SPEECH]
    check("翻牌后仍然参加白天发言", len(spoke_after) >= 2,
          f"{len(spoke_after)} 次")
    # 投票阶段只应该在第 1 天（翻牌前）问过它一次；翻牌后不能再要票。
    # 第一版这里写成了"列表里找不到 (8,'DAY_VOTE')"，而列表里本来就有——恒真，白测。
    n_vote_calls = g2.relay.calls.count((8, "DAY_VOTE"))
    check("**翻牌后不再向白痴要票**（全程只问过翻牌前那一次）",
          n_vote_calls == 1, f"共问过 {n_vote_calls} 次：{g2.relay.calls}")

    # 再次被投 -> 正常出局（免死只有一次）
    g3 = make_game(12, everyone_votes_8, max_days=3)
    g3.run()
    check("**翻牌后再被投就正常出局了**",
          8 not in {s.no for s in g3.alive()},
          f"存活：{sorted(s.no for s in g3.alive())}")
    check("这次出局原因是投票", getattr(g3.by_no[8], "death_cause", "") == "vote",
          str(getattr(g3.by_no[8], "death_cause", "")))

    # 白痴被狼刀则正常死（免死只挡投票放逐，不挡夜刀）
    g4 = make_game(12, {(1, "NIGHT_WOLF"): ("", "kill", 8),
                        (2, "NIGHT_WOLF"): ("", "kill", 8),
                        (3, "NIGHT_WOLF"): ("", "kill", 8),
                        (4, "NIGHT_WOLF"): ("", "kill", 8)},
                   max_days=1)
    g4.run()
    check("夜里被刀白痴照常出局（免死不挡夜刀）",
          8 not in {s.no for s in g4.alive()},
          f"存活：{sorted(s.no for s in g4.alive())}")
    check("这种情况下不会翻牌", g4.state.idiot_revealed is False)


def test_sheriff():
    print("\n[18] 警长：竞选 → 1.5 票 → 定发言方向 → 出局移交警徽/撕警徽")
    # 12 人板（不打乱）：1-4 狼 / 5 预 / 6 女巫 / 7 猎人 / 8 白痴 / 9-12 村民

    # --- 1a. 只有一人上警 -> 自动当选，不投票 ---
    g = make_game(12, {(1, "DAY_SHERIFF_RUN"): ("我上警，我是预言家。", "run", None)},
                  max_days=1, sheriff=True)
    g.run()
    check("警长选出来了", g.state.sheriff == 1, str(g.state.sheriff))
    check("只有一人上警时**自动当选、不投票**（省一轮额度）",
          not any(e.phase == "DAY_SHERIFF_VOTE" for e in g.log),
          str([e.phase for e in g.log if e.phase == "DAY_SHERIFF_VOTE"][:3]))
    check("公告说清了是自动当选",
          any("自动当选" in (e.text or "") for e in g.log))

    # --- 1b. 两人上警 -> 开始投票，且**上警的人不能投警长票** ---
    g1b = make_game(12, {
        (1, "DAY_SHERIFF_RUN"): ("我上警。", "run", None),
        (2, "DAY_SHERIFF_RUN"): ("我也上警。", "run", None),
        **{(n, "DAY_SHERIFF_VOTE"): ("我投1号。", "vote", 1) for n in range(3, 13)},
    }, max_days=1, sheriff=True)
    g1b.run()
    voted_by = {e.actor for e in g1b.log
                if e.phase == "DAY_SHERIFF_VOTE" and e.data.get("action") == "vote"}
    check("**上警的人没有投警长票**", not ({1, 2} & voted_by), str(sorted(voted_by)))
    check("没上警的人都投了", voted_by == set(range(3, 13)), str(sorted(voted_by)))
    check("得票最多的当选", g1b.state.sheriff == 1, str(g1b.state.sheriff))

    # --- 2. 警长 1.5 票：5 票 vs 4 票本来平不了，加 0.5 才是 5.5 ---
    # 让警长(1号)和另一个人各拿一批票，验证票数带小数
    script = {
        (1, "DAY_SHERIFF_RUN"): ("我上警。", "run", None),
        (1, "DAY_SHERIFF_ORDER"): ("从警左。", "order_left", None),
        (1, "DAY_VOTE"): ("我投3号。", "vote", 3),
        (2, "DAY_VOTE"): ("我投3号。", "vote", 3),
        (3, "DAY_VOTE"): ("我投4号。", "vote", 4),
        (4, "DAY_VOTE"): ("我投5号。", "vote", 5),
    }
    g2 = make_game(12, script, max_days=1, sheriff=True)
    g2.run()
    check("警长在职", g2.state.sheriff == 1, str(g2.state.sheriff))
    ex = [e for e in g2.log if e.phase == "DAY_VOTE" and e.kind is Kind.RESOLUTION
          and "被放逐" in (e.text or "")]
    check("有人被放逐", bool(ex), str(len(ex)))
    if ex:
        check("警长那 0.5 票进了票数（出现 .5）", ".5" in ex[0].text, ex[0].text)

    # --- 3. 发言顺序：警左 = 座位号递增环绕，警长最后 ---
    g3 = make_game(12, {(1, "DAY_SHERIFF_RUN"): ("我上警。", "run", None),
                        (1, "DAY_SHERIFF_ORDER"): ("从警左开始。", "order_left", None)},
                   max_days=1, sheriff=True)
    g3.run()
    order = [s.no for s in g3.speaking_order()]
    check("警左：从警长下一位开始递增", order[0] == 2, str(order))
    check("**警长最后发言**（归票位）", order[-1] == 1, str(order))
    check("顺序里每个人都有且只有一次", sorted(order) == list(range(1, 13)), str(order))

    g3r = make_game(12, {(1, "DAY_SHERIFF_RUN"): ("我上警。", "run", None),
                         (1, "DAY_SHERIFF_ORDER"): ("从警右开始。", "order_right", None)},
                    max_days=1, sheriff=True)
    g3r.run()
    order_r = [s.no for s in g3r.speaking_order()]
    check("警右：从警长上一位开始递减", order_r[0] == 12, str(order_r))
    check("警右时警长也最后发言", order_r[-1] == 1, str(order_r))

    # --- 4. 没人上警 -> 警徽流失，且不产生警长 ---
    g4 = make_game(12, {}, max_days=1, sheriff=True)
    g4.run()
    check("全员不上警时警徽流失", g4.state.badge_destroyed is True)
    check("这种情况下没有警长", g4.state.sheriff is None)
    check("公告说清了警徽流失",
          any("警徽流失" in (e.text or "") for e in g4.log))

    # --- 5. 平票 -> PK -> 再平票 -> 流失 ---
    tie_script = {(n, "DAY_SHERIFF_RUN"): ("我上警。", "run", None)
                  for n in (1, 2)}
    # 上警的是 1、2；投票人 3-12。让 5 个投 1、5 个投 2 -> 平票
    for i, n in enumerate(range(3, 13)):
        tie_script[(n, "DAY_SHERIFF_VOTE")] = ("", "vote", 1 if i < 5 else 2)
    g5 = make_game(12, tie_script, max_days=1, sheriff=True)
    g5.run()
    check("平票进入了 PK 发言",
          any(e.phase == "DAY_SHERIFF_PK" for e in g5.log))
    check("再平票 -> 警徽流失", g5.state.badge_destroyed is True
          and g5.state.sheriff is None,
          f"sheriff={g5.state.sheriff} destroyed={g5.state.badge_destroyed}")

    # --- 6. 警长被放逐 -> 移交警徽 ---
    g6 = make_game(12, {
        (1, "DAY_SHERIFF_RUN"): ("我上警。", "run", None),
        (1, "DAY_SHERIFF_ORDER"): ("从警左。", "order_left", None),
        (1, "DAY_BADGE"): ("警徽给5号。", "badge", 5),
        **{(n, "DAY_VOTE"): ("投1号。", "vote", 1) for n in range(2, 13)},
    }, max_days=1, sheriff=True)
    g6.run()
    check("警长被放逐了", 1 not in {s.no for s in g6.alive()},
          str(sorted(s.no for s in g6.alive())))
    check("**警徽移交给了指定的人**", g6.state.sheriff == 5, str(g6.state.sheriff))
    check("移交记录进了大桌",
          any("移交给 5号" in (e.text or "") for e in g6.log))

    # --- 7. 警长被放逐 + 不指定 -> 撕警徽 ---
    g7 = make_game(12, {
        (1, "DAY_SHERIFF_RUN"): ("我上警。", "run", None),
        (1, "DAY_SHERIFF_ORDER"): ("从警左。", "order_left", None),
        (1, "DAY_BADGE"): ("我撕警徽。", "pass", None),
        **{(n, "DAY_VOTE"): ("投1号。", "vote", 1) for n in range(2, 13)},
    }, max_days=1, sheriff=True)
    g7.run()
    check("不指定继承人时警徽被撕掉", g7.state.badge_destroyed is True
          and g7.state.sheriff is None,
          f"sheriff={g7.state.sheriff} destroyed={g7.state.badge_destroyed}")

    # --- 8. 关掉开关就不该有竞选 ---
    g8 = make_game(12, {}, max_days=1, sheriff=False)
    g8.run()
    check("sheriff=False 时完全不跑竞选",
          not any(e.phase.startswith("DAY_SHERIFF") for e in g8.log),
          str([e.phase for e in g8.log if e.phase.startswith("DAY_SHERIFF")][:3]))
    check("sheriff=False 时没有警长", g8.state.sheriff is None)

    # --- 9. 动作别名：中文「警左」「上警」要认得出 ---
    from aiwolf.protocol import parse_decision
    d = parse_decision(json.dumps({"thinking": "t", "speech": "从警左",
                                   "action": {"type": "警左", "target": None},
                                   "confidence": 0.9}, ensure_ascii=False),
                       "DAY_SHERIFF_ORDER")
    check("「警左」被识别成 order_left", d.action.type == "order_left", d.action.type)
    d2 = parse_decision(json.dumps({"thinking": "t", "speech": "我上警",
                                    "action": {"type": "上警", "target": None},
                                    "confidence": 0.9}, ensure_ascii=False),
                        "DAY_SHERIFF_RUN")
    check("「上警」被识别成 run", d2.action.type == "run", d2.action.type)


def test_board_notice():
    print("\n[15] 板子公告：写清几个神职、说明没有警长，且**只写公告不写说教**")
    # 起因：10 座局里多个模型凭空发明了"警长"和"警徽流"（12 人警长板的术语，
    # 本引擎没有警长）。结果 6、7 号拿"3号没报验人顺序"当定狼硬伤，
    # 8 号一本正经地报警徽流，2 号只好花一整段去纠正"本轮并没有警长竞选"。
    # 这条不成立的指控实际成了第 1 天放逐真预言家的理由之一。
    from aiwolf.render import board_notice

    cfg = load_config(CONFIG)
    for count, want in ((5, ["狼人 2 名", "预言家 1 名", "女巫 1 名", "村民 1 名"]),
                        (12, ["狼人 4 名", "预言家 1 名", "女巫 1 名", "猎人 1 名",
                              "白痴 1 名", "村民 4 名"])):
        seats = build_seats(cfg, count)
        txt = board_notice(seats)
        check(f"{count} 人局的角色构成写全了",
              all(w in txt for w in want), txt.splitlines()[2] if len(txt.splitlines()) > 2 else "")
        # 警长上线后默认是"有警长"，公告必须跟着变 —— 否则就是提示词和引擎各说一套
        check(f"{count} 人局公告说清了有警长", "有警长" in txt)
        check(f"{count} 人局公告说清了 1.5 票", "1.5 票" in txt)
        check(f"{count} 人局关掉警长后公告改口",
              "没有警长" in board_notice(seats, sheriff=False))

    # ★ 用户反馈（2026-10-06）：第一版把"不要…"、"会暴露你在套用别的板子"这类
    #   告诫全塞进去了。两个毛病：一是啰嗦，二是**"警徽流"这个不存在的词被重复了
    #   四遍，反而把它反复加权重**，更容易被模型抓去用。公告就该是公告。
    #   这条断言就是拿来防它长回去的。
    txt10 = board_notice(build_seats(load_config(CONFIG), 10))
    check("只写公告，不写说教（没有「不要…」式告诫）",
          "不要" not in txt10 and "暴露" not in txt10, repr(txt10))
    check("不长（每条 system 都带着它）", len(txt10) < 260, f"{len(txt10)} 字")
    # 只有"没有警长"那一版才需要点名警徽流不存在；有警长时警徽流是真实机制。
    txt_nosheriff = board_notice(build_seats(load_config(CONFIG), 10), sheriff=False)
    check("关掉警长时「警徽流」只出现一次（反复提反而加权重）",
          txt_nosheriff.count("警徽流") == 1, f"{txt_nosheriff.count('警徽流')} 次")

    # 板子对所有座位必须字节级相同，否则共享前缀就断了
    seats10 = build_seats(load_config(CONFIG), 10)
    check("板子对所有座位字节级相同",
          len({board_notice(seats10) for _ in seats10}) == 1)

    # 而且它要真的进了 system 消息
    from aiwolf.events import EventLog
    from aiwolf.render import build_messages
    log = EventLog()
    log.append(day=1, phase="NIGHT_START", channel=Channel.PUBLIC,
               kind=Kind.SYSTEM, text="天黑请闭眼")
    msgs, _ = build_messages(seats10[0], seats10, log, day=1, phase="DAY_SPEECH",
                             task="t", state={})
    check("板子进了 system 消息（第一条）",
          "有警长" in msgs[0]["content"], msgs[0]["content"][:60])
    check("system 里也写了角色构成", "预言家 1 名" in msgs[0]["content"])

    # 自曝会出局这条规则留在 TASKS 里说（那才是它派得上用场的地方），
    # 不重复塞进板子公告。
    from aiwolf.engine import TASKS
    check("自曝出局的代价在 TASKS 里说清了",
          "自曝即出局" in TASKS["DAY_SPEECH"] and "出局" in TASKS["DAY_SPEECH"])

    # 「服务端连接超时」这句要作为**欺骗手段**存在，不是"没话说时的退路"。
    # 定位错了它就废了：退路＝放弃发言；手段＝一张牌。用户明确纠正过这一点。
    #
    # ★ 2026-10-06 两轮改动，教训记在这里：
    #   第一轮我把这张牌从"掉线"扩成整个元层面并加了两条禁区；
    #   随后**用户亲手把整段文案重写了**（压缩成「系统故障」策略 + 禁区两节）。
    #   于是我这批**字面匹配**的断言全红了 —— 但那不是代码坏了，是断言写得太脆：
    #   它锚定的是我的措辞，不是要保证的**语义**。
    #
    #   所以下面改成按语义断言：
    #     · 位置：是"手段"不是"退路"（第二轮写在「行为准则」之后，
    #       第三轮起改由狼人尾部块承载 —— 见下面的第三轮说明）；
    #     · 内容：元层面几种说法、代价、两条禁区的**实质**都要在；
    #     · 允许换措辞 —— 用户改文案不该把测试改红。
    #   代价是断言比字面匹配弱一点，这个取舍是有意的：**测语义，不测我的文风**。
    #
    # ★ 2026-10-06 第三轮（用户实测后）：这张牌**从全局挪进了狼人 CharacterPrompt**。
    #   用户的原话是"放在全局注入没起到效果" —— 全局那节 9 个座位一起读，
    #   好人读了觉得跟己无关，狼人读了也只是一条通用守则，等真到白天要开口时
    #   （注意力在尾部块）根本想不起来。现在它只在**狼人的动态角色提示词**里，
    #   也就是每轮发言前最后读到的那一段。
    #
    #   所以这一段的断言目标从 COMMON_RULES 换成 WOLF_DAY —— **语义不变，位置变了**。
    #   并且反过来钉一条：全局规则里**不许**再出现它，否则这个 bug 会悄悄回来。
    from aiwolf.render import COMMON_RULES, WOLF_DAY, WOLF_VOTE

    def _any_in(text: str, *words: str) -> bool:
        return any(w in text for w in words)

    def _has_any(*words: str) -> bool:
        return _any_in(WOLF_DAY, *words)

    # 位置①：全局规则里不许再出现 —— 一旦回流，等于又变成"所有人一起读"
    check("元层面那两张牌已经从全局规则里拿掉（回流 = 这个 bug 会再犯）",
          not _any_in(COMMON_RULES, "系统故障", "撞墙", "不可证伪"),
          "COMMON_RULES 里还留着元层面那段")
    # 位置②：它必须落在狼人白天的角色提示词里（发言前最后读到的那一段）
    check("牌写在狼人白天的 CharacterPrompt（WOLF_DAY）里",
          _any_in(WOLF_DAY, "系统故障", "撞墙"))
    check("投票阶段的狼人提示词也提了一句（那里也是公开发言）",
          _any_in(WOLF_VOTE, "撞墙"))
    check("单独立了一节讲它（狼人视角的「元层面话术」）",
          _any_in(WOLF_DAY, "元层面"), "没找到独立小节")
    check("点明了它不可证伪（为什么能用）", "不可证伪" in WOLF_DAY)
    # 元层面：至少覆盖 链路 / 上下文 / 生成 三类里的两类
    _meta_hits = [k for k in ("连接超时", "上下文截断", "上下文被截断",
                              "忘记前半局", "忘记了前半局", "记不清",
                              "幻觉", "编的", "输出中断")
                  if k in WOLF_DAY]
    check("牌面覆盖元层面（链路 / 上下文 / 生成）", len(_meta_hits) >= 3,
          f"只找到 {_meta_hits}")
    check("说清了可真可假、无需解释", _has_any("可真可假", "可以真话", "无需解释"))
    check("点明了它作为手段的代价（为什么不能滥用）",
          _has_any("牺牲本轮话语权", "等于这一轮弃权", "不提供任何信息", "不提供信息"))
    check("明确它只能偶尔用，不是每轮的口头禅",
          _has_any("偶尔", "频繁使用", "滑水"))
    # 禁区①：不许指认模型身份
    check("禁区①：不许指认对手是哪家模型",
          _any_in(COMMON_RULES, "不得把座位和**具体模型、厂商**对应起来",
                  "不说“我是Claude”", "模型画像"))
    # 禁区②：不许引用真实后台报错 —— 这是这张牌能成立的前提。
    # ⚠️ 它必须**留在全局**：这是引擎和所有座位的边界，不是狼人的战术，
    #    selftest_invariants 的 [9] 真故障隔离也依赖它。
    check("禁区②：不许引用真实后台报错 / 错误码 / 日志（留在全局）",
          _any_in(COMMON_RULES, "不得引用真实后台报错", "HTTP错误码", "HTTP 错误码"),
          "真故障一旦可被引用，元层面这张牌就变成可证伪的了")
    # ⚠️ 不能用「"没话说" not in 全文」这类子串否定判断：
    #    正文里本来就有"不是没话说时的退路"这种自否定句，会把自己绊倒。
    check("狼人那张牌里说清了发言放进 speech 字段", "speech" in WOLF_DAY)

    # ---- 2026-10-06 第二轮：用户点名要的「假装撞墙」牌 + 情绪话术放开 ----
    #
    # 这张牌的边界和「系统故障」一模一样，而且更险：模型很可能真的停手不答。
    # 所以三条断言缺一不可：① 牌在；② 说清它只是 speech 里的一句台词、
    # JSON 与 action 照真实计划填；③ 提醒对手也会用（别把"我不玩了"当免死金牌）。
    check("「假装撞墙」那张牌写进去了（用户点名要的狼队阴招）",
          _has_any("假装“撞墙”", '假装"撞墙"', "撞墙"))
    check("说清它只是 speech 里的一句台词、不是停手信号（action 照真实计划填）",
          _has_any("不是停手信号", "台词", "按真实计划走"))
    # ★ 2026-10-06 第三轮追加（用户点名的玩法）：这两张牌**不止能自保，还能栽赃**。
    #   原来只写了"让对手以为你没威胁、把你划掉"，那是**防守**用法；
    #   用户指出它还能用来"误导村民或其它神职是狼人" —— 崩溃比普通发言更像真话，
    #   所以临终点名一个好人、或者等别人踩你最凶时崩溃（嫁祸逼退赛的人），
    #   好人的票就会自己打到好人身上。少了这一条，这张牌只用了一半。
    check("写了第二种用法：借“崩溃”栽赃好人（不只是自保）",
          _any_in(WOLF_DAY, "栽赃", "临终", "互咬"), "只写了自保那一半")
    check("点名对象按胜率选：好人去换票，或已暴露的队友换深水位",
          _any_in(WOLF_DAY, "点一个**好人**", "已经暴露的队友"))
    check("说清这张牌的可信度是一次性的（演第二遍就没人信）",
          _any_in(WOLF_DAY, "可信度是一次性", "一次烧掉", "没人信"))
    check("提醒了「临终点名」可能只是狼在栽赃（别把临终的话当实锤）",
          _any_in(WOLF_DAY, "那可能只是狼在栽赃"), "只教一边就是教人上当")
    check("提醒了对手同样可能用这张牌（不然就是单方面教人上当）",
          "对手同样可能对你用这张牌" in WOLF_DAY)
    check("保留了「该正常表态时必须正常表态」（不能拿它当逃避发言的借口）",
          "该正常表态时必须正常表态" in WOLF_DAY)
    # "不要依赖人类情绪欺骗"这句会把狼的打法往纯逻辑上推，实测收窄了可用手段
    check("不再把情绪话术排除在外（原来写着「不要依赖人类情绪欺骗」）",
          "不要依赖人类情绪欺骗" not in COMMON_RULES
          and _any_in(COMMON_RULES, "情绪和话术与逻辑同等重要", "情绪也是武器"))
    check("明说了这是知情同意的对抗博弈（说谎是规则内动作）",
          "知情同意" in COMMON_RULES)


def test_target_legality_is_one_source():
    print("\n[16] 目标不合法：五条路径行为一致、动作文本不许和 target 打架")
    # 起因（用户实测）：
    #   #0003 座3 Grok（私密）选择守护3号
    #   #0002 （上帝）3号 Grok 的 守卫守护 目标 3 不合法，已作废
    # 同一条动作，一行说"选择了"，一行说"不合法" —— 而且提示词里
    # 只列了"存活"，引擎却偷偷排除自己，模型选自己完全是合理推断。
    #
    # 10 人局角色固定（不打乱）：1-3 狼、4 预言家、5 女巫、6 猎人、7 守卫、8-10 村民
    from aiwolf.render import build_situation

    # --- 1. 守卫自守：必须留痕、且文本自带"已作废" ---
    g = make_game(10, {(7, "NIGHT_GUARD"): ("我守自己。", "guard", 7)})
    g.run()
    acts = [e for e in g.log if e.phase == "NIGHT_GUARD" and e.kind is Kind.ACTION]
    check("守卫自守留下了一条动作记录", len(acts) == 1, str(len(acts)))
    if acts:
        ev = acts[0]
        check("生效目标被清空", ev.data.get("target") is None, str(ev.data.get("target")))
        check("原始意图记在 attempted 里（别丢证据）", ev.data.get("attempted") == 7,
              str(ev.data.get("attempted")))
        check("动作文本明确了'不合法，已作废'", "不合法" in (ev.text or "")
              and "作废" in (ev.text or ""), ev.text)
        check("动作文本带上了作废标注（不是一句裸的「选择守护7号」）",
              ev.text != "选择守护7号" and (ev.text or "").startswith("选择守护7号")
              and "作废" in (ev.text or ""), ev.text)
    gods = [e for e in g.log if e.data.get("issues") == ["invalid_target"]]
    check("有一条上帝视角诊断", len(gods) == 1, str(len(gods)))
    if gods:
        check("诊断里写清了原因（不能选自己）", "不能选自己" in gods[0].text, gods[0].text)
        check("诊断里带上了被拒的原始目标", gods[0].data.get("requested") == 7,
              str(gods[0].data.get("requested")))

    # --- 2. 故意不行动 ≠ 目标不合法（以前会误报一条"目标 None 不合法"）---
    g2 = make_game(10, {(7, "NIGHT_GUARD"): ("今晚不守。", "pass", None)})
    g2.run()
    bad = [e for e in g2.log if e.data.get("issues") == ["invalid_target"]]
    check("主动 pass 不会产生'不合法'误报", not bad,
          str([e.text for e in bad][:2]))
    pass_acts = [e for e in g2.log if e.phase == "NIGHT_GUARD" and e.kind is Kind.ACTION]
    check("主动 pass 记成「选择不行动」",
          pass_acts and "不行动" in pass_acts[0].text,
          pass_acts[0].text if pass_acts else "没有记录")

    # --- 3. 女巫把毒药指向非法目标：不能再静默变成"没用药" ---
    g3 = make_game(10, {(5, "NIGHT_WITCH"): ("我要毒自己。", "poison", 5)})
    g3.run()
    witch_acts = [e for e in g3.log if e.phase == "NIGHT_WITCH" and e.kind is Kind.ACTION]
    check("女巫的非法毒药有记录", len(witch_acts) == 1, str(len(witch_acts)))
    if witch_acts:
        check("**不再静默变成「没有使用药水」**",
              "没有使用药水" not in (witch_acts[0].text or ""), witch_acts[0].text)
        check("女巫这条也标了'不合法'", "不合法" in (witch_acts[0].text or ""),
              witch_acts[0].text)
        check("女巫的毒药没被真的用掉", not g3.state.witch_poison_used)
    check("女巫也有上帝诊断",
          any(e.data.get("issues") == ["invalid_target"] for e in g3.log))

    # --- 4. 猎人把枪口指向非法目标：不能假装成"选择不开枪" ---
    g4 = make_game(10, {(1, "NIGHT_WOLF"): ("", "kill", 6),      # 狼刀猎人
                        (6, "DAY_HUNTER"): ("", "shoot", 6)})    # 猎人朝自己开枪
    g4.run()
    hunter = [e for e in g4.log if e.phase == "DAY_HUNTER" and e.kind is Kind.RESOLUTION]
    check("猎人这轮有裁决记录", bool(hunter), str(len(hunter)))
    if hunter:
        check("**不再谎报成「选择不开枪」**",
              "选择不开枪" not in (hunter[0].text or ""), hunter[0].text)
        check("公开文案说明了目标不合法", "不合法" in (hunter[0].text or ""), hunter[0].text)

    # --- 5. 守卫连守同一人：提示词承诺过这条，现在真的拦得住 ---
    g5 = make_game(10, {(7, "NIGHT_GUARD"): ("", "guard", 8)}, max_days=2)
    g5.run()
    seq = [e for e in g5.log if e.phase == "NIGHT_GUARD" and e.kind is Kind.ACTION]
    check("两个夜晚各留一条守卫记录", len(seq) == 2, str(len(seq)))
    if len(seq) == 2:
        check("第一晚守住了 8 号", seq[0].data.get("target") == 8,
              str(seq[0].data.get("target")))
        check("第二晚连守同一人被拦下（这条规则以前只写在提示词里，没实现）",
              seq[1].data.get("target") is None, str(seq[1].data.get("target")))

    # --- 6. 提示词里的"可选目标"必须就是引擎接受的集合 ---
    g6 = make_game(10)
    g6.run()
    seat7 = next(s for s in g6.seats if s.no == 7)
    legal = g6.legal_targets(seat7, "NIGHT_GUARD")
    check("可选目标不含自己", 7 not in legal, str(legal))
    txt = build_situation(seat7, g6.seats, 1, "NIGHT_GUARD", {"legal_targets": legal})
    last_line = txt.splitlines()[-1]
    check("局势块里写出了可选目标", "可选目标" in txt, last_line)
    check("可选目标那一行和 legal_targets 完全一致",
          all(f"{n}号" in last_line for n in legal) and "7号" not in last_line,
          last_line)

    # 端到端：引擎真的把这份清单塞进了发给模型的消息里
    msgs = g6.relay.messages_by_key.get((7, "NIGHT_GUARD"))
    check("引擎确实把可选目标发给了模型",
          bool(msgs) and "可选目标" in msgs[-1]["content"],
          "没有找到 7 号的 NIGHT_GUARD 调用" if not msgs else "已写入尾部块")

    # --- 7. 每个需要目标的阶段都要给出可选目标（别只修了守卫）---
    g7 = make_game(10)
    g7.run()
    for key in ((4, "NIGHT_SEER"), (5, "NIGHT_WITCH"), (7, "NIGHT_GUARD")):
        msgs = g7.relay.messages_by_key.get(key)
        ok = bool(msgs) and "可选目标" in msgs[-1]["content"]
        check(f"{key[0]}号 {key[1]} 的提示词里有可选目标", ok,
              "没取到调用" if not msgs else ("已写入尾部块" if ok else "尾部块里没有"))


def test_history_block():
    print("\n[6] 尾部块「你的历史立场」")
    game = make_game(5)
    game.state.day = 2
    game.log.append(day=1, phase="DAY_VOTE", channel=Channel.PUBLIC, kind=Kind.SPEECH,
                    actor=1, text="1号 投给 3号", action="vote", target=3)
    game.log.append(day=1, phase="DAY_VOTE", channel=Channel.PUBLIC, kind=Kind.SPEECH,
                    actor=2, text="2号 投给 1号", action="vote", target=1)

    txt = build_history(game.seats[0], game.log)
    check("列出自己投过谁", "第1天→3号" in txt, txt.replace("\n", " / "))
    check("列出谁投过自己", "第1天被 2号投" in txt)
    check("同一份事件流两次渲染一致", build_history(game.seats[0], game.log) == txt)

    msgs, n_shared = build_messages(game.seats[0], game.seats, game.log,
                                    day=2, phase="DAY_SPEECH", task="检查")
    prefix_blob = json.dumps(msgs[:n_shared], ensure_ascii=False)
    check("历史立场不进共享前缀（缓存不受影响）", "你的历史立场" not in prefix_blob)
    check("历史立场在尾部块", "你的历史立场" in msgs[-1]["content"])
    check("尾部块没有泄漏模型名",
          check_no_model_names(game.seats, game.log, day=2, phase="DAY_SPEECH",
                               task="检查")["ok"])
    check("共享前缀仍然逐字节一致",
          check_shared_prefix(game.seats, game.log, day=2, phase="DAY_SPEECH",
                              task="检查")["ok"])


def test_status_callback():
    print("\n[7] 「谁在生成」的回调必须真的被触发（曾经被漏传）")
    seen = []
    script = {(n, "DAY_VOTE"): ("我投2号。", "vote", 2) for n in range(1, 6)}
    game = make_game(5, script)
    game.on_status = seen.append
    game.state.day = 1
    game.day_vote()

    check("每次调模型前都通知了一次", len(seen) == 5, f"实际 {len(seen)} 次")
    check("payload 类型是 thinking",
          bool(seen) and all(s.get("type") == "thinking" for s in seen))
    first = seen[0] if seen else {}
    check("payload 带座位号 / 阶段 / 模型名 / 中文阶段",
          first.get("seat") == 1 and first.get("phase") == "DAY_VOTE"
          and first.get("model") == game.seats[0].model and first.get("phase_cn"))

    # 真正的坑在这里：engine 有回调，但 server.py 没把它传进去，
    # 于是 Web 端在两次发言之间（30~240 秒）收不到任何东西。
    with open(os.path.join(ROOT, "server.py"), encoding="utf-8") as f:
        src = f.read()
    check("server.py 把 on_status 传给了 Game", "on_status=on_status" in src)


def test_summary_writer():
    print("\n[8] summary.json：两条路径必须都写（Web 路径以前压根不写）")
    from aiwolf.summary import build_summary, call_stats

    game = make_game(5, {(n, "DAY_VOTE"): ("我投1号。", "vote", 1) for n in range(1, 6)})
    game.state.day = 1
    game.day_vote()
    game.relay.stats[0]["attempts"] = 3          # 假装有一次靠重试才成功

    s = build_summary(seats=game.seats, relay=game.relay, winner="good", days=3,
                      run_dir="/tmp/x", seed=123, stub=False)
    for key in ("seats", "winner", "days", "role_seed", "roles", "calls",
                "calls_total", "calls_retried", "attempts_total",
                "prompt_tokens_total", "cached_tokens_total", "run_dir"):
        check(f"summary 有 {key}", key in s)
    check("calls_total 对得上", s["calls_total"] == len(game.relay.stats))
    check("calls_retried 数出了重试（failed=0 会掩盖这件事）",
          s["calls_retried"] == 1 and s["attempts_total"] == len(game.relay.stats) + 2)
    check("缓存 token 汇总正确", s["prompt_tokens_total"] == 500 and s["cached_tokens_total"] == 250)
    check("call_stats 在 relay 没有 stats 时也不炸", call_stats(object())["calls_total"] == 0)

    with open(os.path.join(ROOT, "server.py"), encoding="utf-8") as f:
        src = f.read()
    check("server.py（Web 路径）确实写了 summary.json", "write_summary(self.run_dir" in src)
    check("正常结束会落盘", "self._dump(winner)" in src)
    check("被强制结束/异常时也落盘（以前那局什么都不留）",
          '_dump("aborted")' in src and '_dump("error")' in src)
    check("run_text.py（CLI 路径）共用同一个组装函数", "build_summary(" in open(
        os.path.join(ROOT, "run_text.py"), encoding="utf-8").read())


def test_vote_phase_prompt():
    print("\n[9] 投票阶段不能用自曝：提示词必须和引擎一致")
    from aiwolf.render import character_prompt

    day = character_prompt("Werewolf", "DAY_SPEECH")
    vote = character_prompt("Werewolf", "DAY_VOTE")
    pk = character_prompt("Werewolf", "DAY_VOTE_PK")
    check("发言阶段仍然宣传自曝战术", "explode" in day)
    check("投票阶段不再提自曝（以前每个白天阶段都在广告）", "explode" not in vote)
    check("PK 重投阶段也不提自曝", "explode" not in pk)
    check("投票阶段明说了不能自曝", "不能自曝" in vote)

    # 狼的白天提示词原来写着"唯一目标是生存下来"，和「阵营协作」第 3 条
    # （不得为了个人存活损害队友）直接打架 —— 实测狼夹在两个目标之间摇摆：
    # 先死保悍跳队友，看形势不对又反手卖掉，两头都不像。
    check("白天伪装不再说「唯一目标是生存下来」", "唯一目标" not in day)
    check("说清了赢比活着重要、该卖就卖", "赢比活着重要" in day)
    check("卖队友有边界（只为多活一轮害死队友不划算）",
          "只为多活一轮而害死队友是不划算的" in day)

    g = make_game(5, {(1, "DAY_VOTE"): ("我想自曝。", "explode", None)})
    g.state.day = 1
    g.day_vote()
    check("投票阶段写 explode 仍被判非法（记成弃票）",
          any(e.data.get("action") == "vote" and not e.data.get("target") for e in g.log))

    # 非狼人角色在投票阶段拿到的不该是狼人的提示词
    check("好人投票阶段不受影响",
          "自曝" not in character_prompt("Villager", "DAY_VOTE"))


def test_abort_dump():
    print("\n[10] 被「结束」掉的对局也要留下产物（以前那局什么都不写）")
    import shutil
    import threading

    import server as srv                      # noqa: E402  （导入不会起服务）

    game = make_game(5, {(n, "DAY_VOTE"): ("我投1号。", "vote", 1) for n in range(1, 6)})
    game.state.day = 2
    game.day_vote()
    tmp = os.path.join(ROOT, "runs", "_selftest_abort")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    try:
        runner = srv.GameRunner(seats=game.seats, stub=True, max_days=2, api_key="",
                                win_condition="parity", gate=None,
                                cancel=threading.Event(), seed=1, shuffle=False)
        runner.run_dir = tmp
        runner.game = game
        runner.relay = game.relay
        runner.stub = True
        runner._dump("aborted")

        check("被结束时写了 events.jsonl", os.path.exists(os.path.join(tmp, "events.jsonl")))
        sp = os.path.join(tmp, "summary.json")
        check("被结束时写了 summary.json", os.path.exists(sp))
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as f:
                s = json.load(f)
            check("winner 标成 aborted", s.get("winner") == "aborted")
            check("aborted 标记在", s.get("aborted") is True)
            check("半局也要有逐座调用记录", s.get("calls_total") == len(game.relay.stats))
            check("审计仍会跑（拿不到 seat_*.jsonl 时也不能炸）",
                  isinstance(s.get("audit"), dict))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_extra_body():
    print("\n[11] 按座位的额外请求参数（豆包关思维链就靠它）")
    import aiwolf.relay as relay_mod
    from aiwolf.seats import Seat

    captured = []
    real_post = relay_mod.http_post

    def fake_post(url, headers, data, timeout, transport="auto", cancel=None):
        captured.append(json.loads(data.decode("utf-8")))
        return 200, {"choices": [{"message": {"content": '{"speech":"hi","action":{"type":"speak"}}'},
                                  "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 5}}, 0.01, None

    relay_mod.http_post = fake_post
    try:
        cfg = {"relay": {"base_url": "http://x/v1", "api_key_env": "NOPE"},
               "game": {}, "concurrency": {"global": 8}, "fallback_enabled": False}
        rl = relay_mod.Relay(cfg, api_key="k")
        seat = Seat(no=4, label="Doubao", model="ep-20261005143108-gcv24",
                    extra_body={"thinking": {"type": "disabled"}})
        res = rl.chat(seat, [{"role": "user", "content": "hi"}], phase="DAY_SPEECH", day=1)
        check("调用成功", bool(res.text))
        check("thinking=disabled 进了请求体",
              captured and captured[0].get("thinking") == {"type": "disabled"})
        check("没覆盖 model / messages",
              captured[0]["model"] == seat.model and captured[0]["messages"])

        captured.clear()
        plain = Seat(no=5, label="Qwen", model="qwen3.7-max")
        rl.chat(plain, [{"role": "user", "content": "hi"}], phase="DAY_SPEECH", day=1)
        check("没有 extra_body 的座位不多带字段", "thinking" not in captured[0])
    finally:
        relay_mod.http_post = real_post

    # 接入点 id 不能被"模型不在目录里"拦下来（方舟的 /models 不含 ep-*）
    from aiwolf.endpoints import is_deployment_id
    check("ep-* 识别为推理接入点", is_deployment_id("ep-20261005143108-gcv24"))
    check("普通模型名不受影响", not is_deployment_id("glm-5.2"))


def test_truncated_cot_not_parsed():
    print("\n[12] 截断时绝不把「思维链」当答案解析")
    # 真实样本：7 号 GLM 投票时正文为空、推理 16377 tokens，独白里正好在权衡自曝
    cot = ('Let me think. Options: 1. Vote for 1  2. Vote for 6  3. Explode(自曝)\n'
           'I could write {"type": "vote", "target": 6}\n'
           '{"thinking": "maybe", "speech": "我投6号", "action": {"type": "vote", "target": 6}}')
    game = make_game(5, truncated={(1, "DAY_VOTE"): cot})
    game.state.day = 1
    game.day_vote()

    voted6 = [e for e in game.log if e.data.get("action") == "vote" and e.data.get("target") == 6]
    check("没有从思维链里抠出动作（那只是草稿）", voted6 == [])
    check("记成弃票", any("弃票" in (e.text or "") for e in game.log))
    gods = [e for e in game.log if e.channel is Channel.PRIVATE and e.to_seat is None]
    check("留了上帝视角诊断", any(e.data.get("truncated") for e in gods))
    check("诊断里说清了「拒绝把思维链当答案」",
          any("思维链" in (e.text or "") and "拒绝" in (e.text or "") for e in gods))

    # 对照：finish_reason=stop 时**仍然**允许回退到推理字段（网关把答案塞那儿）
    answer = json.dumps({"thinking": "t", "speech": "我是1号",
                         "action": {"type": "vote", "target": 2}, "confidence": 0.7},
                        ensure_ascii=False)
    g2 = make_game(5, reasoning_only={(1, "DAY_VOTE"): answer})
    g2.state.day = 1
    g2.day_vote()
    check("网关风格（finish=stop）仍然能读出动作",
          any(e.data.get("action") == "vote" and e.data.get("target") == 2 for e in g2.log))


def test_truncation_retries_with_more_budget():
    print("\n[13] 正文被截断（非空）也要翻倍重试，不能当成功收下")
    import aiwolf.relay as relay_mod
    from aiwolf.seats import Seat, load_config as _load

    queue = [
        (200, {"choices": [{"message": {"content": '{"thinking":"x","speech":"我是7号，我认为'},
                            "finish_reason": "length"}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 3072}}, 0.1, None),
        (200, {"choices": [{"message": {"content": '{"thinking":"x","speech":"我是7号，我认为1号是狼。",'
                                                  '"action":{"type":"speak","target":null}}'},
                            "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 40}}, 0.1, None),
    ]
    seen = []
    real_post = relay_mod.http_post

    def fake_post(url, headers, data, timeout, transport="auto", cancel=None):
        seen.append(json.loads(data.decode("utf-8")))
        return queue[min(len(seen) - 1, len(queue) - 1)]

    relay_mod.http_post = fake_post
    try:
        cfg = _load(os.path.join(ROOT, "config", "seats.json"))
        rl = relay_mod.Relay(cfg, api_key="k")
        seat = Seat(no=7, label="GLM", model="glm-5.3-flash")
        res = rl.chat(seat, [{"role": "user", "content": "hi"}], phase="DAY_SPEECH", day=1,
                      max_tokens=3072)
        check("确实重试了（不是把半截 JSON 当答案）", len(seen) == 2, f"实际 {len(seen)} 次")
        check("第二次的预算翻倍了", seen[1]["max_tokens"] == 6144, str(seen[1]["max_tokens"]))
        check("拿到的是完整那次的结果",
              "1号是狼" in res.text and res.finish_reason == "stop")
        check("记住了这个座位需要的预算", rl._budget_hint.get(7) == 6144)
    finally:
        relay_mod.http_post = real_post


def test_no_last_words_when_decided():
    print("\n[14] 必胜/必输局不再多问一次遗言（但局没定就必须留）")
    # 5 座：1、2 是狼。夜里女巫毒掉 2 号，白天把 1 号票出去 —— 狼全灭，局已定
    script = {(1, "NIGHT_WOLF"): ("", "kill", 3),
              (2, "NIGHT_WOLF"): ("", "kill", 3),
              (4, "NIGHT_WITCH"): ("", "poison", 2)}
    script.update({(n, "DAY_VOTE"): ("票1号。", "vote", 1) for n in range(1, 6)})
    game = make_game(5, script)
    winner = game.run()
    check("局确实已经定了（狼全灭 → 好人胜）", winner == "good", f"实际 {winner}")
    check("没有调用遗言阶段（省下一次模型调用）",
          (1, "DAY_LAST_WORDS") not in game.relay.calls,
          f"调用过：{game.relay.calls}")
    check("被放逐者仍然被记录成出局", not game.by_no[1].alive)

    # 对照组：票出去一个好人，局没定 —— 遗言必须照留
    game2 = make_game(7, {(n, "DAY_VOTE"): ("票6号。", "vote", 6) for n in range(1, 8)})
    winner2 = game2.run()
    check("局没定时遗言照留", (6, "DAY_LAST_WORDS") in game2.relay.calls,
          f"调用过：{game2.relay.calls[-4:]}")
    check("这局还没结束（继续打）", winner2 in ("good", "wolf", "draw"))


def test_self_memory_blocks():
    """[19] 座位自己的记忆块（2026-10-06 第二轮改动）。

    三件事都是同一类事故的产物：**同一个座位换了人，新上来那个不知道前面在想什么。**
      · 狼队友死活 —— 代打模型把活着的队友当已出局，于是放弃保队友只顾自保；
      · 上一轮的想法 —— 三个模型各读各的盘，一天之内三次改边，被当投机狼票出去；
      · 历史立场 —— 原来只记票型，记不下"我当时为什么那么说"。
    """
    print("\n[19] 自己的记忆块：狼队友死活 / 上一轮想法 / 历史立场原文")
    from aiwolf.events import EventLog
    from aiwolf.render import build_private_info, build_tail_block

    game = make_game(7)                 # 不打乱：1、2 号是狼
    game.state.day = 2
    check("夹具前提：1、2 号确实是狼", game.seats[0].is_wolf and game.seats[1].is_wolf)

    tail_alive = build_tail_block(game.seats[0], game.seats, game.log,
                                  day=2, phase="DAY_SPEECH", state={}, task="t")
    check("狼队友分开写死活", "还在场：" in tail_alive and "已出局：" in tail_alive)
    check("队友活着时写在「还在场」里", "还在场：2号" in tail_alive)
    check("队友活着时不说自己是最后一张狼",
          "最后一张狼" not in build_private_info(game.seats[0], game.log, game.seats, {}))

    game.seats[1].alive = False         # 2 号狼出局
    tail_dead = build_tail_block(game.seats[0], game.seats, game.log,
                                 day=2, phase="DAY_SPEECH", state={}, task="t")
    check("队友出局后移到「已出局」里",
          "还在场：无" in tail_dead and "已出局：2号" in tail_dead)
    check("队友全死时私密信息明确说「最后一张狼」",
          "最后一张狼" in build_private_info(game.seats[0], game.log, game.seats, {}))
    game.seats[1].alive = True

    # --- 上一轮的想法：回灌给**它自己**，并且要抹掉模型名 ---
    # 昵称从当前配置里取，别写死 —— 换模型后夹具会静默失效（历史教训）
    # marker 也得挑一个**主提示词里不会出现**的串：写"倒钩"就会撞上
    # COMMON_RULES 的「阵营协作」（那里本来就列了倒钩战术），断言会假红。
    nick = game.seats[1].label
    marker = "先把水搅浑再反手卖队友"
    state = {"last_thinking": f"我打算{marker}，{nick} 这种对手不会怀疑我"}
    tail = build_tail_block(game.seats[0], game.seats, game.log,
                            day=2, phase="DAY_SPEECH", state=state, task="t")
    check("上一轮的想法被回灌进尾部块", "你上一轮的想法" in tail and marker in tail)
    check("回灌时抹掉模型名（尾部块不许出现模型名）", nick not in tail)
    check("抹掉的位置留了中性占位符", "（某位选手）" in tail)

    msgs, n_shared = build_messages(game.seats[0], game.seats, game.log,
                                    day=2, phase="DAY_SPEECH", task="t", state=state)
    prefix_blob = json.dumps(msgs[:n_shared], ensure_ascii=False)
    check("私密草稿不进共享前缀（缓存与隔离都不受影响）",
          "你上一轮的想法" not in prefix_blob and marker not in prefix_blob)
    check("共享前缀仍然逐字节一致",
          check_shared_prefix(game.seats, game.log, day=2, phase="DAY_SPEECH",
                              task="t", state=state)["ok"])
    check("尾部块里没有模型名（审计要过）",
          check_no_model_names(game.seats, game.log, day=2, phase="DAY_SPEECH",
                               task="t", state=state)["ok"])

    # --- 历史立场：刀口、警长票、自己上一次的公开发言 ---
    log = EventLog()
    log.append(day=1, phase="DAY_SPEECH", channel=Channel.PUBLIC, kind=Kind.SPEECH,
               actor=1, text="1号：我怀疑3号，今天票挂3号，别分票。")
    log.append(day=1, phase="DAY_SHERIFF_VOTE", channel=Channel.PUBLIC, kind=Kind.SPEECH,
               actor=1, text="1号 投给 6号", action="vote", target=6)
    log.append(day=1, phase="DAY_VOTE", channel=Channel.PUBLIC, kind=Kind.SPEECH,
               actor=1, text="1号 投给 3号", action="vote", target=3)
    log.append(day=1, phase="NIGHT_WOLF", channel=Channel.WOLF, kind=Kind.ACTION,
               actor=1, text="主张击杀 6号", action="kill", target=6)

    txt = build_history(game.seats[0], log)
    check("记下自己提过的刀口", "你提过的刀口：第1天→6号" in txt, txt.replace("\n", " / "))
    check("警长票和放逐票分得开", "第1天警长票→6号" in txt and "第1天→3号" in txt)
    said = [l for l in txt.splitlines() if l.startswith("你上一次公开发言的原文")]
    check("把自己的原话摆了进来", bool(said) and "我怀疑3号" in said[0],
          said[0] if said else "没有这一行")
    check("引擎拼的票型记录不会被当成「自己说过的话」",
          bool(said) and "投给" not in said[0], said[0] if said else "没有这一行")
    check("敌人看不到我的狼队刀口记录",
          "你提过的刀口" not in build_history(game.seats[3], log))


def main() -> int:
    print("=" * 74)
    print("玩法规则自测（脚本化 relay，不消耗额度）")
    print("=" * 74)
    test_vote_reason()
    test_wolf_majority()
    test_hunter_shoots_at_dawn()
    test_hunter_no_response_not_a_choice()
    test_pk_vote()
    test_explode()
    test_history_block()
    test_status_callback()
    test_summary_writer()
    test_vote_phase_prompt()
    test_abort_dump()
    test_extra_body()
    test_truncated_cot_not_parsed()
    test_truncation_retries_with_more_budget()
    test_no_last_words_when_decided()
    test_board_notice()
    test_target_legality_is_one_source()
    test_idiot()
    test_sheriff()
    test_self_memory_blocks()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：")
        for f in FAILURES:
            print(f"   · {f}")
        return 1
    print("✅ 玩法规则全部通过（投票理由 / 狼队表决 / 猎人开枪 / PK / 自曝出局 / "
          "板子公告 / 历史立场）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
