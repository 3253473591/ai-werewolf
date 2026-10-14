#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不变量自测 —— 证明审计和前缀检查**不是空检查**。

一个从不失败的检查等于没有检查。这里做三件事：

  1. 正向控制：干净的事件流 + 干净的 prompt  -> 两个检查都必须通过
  2. 反向 1：让共享前缀依赖座位            -> check_shared_prefix 必须失败
  3. 反向 2：往村民的 prompt 里塞狼队内容    -> audit_prompts 必须失败

用法：python tools/selftest_invariants.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import aiwolf.render as render  # noqa: E402
from aiwolf.audit import audit_prompts  # noqa: E402
from aiwolf.engine import Game  # noqa: E402
from aiwolf.events import Channel, EventLog, Kind  # noqa: E402
from aiwolf.render import check_shared_prefix  # noqa: E402
from aiwolf.relay import LLMResult  # noqa: E402
from aiwolf.seats import Seat, build_seats, load_config  # noqa: E402

FAILURES = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "✅" if condition else "❌"
    print(f"  {mark} {name}" + (f"   {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    cfg = load_config(os.path.join(ROOT, "config", "seats.json"))
    seats = build_seats(cfg, 5)

    log = EventLog()
    log.append(day=1, phase="NIGHT_WOLF_CHAT", channel=Channel.WOLF,
               kind=Kind.SPEECH, actor=1, text="今晚刀2号，他发言太稳了")
    log.append(day=1, phase="NIGHT_SEER", channel=Channel.PRIVATE,
               kind=Kind.RESOLUTION, actor=2, to_seat=2, text="你查验了 3号 Claude，结果是好人")
    log.append(day=1, phase="DAY_SPEECH", channel=Channel.PUBLIC,
               kind=Kind.SPEECH, actor=3, text="我是3号，我先听一圈。")

    print("\n[1] 正向控制")
    clean = check_shared_prefix(seats, log, day=1, phase="DAY_SPEECH", task="t")
    check("共享前缀检查在正常输入上通过", clean["ok"], clean.get("mismatch", ""))

    # 夹具目录放仓库里，不用系统临时目录（受限沙箱/CI 里那玩意儿经常不可写，
    # 测试就会因为环境挂掉而不是代码挂掉 —— 那种失败最误导人）
    tmp = os.path.join(ROOT, "runs", "_selftest_invariants")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    try:
        _write_seat(tmp, 5, day=1, phase="DAY_SPEECH", messages=[
            {"role": "system", "content": "公共规则"},
            {"role": "user", "content": "[第1天 · 白天发言] 3号：我是3号，我先听一圈。"},
            {"role": "user", "content": "# Character Prompt\n\n## 你的身份\n村民\n\n"
                                        "## 你的座位号\n5号"},
        ])
        ok_audit = audit_prompts(tmp, log, seats)
        check("隔离审计在干净 prompt 上通过", ok_audit["ok"],
              str(ok_audit["violations"][:1]))

        print("\n[2] 反向：共享前缀依赖座位")
        original = render.render_shared_prefix
        counter = {"n": 0}

        def flaky(log_, seats_):
            counter["n"] += 1
            return original(log_, seats_) + [f"（座位相关噪声 {counter['n']}）"]

        render.render_shared_prefix = flaky
        try:
            broken = check_shared_prefix(seats, log, day=1, phase="DAY_SPEECH", task="t")
        finally:
            render.render_shared_prefix = original
        check("共享前缀检查能抓到座位相关污染", not broken["ok"],
              broken.get("mismatch", "没抓到！"))

        print("\n[3] 反向：村民 prompt 里塞入狼队内容")
        for stale in os.listdir(tmp):
            os.remove(os.path.join(tmp, stale))
        _write_seat(tmp, 4, day=1, phase="DAY_SPEECH", messages=[
            {"role": "system", "content": "公共规则"},
            {"role": "user", "content": "=== 狼队夜间频道 ===\n1号 Gemini：今晚刀2号，他发言太稳了"},
            {"role": "user", "content": "=== 你的身份 ===\n你是 4号「Grok」，身份：村民。"},
        ])
        leaked = audit_prompts(tmp, log, seats)
        check("隔离审计能抓到狼队频道泄漏", not leaked["ok"],
              str(leaked["violations"][:1]))

        print("\n[4] 反向：村民 prompt 里塞入别人的私密信息")
        for stale in os.listdir(tmp):
            os.remove(os.path.join(tmp, stale))
        _write_seat(tmp, 4, day=1, phase="DAY_SPEECH", messages=[
            {"role": "system", "content": "公共规则"},
            {"role": "user", "content": "你查验了 3号 Claude，结果是好人"},
        ])
        leaked2 = audit_prompts(tmp, log, seats)
        check("隔离审计能抓到他人私密信息泄漏", not leaked2["ok"],
              str(leaked2["violations"][:1]))

        print("\n[5] 反向：尾部块泄漏模型名")
        for stale in os.listdir(tmp):
            os.remove(os.path.join(tmp, stale))
        _write_seat(tmp, 5, day=1, phase="DAY_SPEECH", messages=[
            {"role": "system", "content": "公共规则"},
            {"role": "user", "content": "# Character Prompt\n\n## 你的身份\n狼人\n\n"
                                        "## 你的狼队友\n2号、4号（GPT 偏理智，Grok 爱冲锋）"},
        ])
        leaked3 = audit_prompts(tmp, log, seats)
        check("隔离审计能抓到尾部块模型名泄漏", not leaked3["ok"],
              str(leaked3["violations"][:1]))

        print("\n[6] 反向：尾部块泄漏队友昵称")
        for stale in os.listdir(tmp):
            os.remove(os.path.join(tmp, stale))
        # 昵称从**当前配置**里取，别写死 —— 换个模型（Grok→Doubao）测试就失效了，
        # 而失效的方式是"检查不再报错"，看起来像功能坏了，其实是夹具过期。
        nick = next((s.label for s in seats if s.no != 5), "Gemini")
        _write_seat(tmp, 5, day=1, phase="DAY_SPEECH", messages=[
            {"role": "system", "content": "公共规则"},
            {"role": "user", "content": f"# Character Prompt\n\n## 你的狼队友\n{nick}"},
        ])
        leaked4 = audit_prompts(tmp, log, seats)
        check(f"隔离审计能抓到队友只用昵称不用座位号（用的是 {nick}）", not leaked4["ok"],
              str(leaked4["violations"][:1]))

        print("\n[7] 误报控制：泛化文案与「未来的发言」都不算泄漏（2026-10-06 修复的）")
        # 真实事故：10 座局里狼队夜间频道有一条 `本轮未发言`（某只狼整晚没开口），
        # 而**公开频道**第 2/3 天也有三条一模一样的公告。老逻辑把"狼队说过的话"
        # 一律当秘密，于是 5 个好人座位被判了 15 处泄漏，全是假的。
        # 另一重错误：那条狼队发言在第 4 天，而它指控的是第 3 天的 prompt。
        #
        # ⚠️ 这里必须**动态挑一个好人座位**。写死座位号的话，哪天抽卡让它当了狼，
        #    审计就根本不会检查它，三条断言会全部走"跳过"分支 —— 而**跳过也是绿**。
        #    第一版就是这么写的：6 号恰好是狼，四条断言全是摆设。挑不到就判失败。
        good = next((s for s in seats if not s.is_wolf), None)
        check("夹具前提：能找到一个好人座位来测误报（否则这组测试是摆设）",
              good is not None,
              f"用 {good.no}号 {good.label}" if good else "全是狼，这组测试没法跑")
        gno = good.no if good else 1

        log2 = EventLog()
        log2.append(day=2, phase="DAY_SPEECH", channel=Channel.PUBLIC,
                    kind=Kind.SYSTEM, actor=10, text="本轮未发言")
        log2.append(day=4, phase="NIGHT_WOLF_CHAT", channel=Channel.WOLF,
                    kind=Kind.SYSTEM, actor=1, text="本轮未发言")
        log2.append(day=4, phase="NIGHT_WOLF_CHAT", channel=Channel.WOLF,
                    kind=Kind.SPEECH, actor=1, text="四天后才商量好的刀口是6号")

        for stale in os.listdir(tmp):
            os.remove(os.path.join(tmp, stale))
        # 好人座位的 prompt 里合法地含有公开公告「本轮未发言」
        _write_seat(tmp, gno, day=3, phase="DAY_SPEECH", messages=[
            {"role": "system", "content": "公共规则"},
            {"role": "user", "content": "[第2天 · 白天发言] [公告] 本轮未发言"},
        ])
        fp = audit_prompts(tmp, log2, seats)
        check("公开频道也有的泛化文案不构成泄漏证据", fp["ok"],
              str(fp["violations"][:2]))

        for stale in os.listdir(tmp):
            os.remove(os.path.join(tmp, stale))
        # 第 3 天的 prompt 不可能包含第 4 天才说出口的话
        _write_seat(tmp, gno, day=3, phase="DAY_SPEECH", messages=[
            {"role": "system", "content": "公共规则"},
            {"role": "user", "content": "四天后才商量好的刀口是6号"},
        ])
        future = audit_prompts(tmp, log2, seats)
        check("后来才发生的狼队发言不会指控先前的 prompt", future["ok"],
              str(future["violations"][:2]))

        print("\n[8] 修完之后必须**仍然抓得住**真泄漏（别把守卫修成摆设）")
        for stale in os.listdir(tmp):
            os.remove(os.path.join(tmp, stale))
        # 独属于狼队的一句（公开频道没有过），且发生在 prompt 之前 —— 必须抓到
        _write_seat(tmp, gno, day=4, phase="DAY_VOTE", messages=[
            {"role": "system", "content": "公共规则"},
            {"role": "user", "content": "今晚刀2号，他发言太稳了"},
        ])
        still = audit_prompts(tmp, log, seats)
        check("独属于狼队的台词仍然会被抓到", not still["ok"],
              str(still["violations"][:1]))
        # 狼队频道标题本身也仍然要抓
        for stale in os.listdir(tmp):
            os.remove(os.path.join(tmp, stale))
        _write_seat(tmp, gno, day=1, phase="DAY_SPEECH", messages=[
            {"role": "system", "content": "公共规则"},
            {"role": "user", "content": "=== 狼队夜间频道 ===\n1号：随便说点什么"},
        ])
        still2 = audit_prompts(tmp, log, seats)
        check("狼队频道标题仍然会被抓到", not still2["ok"],
              str(still2["violations"][:1]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    test_real_fault_isolation()
    test_dawn_does_not_leak_wolf_target()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：{FAILURES}")
        return 1
    print("✅ 全部通过：两个守卫都是有效的，不是空检查")
    return 0


# --------------------------------------------------------------------------- #
# 真故障隔离 —— 元层面放开后的新边界（2026-10-06）
# --------------------------------------------------------------------------- #
#
# 背景：狼人的角色提示词（render.WOLF_DAY）**允许狼自己公开伪造系统故障**
# （"我的上下文断了"、"我幻觉了"），那是合法博弈手段，因为不可证伪。
# 但这条策略成立的前提是：**真故障的报错原文绝不能进公开频道。**
# 一旦玩家手里拿到了真实的错误文本，元层面这张牌就变成可证伪的信息 ——
# 谁真掉线、谁在演，当场就能对上号，整张牌作废。
#
# 所以这里钉死三条：真故障只进上帝账本（带 real_fault 标签）、
# 公开频道只有中性措辞、任何座位的 prompt 里都不出现错误原文。

FAULT_MARKER = "invalid temperature: only 1 is allowed for this model"


class _FailingRelay:
    """所有调用一律失败，错误文本就是真实的 Kimi 400 原文。"""

    def __init__(self) -> None:
        self.calls = 0
        self.stats = []
        self.cancel_event = None

    def chat(self, seat, messages, *, phase, day, ctx=None,
             max_tokens=1024, **kw) -> LLMResult:
        self.calls += 1
        self.stats.append({
            "seat": seat.no, "label": seat.label, "phase": phase,
            "model": seat.model, "substituted": False, "latency_s": 0.0,
            "prompt_tokens": 100, "cached_tokens": 0,
            "truncated": False, "error": FAULT_MARKER, "attempts": 2,
        })
        return LLMResult(text="", reasoning="", error=FAULT_MARKER,
                         models_tried=[seat.model], attempts=2,
                         model_used="", finish_reason="")


def _seats_with_roles(roles) -> list:
    return [Seat(no=i + 1, label=f"座位{i + 1}", model=f"fake-{i + 1}", role=r)
            for i, r in enumerate(roles)]


def test_real_fault_isolation() -> None:
    print("\n[9] 真故障隔离（元层面放开后必须守住的边界）")

    # 5 人局：狼 / 预言家 / 3 村民。全部调用都失败。
    game = Game(_seats_with_roles(["Werewolf", "Seer", "Villager",
                                   "Villager", "Villager"]),
                _FailingRelay(), max_days=1, verbose=False, sheriff=False)
    game.night()
    night_public = [e for e in game.log if e.channel is Channel.PUBLIC]
    check("夜间失败不产生任何公开事件（狼频道/私密不外泄）", not night_public,
          f"漏了 {len(night_public)} 条")

    game.day_speeches()          # 白天失败：公开频道这时候才有东西

    evs = list(game.log)
    pub = [e for e in evs if e.channel is Channel.PUBLIC]
    god = [e for e in evs if e.data.get("god_only")]

    leaked = [e for e in pub if FAULT_MARKER in (e.text or "")]
    check("真故障的错误原文不进任何公开事件", not leaked,
          f"漏了 {len(leaked)} 条")

    check("真故障留下了上帝视角记录（有据可查）", bool(god),
          f"god_event={len(god)} 条")
    check("上帝视角那条带 real_fault 标签（能和伪造的故障分开统计）",
          any(e.data.get("real_fault") for e in god))

    # 这是整条边界的关键：错误原文在事件流里可以存在，但**只以 god_only 的形式存在**
    fault_events = [e for e in evs if FAULT_MARKER in (e.text or "")]
    check("错误原文只出现在 god_only 事件里",
          bool(fault_events) and all(e.data.get("god_only") for e in fault_events),
          f"共 {len(fault_events)} 条")

    # 玩家侧拿到的是中性措辞（白天发言失败 -> 公开频道）
    neutrals = [e for e in pub if "本轮未发言" in (e.text or "")]
    check("玩家侧只看到中性措辞「本轮未发言」", bool(neutrals),
          f"{len(neutrals)} 条")

    # 中性措辞也不能是拼接残渣（实测踩过："主张击杀 " + "（未定）" -> 中间多一个空格）
    import re
    malformed = [e.text for e in evs
                 if re.search(r"[\u4e00-\u9fff] +[（(]|[（(] +[\u4e00-\u9fff]", e.text or "")]
    check("公告文案没有中英文拼接残渣（如「主张击杀 （未定）」）", not malformed,
          str(malformed[:2]))

    # 逐座位重建 prompt，确认任何一条都拿不到错误原文
    bad = []
    for seat in game.seats:
        msgs, n_shared = render.build_messages(
            seat, game.seats, game.log,
            day=game.state.day, phase="DAY_SPEECH", task="t", state={})
        blob = "\n".join(m["content"] for m in msgs)
        if FAULT_MARKER in blob or "real_fault" in blob:
            bad.append(seat.no)
    check("任何座位的 prompt 里都没有错误原文 / real_fault 字段", not bad,
          f"污染座位：{bad}")

    # 公共前缀必须字节级相同 —— 隔离不能靠"每个座位渲染时各滤一遍"实现
    prefix = render.render_shared_prefix(game.log, game.seats)
    check("公共前缀不含错误原文", not any(FAULT_MARKER in line for line in prefix))

    # 公开事件里不许出现任何"像后台报错"的东西（错误码 / 栈 / 网关措辞）
    suspicious = [e.text for e in pub
                  if re.search(r"HTTP\s*\d{3}|traceback|Exception|"
                               r"timed out|连接超时|curl\s*\(", e.text or "", re.I)]
    check("公开频道不出现错误码/栈/网关措辞", not suspicious, str(suspicious[:2]))


def _write_seat(directory: str, no: int, **record) -> None:
    """覆盖写（不是追加）—— 每个反向用例必须是隔离的，否则会串味。"""
    path = os.path.join(directory, f"seat_{no}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# 刀口不外泄 —— 天亮公告只说平安夜（2026-10-06）
# --------------------------------------------------------------------------- #
#
# 背景：`resolve_night` 原来往 **PUBLIC** 频道播一条「X号 昨晚被救下了」，
# 等于把当夜的刀口公告给全场。真实规则里天亮只有两个公开信息：
# 「平安夜」和「昨晚死亡的是谁」。刀口只属于被刀者、女巫、狼队。
#
# 这不是洁癖，代价实测过：10 座局第一天，5/9/12 号都拿
# "3号怎么会在6号报银水之前就知道刀口在6"当锁死 3 号的硬伤，
# 最后成了放逐真预言家的理由之一 —— 而那条"越权信息"**本来就是引擎自己播的**。
# 整轮推理预算烧在一句不该存在的公告上。
#
# 所以钉死三条：公开频道不出现刀口、上帝账本留得下、女巫自己知道救没救中。


class _ScriptedRelay:
    """按 (座位号, 阶段) 给死答案，只为驱动夜晚流程。不联网、不花额度。"""

    def __init__(self, script) -> None:
        self.script = script
        self.stats = []
        self.cancel_event = None

    def chat(self, seat, messages, *, phase, day, ctx=None, max_tokens=1024,
             **kw) -> LLMResult:
        act, target = self.script.get((seat.no, phase), ("pass", None))
        return LLMResult(
            text=json.dumps({"thinking": "（脚本答案）", "speech": "",
                             "action": {"type": act, "target": target},
                             "confidence": 0.9}, ensure_ascii=False),
            model_used="script")


def test_dawn_does_not_leak_wolf_target() -> None:
    print("\n[10] 天亮公告不泄漏刀口（公开频道只有「平安夜」）")

    # 5 人板：狼 / 狼 / 预言家 / 女巫 / 村民。狼刀 5 号，女巫用解药救 5 号。
    seats = _seats_with_roles(["Werewolf", "Werewolf", "Seer", "Witch", "Villager"])
    game = Game(seats, _ScriptedRelay({
        (1, "NIGHT_WOLF"): ("kill", 5),
        (2, "NIGHT_WOLF"): ("kill", 5),
        (3, "NIGHT_SEER"): ("check", 1),
        (4, "NIGHT_WITCH"): ("save", None),
    }), max_days=1, verbose=False, sheriff=False)

    game.night()
    deaths = game.resolve_night()

    check("夹具前提：女巫救药真的生效、当晚无人死亡",
          deaths == [] and game.state.saved_target == 5
          and game.state.saved_by == "witch",
          f"deaths={deaths} saved={game.state.saved_target}/{game.state.saved_by}")

    pub = [e for e in game.log if e.channel is Channel.PUBLIC]
    check("整夜没有产生任何公开事件（刀口只走狼队频道 / 上帝账本）",
          not pub, str([e.text for e in pub][:2]))
    check("公开频道没有「被救下了」这种字眼",
          not any("被救" in (e.text or "") for e in pub))

    god = [e for e in game.log if e.data.get("god_only")]
    check("上帝账本把刀口和救药都记下来了（复盘查得到）",
          any(e.data.get("saved_target") == 5 for e in god),
          f"共 {len(god)} 条上帝事件")

    # 逐座位重建 prompt：只有女巫知道救没救中，连被救的那个人自己都不知道。
    leaks = []
    for seat in game.seats:
        msgs, _ = render.build_messages(
            seat, game.seats, game.log, day=1, phase="DAY_SPEECH", task="t",
            state=game._state_dict(seat))
        blob = "\n".join(
            m["content"] if isinstance(m["content"], str)
            else json.dumps(m["content"], ensure_ascii=False) for m in msgs)
        if seat.role == "Witch":
            check("女巫自己的私密信息里写着救药生效（否则她不知道该不该跳）",
                  "救下了 5号" in blob)
        elif "救下了" in blob:
            leaks.append(f"{seat.no}号 {seat.role_cn}")
    check("除了女巫，任何座位都看不到「谁被救了」（连被救者本人也没有）",
          not leaks, f"污染座位：{leaks}")


if __name__ == "__main__":
    sys.exit(main())
