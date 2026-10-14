"""赛后简报 —— 把一整局事件流压成能直接进 prompt 的浓缩记录。

对应 DESIGN.md 第 14 节。**三条口径写死，不许各自发挥**：

  1. **只取公开频道。** 狼队小桌、私密通道、上帝账本一律不进 ——
     和 `tools/roundtable_brief.py` 同一条边界：赛后能引用的必须是场上真实可见的东西。
  2. **裁决与票型全留。** 所有公开 `RESOLUTION`（死讯、放逐、警徽移交、开枪、
     翻牌、自曝）进「出局时间线」；每个投票轮汇总成一行票型。
     引擎拼的「N号 投给 X号」不单独占行 —— 票型那一行已经把整轮合并了。
  3. **发言截到 `max_chars` 字**，带「…（后略）」标记。**不静默砍**：
     读的人得知道这里被截过，否则会把半句话当成说完了。

为什么需要它：12 座 5 天的 `events.jsonl` 约 10 万字节，直接塞进 prompt 是几万 token，
而 12 个座位**各带一份**。采访和圆桌真正要的是"谁什么时候说了什么、票怎么走的"，
不是逐字节的原始流。实测同一局压完约 1.2 万字，够用且不糊。

产物是纯文本：可以落 `brief.md` 存档，也可以直接当 prompt 的一段（见 `reflect.py --brief`）。
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from .events import Channel, Event, EventLog, Kind
from .seats import Seat

CLIP_MARK = "…（后略）"

# 「出局时间线」只收这些：公开频道里的 RESOLUTION 全部是玩法裁决，
# 没有一条是纯噪声，所以整类都收 —— 不靠关键词白名单（白名单一定会漏）。
_NOISE_PHASES = ("NIGHT_START",)

WINNER_CN = {"wolf": "狼人阵营获胜", "good": "好人阵营获胜",
             "draw": "平局", "aborted": "这一局被中途结束"}


def _clip(text: str, limit: int) -> str:
    """压成一行的短文本。换行折成空格 —— 简报是按行读的，多行会散架。"""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + CLIP_MARK


def _roster(seats: List[Seat]) -> str:
    return " ｜ ".join(f"{s.no}号 {s.role_cn}" for s in sorted(seats, key=lambda x: x.no))


def _timeline_line(ev: Event, roles: Dict[int, str]) -> Optional[str]:
    """一条裁决 → 时间线一行。带上底牌，省得读的人来回翻名单。"""
    t = (ev.text or "").strip()
    if not t:
        return None

    if t.startswith("昨晚死亡的是："):
        nos = [int(x) for x in re.findall(r"(\d+)号", t)]
        who = "、".join(f"{n}号（{roles.get(n, '?')}）" for n in nos)
        return f"- 第{ev.day}天 夜里死 {who}"

    target = ev.data.get("target")
    if "被放逐" in t and target:
        tail = t[t.find("（"):].strip("（）") if "（" in t else ""
        tail = f" {tail}" if tail else ""
        return f"- 第{ev.day}天 放逐 {int(target)}号（{roles.get(int(target), '?')}）{tail}"

    suffix = ""
    if target is not None:
        try:
            no = int(target)
        except (TypeError, ValueError):
            no = -1
        if no in roles:
            suffix = f"【{roles[no]}】"
    return f"- 第{ev.day}天 {t}{suffix}"


def _vote_rounds(evs: List[Event]) -> Dict[tuple, List[str]]:
    """把「N号 投给 X号」按 (天, 阶段) 汇总成一行票型。

    弃票也要留 —— "谁到投票环节就消失"本身就是证据（10 座局里 5 号两天弃票，
    那条账最后成了它被放逐的理由）。
    """
    rounds: Dict[tuple, List[str]] = {}
    for ev in evs:
        if ev.kind is not Kind.SPEECH or ev.data.get("action") != "vote":
            continue
        target = ev.data.get("target")
        mark = f"{target}号" if target else "弃票"
        rounds.setdefault((ev.day, ev.phase), []).append(f"{ev.actor}号→{mark}")
    return rounds


def build_brief(log: EventLog, summary: dict, seats: List[Seat],
                *, max_chars: int = 200) -> str:
    """事件流 → 简报纯文本。**确定性**：同一份事件流两次调用结果完全相同。"""
    roles = {s.no: s.role_cn for s in seats}
    evs = [e for e in log if e.channel is Channel.PUBLIC]
    rounds = _vote_rounds(evs)

    winner = WINNER_CN.get(summary.get("winner"), str(summary.get("winner")))
    # 标题只放目录名：summary 里的 run_dir 是绝对路径，写进去又长又会随机器变，
    # 而简报是要存档/对比多局的，带着本机路径反而看不清是哪一局。
    run_name = os.path.basename(str(summary.get("run_dir") or "").rstrip("\\/"))
    out: List[str] = [f"# 赛后简报 · {run_name}".rstrip(" ·"), ""]
    out += [f"**结果**：{winner}，共 {summary.get('days')} 天。", ""]
    out += ["## 全员真实身份（赛后已摊开）", "", _roster(seats), ""]

    out += ["## 出局时间线", ""]
    for ev in evs:
        if ev.kind is not Kind.RESOLUTION:
            continue
        line = _timeline_line(ev, roles)
        if line:
            out.append(line)
    out.append("")

    out += [f"## 大桌公开记录（按天浓缩，单条发言截到 {max_chars} 字）", "",
            "> 只有公开频道。席位上没说话时，场上看到的就是「本轮未发言」，"
            "别的一概不知道（系统故障原文、狼队小桌、私密通道都不在这里）。", ""]

    def tally(key: Optional[tuple]) -> Optional[str]:
        if key is None:
            return None
        return f"- `{key[1]}` 票型：{'、'.join(rounds[key])}"

    # 按天分段渲染。为什么按天：**最后一天常常只剩下一个死讯**（全在时间线里），
    # 逐事件输出的写法会给它留一个空标题 —— 那种残留看起来像简报被截断了。
    days: List[int] = []
    for ev in evs:
        if ev.day not in days:
            days.append(ev.day)

    for d in days:
        block: List[str] = []
        pending: Optional[tuple] = None
        for ev in evs:
            if ev.day != d:
                continue
            key = (ev.day, ev.phase)
            if pending is not None and key != pending:
                line = tally(pending)
                if line:
                    block.append(line)
                pending = None

            if ev.kind is Kind.RESOLUTION:
                continue                 # 已进「出局时间线」
            if ev.kind is Kind.SPEECH and ev.data.get("action") == "vote":
                pending = key            # 这一轮收完再补一行票型
                continue
            if ev.phase in _NOISE_PHASES:
                continue

            text = _clip(ev.text, max_chars)
            if ev.kind is Kind.SPEECH:
                who = f"**{ev.actor}号**" if ev.actor else "公告"
                block.append(f"- `{ev.phase}` {who}：{text}")
            else:
                block.append(f"- `{ev.phase}` {text}")

        line = tally(pending)
        if line:
            block.append(line)
        if block:
            out += [f"### 第 {d} 天", ""] + block + [""]

    return "\n".join(out).rstrip() + "\n"


def write_brief(path: str, text: str) -> None:
    """原子写。简报是给人看/给 prompt 用的存档，写坏一半比没写更糟。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
