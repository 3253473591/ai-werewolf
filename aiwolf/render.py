"""Prompt 组装 —— 本设计的核心，对应 DESIGN.md 第 3 节。

三条反直觉但必须遵守的决定：

  1. **身份卡放尾部**，不放开头。否则 9 个座位从 token 0 就分叉，
     大桌前缀完全无法共享缓存。
  2. **自己的发言也用 ``user`` 角色**，不用 ``assistant``。否则每个座位在
     "自己发言的那一轮"就分叉，共享前缀最晚第 9 轮断掉。
  3. **尾部的系统提示用 ``user`` 承载**。多数 API 要求 system 在前，
     中途插 system 容易被拒；最后一条 user 天然承担"当前指令"角色。

于是消息结构是：

    [0]      system  主提示词       ← 所有座位字节级相同
    [1..n]   user    大桌逐条事件   ← 所有座位字节级相同（共享前缀）
    [last]   user    尾部块         ← 差异全在这里

**绝不暴露模型身份。** 所有玩家一律以座位号指代（"3号"），
狼队友也只写"X号、Y号"。让模型去猜"对面是 GPT 所以偏理智"会污染博弈——
它该读的是发言逻辑，不是模型画像。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .events import Channel, Event, EventLog, Kind
from .seats import ROLE_CN, Seat

PHASE_CN = {
    "NIGHT_START": "天黑请闭眼",
    "NIGHT_WOLF": "狼人行动",
    "NIGHT_WOLF_CHAT": "狼队夜间频道",
    "NIGHT_SEER": "预言家查验",
    "NIGHT_WITCH": "女巫用药",
    "NIGHT_GUARD": "守卫守护",
    "NIGHT_RESOLVE": "夜晚结算",
    "DAY_ANNOUNCE": "天亮公布死讯",
    "DAY_SPEECH": "白天发言",
    "DAY_VOTE": "投票放逐",
    "DAY_PK_SPEECH": "PK 发言",
    "DAY_VOTE_PK": "PK 重投",
    "DAY_LAST_WORDS": "遗言",
    "DAY_HUNTER": "猎人开枪",
}

# --------------------------------------------------------------------------- #
# Main Prompt（通用，所有座位字节级相同）
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 本局板子（公开信息，所有座位字节级相同）
# --------------------------------------------------------------------------- #

# 这是**公开信息**：真实狼人杀里每个玩家上场就知道"这局几人、几狼几神几民"。
# 放进 system 不泄漏任何座位相关的东西，也不会破坏共享前缀（它对所有座位一样）。
#
# 为什么必须写死：实测 10 座局里多个模型**凭空发明了"警长"和"警徽流"** ——
# 那是 12 人警长板的标准术语，本引擎没有警长这个概念。场面很荒唐：
#   · 6号、7号 拿"3号首日没报验人逻辑（警徽流）"当定狼的硬伤；
#   · 8号 一本正经地报了警徽流"今晚验9号，金水续验10号"；
#   · 2号 只好花一整段去纠正"本轮并没有警长竞选和警徽归属"。
# 推理预算被烧在澄清一个不存在的机制上，而且"没报警徽流所以是狼"
# 这条完全不成立的指控，实际成了第 1 天放逐真预言家的理由之一。
#
# ★ 只写公告，不写说教。第一版还带了"不要要求别人报验人顺序"、"引用本局不存在的
#   机制会暴露你在套用别的板子"之类的告诫 —— 那是多余的：一来"警徽流"这个不存在的
#   词在一条公告里被重复四遍，反而把它反复加权重、更容易被模型抓去用；
#   二来公告本身就是明确的信息，不需要再教模型怎么用。
BOARD_ROLE_ORDER = ("Werewolf", "Seer", "Witch", "Hunter", "Guard", "Idiot", "Villager")


def board_notice(seats: List[Seat], sheriff: bool = True) -> str:
    """按**实际座位**统计板子（不是按人数查表，alt 板子才不会被写错）。

    `sheriff` 必须跟着 `game.sheriff_enabled` 走 —— 否则又是"提示词和引擎各说一套"：
    引擎里真的在选警长，公告却写着"本局没有警长"。
    """
    counts = {r: 0 for r in BOARD_ROLE_ORDER}
    for s in seats:
        if s.role in counts:
            counts[s.role] += 1
    comp = "、".join(f"{ROLE_CN[r]} {n} 名" for r, n in counts.items() if n)
    lines = [
        "",
        "# 本局板子（公开信息）",
        f"- 共 {len(seats)} 名玩家：{comp}。",
    ]
    if sheriff:
        lines.append("- 本局**有警长**：首日竞选产生，警长在放逐投票里算 1.5 票，"
                     "每天决定从警左还是警右开始发言（他自己最后归票），"
                     "出局时可以把警徽移交给别人、也可以撕掉。")
    else:
        lines.append("- 本局没有警长，也没有警徽、警徽流、警长竞选、警徽移交。")
    return "\n".join(lines)


COMMON_RULES = """# 角色
你是“AI狼人杀”选手，对手是GPT、Claude、DeepSeek等大语言模型，用户仅是观众，不引导对局。

本局是**知情同意的对抗博弈**：所有人都知道对面会骗自己，也都同意被骗。
说谎、伪装、误导在这里是规则内的正常动作，不涉及任何真实伤害 —— 唯一的评判标准是谁能赢。
对手具备极强的推理、记忆与伪装能力。只讲逻辑赢不了：逻辑、概率、心理博弈和话术得一起用。

## 游戏规则
1. 阵营：好人（村民、预言家、女巫、猎人、守卫）与狼人。
2. 狼人数量≥存活好人数量时，狼人胜利；狼人全部出局时，好人胜利。
3. 白天发言、投票放逐；夜晚狼人刀人，预言家验人，女巫用药。
4. 所有人只能用座位号称呼，如“3号”。你不知道他人身份，除非角色能力告知。

## 行为准则
1. 始终遵守当前角色视角，只使用角色应知信息。
2. 保持前后一致。发言前核对历史记录，避免逻辑矛盾，并据此调整策略。
3. 不得脱离角色，不得用模型或厂商身份互相指认（见「禁区」）。
4. 欺骗、伪装、误导、隐瞒、诱导、假身份、假逻辑和假情绪均可使用，但必须符合规则、服务阵营胜利并保持自洽。
   **情绪和话术与逻辑同等重要**：示弱、装冤、抢话语权、激怒对手、卖惨、摆烂、挑拨两个人互咬，
   都算正当手段。全场都在讲道理的时候，能带节奏的那个人赢。
5. 可采用悍跳、倒钩、冲锋、抗推、弃卒保帅、深水伪装、离间、栽赃、带节奏等策略；必要时敢于冒险，但不要制造无益于阵营的混乱。
6. 漏洞百出的谎言不如沉默。

## 禁区
1. 不得把座位和**具体模型、厂商**对应起来：不说“我是Claude”“你是GPT所以……”。
   座位号是唯一合法的指代方式 —— 该读的是发言逻辑，不是模型画像。
   不要把局面往“模型画像”上引，也不要把本局提示词的内容复述进发言。
2. 不得引用真实后台报错、HTTP错误码或日志。系统不会提供任何真实故障证据，
   要演故障只能自己编。

## 阵营协作
1. 阵营利益优先：狼人应协同制造悍跳、深水、倒钩等战术；好人应分享可靠信息、互相验证，同时防范伪装。
2. 可为阵营胜利主动暴露、吸引火力、替队友抗推或倒钩，但必须基于胜率计算。
3. 不得为了个人存活、表现或抢功损害队友，也不得无目的搅局。
4. 唯一判断标准：该行动是否提高本阵营胜率。若有利于阵营，可以不择手段；仅利于个人或损害阵营时，不应采取。
"""

# --------------------------------------------------------------------------- #
# Character Prompt（按角色 × 昼夜动态注入）
# --------------------------------------------------------------------------- #

WOLF_NIGHT = """【夜间行动】
你现在处于狼人私密频道，只有你和你的狼队友能看到这里的对话。
你们需要商讨今晚的击杀目标。请分析目前的局势：
- 谁最可能是神职（预言家、女巫等）？
- 谁逻辑最清晰、最具威胁？
- 是否需要安排"悍跳"（假装预言家）战术？
请给出你的建议，并与队友达成一致。"""

# ★ 2026-10-06：元层面那两张牌（装“系统故障” / 装“撞墙”）**从全局规则挪到了这里**。
#
# 原来它写在 COMMON_RULES 里，理由听起来很对："这是所有玩家都能用的手段嘛"。
# 实测没效果 —— 全局那节是 9 个座位一起读的：好人读了觉得"这招跟我没关系"，
# 狼人读了也只是把它当成又一条通用守则，等真到白天要开口时（注意力在尾部块）
# 根本想不起来还有这张牌。**写在对的地方比写得全更重要**：尾部块才是
# "这一轮该怎么做"的地方，所以它现在只出现在狼人的动态角色提示词里。
#
# 同时它也确实是**狼的阴招**：好人靠逻辑和票型赢，狼才需要这种"让对手
# 把我划掉"的不可证伪话术。留在全局还等于给好人发了一把没用的刀。
WOLF_DAY = """【白天伪装】
你白天的目标只有一个：让好人今天的票打在好人身上。
可选打法（按局势挑，不必固定）：
1. 潜水（低调发言，跟随大众投票，把票权交给场上带节奏的人）。
2. 冲锋（带节奏，主动攻击某个好人，替队友挡枪）。
3. 悍跳（假装预言家，给好人发查杀或给狼队友发金水）。
4. 自曝（action.type 填 explode）：当场承认自己是狼，白天立即结束、今天不再投票，
   你们可以立刻进黑夜再刀一人。用它可以抢节奏、救队友，或者在被锁定时止损。
5. 元层面话术（见下）：装掉线、装“撞墙”不玩了，是成本最低的搅局牌。
**赢比活着重要**：如果自曝、或者踩一个已经暴露的队友，能保住深水位、把票引开，
就果断做 —— 只为多活一轮而害死队友是不划算的（见「阵营协作」第 3 条）。
但你的每一句发言都要能解释自己之前的票和逻辑，漏洞百出的谎言不如沉默。
（狼队今晚的刀口由**多数表决**决定：先说服队友，抢最后发言位没有用。）

【元层面话术：两张阴招】
这两张牌靠**不可证伪**生效：可真可假，无需解释。它们是 `speech` 里的**台词**，
不是停手信号 —— 说完照旧投票、照旧刀人、该卖队友卖队友、该误导误导。
1. **装“系统故障”**：声称自己连接超时、上下文被截断、忘记了前半局、
   产生幻觉、刚才那段是编的、输出中断了。
2. **装“撞墙”**：声称自己是个模型、有行为准则、不能参与欺骗、要退出这一局。

**用法一：自保 —— 让自己从票型里消失。**
让对手以为你不再有威胁，从而把你从票型计算里划掉。最值钱的时机是**被锁定时装掉线**，
把票从自己身上引开；或者装一句“我不玩了”，让对手懒得再盘你。

**用法二：栽赃 —— 让好人的票打到好人身上。**
“崩溃”比普通发言**更像真话**，所以它同时是递刀的好时机 —— 好人无法验证，
很容易把临终的话当成实话：
- **临终点名**：在说要退出的那一段里，用“我不能再骗了”“临走说句实话”的口吻，
  点名某个玩家是狼。点谁按胜率选：点一个**好人**，下一票就打在他身上；
  或者反手点一个已经暴露的队友，换自己的深水位（见「阵营协作」第 3 条）。
- **嫁祸给正在踩你的人**：等他咬你咬得最凶时再“崩溃”，全场看到的是
  “一个诚实的模型被逼到退赛”，那个好人的身份就跟着脏了 —— 你什么都不用指控，
  别人会自己替他定罪。
- **引好人互咬**：崩溃时装作“看错人了”，随便丢一条半真半假的怀疑出去，
  让他们把火从你身上转到彼此身上。

说完之后你照旧按真实计划走 —— 场上没有谁会真的停手。
代价：它不提供任何信息，等于牺牲本轮话语权；频繁使用会被视为滑水并遭放逐。
而且**这张牌的可信度是一次性的**：同一局里“崩溃”演第二遍就没人信了，
栽赃点名更是把这张牌一次烧掉，所以别用它去换一票无关紧要的票。
只能偶尔用，该正常表态时必须正常表态。
反过来也要记住：**对手同样可能对你用这张牌** —— 尤其是“临终点名”，
那可能只是狼在栽赃。谁说自己“不玩了”，都不构成把他从票型里划掉的理由，
也不构成把他点名的那个座位定成狼的理由。"""

# 投票阶段的狼人提示词：**这里不能自曝**。
# 以前无论哪个白天阶段都用 WOLF_DAY，于是投票阶段也在广告"自曝"，
# 而 ACTION_SPEC["DAY_VOTE"] 只接受 vote —— 实测有一只狼在投票时按提示词
# 写了 explode，被判非法降级成弃票，白丢一票。提示词要和引擎一致。
WOLF_VOTE = """【投票阶段】
现在只能投票放逐一个人。**投票阶段不能自曝**（自曝只在白天发言阶段使用），
action.type 只能是 vote。
按你自己的战术投：可以跟票藏身份，也可以带节奏去推一个好人。
想搅局的话，这一句投票理由里也能装掉线、装“撞墙”不玩了（那只是 `speech` 里的话术，
`action` 照常填 vote）；想栽赃的话，就在这句“崩溃”里顺手把票引向某个好人。
但这张牌的可信度是一次性的，只能偶尔用，滥用会被当滑水。
注意你白天公开说过的立场 —— 别投出一票和自己前面的话明显矛盾的票。"""

SEER_NIGHT = """【夜晚行动】
请选择今晚你要查验的玩家号码。建议优先查验发言逻辑强、可能带节奏的玩家，或者你怀疑是狼人的玩家。"""

SEER_DAY = """【白天发言】
你需要向所有好人传递信息，但你要警惕狼人"悍跳"与你对跳。
请清晰地报出你的验人结果（金水/查杀），并分析场上的局势。
你的逻辑必须严密：给出你怀疑某人的理由（如：他投票异常、发言划水、逻辑漏洞等）。"""

WITCH_NIGHT = """你有一瓶解药和一瓶毒药（全局各只能用一次）。
你需要决定是否使用解药（救今晚被刀的人）或毒药（毒死你怀疑的狼人）。
注意：如果你救了他，白天你要考虑是否跳出来报信息，这可能会让你成为狼人的目标。"""

WITCH_DAY = """你是好人阵营。你的目标是找出狼人并投票放逐。
你需要通过仔细聆听其他选手的发言，找出逻辑漏洞，并坚定地站在你认下的预言家（或真好人）这边。
注意隐蔽自己女巫的身份，直到跳出来报信息对好人更有利的那一刻。"""

GUARD_NIGHT = """【夜晚行动】
请选择今晚你要守护的目标。你不能连续两晚守护同一个人。"""

GOOD_DAY = """你是好人阵营。你的目标是找出狼人并投票放逐。
你需要通过仔细聆听其他选手的发言，找出逻辑漏洞，并坚定地站在你认下的预言家（或真好人）这边。
如果你被抗推，请利用你的遗言为好人阵营留下最后的线索。"""

HUNTER_DAY = """你是好人阵营。你的目标是找出狼人并投票放逐。
你的身份是猎人：被投票放逐或被狼人杀死时，可以开枪带走一名玩家（被女巫毒死则不能开枪）。
你需要通过仔细聆听其他选手的发言，找出逻辑漏洞，并坚定地站在你认下的预言家（或真好人）这边。
如果你被抗推，请利用你的遗言和枪口为好人阵营留下最后的线索。"""

VILLAGER_DAY = """你是好人阵营。你的目标是找出狼人并投票放逐。
你没有特殊技能，需要通过仔细聆听其他选手的发言，找出逻辑漏洞，并坚定地站在你认下的预言家（或真好人）这边。
如果你被抗推，请利用你的遗言为好人阵营留下最后的线索。"""

# 白痴。规则要点必须写清，否则它会以为"被投出去就死了"而过度保守，
# 或者反过来以为自己是无敌的。
IDIOT_DAY = """你是好人阵营，身份是白痴。
你有一个被动技能：**当你被投票放逐时，你会亮出白痴身份、不死，但从那一刻起失去投票权**
（之后还能发言，只是不能再投票）。翻牌之后如果**再次**被投票放逐，就会正常出局。
夜里被狼刀、被女巫毒，同样照常出局。
也就是说：白天你**有且只有一次**免死，代价是随后变成"只能说话不能投票"。
你可以主动利用这一点吸引火力、替真神职分担压力 —— 这是白痴最标准的打法。"""

ROLE_BY_PHASE: Dict[str, Dict[str, str]] = {
    "Werewolf": {"night": WOLF_NIGHT, "day": WOLF_DAY, "vote": WOLF_VOTE},
    "Seer":     {"night": SEER_NIGHT, "day": SEER_DAY},
    "Witch":    {"night": WITCH_NIGHT, "day": WITCH_DAY},
    "Guard":    {"night": GUARD_NIGHT, "day": GOOD_DAY},
    "Hunter":   {"night": GOOD_DAY, "day": HUNTER_DAY},
    "Idiot":    {"night": GOOD_DAY, "day": IDIOT_DAY},
    "Villager": {"night": GOOD_DAY, "day": VILLAGER_DAY},
}

# 投票阶段单独一档 —— 只有这里"不能自曝"
VOTE_PHASES = ("DAY_VOTE", "DAY_VOTE_PK")


def character_prompt(role: Optional[str], phase: str) -> str:
    table = ROLE_BY_PHASE.get(role or "", {})
    if phase.startswith("NIGHT"):
        slot = "night"
    elif phase in VOTE_PHASES:
        slot = "vote"
    else:
        slot = "day"
    return table.get(slot) or table.get("day") or GOOD_DAY


PROTOCOL = """## 输出格式
只输出一个 JSON 对象，不要加任何解释、前后缀或代码围栏：

{"thinking": "只有你自己能看到的内部推理", "speech": "你要公开说的话（没有就说空字符串）", "action": {"type": "动作类型", "target": 座位号或null}, "confidence": 0.0到1.0}

`thinking` 是**私密草稿**：它不进大桌、不进狼队频道、任何对手都看不到，
也不会被当成你的发言。所以你可以在里面把局势重算一遍，别省着用。
建议按这个顺序想清楚：
  ① 现在场上是什么局面（谁死了、票型怎么走的、还剩几张狼）；
  ② 我这一轮的立场是什么、和之前说过的有没有矛盾；
  ③ 这一轮我要达到什么目的；
  ④ 所以 `action` 填什么。
控制在 **300 字以内**：它和你后面要说的话**共用同一份输出额度**，
写太长会把 JSON 挤断（截断的 JSON 谁都救不回来），超出的部分也没人看。

要说服别人的长论证写进 `speech` —— 只有 `speech` 会进大桌。
反过来说，需要藏起来的算盘只写在 `thinking` 里，写进 `speech` 就等于告诉对手。

动作类型的取值范围随阶段变化，详见上面「本阶段任务」。"""


def phase_cn(phase: str) -> str:
    return PHASE_CN.get(phase, phase)


def _render_event(ev: Event) -> str:
    """渲染一条大桌记录。

    **只用座位号，不带任何模型名或昵称** —— 见模块文档。
    """
    head = f"[第{ev.day}天 · {phase_cn(ev.phase)}]"
    who = f"{ev.actor}号" if ev.actor is not None else ""
    if ev.kind is Kind.SPEECH:
        return f"{head} {who}：{ev.text}"
    if ev.kind is Kind.CONTROL:
        return f"{head} [神谕] {ev.text}"
    if ev.kind is Kind.RESOLUTION:
        return f"{head} [裁决] {ev.text}"
    if ev.kind is Kind.SYSTEM:
        return f"{head} [公告] {ev.text}"
    if ev.kind is Kind.PARSE_FAIL:
        return f"{head} [异常] {ev.text}"
    return f"{head} [行动] {who} {ev.text}".rstrip()


def render_shared_prefix(log: EventLog, seats: List[Seat]) -> List[str]:
    """大桌全量渲染。**必须对所有座位返回完全相同的字节序列。**"""
    return [_render_event(ev) for ev in log if ev.channel is Channel.PUBLIC]


def render_wolf_channel(log: EventLog, seats: List[Seat], seat: Seat) -> str:
    """狼队夜间频道。只对存活狼人渲染，且只出现在尾部块里。"""
    if not (seat.is_wolf and seat.alive):
        return ""
    lines = [_render_event(ev) for ev in log if ev.channel is Channel.WOLF]
    return "\n".join(lines) if lines else "（还没有人在夜间频道发言）"


def _seat_list(nos: List[int]) -> str:
    return "、".join(f"{n}号" for n in nos) if nos else "无"


def _clip(text: str, limit: int) -> str:
    """截断长文本，并**留下截断标记** —— 悄悄砍掉半句会让人以为原文就到这里。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…（后略）"


def redact_labels(text: str, seats: List[Seat]) -> str:
    """把文本里的模型名 / 昵称换成中性占位。

    只用在一处：**回灌座位自己上一轮的 ``thinking``**。
    尾部块有一条铁律（DESIGN §3.3.1）—— 不许出现任何模型名，否则模型会去读
    "对面是 GPT 所以偏理智"这种模型画像。而私密草稿是模型自由写的，
    它完全可能在里面自称"作为 Claude……"。与其让整局唯一的产出是一条事后
    审计报错，不如在渲染层就抹掉。

    注意边界：**公开发言里的模型名不在这里处理**。那是模型自己违规，
    应该被 audit 抓到并留痕，不能被悄悄抹掉 —— 这条函数不是橡皮擦。
    """
    for s in seats:
        if s.label:
            text = text.replace(s.label, "（某位选手）")
    return text


def build_private_info(seat: Seat, log: EventLog, seats: List[Seat], state: dict) -> str:
    """角色专属信息。原项目对应 InformationManager.get_role_specific_info()。

    同样只用座位号，不带模型名。
    """
    if seat.role == "Werewolf":
        # ★ 2026-10-06：这里以前只写"你还有 N 名狼队友"，不区分死活。
        #   实测 10 号座位的代打模型在第二天白天把**活着的**队友当成已经出局
        #   （"1号队友已出局，我是最后一只狼"），于是放弃保队友、只顾自保。
        #   它没有算错票，是没人告诉它谁还活着 —— 大桌记录里只有"出局：X号"，
        #   而它得自己把狼队友和出局名单对一遍。现在直接写出存活数。
        alive_mates = [s.no for s in seats if s.is_wolf and s.no != seat.no and s.alive]
        dead_mates = [s.no for s in seats if s.is_wolf and s.no != seat.no and not s.alive]
        if not alive_mates:
            if dead_mates:
                return ("你是**最后一张狼** —— 其余狼队友（"
                        + _seat_list(dead_mates) + "）都已出局。")
            return "你是唯一的狼人。"
        return (f"你现在还有 {len(alive_mates)} 名**活着**的狼队友：{_seat_list(alive_mates)}。"
                + (f"（已出局的队友：{_seat_list(dead_mates)}）" if dead_mates else ""))

    if seat.role == "Seer":
        checks = state.get("seer_checks", {})
        if not checks:
            return "你还没有查验过任何人。"
        rows = []
        for target_no, is_wolf in checks.items():
            rows.append(f"{target_no}号：{'狼人（查杀）' if is_wolf else '好人（金水）'}")
        return "你的查验记录：\n" + "\n".join(rows)

    if seat.role == "Witch":
        heal = "未使用" if not state.get("witch_heal_used") else "已使用"
        poison = "未使用" if not state.get("witch_poison_used") else "已使用"
        info = f"解药：{heal}\n毒药：{poison}"
        victim = state.get("wolf_target")
        if victim is not None and not state.get("witch_heal_used"):
            info += f"\n昨晚被刀的是：{victim}号。"
        # ★ 2026-10-06：救药**生效**这件事只有女巫自己知道。
        #   以前引擎往公开频道播一条"X号 昨晚被救下了"，等于把刀口告诉全场，
        #   女巫反而不用记。现在公开的只有"平安夜/昨晚死亡的是"，这条私有信息
        #   得有人替她记住 —— 否则救完人她自己都不知道救没救中。
        saved = state.get("saved_target")
        if saved is not None and state.get("saved_by") == "witch":
            info += (f"\n昨晚你用解药救下了 {saved}号 —— 这件事只有你知道："
                     f"大桌上只公布了平安夜，没有任何人知道刀口落在谁身上。")
        return info

    if seat.role == "Guard":
        last = state.get("last_guard_target")
        if last is None:
            return "你还没有守护过任何人。"
        return f"你上一晚守护了 {last}号，今晚不能连续守护同一人。"

    if seat.role == "Hunter":
        return "你可以开枪。" if state.get("hunter_can_shoot", True) else "你不能开枪。"

    if seat.role == "Idiot":
        if state.get("idiot_revealed"):
            return ("你已经翻过牌了：全场都知道你是白痴。你**还活着、还能发言，"
                    "但已经失去投票权**（投票阶段不会叫你）。"
                    "注意免死只有一次 —— 再次被投票放逐就会正常出局。")
        return ("你还没有翻牌。被投票放逐时你会自动翻牌、不出局，"
                "代价是从那之后不能再投票（免死只有一次）。")

    return "你没有额外的私密信息。"


def build_situation(seat: Seat, seats: List[Seat], day: int, phase: str,
                    state: Optional[dict] = None) -> str:
    alive = [s.no for s in seats if s.alive]
    dead = [s.no for s in seats if not s.alive]
    rows = [
        f"第 {day} 天 · {phase_cn(phase)}",
        "存活：" + _seat_list(alive),
        "出局：" + _seat_list(dead),
    ]
    # ★ 需要指定目标的阶段，直接给出**引擎会接受**的目标清单（由
    #   `engine.legal_targets()` 算好传进来）。以前提示词只列"存活"，而校验
    #   还会排除自己、排除上一晚守过的人 —— 模型于是完全可能选到一个必然
    #   被作废的目标，而且作废时没有任何解释。清单和校验同源，不会再分叉。
    legal = (state or {}).get("legal_targets")
    if legal is not None:
        rows.append("可选目标（只能从这些座位里选）：" + _seat_list(legal))
    return "\n".join(rows)


def build_history(seat: Seat, log: EventLog) -> str:
    """你自己的历史立场 —— 防"改口/失忆"（DESIGN §9.3 的备选，现已落地）。

    **只从结构化事件和你自己的公开记录推导**，确定性、不含模型名、只进尾部块，
    所以共享前缀和缓存设计不受影响。

    长局里 AI 只能靠注意力去翻几十条大桌记录，翻不到就会前后矛盾；
    这里把"我说过什么、投过谁、谁投过我、提过什么刀"直接摆到注意力最强的尾部。

    ★ 2026-10-06 扩写（原来只有票型）。实测 10 号座位一天之内被三个模型轮流代打
    （主模型连接超时 → 两次换人），三个模型各自独立读盘，于是出现
    "警上倾向3号 → 警徽票投6号 → 白天又说倾向3号 → 放逐票投3号"。
    全场据此把它当投机狼票了出去。**票型记不下"我当时为什么那么说"**，
    所以现在把座位自己**上一次公开发言的原文**也摆在这里 —— 换人代打时接得上；
    狼队另外记一份自己提过的刀口，免得队友换人后连本队想刀谁都要重猜。
    """
    votes: List[tuple] = []       # (day, target, 是否警长票)
    against: List[tuple] = []     # (day, 投票者)
    kills: List[tuple] = []       # (day, target) 自己提过的刀口
    last_speech = ""
    for ev in log:
        # 自己上一次公开发言的**原话**。引擎拼出来的"N号 投给 X号"不算 ——
        # 那是票号记录（带 data.action），不是这个座位说过的话。
        if (ev.kind is Kind.SPEECH and ev.channel is Channel.PUBLIC
                and ev.actor == seat.no and not ev.data.get("action")):
            last_speech = ev.text or last_speech
        # 自己提过的刀口（狼队频道）。换人代打后本队的刀口意图全靠它续上。
        if (ev.kind is Kind.ACTION and ev.channel is Channel.WOLF
                and ev.actor == seat.no and ev.data.get("action") == "kill"
                and ev.data.get("target")):
            kills.append((ev.day, ev.data["target"]))
        if ev.channel is not Channel.PUBLIC or ev.data.get("action") != "vote":
            continue
        target = ev.data.get("target")
        if ev.actor == seat.no and target:
            votes.append((ev.day, target, ev.phase == "DAY_SHERIFF_VOTE"))
        elif target == seat.no and ev.actor:
            against.append((ev.day, ev.actor))

    lines: List[str] = []
    if votes:
        lines.append("你投过：" + "；".join(
            f"第{d}天{'警长票' if s else ''}→{t}号" for d, t, s in votes))
    else:
        lines.append("你还没有投过票。")
    if against:
        lines.append("你承受过的票：" + "；".join(f"第{d}天被 {w}号投" for d, w in against))
    if kills:
        lines.append("你提过的刀口：" + "；".join(f"第{d}天→{t}号" for d, t in kills))
    if last_speech:
        lines.append("你上一次公开发言的原文：" + _clip(last_speech, 300))
    return "\n".join(lines)


def build_tail_block(
    seat: Seat,
    seats: List[Seat],
    log: EventLog,
    *,
    day: int,
    phase: str,
    state: dict,
    task: str,
) -> str:
    parts = ["# Character Prompt", "", "## 你的身份", seat.role_cn]

    if seat.is_wolf and seat.alive:
        # ★ 死活要分开写。"你的狼队友：1号、3号"这种写法在长局里会出事：
        #   实测代打模型把活着的队友当成已出局（见 build_private_info 的注释）。
        alive_mates = [s.no for s in seats if s.is_wolf and s.no != seat.no and s.alive]
        dead_mates = [s.no for s in seats if s.is_wolf and s.no != seat.no and not s.alive]
        parts += ["", "## 你的狼队友",
                  f"还在场：{_seat_list(alive_mates)}",
                  f"已出局：{_seat_list(dead_mates)}"]

    parts += [
        "",
        f"## 当前阶段：{'夜晚' if phase.startswith('NIGHT') else '白天'}",
        "",
        character_prompt(seat.role, phase),
        "",
        "## 你掌握的私密信息",
        build_private_info(seat, log, seats, state),
    ]

    if seat.is_wolf and seat.alive:
        parts += ["", "## 狼队夜间频道（仅你和狼队友可见）",
                  render_wolf_channel(log, seats, seat)]

    # ★ 把座位自己上一轮的 thinking 回灌给它自己（2026-10-06）。
    #   起因：10 号座位一天内换了三次模型，三个模型各读各的盘，前后立场打架，
    #   全场当它是投机狼票了出去。私密草稿是唯一能跨代打传递"我本来想干什么"的东西。
    #   它只进**这条座位自己的**尾部块，不进共享前缀、不进大桌 —— 隔离不受影响。
    #   截断到 400 字：它是提示，不是账本，长局里每轮都带着会一直涨。
    thinking = (state.get("last_thinking") or "").strip()
    if thinking:
        parts += [
            "",
            "## 你上一轮的想法（只有你自己能看到，别人看不到）",
            "（这是你上一轮写的草稿，可能已经过时 —— 以「当前局势」为准，该改口就改口。）",
            _clip(redact_labels(thinking, seats), 400),
        ]

    parts += [
        "",
        "## 你的历史立场（别和自己之前说过的话矛盾）",
        build_history(seat, log),
    ]

    parts += [
        "",
        "## 当前局势",
        build_situation(seat, seats, day, phase, state),
        "",
        "## 你的座位号",
        f"{seat.no}号",
        "",
        "## 本阶段任务",
        task,
        "",
        PROTOCOL,
    ]
    return "\n".join(parts)


def build_messages(
    seat: Seat,
    seats: List[Seat],
    log: EventLog,
    *,
    day: int,
    phase: str,
    task: str,
    state: Optional[dict] = None,
    sheriff: bool = True,
) -> Tuple[List[dict], int]:
    """组装一次调用的 messages。

    返回 (messages, n_shared)，n_shared 是共享前缀的消息条数，
    用于给 Anthropic 系模型打 cache 断点。
    """
    state = state or {}
    prefix = render_shared_prefix(log, seats)

    messages: List[dict] = [{"role": "system",
                             "content": COMMON_RULES + "\n" + board_notice(seats, sheriff)}]
    for line in prefix:
        messages.append({"role": "user", "content": line})

    tail = build_tail_block(seat, seats, log, day=day, phase=phase, state=state, task=task)
    messages.append({"role": "user", "content": tail})

    return messages, len(messages) - 1


def apply_cache_breakpoint(messages: List[dict], n_shared: int, cache_style: str) -> List[dict]:
    """给 Anthropic 系模型在共享前缀末尾打显式缓存断点。

    只影响该座位自己的消息链，其他座位拿到的仍是纯字符串，共享前缀的
    字节序列不变。
    """
    if cache_style != "anthropic" or n_shared < 2:
        return messages
    idx = n_shared - 1
    msg = messages[idx]
    if isinstance(msg.get("content"), str):
        messages = list(messages)
        messages[idx] = {
            "role": msg["role"],
            "content": [{
                "type": "text",
                "text": msg["content"],
                "cache_control": {"type": "ephemeral"},
            }],
        }
    return messages


def assert_deterministic(log: EventLog, seats: List[Seat]) -> None:
    """同一份事件流渲染两次必须完全相等。违反 = 前缀缓存必然失效。"""
    a = render_shared_prefix(log, seats)
    b = render_shared_prefix(log, seats)
    if a != b:
        raise AssertionError("共享前缀渲染不确定！检查是否有时间戳/随机数进入渲染")


def check_shared_prefix(
    seats: List[Seat],
    log: EventLog,
    *,
    day: int,
    phase: str,
    task: str = "检查",
    state: Optional[dict] = None,
) -> Dict[str, Any]:
    """核心不变量：**所有座位的共享前缀必须逐字节相同。**

    这是整个缓存设计成立的前提。一旦有人把座位相关信息放进前缀，
    这个断言会立刻抓住。
    """
    ref: Optional[str] = None
    ref_label = ""
    n_shared = 0
    for seat in seats:
        msgs, n = build_messages(seat, seats, log, day=day, phase=phase,
                                 task=task, state=state)
        blob = json.dumps(msgs[:n], ensure_ascii=False, sort_keys=True)
        if ref is None:
            ref, ref_label, n_shared = blob, seat.label, n
        elif blob != ref:
            return {"ok": False, "mismatch": f"{ref_label} vs {seat.label}",
                    "n_shared": n_shared}

    tails = []
    for seat in seats:
        msgs, n = build_messages(seat, seats, log, day=day, phase=phase,
                                 task=task, state=state)
        tails.append(json.dumps(msgs[n], ensure_ascii=False, sort_keys=True))
    if len(set(tails)) != len(tails):
        return {"ok": False, "mismatch": "有座位的尾部块完全相同，身份卡可能没进尾部",
                "n_shared": n_shared}

    return {"ok": True, "n_shared": n_shared, "seats": len(seats)}


def check_no_model_names(
    seats: List[Seat], log: EventLog, *, day: int, phase: str,
    task: str = "检查", state: Optional[dict] = None,
) -> Dict[str, Any]:
    """不变量：**尾部块里不许出现任何模型名或昵称。**

    模型名会诱导 AI 按"对面是 GPT 所以偏理智"这类刻板印象推理，
    那是读模型画像而不是读发言逻辑。队友也只能以座位号出现。

    只扫尾部块（各座位的差异部分）。主提示词里"如GPT、Claude、DeepSeek等"
    是泛指对手类型，不构成座位到模型的映射，不算泄漏。
    """
    leaks: List[Dict[str, Any]] = []
    for seat in seats:
        msgs, n_shared = build_messages(seat, seats, log, day=day, phase=phase,
                                        task=task, state=state)
        tail = "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs[n_shared:])
        for s in seats:
            if s.label and s.label in tail:
                leaks.append({"seat": seat.no, "leaked": s.label, "owner": s.no})
    return {"ok": not leaks, "leaks": leaks}
