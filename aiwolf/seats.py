"""座位、角色与模型配置。"""
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ROLE_CN = {
    "Werewolf": "狼人",
    "Seer": "预言家",
    "Witch": "女巫",
    "Hunter": "猎人",
    "Guard": "守卫",
    "Idiot": "白痴",
    "Villager": "村民",
}

# 白痴：被投票放逐时亮身份、**不出局**，但从此失去投票权（被刀/被毒照常出局）。
IDIOT_ROLE = "Idiot"

WIN_CONDITION = {
    "Werewolf": "杀光所有好人（村民、预言家、女巫、猎人、守卫），狼人阵营获胜",
}
GOOD_WIN = "投票放逐所有狼人，好人阵营获胜"

# 各人数下的角色配置（唯一事实源，顺序即座位号 1..N 的角色）。
#
#   3  人（调试 UI / 功能测试）：1 狼 1 预 1 民
#   4  人                    ：1 狼 1 预 2 民
#   5  人                    ：2 狼 1 预 1 女巫 1 民
#   6  人                    ：2 狼 1 预 1 女巫 2 民
#   7  人                    ：2 狼 1 预 1 女巫 1 猎人 2 民
#   8  人                    ：3 狼 1 预 1 女巫 1 猎人 2 民
#   9  人（上下文测试）        ：3 狼 1 预 1 女巫 1 猎人 3 民
#   10 人                    ：3 狼 1 预 1 女巫 1 猎人 1 守卫 3 民
#   11 人                    ：4 狼 1 预 1 女巫 1 猎人 1 白痴 3 民
#   12 人（主流"预女猎白"板）：4 狼 1 预 1 女巫 1 猎人 1 白痴 4 民
ROLE_SETS: Dict[int, List[str]] = {
    3:  ["Werewolf", "Seer", "Villager"],
    4:  ["Werewolf", "Seer", "Villager", "Villager"],
    5:  ["Werewolf", "Werewolf", "Seer", "Witch", "Villager"],
    6:  ["Werewolf", "Werewolf", "Seer", "Witch", "Villager", "Villager"],
    7:  ["Werewolf", "Werewolf", "Seer", "Witch", "Hunter", "Villager", "Villager"],
    8:  ["Werewolf", "Werewolf", "Werewolf", "Seer", "Witch", "Hunter", "Villager", "Villager"],
    9:  ["Werewolf", "Werewolf", "Werewolf", "Seer", "Witch", "Hunter",
         "Villager", "Villager", "Villager"],
    10: ["Werewolf", "Werewolf", "Werewolf", "Seer", "Witch", "Hunter", "Guard",
         "Villager", "Villager", "Villager"],
    11: ["Werewolf", "Werewolf", "Werewolf", "Werewolf", "Seer", "Witch", "Hunter",
         "Idiot", "Villager", "Villager", "Villager"],
    12: ["Werewolf", "Werewolf", "Werewolf", "Werewolf", "Seer", "Witch", "Hunter",
         "Idiot", "Villager", "Villager", "Villager", "Villager"],
}

# 备用板子。10 人：4 狼不带守卫（原样保留）。
# 12 人：把白痴换成守卫 —— "预女猎守"，另一套主流 12 人板。
ROLE_SETS_ALT: Dict[int, List[str]] = {
    10: ["Werewolf", "Werewolf", "Werewolf", "Werewolf",
         "Seer", "Witch", "Hunter", "Villager", "Villager", "Villager"],
    12: ["Werewolf", "Werewolf", "Werewolf", "Werewolf", "Seer", "Witch", "Hunter",
         "Guard", "Villager", "Villager", "Villager", "Villager"],
}

PLAYER_COUNTS = sorted(ROLE_SETS)


def role_summary(count: int) -> str:
    roles = ROLE_SETS.get(count, [])
    parts = []
    for r in ("Werewolf", "Seer", "Witch", "Hunter", "Guard", "Idiot", "Villager"):
        n = roles.count(r)
        if n:
            parts.append(f"{n}{ROLE_CN[r]}")
    return " ".join(parts)


@dataclass
class Seat:
    no: int
    label: str
    model: str
    fallback: List[str] = field(default_factory=list)
    temperature: float = 0.9
    cache_style: str = "auto"
    cache_verified: bool = False
    role: Optional[str] = None
    alive: bool = True
    is_human: bool = False
    death_cause: Optional[str] = None
    endpoint: str = ""          # 走哪条通道（base_url+key），见 endpoints.py
    # 每个座位额外的请求体参数，直接合进 payload。
    # 例：豆包/方舟用它关掉思维链 —— {"thinking": {"type": "disabled"}}
    # 实测同一模型狼队频道从 71.0s 降到 2.7s，而且协议要的 JSON 照样完整。
    extra_body: Dict[str, Any] = field(default_factory=dict)
    # 代打模型 → 指定通道。config 里写 "模型@通道"（如 "ep-xxx@ep4"）时填这里；
    # 没写的走默认路由（座位通道，跑不了就挑一条能跑的）。
    fallback_endpoints: Dict[str, str] = field(default_factory=dict)

    @property
    def is_wolf(self) -> bool:
        return self.role == "Werewolf"

    @property
    def display(self) -> str:
        return f"{self.no}号 {self.label}"

    @property
    def role_cn(self) -> str:
        return ROLE_CN.get(self.role or "", self.role or "未知")


def load_config(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict, path: str | Path) -> None:
    """原子写回 seats.json。"""
    path = str(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# 前端可以调的"代打 / 超时"参数：字段 -> (类型, 最小值, 最大值)
# 走白名单 + 范围校验 —— 前端传什么都不能把配置写坏（写坏的后果是整局跑不起来）。
GAME_TUNABLES: Dict[str, tuple] = {
    "request_timeout_s": ("int", 10, 3600),
    "max_tokens_cap": ("int", 1024, 200000),
    "max_attempts_per_model": ("int", 1, 10),
    "call_deadline_s": ("int", 0, 7200),
    "switch_model_on_timeout": ("bool", None, None),
    "fallback_enabled": ("bool", None, None),
}


def update_game_cfg(path: str | Path, **fields) -> dict:
    """改「代打 / 超时」这几个参数并落盘。返回实际写入的值。"""
    cfg = load_config(path)
    game = cfg.setdefault("game", {})
    changed: Dict[str, Any] = {}
    for key, raw in fields.items():
        spec = GAME_TUNABLES.get(key)
        if spec is None:
            raise KeyError(f"{key} 不是可调项")
        kind, lo, hi = spec
        if kind == "bool":
            val: Any = bool(raw)
        else:
            try:
                val = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{key} 要是整数") from None
            if lo is not None and not (lo <= val <= hi):
                raise ValueError(f"{key} 要在 {lo}~{hi} 之间")
        changed[key] = val
        if key == "fallback_enabled":
            cfg[key] = val          # 顶层字段（历史原因）
        else:
            game[key] = val
    save_config(cfg, path)
    return changed


def update_seat_def(path: str | Path, no: int, **fields) -> dict:
    """改某个座位的配置并**落盘**。

    页面上给座位换模型（多通道下每个通道能提供的模型不一样）之后必须持久化，
    否则每次重启都要重选一遍。
    """
    cfg = load_config(path)
    for definition in cfg.get("seats", []):
        if int(definition.get("no", -1)) == int(no):
            for k, v in fields.items():
                if v is None:
                    definition.pop(k, None)
                else:
                    definition[k] = v
            save_config(cfg, path)
            return definition
    raise KeyError(f"配置里没有 {no} 号座位")


def resolve_seed(cfg: dict) -> int:
    """决定本局的角色抽取种子。

    config 里 ``role_seed`` 为 null 时**每局随机抽一次**，
    并把这个种子记进 events.jsonl / summary.json，所以随时可复现。
    写死一个数字则每局都一样——那样"抽卡"就名存实亡了。
    """
    raw = (cfg.get("game") or {}).get("role_seed")
    if raw is None or raw == "":
        return random.SystemRandom().randrange(1, 2 ** 31)
    return int(raw)


def wants_shuffle(cfg: dict) -> bool:
    return bool((cfg.get("game") or {}).get("shuffle_roles", True))


def parse_fallback(entries) -> tuple[List[str], Dict[str, str]]:
    """把 config 里的 fallback 列表规范化成 (模型名列表, 模型→指定通道)。

    支持三种写法（想跨厂商代打就必须能指定通道 —— 一个座位只绑一条通道，
    而 grok 在 ep3、豆包在 ep4）：

        "gpt-5.5"                        纯字符串：走默认路由
        "ep-2026…-gcv24@ep4"             模型@通道：明确指定
        {"model": "…", "endpoint": "ep4"}  对象形式（老写法也兼容）
    """
    models: List[str] = []
    routes: Dict[str, str] = {}
    for item in entries or []:
        if isinstance(item, dict):
            m = str(item.get("model") or "").strip()
            ep = str(item.get("endpoint") or "").strip()
        else:
            raw = str(item or "").strip()
            m, _, ep = raw.partition("@")
            m, ep = m.strip(), ep.strip()
        if not m:
            continue
        models.append(m)
        if ep:
            routes[m] = ep
    return models, routes


def build_seats(cfg: dict, count: int, *, shuffle: bool = False,
                seed: Optional[int] = None, alt: bool = False,
                registry=None) -> List[Seat]:
    """从配置里取前 count 个座位，并按 ROLE_SETS 抽角色。

    角色顺序即座位号顺序。``shuffle=True`` 时用 seed 打乱角色——
    不打乱的话 1-3 号永远是狼，而 1、2 号恰好是 Gemini 和 GPT，
    几局下来就会被看出"某某总是狼"。打乱后同一局仍可复现。

    ``registry`` 是 EndpointRegistry；给了就按"座位配置里写死的 endpoint，
    否则轮转分配"给每个座位绑定一条通道（多把 key 摊并发）。
    """
    seat_defs = cfg["seats"][:count]
    if len(seat_defs) < count:
        raise ValueError(
            f"配置里只有 {len(seat_defs)} 个座位，需要 {count} 个。"
            f"请往 config 的 seats 数组里补座位。")

    table = ROLE_SETS_ALT if alt else ROLE_SETS
    roles = list(table.get(count) or [])
    if not roles:
        raise ValueError(f"没有 {count} 人局的角色配置，可选：{PLAYER_COUNTS}")

    if shuffle:
        random.Random(seed).shuffle(roles)

    seats: List[Seat] = []
    for definition, role in zip(seat_defs, roles):
        endpoint = definition.get("endpoint", "")
        if registry is not None and not endpoint:
            # 自动分配时带上模型名，让 registry 只挑跑得动它的通道
            ep = registry.by_seat(definition["no"], definition.get("model", ""))
            endpoint = ep.id if ep else ""
        fb_models, fb_routes = parse_fallback(definition.get("fallback"))
        seats.append(Seat(
            no=definition["no"],
            label=definition["label"],
            model=definition["model"],
            fallback=fb_models,
            temperature=float(definition.get("temperature", 0.9)),
            cache_style=definition.get("cache_style", "auto"),
            cache_verified=bool(definition.get("cache_verified", False)),
            role=role,
            endpoint=endpoint,
            extra_body=dict(definition.get("extra_body") or {}),
            fallback_endpoints=fb_routes,
        ))
    return seats


def alive_seats(seats: List[Seat]) -> List[Seat]:
    return [s for s in seats if s.alive]


def wolves(seats: List[Seat]) -> List[Seat]:
    return [s for s in seats if s.is_wolf and s.alive]


def good(seats: List[Seat]) -> List[Seat]:
    return [s for s in seats if not s.is_wolf and s.alive]
