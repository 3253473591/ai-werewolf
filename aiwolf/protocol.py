"""双通道输出协议与解析降级链。对应 DESIGN.md 第 4 节。

解析链：

    1. json.loads（先脱代码围栏）
    2. 正则抠第一个平衡的 {...}
    3. 从自由文本里提取座位号
    4. 全失败 -> 返回 Decision(ok=False)，由调用方记 PARSE_FAIL 事件

第 4 级**绝不能静默**：必须留痕，否则你不知道哪几局的分析建立在意外的随机动作上。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 各阶段允许的动作类型；target 是否必填
ACTION_SPEC: Dict[str, Dict[str, Any]] = {
    "DAY_SPEECH":     {"types": ["speak", "explode"], "needs_target": False},
    "DAY_VOTE":       {"types": ["vote"],       "needs_target": True},
    # 平票 PK：先台上发言，再由台外玩家重投（只能投台上的人）
    "DAY_PK_SPEECH":  {"types": ["speak"],      "needs_target": False},
    "DAY_VOTE_PK":    {"types": ["vote"],       "needs_target": True},
    "DAY_LAST_WORDS": {"types": ["speak"],      "needs_target": False},
    "DAY_HUNTER":     {"types": ["shoot", "pass"], "needs_target": False},
    "NIGHT_WOLF":     {"types": ["kill"],       "needs_target": True},
    "NIGHT_WOLF_CHAT": {"types": ["chat"],      "needs_target": False},
    "NIGHT_SEER":     {"types": ["check"],      "needs_target": True},
    "NIGHT_WITCH":    {"types": ["save", "poison", "pass"], "needs_target": False},
    "NIGHT_GUARD":    {"types": ["guard"],      "needs_target": True},
    # 警长（2026-10-06 上线）
    "DAY_SHERIFF_RUN":   {"types": ["run", "pass"], "needs_target": False},
    "DAY_SHERIFF_VOTE":  {"types": ["vote"],       "needs_target": True},
    "DAY_SHERIFF_PK":    {"types": ["speak"],      "needs_target": False},
    # 警长每天决定从警左还是警右开始发言（他自己最后发言）
    "DAY_SHERIFF_ORDER": {"types": ["order_left", "order_right"],
                          "needs_target": False},
    # 警长出局时移交警徽（target=继承人）或撕掉（pass）
    "DAY_BADGE":         {"types": ["badge", "pass"], "needs_target": True},
}

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

# 思维链标签。**只剥成对标签，不"从头砍到尾巴"** —— 有些模型正文里会正常用到
# 这个写法，一刀切会把整段话切没。（同一个正则，reflect.clean_reflection 也用）
#
# ★ 2026-10-11：这是解析链上**必须**的第一步，不是洁癖。实测 8号 MiniMax（ep10）
# 把思维链**内联在 content 里**（11/11 次调用都是 `<think>…</think>\n\n{json}`），
# 而草稿里往往先写一份**完整的 JSON 模板**，例如：
#
#     <think>… I could write {"thinking": "…", "speech": "...", "action": {...}} …</think>
#     {"thinking": "…", "speech": "12号两天独投6号…投票12号。", "action": {"type": "vote", …}}
#
# 不剥的话三连崩：整段解析在第 0 个字符就失败（"<"）；括号恢复抓到草稿里的
# 集合写法 `{1,12}`（不是 JSON）；按字段抢救的正则在**整段**上匹配，先命中的是
# 草稿里的 `"speech": "..."` —— 于是 8号第 4 天的投票理由被公开发成了 `...`
# （三个点），全桌都看见了。剥掉思维链后，答案 JSON 本身是完全合法的，零 issues。
THINK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>",
                      re.IGNORECASE | re.DOTALL)

# 动作类型的同义词。模型经常不按约定的英文枚举回答 —— 实测 Claude 会直接返回
# {"type": "击杀", "target": 2}。识别不出来就会被降级成 pass，
# 狼的刀、预言家的验人就凭空丢了（而且不留明显痕迹）。
ACTION_ALIAS: Dict[str, List[str]] = {
    "vote":   ["vote", "voting", "ballot", "放逐", "投票", "投", "票"],
    "kill":   ["kill", "attack", "murder", "击杀", "刀", "杀", "袭击"],
    "check":  ["check", "verify", "inspect", "查验", "验", "查"],
    "guard":  ["guard", "protect", "守护", "守", "保护"],
    "save":   ["save", "heal", "rescue", "解药", "救"],
    "poison": ["poison", "毒杀", "下毒", "毒"],
    "shoot":  ["shoot", "shot", "开枪", "带走", "枪"],
    "explode": ["explode", "self_destruct", "selfdestruct", "自曝", "自爆", "爆狼", "认狼"],
    "speak":  ["speak", "speech", "say", "talk", "发言", "说"],
    "chat":   ["chat", "talk", "讨论", "商量", "发言"],
    "pass":   ["pass", "skip", "none", "不行动", "弃权", "放弃", "过"],
    # 警长相关。
    # ⚠️ 顺序注意：「警左」必须比「左」先匹配，所以别名表按长度倒序排（见下），
    #    这里只管把同义词列全。
    "run":    ["run", "candidate", "campaign", "上警", "竞选", "参选", "举手"],
    "order_left":  ["order_left", "left", "clockwise", "警左", "向左", "顺时针"],
    "order_right": ["order_right", "right", "counterclockwise", "counter_clockwise",
                    "警右", "向右", "逆时针"],
    "badge":  ["badge", "transfer", "移交警徽", "警徽移交", "移交", "传警徽"],
}

# 按模式长度倒序，优先匹配更具体的（"毒杀" 要胜过 "杀"）
_ALIAS_FLAT: List[tuple] = sorted(
    ((pat, canon) for canon, pats in ACTION_ALIAS.items() for pat in pats),
    key=lambda x: -len(x[0]),
)


def _canonical_action(raw_type: str) -> Optional[str]:
    t = (raw_type or "").strip().lower()
    if not t:
        return None
    for pat, canon in _ALIAS_FLAT:
        if t == pat:
            return canon
    for pat, canon in _ALIAS_FLAT:
        if pat in t:
            return canon
    return None


@dataclass
class Action:
    type: str = "pass"
    target: Optional[int] = None


@dataclass
class Decision:
    thinking: str = ""
    speech: str = ""
    action: Action = field(default_factory=Action)
    confidence: float = 0.5
    raw: str = ""
    issues: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues or "unparsed" not in self.issues


def _strip_fences(text: str) -> str:
    m = _FENCE.search(text)
    return m.group(1).strip() if m else text.strip()


def _first_balanced_object(text: str) -> Optional[str]:
    """找第一个括号平衡的 {...}，能容忍嵌套与字符串里的括号。"""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def _as_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"\d+", v)
        if m:
            return int(m.group(0))
    return None


_KNOWN_KEYS = ("thinking", "speech", "action", "confidence")
_KEYS_RE = "|".join(_KNOWN_KEYS)


def _extract_string_field(text: str, key: str) -> Optional[str]:
    """从（可能非法的）JSON 文本里抠出字符串字段。

    **关键：用「下一个已知字段名」当结束锚点，而不是「下一个引号」。**

    模型经常在发言里写未转义的引号 —— 实测 Claude 写了
    ``非常"完美"——先验5号…``，整段 JSON 就非法了。原来的实现按引号切，
    发言被腰斩成半句（"…警徽流设计得非常"），**症状看起来像 max_tokens 截断，
    其实是解析切错**。按字段名锚定就不会。
    """
    # 到下一个已知字段名为止（正常与非法 JSON 都能用）
    m = re.search(rf'"{key}"\s*:\s*"(.*?)"\s*,\s*"(?:{_KEYS_RE})"\s*:',
                  text, re.DOTALL)
    if m:
        return m.group(1)
    # 该字段是最后一个 / 输出真被截断：值一直到文末
    m = re.search(rf'"{key}"\s*:\s*"(.*)$', text, re.DOTALL)
    if m:
        s = re.split(rf'"\s*,\s*"(?:{_KEYS_RE})"\s*:', m.group(1))[0]
        return s.rstrip().rstrip('"').rstrip()
    return None


def _salvage_truncated(text: str) -> Dict[str, Any]:
    """从非法 JSON 里按字段抢救。能救多少救多少，绝不整段丢掉。"""
    out: Dict[str, Any] = {}

    speech = _extract_string_field(text, "speech")
    if speech is not None:
        # 抢救出来的是原文，可能还带着字面量转义
        out["speech"] = speech.replace('\\"', '"').replace("\\n", "\n")

    thinking = _extract_string_field(text, "thinking")
    if thinking is not None:
        out["thinking"] = thinking

    # action 是个对象，用括号配平扫，比正则可靠
    m = re.search(r'"action"\s*:\s*', text)
    if m:
        chunk = _first_balanced_object(text[m.end():])
        if chunk:
            try:
                act = json.loads(chunk)
                if isinstance(act, dict):
                    out["type"] = act.get("type")
                    out["target"] = act.get("target")
            except (json.JSONDecodeError, ValueError):
                pass
        if "type" not in out:
            # action 也被截断了：退回到按引号切（此时确实没有更好的锚点）
            tail = text[m.end():]
            mt = re.search(r'"type"\s*:\s*"([A-Za-z_\u4e00-\u9fff]+)"', tail)
            if mt:
                out["type"] = mt.group(1)
            mn = re.search(r'"target"\s*:\s*(\d+)', tail)
            if mn:
                out["target"] = int(mn.group(1))

    mc = re.search(r'"confidence"\s*:\s*([0-9.]+)', text)
    if mc:
        try:
            out["confidence"] = float(mc.group(1))
        except ValueError:
            pass

    return out


def _iter_balanced_objects(text: str):
    """按出现顺序枚举文本里的平衡 ``{...}``。"""
    pos = 0
    while pos < len(text):
        start = text.find("{", pos)
        if start == -1:
            return
        chunk = _first_balanced_object(text[start:])
        if not chunk:
            return
        yield chunk
        pos = start + len(chunk)


def _pick_answer_object(text: str) -> Optional[dict]:
    """从（可能带前置说明的）文本里挑出**答案对象**。

    为什么不能"取第一个平衡块"：模型经常在 JSON 前面先写一段自然语言
    （思维链、或 "Let me analyze…"），而那段话里几乎必然出现花括号 ——
    "one of {6号, 11号}"、"剩1狼在{1,12}"。取第一个就命中这些集合写法，
    报 `Expecting property name enclosed in double quotes`，
    于是明明完整的答案被当成非法 JSON，掉进按字段抢救。

    判据是**字段名**而不是位置：只有含 thinking/speech/action/confidence
    之一的对象才算答案。这一条同时挡住另一种坑 —— 抢救前先在文本里遇到的
    `{"type": "vote", "target": 12}` 这种 action 子对象：它也不是答案，
    被当成 payload 会把 speech 丢光。
    """
    for chunk in _iter_balanced_objects(text):
        try:
            cand = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(cand, dict) and any(k in cand for k in _KNOWN_KEYS):
            return cand
    return None


def parse_decision(raw: str, phase: str = "") -> Decision:
    if raw is None:
        raw = ""
    d = Decision(raw=raw)
    text = _strip_fences(raw)

    # ★ 第一步就是剥思维链（见 THINK_RE 的说明）。
    #   剥完为空说明"整段都是草稿"（个别网关把答案塞在标签里），那就退回原文 ——
    #   那种情况下只有草稿可救，有总比没有强。
    thinkless = THINK_RE.sub("", text).strip()
    text = thinkless or text

    payload: Optional[dict] = None
    try:
        candidate = json.loads(text)
        if isinstance(candidate, dict):
            payload = candidate
    except (json.JSONDecodeError, ValueError):
        payload = None

    if payload is None:
        # 第二级：从带前缀的文本里**按字段名**挑答案对象，而不是取第一个 {...}
        candidate = _pick_answer_object(text)
        if candidate is not None:
            payload = candidate
            d.issues.append("braces_recovered")

    if payload is None:
        # 第三级：JSON 真的非法（未转义的引号）或真被截断 —— 按字段抢救。
        # **在剥掉思维链的文本上做**：草稿里那份 JSON 模板会让正则在整段上
        # 先命中草稿，把草稿当答案发出去（见 THINK_RE 说明里的 "..." 事故）。
        salvaged = _salvage_truncated(text)
        if salvaged:
            payload = {
                "thinking": salvaged.get("thinking", ""),
                "speech": salvaged.get("speech", ""),
                "action": {"type": salvaged.get("type", "pass"),
                           "target": salvaged.get("target")},
                "confidence": salvaged.get("confidence", 0.5),
            }
            d.issues.append("json_salvaged")

    if payload is not None:
        d.thinking = str(payload.get("thinking") or "")[:4000]
        d.speech = str(payload.get("speech") or "").strip()
        try:
            d.confidence = float(payload.get("confidence", 0.5))
        except (TypeError, ValueError):
            d.confidence = 0.5

        act = payload.get("action") or {}
        if isinstance(act, dict):
            d.action = Action(
                type=str(act.get("type") or "pass").strip().lower(),
                target=_as_int(act.get("target")),
            )
        elif isinstance(act, (int, float, str)):
            d.action = Action(type="pass", target=_as_int(act))
            d.issues.append("action_not_object")
    else:
        # 全失败。**绝不把原始文本当成发言** —— 那会把一整坨 JSON 灌进大桌。
        d.issues.append("unparsed")
        d.speech = ""

    _validate(d, phase)
    return d


def _validate(d: Decision, phase: str) -> None:
    """按阶段校验动作合法性。不合法就降级成 pass 并留痕。"""
    spec = ACTION_SPEC.get(phase)
    if not spec:
        return

    if d.action.type not in spec["types"]:
        mapped = _canonical_action(d.action.type)
        if mapped in spec["types"]:
            d.issues.append(f"action_alias:{d.action.type}->{mapped}")
            d.action.type = mapped
        else:
            d.issues.append(f"action_invalid:{d.action.type}")
            d.action.type = "pass"

    if spec["needs_target"] and d.action.target is None:
        d.issues.append("target_missing")
        d.action.type = "pass"


def describe_action(seat_no: int, action: Action) -> str:
    """把动作渲染成人类可读的记录文本。

    **只用座位号，不带模型名或昵称** —— 这些文本会进大桌或狼队频道，
    带上模型名就等于把身份映射泄给了对手。
    """
    t = action.target
    tn = f"{t}号" if t is not None else ""

    table = {
        "vote":   f"{seat_no}号 投票放逐{tn}",
        "kill":   f"选择击杀{tn}",
        "check":  f"选择查验{tn}",
        "guard":  f"选择守护{tn}",
        "save":   "使用解药救人",
        "poison": f"使用毒药{tn}",
        "shoot":  f"开枪带走{tn}",
        "explode": "自曝狼人身份",
        "speak":  "发言",
        "chat":   "在狼队频道发言",
        "pass":   "选择不行动",
        # 警长
        "run":         "上警竞选",
        "order_left":  "决定从警左开始发言（自己最后归票）",
        "order_right": "决定从警右开始发言（自己最后归票）",
        "badge":       f"把警徽移交给{tn}",
    }
    return table.get(action.type, f"执行 {action.type}{tn}")
