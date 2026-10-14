"""游戏循环 —— 把事件流、渲染器、relay 和规则机串起来。

对应 docs/DESIGN.md 第 5 节。这里刻意保持同步、单线程的顺序执行：
白天发言必须串行（后发言者要看到前面的话），夜晚的并行留到 M2。

事件通道约定：
  PUBLIC           大桌，所有人可见
  WOLF             小桌，仅存活狼人可见
  PRIVATE(to_seat) 定向私密（查验结果、女巫用药、守卫守护）
  PRIVATE(None)    仅上帝可见的内部异常（解析失败、调用失败），任何玩家都看不到
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .events import Channel, EventLog, Kind
from .protocol import Decision, describe_action, parse_decision
from .relay import Relay
from .render import apply_cache_breakpoint, assert_deterministic, build_messages, phase_cn
from .seats import Seat, good as good_seats, wolves as wolf_seats
from .visibility import public_events, wolf_channel

TASKS = {
    "NIGHT_WOLF_CHAT": "现在是狼队夜间频道，只有你和狼队友能看到。商量今晚刀谁，说一句话。"
                       "今晚的刀口由狼队多数表决决定，所以你要说服队友，而不是抢最后发言。",
    "NIGHT_WOLF": "现在是狼人行动阶段。选择今晚要击杀的目标（狼队按多数表决汇总）。",
    "NIGHT_SEER": "现在是预言家查验阶段。选择今晚要查验的目标。",
    "NIGHT_WITCH": "现在是女巫用药阶段。救人用 save，毒人用 poison 并把 target 填成座位号，不用药就 pass。",
    "NIGHT_GUARD": "现在是守卫守护阶段。选择今晚要守护的目标，不能连续两晚守护同一人。",
    "DAY_SPEECH": "现在是白天发言阶段。公开发言，表明立场、分析或指认。"
                  "如果你是狼人且局势合适，可以在发言里直接自曝（action.type 填 explode）。"
                  "代价要说清楚：**自曝即出局** —— 你会立刻离场，当天不再投票、直接进黑夜，"
                  "换来的只是「跳过这一轮投票」和提振队友士气。只有在你判断自己"
                  "横竖要被投出去、或者必须打断好人的节奏时才划算。",
    "DAY_VOTE": "现在是投票阶段。**先用一句话说明你的投票理由（会公开进大桌，后置位都会看到），"
                "再给出你要放逐的对象。**",
    "DAY_PK_SPEECH": "你和另一位玩家平票，现在进行 PK 发言。为自己辩护，并说服场下的人"
                     "把票投给你的对手。你之后没有机会再补充。",
    "DAY_VOTE_PK": "现在是 PK 重投。你只能在 PK 台上的那几个人里选择放逐对象"
                   "（你自己不能投票，也不能投给台外的人）。",
    "DAY_LAST_WORDS": "你被放逐了。说一句遗言，然后就要离场。",
    "DAY_HUNTER": "你可以开枪带走一名玩家（shoot），也可以选择不开枪（pass）。",
    # ---- 警长 ----
    "DAY_SHERIFF_RUN": "现在是警长竞选（上警）。如果你要竞选警长，"
                       "action.type 填 run，并在发言里说清你为什么值得当警长"
                       "（你是好人吗？你打算怎么带队？）。不想竞选就填 pass，"
                       "简短说一句理由即可。**上警的人不能投警长票。**",
    "DAY_SHERIFF_VOTE": "现在给警长候选人投票。你只能在候选人里选一个（action.type 填 vote，"
                        "target 填座位号），并在发言里说明为什么。你**没有上警**，所以有这一票。",
    "DAY_SHERIFF_PK": "警长竞选平票，你们并列第一。现在做 PK 发言，"
                      "说服没上警的人把票投给你。你之后没有机会再补充。",
    "DAY_SHERIFF_ORDER": "你是警长，现在决定今天的发言顺序：从警左（order_left）"
                         "还是从警右（order_right）开始。按惯例**你自己最后发言**，"
                         "也就是你的归票位 —— 这是警长最大的权力，别浪费。",
    "DAY_BADGE": "你出局了，现在处理警徽：action.type 填 badge、target 填你要移交的"
                 "存活玩家，或者填 pass 表示**撕掉警徽**（本局不再有警长）。"
                 "移交前请想清楚把警徽给谁对你自己阵营最有利。",
}

# 需要"指定一个目标"的阶段。这些阶段的提示词里必须带上**可选目标**，
# 而且那份清单和引擎校验取自同一个 `legal_targets()` —— 不许各写一套。
TARGETED_PHASES = ("NIGHT_GUARD", "NIGHT_SEER", "NIGHT_WITCH", "NIGHT_WOLF", "DAY_HUNTER")

# 神职：狼人杀光其中任意一边（神职全灭或平民全灭）即获胜 —— 即"屠边"
GOD_ROLES = {"Seer", "Witch", "Hunter", "Guard", "Idiot"}


@dataclass
class GameState:
    day: int = 0
    seer_checks: Dict[int, bool] = field(default_factory=dict)
    witch_heal_used: bool = False
    witch_poison_used: bool = False
    witch_save_target: Optional[int] = None   # ★ 每夜重置，不是粘性标志
    last_guard_target: Optional[int] = None
    wolf_target: Optional[int] = None
    poison_target: Optional[int] = None
    guard_target: Optional[int] = None
    # 今晚**真的被救活**的人，以及是谁救的（"witch" / "guard"）。每夜重置。
    # ★ 2026-10-06 新增。以前这件事是往公开频道播一条"X号 昨晚被救下了"，
    #   等于把当夜刀口公告给全场（见 resolve_night 的注释）。现在只进上帝账本，
    #   女巫从自己的私密信息里看到结果。
    saved_target: Optional[int] = None
    saved_by: Optional[str] = None
    # 每个座位上一轮的 thinking（私密草稿）。只回灌给**它自己**的尾部块。
    # 起因：同一个座位一天之内换三次代打模型，三个模型各自独立读盘、立场打架，
    # 全场当它是投机狼票了出去。私密草稿是唯一能跨代打传递"我本来想干什么"的东西。
    last_thinking: Dict[int, str] = field(default_factory=dict)
    # 白痴是否已经翻牌。翻牌后它**还在场上、还能发言，但不能再投票**。
    idiot_revealed: bool = False
    # 警长（座位号）。None = 本局没有警长 / 警徽已流失。
    sheriff: Optional[int] = None
    # 警徽是否已经被撕掉（警长死在夜里且不指定继承人时）
    badge_destroyed: bool = False
    # 警长今天选的发言方向："left"（警左，座位号递增环绕）/"right"（警右，递减环绕）
    sheriff_direction: Optional[str] = None


class Game:
    def __init__(self, seats: List[Seat], relay: Relay, *, max_days: int = 6,
                 verbose: bool = True, win_condition: str = "parity",
                 on_status=None, on_finish=None, gate=None,
                 seed: Optional[int] = None, rotate_speaker_start: bool = True,
                 sheriff: bool = True) -> None:
        self.seats = seats
        self.by_no: Dict[int, Seat] = {s.no: s for s in seats}
        self.log = EventLog()
        self.state = GameState()
        self.relay = relay
        self.max_days = max_days
        self.verbose = verbose
        self.win_condition = win_condition
        self.on_status = on_status      # 回调：某座位开始思考，供 Web 端显示
        self.on_finish = on_finish      # 回调：对局结束
        self.gate = gate                # 闸门：强制暂停 / 单步，见 aiwolf/gate.py
        # 每局随机决定"今天从谁开始发言"。否则永远从 1 号起，
        # 座位号小的模型就永远吃首置位的亏（信息最少、最容易被打）。
        self.rng = random.Random(seed if seed is not None else 0)
        self.rotate_speaker_start = rotate_speaker_start
        # 警长是否上线。开着的话首日会走一轮完整竞选（N 次调用），
        # 所以给个开关：小局/单测可以关掉，省额度也省得测偏。
        self.sheriff_on = sheriff
        self.day_start_seat: Optional[int] = None
        # 开局快照，用于屠边判定（见 check_win）
        self.setup_gods = [s.no for s in seats if s.role in GOD_ROLES]
        self.setup_villagers = [s.no for s in seats if s.role == "Villager"]

    # ---------------- 工具 ----------------

    def say(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    def alive(self) -> List[Seat]:
        return [s for s in self.seats if s.alive]

    def alive_nos(self) -> List[int]:
        return [s.no for s in self.alive()]

    def targets_for(self, seat: Seat, exclude_self: bool = True,
                    exclude: Optional[set] = None) -> List[int]:
        bad = set(exclude or ())
        return [n for n in self.alive_nos()
                if n not in bad and not (exclude_self and n == seat.no)]

    def legal_targets(self, seat: Seat, phase: str) -> List[int]:
        """这个座位在这个阶段**真正可选**的目标 —— 唯一的合法性事实源。

        ★ 2026-10-06：以前"引擎校验"和"提示词描述"是两套东西，于是两头都对不上：
          · 引擎强制 `exclude_self`（不能选自己），**提示词一个字没提**，
            而 `build_situation` 还把守卫自己列在"存活"里 —— 模型选自己是完全
            合理的推断，然后被判非法且不解释（实测守卫自守就是这么来的）；
          · 反过来，`build_private_info` 跟守卫说"今晚不能连续守护同一人"，
            但校验里**根本没排除上一晚的目标，这条规则压根没实现**。
        现在提示词里的"可选目标"和校验都用这一个函数算，不可能再分叉。
        """
        if phase == "NIGHT_GUARD":
            last = self.state.last_guard_target
            # 不能连守同一人（这条以前只写在提示词里，没实现）
            return self.targets_for(seat, exclude={last} if last is not None else None)
        return self.targets_for(seat)

    def _resolve_target(self, seat: Seat, decision: Decision, *, phase: str
                        ) -> Tuple[Optional[int], str]:
        """把模型给的目标过一遍合法性，返回 (有效目标, 备注)。

        合法 -> (target, "")；**故意不行动** -> (None, "")；不合法 -> (None, 说明)。

        为什么要收成一个函数：同一个"目标不合法"，五条路径以前是五种行为 ——
          · 守卫：报一条上帝诊断，但私密动作里照样写"选择守护3号"（自相矛盾）
          · 预言家：报诊断然后**什么都不记**
          · 女巫：静默当成"没用药"（连诊断都没有）
          · 狼人：静默变成"（未定）"
          · 猎人：静默变成"不开枪"
        其中**静默吞掉动作最危险**：玩家以为自己做了事、日志里却没有，
        事后分不清是模型没说清还是引擎丢的。这和以前"击杀被降级成 pass、
        狼刀凭空消失"是同一类事故。
        """
        if decision.action.type == "pass" or decision.action.target is None:
            return None, ""                      # 主动不行动，不是错误，不报警
        target = decision.action.target
        if target in self.legal_targets(seat, phase):
            return target, ""
        if target == seat.no:
            why = "不能选自己"
        elif target not in self.alive_nos():
            why = "已出局"
        else:
            why = "本阶段不可选（如上一晚已守护过）"
        note = (f"{seat.display} 的 {phase_cn(phase)} 想选 {target}号，"
                f"但不合法（{why}），已作废")
        self._god(note, seat=seat.no, issues=["invalid_target"],
                  requested=target, reason=why)
        return None, note

    def speaking_order(self) -> List[Seat]:
        """今天的发言/投票顺序。

        没有警长时：从 `day_start_seat` 起顺时针（固定从 1 号开始会让座位号小的
        模型永远吃首置位的亏）。

        **有警长时改用警长指定的方向**（2026-10-06）：警左 = 座位号递增环绕，
        警右 = 递减环绕；两种方向都**绕一圈最后回到警长自己** —— 这正是标准规则里
        「警长最后发言（归票位）」的实现方式，不用额外特判。
        """
        alive = self.alive()
        if len(alive) < 2:
            return alive
        nos = [s.no for s in alive]

        sheriff = self.state.sheriff
        direction = self.state.sheriff_direction
        if sheriff in nos and direction in ("left", "right"):
            i = nos.index(sheriff)
            n = len(nos)
            step = 1 if direction == "left" else -1
            seq = [nos[(i + step * k) % n] for k in range(1, n + 1)]
            return [self.by_no[x] for x in seq]

        if (not self.rotate_speaker_start or self.day_start_seat is None):
            return alive
        if self.day_start_seat not in nos:
            return alive
        i = nos.index(self.day_start_seat)
        return [self.by_no[n] for n in (nos[i:] + nos[:i])]

    def roll_speaker_start(self) -> None:
        alive = self.alive_nos()
        if not alive or not self.rotate_speaker_start:
            self.day_start_seat = alive[0] if alive else None
            return
        self.day_start_seat = self.rng.choice(alive)

    def _state_dict(self, seat: Seat) -> dict:
        return {
            "seer_checks": dict(self.state.seer_checks),
            "witch_heal_used": self.state.witch_heal_used,
            "witch_poison_used": self.state.witch_poison_used,
            "last_guard_target": self.state.last_guard_target,
            "wolf_target": self.state.wolf_target,
            "hunter_can_shoot": True,
            # 白痴翻牌后失去投票权；警长有 1.5 票。两者都必须让模型自己知道。
            "idiot_revealed": self.state.idiot_revealed,
            "sheriff": self.state.sheriff,
            "badge_destroyed": self.state.badge_destroyed,
            # 救药生效只对女巫可见（公开频道只有"平安夜/死讯"）。
            "saved_target": self.state.saved_target,
            "saved_by": self.state.saved_by,
            # 这个座位自己上一轮的私密草稿（按座位取，所以这不是共享前缀的一部分）。
            "last_thinking": self.state.last_thinking.get(seat.no, ""),
        }

    def _god(self, text: str, **data) -> None:
        """仅上帝可见的内部记录，任何玩家都看不到。"""
        self.log.append(day=self.state.day, phase=self.state.phase,
                        channel=Channel.PRIVATE, kind=Kind.SYSTEM, to_seat=None,
                        text=text, god_only=True, **data)

    def _ask(self, seat: Seat, phase: str, *, max_tokens: int = 2048) -> Decision:
        assert_deterministic(self.log, self.seats)
        state = self._state_dict(seat)
        if phase in TARGETED_PHASES:
            # 提示词里的"可选目标"= 引擎真正会接受的集合。同源，不可能分叉。
            state["legal_targets"] = self.legal_targets(seat, phase)
        messages, n_shared = build_messages(
            seat, self.seats, self.log,
            day=self.state.day, phase=phase,
            task=TASKS.get(phase, "请行动。"),
            state=state,
            sheriff=self.sheriff_on,
        )
        messages = apply_cache_breakpoint(messages, n_shared, seat.cache_style)

        # 闸门：被暂停时阻塞在这里，不发请求、不烧额度
        if self.gate:
            self.gate.wait(label=f"第{self.state.day}天 · {phase_cn(phase)} · "
                                 f"{seat.no}号 {seat.label}")

        if self.on_status:
            # model / endpoint 只用于观战界面显示（上帝视角的 UI 本来就显示模型名），
            # 不会进任何 prompt —— 玩家侧永远只以座位号指代。
            self.on_status({"type": "thinking", "seat": seat.no, "label": seat.label,
                            "role": seat.role_cn, "phase": phase,
                            "phase_cn": phase_cn(phase), "day": self.state.day,
                            "model": seat.model, "endpoint": seat.endpoint})

        result = self.relay.chat(
            seat, messages, phase=phase, day=self.state.day,
            # stub 也拿同一份可选目标（否则离线对局又会和真局行为不一致）
            ctx={"targets": state.get("legal_targets") or self.targets_for(seat)},
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        if result.error and not (result.text or result.reasoning):
            tried = result.models_tried or [seat.model]
            # ★ 真故障的账本记录（上帝可见）。**两侧的边界就在这里**：
            #   · 这一条只进 Channel.PRIVATE + god_only，任何座位的 prompt 里都不会出现；
            #   · 玩家侧只拿到下面 _append_speech 的中性 `本轮未发言`。
            # 为什么必须这么分：狼人的动态角色提示词（render.WOLF_DAY / WOLF_VOTE 的
            # 「元层面话术」一节）**允许狼自己公开伪造系统故障**（"我的上下文断了"、
            # "我幻觉了"），那是合法博弈手段；
            # 但真故障的报错原文一旦进公开频道，就变成"可证伪"的信息 ——
            # 别人拿它一对，就知道谁真掉线、谁在演，元层面这张牌当场作废。
            # `real_fault=True` 是给上帝视角/复盘用的标签，便于把"伪造的故障"
            # 和"真发生的故障"分开统计；它对玩家不可见，也不参与任何规则判定。
            self._god(f"{seat.display} 的调用全部失败（已试 {len(tried)} 个模型："
                      f"{'、'.join(tried)}）：{result.error[:200]}",
                      failed_seat=seat.no, models_tried=tried, attempts=result.attempts,
                      real_fault=True)
            return Decision(issues=["call_failed"])

        # ★ 代打要看得见，但**不能让玩家看见**。
        # 写进大桌就等于告诉所有人"4 号换了张牌"，正好踩了 DESIGN §3.3.1
        # （绝不以模型身份指代座位）。所以记成上帝视角事件：行动日志里看得到，
        # 任何座位的 prompt 里都不会出现。summary 里也有 calls_substituted 计数。
        if result.substituted and result.model_used:
            why = f"，主模型失败原因：{result.primary_error[:160]}" if result.primary_error else ""
            self._god(f"{seat.display} 主模型 {seat.model} 失败，改由 {result.model_used} 代打"
                      f"（第{self.state.day}天 {phase_cn(phase)}，共尝试 {result.attempts} 次{why}）",
                      seat=seat.no, substituted_by=result.model_used,
                      requested_model=seat.model, attempts=result.attempts,
                      primary_error=result.primary_error[:300])

        # ★ 正文为空时**不能无条件**回退到推理字段。
        #
        # 回退本来是给"网关把答案塞在 reasoning 里"准备的（那种 finish_reason=stop）。
        # 但被 max_tokens 截断时也是正文空、推理一大堆 —— 那是**半截思维链**，
        # 不是答案。拿它去跑解析降级链，抢救逻辑会从模型的内心独白里抠出一个动作：
        # 实测 7号 GLM 投票时正文为空（16377/16384 花在思维链上），它的独白里正好
        # 在权衡"3. Explode(自曝)"，于是引擎"读出"了它写 explode —— 那不是它的决定，
        # 是它的草稿。更糟的情况：草稿里写 "vote 6"，那一票就真的投出去了，
        # 而且没有任何地方会显示出异常。
        truncated_no_answer = (not result.text.strip()) and result.truncated
        source = result.text
        if not source.strip() and not result.truncated and result.reasoning.strip():
            source = result.reasoning            # 网关风格：答案就在推理字段里

        decision = parse_decision(source, phase)

        if truncated_no_answer:
            decision.issues.append("truncated_no_answer")
            self._god(
                f"{seat.display} 在第{self.state.day}天 {phase_cn(phase)} 的输出被 max_tokens "
                f"截断：正文为空、推理 {len(result.reasoning)} 字。"
                f"**拒绝把思维链当答案解析**，本回合按失败处理（不产生任何动作）",
                seat=seat.no, issues=decision.issues, truncated=True,
                reasoning_chars=len(result.reasoning), finish_reason=result.finish_reason)
        elif "call_failed" in decision.issues or "unparsed" in decision.issues:
            self._god(
                f"{seat.display} 在第{self.state.day}天 {phase_cn(phase)} 的动作解析失败，已兜底"
                + ("（输出被 max_tokens 截断）" if result.truncated else ""),
                seat=seat.no, issues=decision.issues,
                truncated=result.truncated, finish_reason=result.finish_reason,
                raw=(result.text or "")[:400])
        elif "json_salvaged" in decision.issues:
            # ★ 2026-10-11：这里原来写死了"（多半是值里有未转义的引号）"，是个
            #   `if truncated else` 的猜测，而它**猜错过**：8号 MiniMax 真正的原因
            #   是"整段输出前面挂了一段 <think> 思维链"，答案 JSON 本身完全合法。
            #   那条假线索把排查方向直接带偏了。现在宁可说得笼统，也不下没有依据的断言。
            self._god(
                f"{seat.display} 在第{self.state.day}天 {phase_cn(phase)} 的输出不是合法 JSON"
                + ("（被 max_tokens 截断）" if result.truncated
                   else "（JSON 不完整，或正文里有未转义的引号）")
                + "，已按字段抢救",
                seat=seat.no, issues=decision.issues, finish_reason=result.finish_reason)
        # ★ 私密草稿留一份给**下一轮**（2026-10-06），只回灌给这个座位自己。
        #   注意是"不是空的才覆盖"：调用失败、或者模型这轮压根没写 thinking 时，
        #   上一轮的想法还留着，比清空更有用。
        if decision.thinking.strip():
            self.state.last_thinking[seat.no] = decision.thinking
        return decision

    def _append_speech(self, seat: Seat, decision: Decision, channel: Channel, phase: str) -> None:
        if decision.speech:
            self.log.append(day=self.state.day, phase=phase, channel=channel,
                            kind=Kind.SPEECH, actor=seat.no, text=decision.speech)
            return

        # ★ 没说出话也必须留痕。否则大桌上就是"凭空少了一个人"——
        # 其他模型不知道该座位是策略性沉默还是调用失败，看页面的人更是只看到跳号。
        # 用中性的"未发言"措辞，不向玩家暴露基础设施故障。
        if phase == "DAY_LAST_WORDS":
            text = "没有留下遗言"
        elif phase == "DAY_SPEECH":
            text = "本轮未发言"
        else:
            text = "本轮未发言"
        self.log.append(day=self.state.day, phase=phase, channel=channel,
                        kind=Kind.SYSTEM, actor=seat.no, text=text,
                        silent=True, issues=list(decision.issues))

    # ---------------- 胜负 ----------------

    def check_win(self) -> Optional[str]:
        """胜负判定。

        parity（默认）：存活好人数量 ≤ 存活狼人数量 → 狼胜；狼全灭 → 好人胜。
        side（屠边）  ：狼人杀光所有神职 或 所有平民 → 狼胜。

        屠边会让局数变长、烧更多 token，所以默认走 parity。
        """
        wolves = wolf_seats(self.seats)
        good = good_seats(self.seats)
        if not wolves:
            return "good"
        if not good:
            return "wolf"

        if self.win_condition == "parity":
            return "wolf" if len(wolves) >= len(good) else None

        # 屠边。用开局快照判断这一边本来是否存在，
        # 否则一局里没有神职就会被立刻判狼胜。
        gods = [self.by_no[n] for n in self.setup_gods]
        if gods and not any(s.alive for s in gods):
            return "wolf"
        villagers = [self.by_no[n] for n in self.setup_villagers]
        if villagers and not any(s.alive for s in villagers):
            return "wolf"
        return None

    def kill(self, seat_no: int, cause: str) -> None:
        if seat_no in self.by_no:
            self.by_no[seat_no].alive = False
            self.by_no[seat_no].death_cause = cause

    # ---------------- 白痴 ----------------

    def idiot_revealed(self) -> bool:
        return self.state.idiot_revealed

    def voters(self) -> List[Seat]:
        """有投票权的人。

        白痴翻牌后**还在场上、还能发言，但失去投票权** —— 所以投票列表
        不能直接用 `speaking_order()`。
        """
        return [s for s in self.speaking_order()
                if not (s.role == "Idiot" and self.state.idiot_revealed)]

    def _idiot_survives_exile(self, seat: Seat) -> bool:
        """被投票放逐的是（还没翻牌的）白痴 -> 翻牌、不死。返回 True 表示已处理。"""
        if seat.role != "Idiot" or self.state.idiot_revealed:
            return False
        self.state.idiot_revealed = True
        self.log.append(day=self.state.day, phase="DAY_VOTE", channel=Channel.PUBLIC,
                        kind=Kind.RESOLUTION, actor=seat.no,
                        text=f"{seat.no}号 翻开白痴身份：**本轮不出局**，"
                             f"但从现在起失去投票权（仍可发言）",
                        idiot_revealed=True, seat=seat.no)
        return True

    # ---------------- 夜晚 ----------------

    def night(self) -> None:
        self.state.wolf_target = None
        self.state.poison_target = None
        self.state.guard_target = None
        self.state.witch_save_target = None
        self.state.saved_target = None
        self.state.saved_by = None

        guard = next((s for s in self.alive() if s.role == "Guard"), None)
        if guard:
            self.state.guard_target = self._night_target(guard, "NIGHT_GUARD")

        wolves = [s for s in self.alive() if s.is_wolf]
        for wolf in wolves:
            self._wolf_chat(wolf)
        for wolf in wolves:
            self._wolf_kill(wolf)

        # ★ 狼队多数表决。原来是"以最后一只活着狼人的选择为准"，
        # 那等于让最后发言的狼独裁：前三只狼的战术讨论全是装饰，
        # 结果只取决于发言顺序。改成按票数汇总，平票时按"谁先提出"定。
        self.state.wolf_target = self._wolf_consensus()

        self._witch()
        self._seer()

    def _wolf_consensus(self) -> Optional[int]:
        """把今晚每只狼的杀票汇总成狼队决议。只看今天的提议。"""
        counts: Dict[int, List[int]] = {}
        first_seen: Dict[int, int] = {}
        for ev in wolf_channel(self.log):
            if ev.day != self.state.day:
                continue
            if ev.kind is not Kind.ACTION or ev.data.get("action") != "kill":
                continue
            target = ev.data.get("target")
            if not target:
                continue
            counts.setdefault(target, []).append(ev.actor)
            first_seen.setdefault(target, ev.seq)

        if not counts:
            self.log.append(day=self.state.day, phase="NIGHT_WOLF", channel=Channel.WOLF,
                            kind=Kind.RESOLUTION, text="狼队今晚没有提出任何有效刀口（空刀）",
                            votes={}, total=0)
            return None

        total = sum(len(v) for v in counts.values())
        target, backers = sorted(
            counts.items(), key=lambda kv: (-len(kv[1]), first_seen[kv[0]]))[0]
        top = len(backers)
        tied = [t for t, bs in counts.items() if len(bs) == top]
        back = "、".join(f"{n}号" for n in backers)
        if len(tied) > 1:
            why = f"{len(tied)} 人并列，按最先提出的方案"
        else:
            why = "多数通过"
        self.log.append(day=self.state.day, phase="NIGHT_WOLF", channel=Channel.WOLF,
                        kind=Kind.RESOLUTION,
                        text=f"狼队决议：今晚击杀 {target}号（{top}/{total} 票，{why}，"
                             f"提议者 {back}）",
                        target=target, votes=top, total=total, tied=len(tied) > 1)
        return target

    def _wolf_chat(self, wolf: Seat) -> None:
        self.state.phase = "NIGHT_WOLF_CHAT"
        decision = self._ask(wolf, "NIGHT_WOLF_CHAT")
        self._append_speech(wolf, decision, Channel.WOLF, "NIGHT_WOLF_CHAT")

    def _wolf_kill(self, wolf: Seat) -> None:
        self.state.phase = "NIGHT_WOLF"
        decision = self._ask(wolf, "NIGHT_WOLF")
        target, note = self._resolve_target(wolf, decision, phase="NIGHT_WOLF")
        # ★ 尾空格 + 括号开头会拼成「主张击杀 （未定）」（2026-10-06 修）。
        # 条件表达式两边的空格是**语法要求**，那就把尾空格从字面量里拿掉，
        # 而不是靠人去记得别写错。
        #
        # ★ 调用全失败时不能说它"主张击杀"——它压根没开口（同 hunter_shoot）。
        #   这条走的是 WOLF 频道，其他狼看得到，写错了会污染狼队的判断。
        failed = "call_failed" in decision.issues
        text = ("本轮未表态（没有回应刀口询问）" if failed else
                "主张击杀" + (f" {target}号" if target else "（未定）")
                + ("（目标不合法，已作废）" if note else ""))
        self.log.append(day=self.state.day, phase="NIGHT_WOLF", channel=Channel.WOLF,
                        kind=Kind.ACTION, actor=wolf.no, text=text,
                        action="kill", target=target,
                        **({"no_response": True, "issues": list(decision.issues)}
                           if failed else {}))

    def _night_target(self, seat: Seat, phase: str) -> Optional[int]:
        """夜间定向技能：动作写进 PRIVATE(to_seat)，不污染大桌。"""
        self.state.phase = phase
        decision = self._ask(seat, phase)
        target, note = self._resolve_target(seat, decision, phase=phase)
        # 文本与 target 必须一致：以前无论合不合法都渲染原始动作，
        # 于是日志里同时出现「选择守护3号」和 target=None，读的人一头雾水。
        text = describe_action(seat.no, decision.action)
        if note:
            text += "（目标不合法，已作废）"
        elif "call_failed" in decision.issues:
            # ★ 同 hunter_shoot：调用全失败时不能写成"选择不行动"。
            # 那是一次基础设施故障，不是决策 —— 写成主动选择就是引擎在伪造。
            text = "本轮未表态（没有回应行动询问）"
        elif target is None:
            text = "选择不行动"
        self.log.append(day=self.state.day, phase=phase, channel=Channel.PRIVATE,
                        kind=Kind.ACTION, actor=seat.no, to_seat=seat.no,
                        text=text, action=decision.action.type, target=target,
                        **({"attempted": decision.action.target} if note else {}),
                        **({"no_response": True, "issues": list(decision.issues)}
                           if "call_failed" in decision.issues else {}))
        return target

    def _seer(self) -> None:
        seer = next((s for s in self.alive() if s.role == "Seer"), None)
        if not seer:
            return
        self.state.phase = "NIGHT_SEER"
        decision = self._ask(seer, "NIGHT_SEER")
        target, note = self._resolve_target(seer, decision, phase="NIGHT_SEER")
        if target is None:
            # 不合法时以前是"报个诊断然后什么都不记"，私密日志里凭空断一条。
            #
            # ★ 调用全失败时**不能**写"选择不查验任何人"（同 hunter_shoot）：
            #   那是故障，不是决策。这条路径是 _night_target 的副本，
            #   所以修 _night_target 不够 —— 实测就是这么漏掉预言家的。
            if "call_failed" in decision.issues:
                text = "本轮未表态（没有回应查验询问）"
            elif not note:
                text = "选择不查验任何人"
            else:
                text = f"想查验 {decision.action.target}号（目标不合法，已作废）"
            extra = {"no_response": True, "issues": list(decision.issues)} \
                if "call_failed" in decision.issues else \
                ({"attempted": decision.action.target} if note else {})
            self.log.append(day=self.state.day, phase="NIGHT_SEER", channel=Channel.PRIVATE,
                            kind=Kind.ACTION, actor=seer.no, to_seat=seer.no,
                            text=text, action="check", target=None, **extra)
            return
        is_wolf = self.by_no[target].is_wolf
        self.state.seer_checks[target] = is_wolf
        self.log.append(day=self.state.day, phase="NIGHT_SEER", channel=Channel.PRIVATE,
                        kind=Kind.RESOLUTION, actor=seer.no, to_seat=seer.no,
                        text=f"你查验了 {target}号 {self.by_no[target].label}，结果是"
                             f"{'狼人' if is_wolf else '好人'}",
                        target=target, is_wolf=is_wolf)

    def _witch(self) -> None:
        witch = next((s for s in self.alive() if s.role == "Witch"), None)
        if not witch:
            return
        self.state.phase = "NIGHT_WITCH"
        decision = self._ask(witch, "NIGHT_WITCH")
        act = decision.action.type
        if act == "save" and not self.state.witch_heal_used:
            self.state.witch_heal_used = True
            # ★ 只对"今晚的刀口"生效，不能变成永久免死
            self.state.witch_save_target = self.state.wolf_target
            self.log.append(day=self.state.day, phase="NIGHT_WITCH", channel=Channel.PRIVATE,
                            kind=Kind.ACTION, actor=witch.no, to_seat=witch.no,
                            text="你使用了救药", action="save",
                            target=self.state.witch_save_target)
        elif act == "poison" and not self.state.witch_poison_used:
            target, note = self._resolve_target(witch, decision, phase="NIGHT_WITCH")
            if target is not None:
                self.state.witch_poison_used = True
                self.state.poison_target = target
                self.log.append(day=self.state.day, phase="NIGHT_WITCH", channel=Channel.PRIVATE,
                                kind=Kind.ACTION, actor=witch.no, to_seat=witch.no,
                                text=f"你对 {target}号 使用了毒药", action="poison", target=target)
            else:
                # 以前这里**静默**落到"没有使用药水"：毒药凭空消失、连诊断都没有，
                # 事后查不出是模型没说清还是引擎丢的。现在无论哪条路都留痕。
                self.log.append(day=self.state.day, phase="NIGHT_WITCH", channel=Channel.PRIVATE,
                                kind=Kind.ACTION, actor=witch.no, to_seat=witch.no,
                                text=(f"想对 {decision.action.target}号 使用毒药"
                                      f"（目标不合法，已作废）" if note else "你没有使用药水"),
                                action="poison" if note else "pass", target=None,
                                **({"attempted": decision.action.target} if note else {}))
        else:
            self.log.append(day=self.state.day, phase="NIGHT_WITCH", channel=Channel.PRIVATE,
                            kind=Kind.ACTION, actor=witch.no, to_seat=witch.no,
                            text="你没有使用药水", action="pass")

    def resolve_night(self) -> List[Tuple[int, str]]:
        """返回 [(座位号, 死因)]。死因决定猎人能否开枪。"""
        deaths: List[Tuple[int, str]] = []
        self.state.phase = "NIGHT_RESOLVE"
        w = self.state.wolf_target
        g = self.state.guard_target
        # ★ 只认"今晚"的救药，不能用粘性的 witch_heal_used
        saved = (self.state.witch_save_target is not None
                 and self.state.witch_save_target == w)

        if w is not None and w in self.alive_nos():
            protected = (g == w)
            if protected and saved:
                pass  # 同守同救 -> 该死
            elif protected or saved:
                # ★ 2026-10-06：这条原来是 **PUBLIC**，等于把"今晚谁被刀"公告给全场。
                #   真实规则里天亮只有两个公开信息：平安夜 / 昨晚死亡的是谁。
                #   刀口属于被刀者、女巫、狼队三方，跟票的人本来不该知道。
                #
                #   代价是实测出来的：10 座局第一天全场拿"3号怎么会在6号报银水之前
                #   就知道刀口在6"当锁死 3 号的硬伤，5/9/12 号都写了这条，最后成了
                #   放逐真预言家的理由之一 —— 而那条信息**本来就是引擎自己播报的**。
                #   整轮推理预算烧在一句不该存在的公告上。
                #
                #   现在只记上帝账本（保留可复盘性），女巫从自己的私密信息里看到结果。
                self.state.saved_target = w
                self.state.saved_by = "guard" if protected else "witch"
                self._god(f"{w}号 昨晚被救下了"
                          f"（{'守卫守住' if protected else '女巫解药'}）"
                          f"—— 公开频道只说平安夜，玩家侧不得出现刀口",
                          saved_target=w, saved_by=self.state.saved_by)
                w = None
            if w is not None:
                deaths.append((w, "wolf"))

        p = self.state.poison_target
        if p is not None and p in self.alive_nos():
            deaths.append((p, "poison"))

        # 上一晚的守护不能延续到今晚
        self.state.last_guard_target = g
        return deaths

    # ---------------- 警长（2026-10-06 上线，按"方案 A"） ----------------

    def sheriff_election(self) -> None:
        """首日警长竞选。

        方案 A 的完整链条：上警（竞选发言）→ **只有没上警的人**投警长票 →
        平票则台上 PK 再投一次 → 再平票就**警徽流失**（本局没有警长）。

        两个容易做丢的点：
          · 「上警的人不能投警长票」——投票人列表要减掉候选人，否则等于自己选自己；
          · 候选人只有 1 个时直接当选（标准做法），不必再走一轮投票白烧额度。
        """
        self.state.phase = "DAY_SHERIFF_RUN"
        candidates: List[int] = []
        for seat in self.speaking_order():
            decision = self._ask(seat, "DAY_SHERIFF_RUN", max_tokens=4096)
            if decision.action.type == "run":
                self._append_speech(seat, decision, Channel.PUBLIC, "DAY_SHERIFF_RUN")
                candidates.append(seat.no)
                self.log.append(day=self.state.day, phase="DAY_SHERIFF_RUN",
                                channel=Channel.PUBLIC, kind=Kind.SYSTEM,
                                actor=seat.no, text=f"{seat.no}号 上警竞选警长",
                                run=True, seat=seat.no)
            else:
                # 「不上警」和「本轮未发言」是两件事，不能共用那句标记 ——
                # 否则大桌上会看到 10 条"未发言"，像是大家都掉线了。
                if (decision.speech or "").strip():
                    self._append_speech(seat, decision, Channel.PUBLIC, "DAY_SHERIFF_RUN")
                self.log.append(day=self.state.day, phase="DAY_SHERIFF_RUN",
                                channel=Channel.PUBLIC, kind=Kind.SYSTEM,
                                actor=seat.no, text=f"{seat.no}号 不上警",
                                run=False, seat=seat.no)

        if not candidates:
            self._sheriff_none("全员都没有上警，警徽流失：本局没有警长")
            return
        if len(candidates) == 1:
            self._sheriff_set(candidates[0], "只有一人上警，自动当选警长")
            return

        voters = [s for s in self.voters() if s.no not in candidates]
        if not voters:
            self._sheriff_none("全员上警，没有人能投票，警徽流失")
            return

        self.state.phase = "DAY_SHERIFF_VOTE"
        tally = self._vote_round(voters, "DAY_SHERIFF_VOTE", restrict=set(candidates))
        if not tally:
            self._sheriff_none("警长竞选全员弃票，警徽流失")
            return

        counts = {t: float(len(vs)) for t, vs in tally.items()}   # 竞选不算 1.5 票
        ranked = self._rank(counts)
        top, votes = ranked[0]
        tied = sorted(n for n, c in counts.items() if c == votes)
        if len(tied) == 1:
            self._sheriff_set(top, f"警长竞选得票最高（{self._fmt_votes(votes)} 票）")
            return

        # 平票 → 台上 PK 发言 → 只在这几个人里重投一次
        names = "、".join(f"{n}号" for n in tied)
        self.log.append(day=self.state.day, phase="DAY_SHERIFF_VOTE", channel=Channel.PUBLIC,
                        kind=Kind.RESOLUTION,
                        text=f"警长竞选平票（各 {self._fmt_votes(votes)} 票）：{names} 进入 PK",
                        tied=tied)
        self.state.phase = "DAY_SHERIFF_PK"
        for seat in self.speaking_order():
            if seat.no in tied:
                decision = self._ask(seat, "DAY_SHERIFF_PK", max_tokens=4096)
                self._append_speech(seat, decision, Channel.PUBLIC, "DAY_SHERIFF_PK")

        self.state.phase = "DAY_SHERIFF_VOTE"
        tally2 = self._vote_round(voters, "DAY_SHERIFF_VOTE", restrict=set(tied))
        if not tally2:
            self._sheriff_none("警长 PK 重投全员弃票，警徽流失")
            return
        counts2 = {t: float(len(vs)) for t, vs in tally2.items()}
        ranked2 = self._rank(counts2)
        top2, votes2 = ranked2[0]
        if len(ranked2) > 1 and ranked2[1][1] == votes2:
            self._sheriff_none("警长 PK 再次平票，警徽流失")
            return
        self._sheriff_set(top2, f"警长 PK 重投胜出（{self._fmt_votes(votes2)} 票）")

    def _sheriff_set(self, no: int, why: str) -> None:
        self.state.sheriff = no
        self.state.badge_destroyed = False
        self.log.append(day=self.state.day, phase=self.state.phase, channel=Channel.PUBLIC,
                        kind=Kind.RESOLUTION,
                        text=f"{no}号 当选警长（{why}）。警长在放逐投票里算 **1.5 票**，"
                             f"并决定每天的发言顺序、自己最后归票",
                        sheriff=no)

    def _sheriff_none(self, why: str) -> None:
        self.state.sheriff = None
        self.state.badge_destroyed = True
        self.state.sheriff_direction = None
        self.log.append(day=self.state.day, phase=self.state.phase, channel=Channel.PUBLIC,
                        kind=Kind.RESOLUTION, text=why, sheriff=None, badge_destroyed=True)

    def sheriff_pick_order(self) -> None:
        """警长决定今天的发言顺序。没有警长时什么也不做。"""
        no = self.state.sheriff
        if no is None or no not in self.alive_nos():
            return
        self.state.phase = "DAY_SHERIFF_ORDER"
        seat = self.by_no[no]
        decision = self._ask(seat, "DAY_SHERIFF_ORDER", max_tokens=1024)
        self._append_speech(seat, decision, Channel.PUBLIC, "DAY_SHERIFF_ORDER")
        # 解析不出来就按警左，别让一次脏输出把发言顺序搞成随机
        direction = "right" if decision.action.type == "order_right" else "left"
        self.state.sheriff_direction = direction
        self.log.append(day=self.state.day, phase="DAY_SHERIFF_ORDER", channel=Channel.PUBLIC,
                        kind=Kind.RESOLUTION,
                        text=f"{no}号 警长决定从**警{'右' if direction == 'right' else '左'}**"
                             f"开始发言（他自己最后归票）",
                        sheriff=no, direction=direction)

    def badge_transfer(self, dead: Seat) -> None:
        """警长出局时移交警徽，或撕掉。"""
        if self.state.sheriff != dead.no:
            return
        self.state.phase = "DAY_BADGE"
        decision = self._ask(dead, "DAY_BADGE", max_tokens=1024)
        target = decision.action.target
        legal = [n for n in self.alive_nos() if n != dead.no]
        if decision.action.type == "badge" and target in legal:
            self.state.sheriff = target
            self.log.append(day=self.state.day, phase="DAY_BADGE", channel=Channel.PUBLIC,
                            kind=Kind.RESOLUTION,
                            text=f"{dead.no}号 出局前把警徽移交给 {target}号，"
                                 f"{target}号 成为新警长",
                            sheriff=target, from_seat=dead.no)
        else:
            self._sheriff_none(f"{dead.no}号 出局，警徽被撕掉 —— 本局不再有警长")

    def _fmt_votes(self, v: float) -> str:
        """票数显示：1.5 票要看得见小数，整数别显示成 6.0。"""
        return str(int(v)) if float(v).is_integer() else f"{v:g}"

    # ---------------- 白天 ----------------

    def day_speeches(self) -> bool:
        """白天依次发言。返回是否有人自曝 —— 自曝则当天立即结束、不投票。

        ★ 2026-10-06 修：**自曝的狼人要出局**。

        老实现只是 `return True` 跳过投票，谁也没死。后果在 10 座局里实测到了：
        1 号自曝之后**又活了整整两天** —— 第 3 天照常发言、照常投票（全场都知道
        它是狼，它还在投），第 3、4 夜继续参与狼队表决，最后终局正好卡在
        「2 狼 vs 2 好人」的 parity 上判狼胜。
        也就是说自曝当时是**纯赚**：既跳过一轮投票、又不用死。

        标准规则：自曝 = 狼人亮明身份并出局，白天立即结束进入黑夜。
        按这条规则，那局自曝之后狼只剩 1 只、好人还有 6 个，parity 再也无法成立。
        有没有代价，是"自曝"这个动作成不成立的关键。
        """
        self.state.phase = "DAY_SPEECH"
        for seat in self.speaking_order():
            decision = self._ask(seat, "DAY_SPEECH", max_tokens=8192)
            explode = seat.is_wolf and decision.action.type == "explode"
            self._append_speech(seat, decision, Channel.PUBLIC, "DAY_SPEECH")
            if explode:
                # 先记公告（此时 seat.alive 还是 True，公告才能正常渲染出说话人），
                # 再判出局。顺序反了的话事件里就只剩一个没有人的座位号。
                self.log.append(day=self.state.day, phase="DAY_SPEECH",
                                channel=Channel.PUBLIC, kind=Kind.RESOLUTION,
                                actor=seat.no,
                                text=f"{seat.no}号 自曝狼人身份并出局，白天立即结束，"
                                     f"直接进入黑夜（今天不再发言、不再投票）",
                                exploded=True, seat=seat.no)
                self.kill(seat.no, "explode")
                return True
        return False

    # ---- 投票（含平票 PK） ----

    def _vote_round(self, voters: List[Seat], phase: str,
                    restrict: Optional[set] = None) -> Dict[int, List[int]]:
        """收一轮票，返回 {被投者: [投票者…]}。

        ★ 投票阶段的 ``speech`` **不再被丢弃**：它是后置位唯一能拿到的
        "他为什么投这个人"。以前只落一条自动生成的「N号 投给 X号」，
        票型里没有任何可推理的信息，等于逼后置位盲投。
        """
        tally: Dict[int, List[int]] = {}
        allowed = set(restrict) if restrict else None
        for voter in voters:
            decision = self._ask(voter, phase, max_tokens=2048)
            reason = (decision.speech or "").strip()
            if reason:
                self.log.append(day=self.state.day, phase=phase, channel=Channel.PUBLIC,
                                kind=Kind.SPEECH, actor=voter.no, text=reason)
            target = decision.action.target
            if (target is None or target not in self.targets_for(voter)
                    or (allowed is not None and target not in allowed)):
                self.log.append(day=self.state.day, phase=phase, channel=Channel.PUBLIC,
                                kind=Kind.SPEECH, actor=voter.no,
                                text=f"{voter.no}号 弃票",
                                action="vote", target=None)
                continue
            tally.setdefault(target, []).append(voter.no)
            self.log.append(day=self.state.day, phase=phase, channel=Channel.PUBLIC,
                            kind=Kind.SPEECH, actor=voter.no,
                            text=f"{voter.no}号 投给 {target}号",
                            action="vote", target=target)
        return tally

    def _note_no_vote_seats(self, voters: List[Seat], phase: str,
                            *, pk_stage: Optional[set] = None) -> None:
        """把"在场但没有投票权的人"明确记一条。

        白痴翻牌后不在投票列表里，如果什么都不写，大桌上就是"凭空少了一票" ——
        其他模型会以为它弃票或者没说话。这是 DESIGN §4.2 第 6 步那条
        "静默缺人必须留痕"的同一条规矩。

        ★ 2026-10-11 修：这里原来**见人就写"已翻牌白痴"**，于是 PK 重投时
        台上的两个平票者被公开宣布成"翻牌白痴"。实测（12 座局第 4 天）：

            12号 已翻牌白痴，没有投票权（本轮不投票）
            1号  已翻牌白痴，没有投票权（本轮不投票）

        而本局**根本没有人翻过牌** —— 他们只是站在 PK 台上、按规则不投票。
        这句话直接把全场读盘带歪：8号 MiniMax 整段推理都在纠结"怎么1号和12号
        两个人都报了白痴，白痴只有一张"，把票型计算全烧在澄清一条引擎自己编的
        假事实上。**引擎不能撒谎**（同 `_idiot_survives_exile` 与猎人开枪那两处），
        没有票的原因必须分得开：台外重投的规则排除 ≠ 白痴翻牌。
        """
        voted = {s.no for s in voters}
        pk_stage = pk_stage or set()
        for s in self.speaking_order():
            if s.no in voted:
                continue
            if s.no in pk_stage:
                self.log.append(day=self.state.day, phase=phase, channel=Channel.PUBLIC,
                                kind=Kind.SYSTEM, actor=s.no,
                                text=f"{s.no}号 在 PK 台上，本轮不投票"
                                     f"（台外玩家重新投票）",
                                pk_stage=True, seat=s.no)
            elif s.role == "Idiot" and self.state.idiot_revealed:
                self.log.append(day=self.state.day, phase=phase, channel=Channel.PUBLIC,
                                kind=Kind.SYSTEM, actor=s.no,
                                text=f"{s.no}号 已翻牌白痴，没有投票权（本轮不投票）",
                                no_vote=True, seat=s.no)
            else:
                # 既不在 PK 台上、又不是翻牌白痴，却没有票 —— 不该发生。
                # 静默会让人以为它弃票，所以照样留痕，但**不许**说成白痴。
                self.log.append(day=self.state.day, phase=phase, channel=Channel.PUBLIC,
                                kind=Kind.SYSTEM, actor=s.no,
                                text=f"{s.no}号 本轮不投票（不是 PK 台上的人）",
                                no_vote=True, seat=s.no)

    def _weight(self, seat_no: int) -> float:
        """放逐投票里警长算 **1.5 票**。

        警长竞选自己的投票不适用（那时还没选出警长），所以竞选那轮直接用人数。
        """
        return 1.5 if seat_no == self.state.sheriff else 1.0

    def _counts(self, tally: Dict[int, List[int]]) -> Dict[int, float]:
        return {t: sum(self._weight(v) for v in vs) for t, vs in tally.items()}

    @staticmethod
    def _rank(counts: Dict[int, float]) -> List[tuple]:
        # 票数多的在前；同票时座位号小的在前，保证渲染与回放确定性
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def _exile_text(self, target: int, votes: float, *, pk: bool) -> str:
        v = self._fmt_votes(votes)
        return (f"{target}号 被放逐（PK 重投 {v}票）" if pk
                else f"{target}号 被放逐（{v}票）")

    def day_vote(self) -> Optional[int]:
        """白天投票。平票则进入 PK 发言 + 重投，再平票才无人出局。"""
        self.state.phase = "DAY_VOTE"
        voters = self.voters()
        self._note_no_vote_seats(voters, "DAY_VOTE")
        tally = self._vote_round(voters, "DAY_VOTE")

        if not tally:
            self.log.append(day=self.state.day, phase="DAY_VOTE", channel=Channel.PUBLIC,
                            kind=Kind.RESOLUTION, text="本轮无人被放逐（全员弃票）")
            return None

        counts = self._counts(tally)
        ranked = self._rank(counts)
        top, votes = ranked[0]
        tied = sorted(n for n, c in counts.items() if c == votes)
        if len(tied) > 1:
            return self._pk_vote(tied, votes)

        self.log.append(day=self.state.day, phase="DAY_VOTE", channel=Channel.PUBLIC,
                        kind=Kind.RESOLUTION, text=self._exile_text(top, votes, pk=False),
                        target=top, votes=votes)
        return top

    def _pk_vote(self, tied: List[int], votes: float) -> Optional[int]:
        """平票 PK：台上发言 → 台下重投（只能投台上的人）→ 再平票无人出局。"""
        names = "、".join(f"{n}号" for n in tied)
        self.log.append(day=self.state.day, phase="DAY_VOTE", channel=Channel.PUBLIC,
                        kind=Kind.RESOLUTION,
                        text=f"平票（各 {self._fmt_votes(votes)} 票）：{names} "
                             f"进入 PK 发言，台外玩家重新投票",
                        tied=tied, votes=votes)

        self.state.phase = "DAY_PK_SPEECH"
        pk_order = [s for s in self.speaking_order() if s.no in tied]
        for seat in pk_order:
            decision = self._ask(seat, "DAY_PK_SPEECH", max_tokens=8192)
            self._append_speech(seat, decision, Channel.PUBLIC, "DAY_PK_SPEECH")

        voters = [s for s in self.voters() if s.no not in tied]
        self.state.phase = "DAY_VOTE_PK"
        # ★ 台上的人不投票是**规则**，不是白痴翻牌 —— 必须分开说，
        #   否则大桌上会出现两个"已翻牌白痴"（本局一张白痴牌都没翻）。
        self._note_no_vote_seats(voters, "DAY_VOTE_PK", pk_stage=set(tied))
        if not voters:
            self.log.append(day=self.state.day, phase="DAY_VOTE_PK", channel=Channel.PUBLIC,
                            kind=Kind.RESOLUTION,
                            text="PK 台上已无台外玩家，本轮无人被放逐")
            return None

        tally = self._vote_round(voters, "DAY_VOTE_PK", restrict=set(tied))
        if not tally:
            self.log.append(day=self.state.day, phase="DAY_VOTE_PK", channel=Channel.PUBLIC,
                            kind=Kind.RESOLUTION, text="PK 重投全员弃票，本轮无人被放逐")
            return None

        counts = self._counts(tally)
        ranked = self._rank(counts)
        top, votes2 = ranked[0]
        if len(ranked) > 1 and ranked[1][1] == votes2:
            self.log.append(day=self.state.day, phase="DAY_VOTE_PK", channel=Channel.PUBLIC,
                            kind=Kind.RESOLUTION, text="PK 再次平票，本轮无人被放逐")
            return None

        self.log.append(day=self.state.day, phase="DAY_VOTE_PK", channel=Channel.PUBLIC,
                        kind=Kind.RESOLUTION, text=self._exile_text(top, votes2, pk=True),
                        target=top, votes=votes2)
        return top

    def last_words(self, seat: Seat) -> None:
        self.state.phase = "DAY_LAST_WORDS"
        decision = self._ask(seat, "DAY_LAST_WORDS")
        self._append_speech(seat, decision, Channel.PUBLIC, "DAY_LAST_WORDS")

    def hunter_shoot(self, hunter: Seat, can_shoot: bool) -> None:
        self.state.phase = "DAY_HUNTER"
        if not can_shoot:
            self.log.append(day=self.state.day, phase="DAY_HUNTER", channel=Channel.PUBLIC,
                            kind=Kind.RESOLUTION, text="猎人被毒死，无法开枪")
            return
        decision = self._ask(hunter, "DAY_HUNTER")
        target, note = self._resolve_target(hunter, decision, phase="DAY_HUNTER")
        if decision.action.type == "shoot" and target is not None:
            self.kill(target, "hunter")
            self.log.append(day=self.state.day, phase="DAY_HUNTER", channel=Channel.PUBLIC,
                            kind=Kind.RESOLUTION,
                            text=f"{hunter.no}号 猎人开枪带走了 {target}号", target=target)
        elif "call_failed" in decision.issues:
            # ★ 调用全失败时**绝不能写成"他选择不开枪"**（2026-10-06 实测的假信息）。
            #
            # 那局 12号 的 DAY_HUNTER 两次尝试：一次 curl 超时、一次 HTTP 200 但响应体
            # **完全是空的**。这是基础设施故障，不是决策。可公开公告只写了
            # 「12号 猎人选择不开枪」—— 于是全场（包括 8号，它拿这句去推"12号自己
            # 也不确定谁是最后一张狼"）读到的是一条**引擎伪造出来的选择**。
            #
            # 为什么必须修而不是忍：**玩家可以撒谎，引擎不能。**
            # 狼人的角色提示词本来就允许它自己伪造系统故障（那是合法的狼队阴招），
            # 但引擎一旦替模型宣布一个它没做过的决定，就等于把伪造写进了权威记录，
            # 后面所有复盘、所有推理都被这条假信息污染。
            # 措辞必须比"选择不开枪"更弱：只能说"没表态"，不能替它认下一个选择。
            self.log.append(day=self.state.day, phase="DAY_HUNTER", channel=Channel.PUBLIC,
                            kind=Kind.RESOLUTION,
                            text=f"{hunter.no}号 本轮未表态（没有回应开枪询问）",
                            no_response=True, issues=list(decision.issues))
        elif note:
            # 以前这种情况和"主动不开枪"共用同一句文案 —— 等于把一次失效的
            # 开枪说成"他选择不开枪"，是条假信息。
            self.log.append(day=self.state.day, phase="DAY_HUNTER", channel=Channel.PUBLIC,
                            kind=Kind.RESOLUTION,
                            text=f"{hunter.no}号 猎人开枪的目标不合法，本轮未开枪")
        else:
            # 走到这里才是**真的**主动放弃（解析成功、动作是 pass）
            self.log.append(day=self.state.day, phase="DAY_HUNTER", channel=Channel.PUBLIC,
                            kind=Kind.RESOLUTION, text=f"{hunter.no}号 猎人选择不开枪")

    # ---------------- 主循环 ----------------

    def run(self) -> str:
        winner: Optional[str] = None
        while self.state.day < self.max_days:
            self.state.day += 1
            self.say(f"\n{'=' * 66}\n第 {self.state.day} 天 · 夜晚\n{'=' * 66}")
            self.log.append(day=self.state.day, phase="NIGHT_START", channel=Channel.PUBLIC,
                            kind=Kind.SYSTEM, text="天黑请闭眼")
            self.night()
            deaths = self.resolve_night()

            if deaths:
                names = "、".join(f"{n}号" for n, _ in deaths)
                announce = f"昨晚死亡的是：{names}"
                for n, cause in deaths:
                    self.kill(n, cause)
            else:
                announce = "昨晚是平安夜"
            self.log.append(day=self.state.day, phase="DAY_ANNOUNCE", channel=Channel.PUBLIC,
                            kind=Kind.RESOLUTION, text=announce)
            self.say(f"\n-- 天亮 --\n   {announce}")

            # ★ 夜里被刀死的猎人，**天亮立刻开枪**（被毒死不能开枪）。
            # 原来要等"发言 + 投票 + 遗言"全走完才补枪，玩家看到的是
            # "先讨论投票，然后昨晚死的人突然开枪"；而且天亮时若已判出胜负，
            # break 会让猎人永远开不了枪。
            for n, cause in deaths:
                fallen = self.by_no[n]
                if fallen.role == "Hunter":
                    self.hunter_shoot(fallen, can_shoot=(cause != "poison"))

            winner = self.check_win()
            if winner:
                break

            # ---- 警长：首日竞选，之后每天定发言方向 ----
            # 顺序要卡在"公布死讯之后、发言之前"：警长得先上任/先定顺序，
            # 发言顺序才有依据。
            if (self.sheriff_on and self.state.day == 1 and self.state.sheriff is None
                    and not self.state.badge_destroyed):
                self.say("\n-- 警长竞选 --")
                self.sheriff_election()
            # 警长（含夜里刚死的那个）先处理警徽去向，再定今天的顺序
            for n, cause in deaths:
                if self.state.sheriff == n:
                    self.badge_transfer(self.by_no[n])
            if self.state.sheriff is not None:
                self.sheriff_pick_order()

            self.roll_speaker_start()
            order_txt = " → ".join(f"{s.no}号" for s in self.speaking_order())
            self.log.append(day=self.state.day, phase="DAY_SPEECH", channel=Channel.PUBLIC,
                            kind=Kind.SYSTEM, text=f"本轮发言顺序：{order_txt}")

            self.say(f"\n-- 白天发言 --\n   {order_txt}")
            exploded = self.day_speeches()

            if exploded:
                # 自曝：白天立即结束，今天不投票，直接进黑夜
                self.say("\n-- 有狼人自曝，白天立即结束，直接进入黑夜 --")
            else:
                self.say(f"\n-- 投票 --")
                out = self.day_vote()
                if out is not None:
                    out_seat = self.by_no[out]
                    # ★ 白痴：被投票放逐时翻牌、**不出局**，代价是失去投票权。
                    #   所以这里不能直接 kill —— 先问一句"这票对白痴算不算数"。
                    if self._idiot_survives_exile(out_seat):
                        pass
                    else:
                        self.kill(out, "vote")
                        # 警长出局：处理警徽去向（移交或撕掉）。
                        # 放在遗言之前 —— 遗言是"最后说的话"，移交是机制动作。
                        self.badge_transfer(out_seat)
                        # ★ 这一票如果已经把胜负定下来了，就不再留遗言。
                        # 必胜/必输局的遗言对局势没有任何影响，只是白多一次调用
                        # （实测一局最后一句遗言让结束多等了半分钟）。
                        # 注意先 kill 再判：遗言属于"已经被放逐的人"。
                        if self.check_win() is None:
                            self.last_words(out_seat)
                        # 猎人开枪要留着 —— 它能一枪把已经判定的胜负打回来
                        if out_seat.role == "Hunter":
                            self.hunter_shoot(out_seat, can_shoot=True)

            winner = self.check_win()
            if winner:
                break

        return winner or "draw"
