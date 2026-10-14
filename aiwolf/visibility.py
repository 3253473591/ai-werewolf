"""可见性判定 —— 信息隔离的唯一入口。

对应 docs/DESIGN.md 第 2 节。整个系统的生命线是：**村民座位的 prompt 里
永远不能出现 WOLF 频道内容、真实身份或狼队友名单。**
"""
from __future__ import annotations

from typing import Dict, List

from .events import Channel, Event, EventLog
from .seats import Seat


def is_visible(ev: Event, seat: Seat, by_no: Dict[int, Seat]) -> bool:
    if ev.channel is Channel.PUBLIC:
        return True
    if ev.channel is Channel.WOLF:
        # 出局狼退出小桌（真实规则如此）
        return seat.is_wolf and seat.alive
    if ev.channel is Channel.PRIVATE:
        return ev.to_seat == seat.no
    return False


def visible_events(log: EventLog, seat: Seat, by_no: Dict[int, Seat]) -> List[Event]:
    return [ev for ev in log if is_visible(ev, seat, by_no)]


def public_events(log: EventLog) -> List[Event]:
    """大桌全量。**对所有座位字节级相同** —— 共享前缀就是它。"""
    return [ev for ev in log if ev.channel is Channel.PUBLIC]


def wolf_channel(log: EventLog) -> List[Event]:
    return [ev for ev in log if ev.channel is Channel.WOLF]


def private_events(log: EventLog, seat: Seat) -> List[Event]:
    return [ev for ev in log if ev.channel is Channel.PRIVATE and ev.to_seat == seat.no]
