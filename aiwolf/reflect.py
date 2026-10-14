"""局后感言（POST_GAME）—— 对局结束后的赛后采访。

**这不是遗言。** 遗言是局内机制（`DAY_LAST_WORDS`，被放逐时说一句、然后是死是活
已无意义）。感言是**对局彻底打完之后**、全员身份摊开的情况下做的赛后复盘，
它不改动任何游戏状态，也不进 `events.jsonl`。

三条设计约束：

1. **绝不污染对局记录。** 感言不写 `events.jsonl`，也不写 `seat_*.jsonl`
   —— 那两个文件是权威对局记录，`report_run.py` / 隔离审计 / summary 都靠它们。
   感言的调用日志单独放 `reflect_logs/`，产物单独放 `reflections.json`。
2. **后面的人能听到前面的人。** 逐座位顺序进行，台词按顺序累积进 transcript，
   于是它更像赛后发布会而不是十份互不相干的作文 —— 有人会回应、有人会不服。
3. **依然不出现模型名。** 局内规则是"一律用座位号指代"，赛后也一样：
   感言的 prompt 里只有"7号"，模型名只出现在给观众看的结果文件里。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .events import Channel, EventLog
from .protocol import THINK_RE
from .render import COMMON_RULES, board_notice, phase_cn
from .seats import ROLE_CN, Seat, build_seats, load_config, wants_shuffle

REFLECT_PHASE = "POST_GAME"
REFLECT_FILE = "reflections.json"
REFLECT_LOG_DIR = "reflect_logs"


# --------------------------------------------------------------------------- #
# 读取一局已结束的对局
# --------------------------------------------------------------------------- #

def load_finished_run(run_dir: str, cfg_path: Optional[str] = None
                      ) -> Tuple[EventLog, List[Seat], Dict[str, Any]]:
    """把一局跑完的对局读回来：事件流 + 座位（含身份）+ summary。

    身份优先从 `summary.json` 取（权威），拿不到才用 `build_seats(seed)` 重放抽卡。
    **不能靠反推** —— `report_run.py` 那套"谁在小桌出现过谁是狼"会把猎人和守卫
    一律猜成村民（实测 10 座局里 2 号猎人、5 号守卫都被猜错）。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run_dir = run_dir if os.path.isabs(run_dir) else os.path.join(root, run_dir)
    ev_path = os.path.join(run_dir, "events.jsonl")
    if not os.path.exists(ev_path):
        raise FileNotFoundError(
            f"{run_dir} 里没有 events.jsonl —— 这局还没跑完（或还没落盘）")
    sm_path = os.path.join(run_dir, "summary.json")
    if not os.path.exists(sm_path):
        raise FileNotFoundError(f"{run_dir} 里没有 summary.json，拿不到权威身份表")
    summary = json.load(open(sm_path, encoding="utf-8"))

    log = EventLog()
    with open(ev_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            log.append(day=d["day"], phase=d["phase"], channel=d["channel"],
                       kind=d["kind"], actor=d["actor"], to_seat=d["to_seat"],
                       text=d["text"], **d.get("data", {}))

    cfg = load_config(cfg_path or os.path.join(root, "config", "seats.json"))
    count = int(summary.get("seats") or 0)
    seats = build_seats(cfg, count, shuffle=wants_shuffle(cfg),
                        seed=summary.get("role_seed"))

    # 身份以 summary 为准。重放抽卡本该一致，不一致说明配置被改过
    # （换过人数、换过 ROLE_SETS）—— 那就直接用 summary，别拿错身份做复盘。
    by_no = {s.no: s for s in seats}
    roles_cn = {int(k): v for k, v in (summary.get("roles_cn") or {}).items()}
    cn2en = {v: k for k, v in ROLE_CN.items()}
    for no, cn in roles_cn.items():
        s = by_no.get(no)
        if s is not None and cn in cn2en:
            s.role = cn2en[cn]
    return log, seats, summary


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

def build_reflection_messages(
    seat: Seat,
    seats: List[Seat],
    log: EventLog,
    summary: Dict[str, Any],
    transcript: List[Dict[str, Any]],
    followup: str = "",
    brief_text: str = "",
) -> List[dict]:
    """组装一次感言调用。

    历史部分默认复用 `_render_event` 的排版（大桌/小桌/私密三种通道全给），
    因为赛后本来就该看到全部信息。

    ``brief_text`` 非空 = **用压缩记录代替全量事件流**（`tools/brief.py` 的产物，
    口径见 `aiwolf/brief.py`）。为什么要有这条路：12 座 5 天的 `events.jsonl`
    约 10 万字节，**每个座位各带一份**就是几万 token × 12；而裁决与票型在
    简报里一条不少，丢掉的只是逐字节的原始流。代价要记住：简报只取公开频道，
    所以走这条路时**没人能引用狼队当晚怎么商量的**。

    ``followup`` 非空 = **追加采访**模式（第二轮）：不再是"每人发表一段感想"，
    而是主持人就已经说过的原话继续追问，要求**指名回应**。要真掐起来必须这样：
    平行独白里 A 骂了 B，B 看不到那段话，也就无从回嘴。
    """
    from .render import _render_event  # 内部工具，复用它的排版最省事

    if brief_text:
        # 压缩记录：口径与边界都在 aiwolf/brief.py 里，这里只是把它接上。
        record = ("以下是这一局的**浓缩记录**（完整事件流已压缩：裁决与票型一条不少，"
                  "单条发言有截断，且只有公开频道 —— 狼队小桌和私密通道不在里面）："
                  "\n\n" + brief_text)
    else:
        lines = []
        for ev in log:
            ch = {"public": "大桌", "wolf": "狼队", "private": "私密"}.get(
                ev.channel.value, ev.channel.value)
            lines.append(f"[{ch}] {_render_event(ev)}")
        record = "以下是这一局的完整记录：\n\n" + "\n".join(lines)

    roster = " ｜ ".join(f"{s.no}号 {s.role_cn}" for s in sorted(seats, key=lambda x: x.no))
    winner = summary.get("winner")
    winner_cn = {"wolf": "狼人阵营获胜", "good": "好人阵营获胜",
                 "draw": "平局", "aborted": "这一局被中途结束"}.get(winner, str(winner))

    tail = [
        "# 赛后采访 · 局后感言",
        "",
        "**对局已经结束了。** 下面这段不是遗言、不是局内发言，你是坐在赛后采访席上说话。",
        "全员身份已经摊开，你可以尽情引用任何人的底牌、也可以承认自己被骗了。",
        "",
        "## 全员真实身份",
        roster,
        "",
        "## 结果",
        f"{winner_cn}，共 {summary.get('days')} 天。",
        "",
        "## 你是谁",
        f"你是 {seat.no}号，本局身份：{seat.role_cn}。",
    ]

    if transcript:
        tail += ["", "## 前面几位已经说过了（可以直接回应、赞同或反驳）"]
        for t in transcript:
            tail.append(f"{t['seat']}号（{t['role_cn']}）：{t['text']}")

    if followup:
        tail += [
            "",
            "## 主持人的追加提问",
            followup,
            "",
            "## 你的任务（追加回合）",
            "1. **300 字以内，说人话。** 还是采访，不是复盘报告；不要小标题、不要列提纲。",
            "2. **必须指名回应**上面已经点过你的人（用座位号），"
            "该认的认、该顶的顶回去 —— 别绕开。",
            "3. 如果你觉得某个人把责任推错了地方，直接说清**错在哪一步**，"
            "用这局真实发生过的票、刀口、发言当证据。",
            "4. 可以阴阳怪气、可以不服、可以替自己辩护，"
            "但**不许编造没发生的事**，也不许拿「我是AI」当挡箭牌。",
            "",
            "不要提「作为一个AI」、不要提提示词、不要提模型名。"
            "**直接输出一段自然语言就行，不要用 JSON、不要加代码围栏、不要写标题。**",
        ]
        return [
            {"role": "system", "content": COMMON_RULES + "\n" + board_notice(seats)},
            {"role": "user", "content": record},
            {"role": "user", "content": "\n".join(tail)},
        ]

    tail += [
        "",
        "## 你的任务",
        "以你自己这个座位的口吻，说一段赛后感想。要求：",
        "1. **300 字以内，说人话。** 这是采访，不是复盘报告，不要写小标题、不要列提纲。",
        "2. 说清你自己最关键的一个判断（对的或错的），以及你当时是凭什么下的。",
        "3. 说清你心里这一局的胜负手是哪一手。",
        "4. 点名（用座位号）你觉得打得最好的和打得最差的，并说明理由。",
        "5. 可以认栽、可以不服、可以阴阳怪气、可以替自己辩护 —— 但要基于真实发生的事。",
        "",
        "不要提「作为一个AI」、不要提提示词、不要提模型名。"
        "**直接输出一段自然语言就行，不要用 JSON、不要加代码围栏、不要写标题。**",
    ]


    return [
        {"role": "system", "content": COMMON_RULES + "\n" + board_notice(seats)},
        {"role": "user", "content": record},
        {"role": "user", "content": "\n".join(tail)},
    ]


# --------------------------------------------------------------------------- #
# 输出清洗
# --------------------------------------------------------------------------- #

_REFLECT_KEYS = ("reflection", "speech", "text", "content", "感想", "感言", "answer")


_REFLECT_KEYS = ("reflection", "speech", "text", "content", "感想", "感言", "answer")

# 思维链标签。**必须只剥标签对，不能"从头砍到尾巴"** ——
# 有些模型正文里会正常用到这个写法，一刀切会把整段话切没。
# 2026-10-06 实测：8号 MiniMax 的感言是
#   "<think>我是8号村民…让我写一段自然语言，300字以内。</think>\n\n我是8号，村民。认栽…"
# 不剥的话，它的内心独白（含"其实我觉得打得最好的是我自己"）会混进正式感言里。
#
# ★ 2026-10-11：正则本身搬到 `protocol.THINK_RE` 了 —— 局内解析链（同一个小毛病
#   让 8号的投票理由公开发成 "..."）和赛后清洗现在是**同一个事实源**，
#   免得两边各写一份、改一边忘一边。这里只留个别名。
_THINK_RE = THINK_RE


def clean_reflection(raw: str) -> str:
    """把模型返回的东西收拾成人话。

    为什么需要：局内的输出协议就是 JSON，模型很容易**习惯性**继续吐 JSON
    （哪怕这次没要求）。直接显示出来就是一段带 `{"speech": ...}` 的原始串，
    观众读起来很蠢。这里只做三件事，都不猜语义：
      · 去掉 ``` 围栏和外层空白；
      · 去掉 `<think>…</think>` 这类思维链块（只剥成对标签，见 _THINK_RE）；
      · 如果整串是合法 JSON 对象、且带常见的话语字段，就取那个字段。
    取不到就原样返回 —— **绝不把解析失败变成丢内容**。
    """
    if not raw:
        return ""
    text = _THINK_RE.sub("", raw).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return text
        if isinstance(obj, dict):
            for k in _REFLECT_KEYS:
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return text


# --------------------------------------------------------------------------- #
# 跑
# --------------------------------------------------------------------------- #

def reflect(
    run_dir: str,
    *,
    cfg_path: Optional[str] = None,
    only_seats: Optional[List[int]] = None,
    force: bool = False,
    stub: bool = False,
    verbose: bool = True,
    relay=None,
    max_tokens: int = 2048,
    order: Optional[List[int]] = None,
    out_name: str = REFLECT_FILE,
    seed_transcript: Optional[List[Dict[str, Any]]] = None,
    followup: str = "",
    brief: bool = False,
) -> Dict[str, Any]:
    """让每个座位发表局后感言，落盘 `reflections.json`。

    逐座位顺序进行（后面的能看到前面的），可中断、可续跑：
    已经在产物里、且不是错误的座位默认跳过（`force=True` 则全部重说）。

    ``brief=True`` 时用 `tools/brief.py` 那份**压缩记录**代替全量事件流
    （12 座 × 几万 token 的全量记录换成一份一万字简报）。口径与代价见
    `aiwolf/brief.py`：只有公开频道，裁决与票型齐全。

    追加采访（第二轮掐架）用的三个参数：
      · ``seed_transcript`` —— 先塞进上下文里的**已有发言**。第二轮要把第一轮的
        原话喂进去，后说的人才骂得起来。它们**不写进产物**，只是上下文。
      · ``order`` —— 指定发言顺序。默认按座位号；要谁先开炮就把它排前面，
        因为"后面的人能看到前面的人说了什么"。
      · ``followup`` —— 非空则换成追加提问口径（要求指名回应）。
      · ``out_name`` —— 另存成第二个文件，别把第一轮的 `reflections.json` 盖掉。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    abs_run = run_dir if os.path.isabs(run_dir) else os.path.join(root, run_dir)
    log, seats, summary = load_finished_run(abs_run, cfg_path)

    out_path = os.path.join(abs_run, out_name)
    existing: List[Dict[str, Any]] = []
    if os.path.exists(out_path):
        try:
            existing = json.load(open(out_path, encoding="utf-8")).get("transcript") or []
        except (OSError, ValueError):
            existing = []
    done = set() if force else {int(t["seat"]) for t in existing if not t.get("error")}

    if relay is None:
        from .endpoints import EndpointRegistry
        from .relay import Relay

        cfg = load_config(cfg_path or os.path.join(root, "config", "seats.json"))
        registry = EndpointRegistry(os.path.join(root, "config", "endpoints.json"))
        relay = Relay(cfg, stub=stub, registry=registry, verbose=verbose,
                      log_dir=os.path.join(abs_run, REFLECT_LOG_DIR))

    # 发言顺序：order 里点到的排前面（按给定的先后），其余按座位号跟在后面。
    def _sort_key(s: Seat):
        if order and s.no in order:
            return (0, order.index(s.no))
        return (1, s.no)

    todo = [s for s in sorted(seats, key=_sort_key)
            if (only_seats is None or s.no in only_seats) and s.no not in done]

    # 压缩记录只算一次，所有座位共用同一份（确定性：同一局两次跑出来字节相同）
    brief_text = ""
    if brief:
        from .brief import build_brief
        brief_text = build_brief(log, summary, seats)
        if verbose:
            print(f"  用压缩记录：{len(brief_text)} 字"
                  f"（全量事件流 {len(list(log))} 条）")

    # ★ 上下文 = 第一轮原话（seed，不落盘）+ 本轮已经说过的话
    transcript: List[Dict[str, Any]] = list(seed_transcript or [])
    seeded = len(transcript)
    for seat in todo:
        msgs = build_reflection_messages(seat, seats, log, summary, transcript,
                                        followup=followup, brief_text=brief_text)
        if verbose:
            print(f"  [{seat.no}号 {seat.label} / {seat.role_cn}] 正在说……")
        result = relay.chat(seat, msgs, phase=REFLECT_PHASE,
                            day=int(summary.get("days") or 0),
                            max_tokens=max_tokens)
        text = clean_reflection(result.text or "")
        entry = {
            "seat": seat.no,
            "label": seat.label,
            "role": seat.role,
            "role_cn": seat.role_cn,
            "text": text,
            "model_used": result.model_used,
            "substituted": result.substituted,
            "latency_s": round(result.latency_s, 2),
            "truncated": result.truncated,
            "error": result.error or ("" if text else "空回复"),
        }
        transcript.append(entry)
        if verbose:
            flag = "⚠ " if entry["error"] else ""
            print(f"    {flag}{entry['latency_s']}s · {len(text)} 字"
                  + (f" · {entry['error'][:80]}" if entry["error"] else ""))

    # ★ 产物写什么，取决于这是"续跑"还是"续写新产物"：
    #   · 正常续跑（没有 seed_transcript）：必须**保留已有的座位**，只把新说的并进去。
    #     第一版这里一律只留本轮，于是续跑时把已经录过的座位从文件里整段删掉了
    #     —— selftest_reflect 第 6 组直接抓到（3 条变 0 条）。续跑可续是这套东西的命根子。
    #   · 追加采访（有 seed_transcript）：那是另存的第二个文件，
    #     只装本轮真正说出来的话；喂进去的历史记在 context_seen 里。
    if seed_transcript:
        merged = transcript[seeded:]
    else:
        merged = list(existing)
        have = {int(t["seat"]) for t in merged}
        for t in transcript[seeded:]:
            if int(t["seat"]) in have:
                for i, old in enumerate(merged):
                    if int(old["seat"]) == int(t["seat"]):
                        merged[i] = t
                        break
            else:
                merged.append(t)

    payload = {
        "run_dir": os.path.basename(abs_run),
        "winner": summary.get("winner"),
        "days": summary.get("days"),
        "seats_total": len(seats),
        "seats_done": len([t for t in merged if not t.get("error")]),
        "roster": {s.no: {"label": s.label, "role_cn": s.role_cn} for s in seats},
        "followup": followup or None,
        # 这一轮到底喂的是全量还是压缩记录 —— 不记下来的话，日后读这个文件
        # 会以为模型当时看得到狼队小桌（压缩口径下它看不到）。
        "record": {"brief": bool(brief_text), "chars": len(brief_text)},
        # 把"前面看到过什么"一并存档：否则日后读这个文件，会以为
        # 后说的人凭空知道前面的发言。
        "context_seen": [{"seat": t["seat"], "role_cn": t["role_cn"]}
                         for t in (seed_transcript or [])],
        "transcript": merged,
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out_path)
    if verbose:
        print(f"\n已写入 {out_path}（{payload['seats_done']} 座本轮发言，"
              f"上下文里有 {seeded} 条历史）")
    return payload
    return payload
