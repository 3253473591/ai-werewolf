"""事件流 —— 大桌 / 小桌 / 私密通道的唯一事实源。

设计依据见 docs/docs/DESIGN.md 第 1 节。三条铁律：

  1. **只追加**。事件永不修改、永不删除。要更正就追加一条新事件。
  2. **seq 是唯一排序键**。渲染严格按 seq 升序，禁止按时间戳或插入顺序排。
  3. **时间戳不进渲染**。任何 ``datetime.now()`` / ``uuid4()`` / ``id()`` /
     集合迭代顺序进入 prompt 都会打死前缀缓存。渲染器入口有断言兜底。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional


class Channel(str, Enum):
    """事件可见范围。"""

    PUBLIC = "public"      # 大桌：所有人可见
    WOLF = "wolf"          # 小桌：仅存活狼人可见
    PRIVATE = "private"    # 私密通道，按 to_seat 定向


class Kind(str, Enum):
    SPEECH = "speech"          # 发言原文
    ACTION = "action"          # 夜间 / 投票动作（结构化）
    RESOLUTION = "resolution"  # 裁决：谁死了、查验结果、投票结果
    SYSTEM = "system"          # 系统公告：天黑请闭眼、平安夜
    CONTROL = "control"        # 神谕：人类上帝注入的提示词
    PARSE_FAIL = "parse_fail"  # 动作解析失败被兜底，必须留痕（见 DESIGN §4.2）


@dataclass(frozen=True, eq=False)
class Event:
    seq: int
    day: int
    phase: str
    channel: Channel
    kind: Kind
    actor: Optional[int] = None     # 座位号，系统事件为 None
    to_seat: Optional[int] = None   # PRIVATE 通道的收件人
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    # frozen + dict 字段无法自动生成可用哈希，按 seq 定义
    def __hash__(self) -> int:
        return hash(self.seq)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Event) and self.seq == other.seq


class EventLog:
    """append-only 事件流。

    ``on_append`` 是给 Web 端推流用的回调，内核本身不依赖它。
    """

    def __init__(self, on_append=None) -> None:
        self._events: List[Event] = []
        self._next_seq = 0
        self.on_append = on_append

    def append(
        self,
        *,
        day: int,
        phase: str,
        channel: Channel | str,
        kind: Kind | str,
        actor: Optional[int] = None,
        to_seat: Optional[int] = None,
        text: str = "",
        **data: Any,
    ) -> Event:
        ev = Event(
            seq=self._next_seq,
            day=day,
            phase=phase,
            channel=Channel(channel),
            kind=Kind(kind),
            actor=actor,
            to_seat=to_seat,
            text=text,
            data=dict(data),
        )
        self._next_seq += 1
        self._events.append(ev)
        if self.on_append:
            try:
                self.on_append(ev)
            except Exception:
                pass  # 推流失败绝不能影响对局
        return ev

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __getitem__(self, i: int) -> Event:
        return self._events[i]

    def all(self) -> List[Event]:
        return list(self._events)

    def to_jsonl(self, path: str) -> None:
        import json

        with open(path, "w", encoding="utf-8") as f:
            for ev in self._events:
                f.write(json.dumps({
                    "seq": ev.seq,
                    "day": ev.day,
                    "phase": ev.phase,
                    "channel": ev.channel.value,
                    "kind": ev.kind.value,
                    "actor": ev.actor,
                    "to_seat": ev.to_seat,
                    "text": ev.text,
                    "data": ev.data,
                }, ensure_ascii=False) + "\n")
