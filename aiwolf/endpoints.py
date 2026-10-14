"""多通道（base_url + key）管理。

起因：Grok 那类模型有**模型级并发上限**，实测一局里连着三次撞
``concurrency_limit_exceeded`` 直接导致该座位"未发言"。同一个 key 的并发额度是
共享的，光靠调低限流治不好——真正的解法是**多把 key、多条通道**，
每个通道各自维护一份并发预算。

同一中转站可以配多条通道（不同 key），同一个 base_url+key 也允许重复登记
（比如某把 key 的额度更高时想多分几条）。

模型清单是**动态拉的**：填完 base_url + key 后调 ``/v1/models``，
所以通道上有什么模型就以实际为准 —— 这意味座位上的模型不一定还是预设的九个。
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config", "endpoints.json")
MODEL_CACHE_TTL = 300.0


def is_deployment_id(model: str) -> bool:
    """判断这是个"推理接入点/部署名"而不是目录里的模型名。

    火山方舟的接入点长这样：``ep-20261005143108-gcv24`` —— 它**不会出现在
    `/models` 目录里**，因为它是用户自己建的部署。拿目录去比对必然误报，
    然后开局体检就会把一整局拦下来（实测踩过）。Azure 的 deployment 名同理。
    """
    return str(model or "").lower().startswith("ep-")


@dataclass
class Endpoint:
    id: str
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    concurrency: int = 4
    enabled: bool = True
    note: str = ""
    synthetic: bool = False     # 从 seats.json 兜底生成的临时通道

    # 运行期状态（不落盘）
    models: List[str] = field(default_factory=list)
    models_at: float = 0.0
    last_error: str = ""
    ok_calls: int = 0
    failed_calls: int = 0
    _sem: Optional[threading.BoundedSemaphore] = None

    def resolve_key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""

    def semaphore(self) -> threading.BoundedSemaphore:
        if self._sem is None:
            self._sem = threading.BoundedSemaphore(max(1, int(self.concurrency)))
        return self._sem

    def mask(self) -> dict:
        """给前端看的安全形态：key 只留头尾。"""
        key = self.resolve_key()
        if not key:
            masked = ""
        elif len(key) <= 6:
            masked = key[:2] + "***"
        else:
            masked = key[:4] + "***" + key[-3:]
        return {
            "id": self.id,
            "name": self.name or self.id,
            "base_url": self.base_url,
            "api_key_masked": masked,
            "has_key": bool(key),
            "api_key_env": self.api_key_env,
            "concurrency": self.concurrency,
            "enabled": self.enabled,
            "note": self.note,
            "model_count": len(self.models),
            "models_age_s": round(time.time() - self.models_at, 1) if self.models_at else None,
            "last_error": self.last_error,
            "ok_calls": self.ok_calls,
            "failed_calls": self.failed_calls,
        }

    def to_json(self, *, reveal_key: bool = False) -> dict:
        d = {
            "id": self.id, "name": self.name, "base_url": self.base_url,
            "api_key_env": self.api_key_env, "concurrency": self.concurrency,
            "enabled": self.enabled, "note": self.note,
        }
        if self.synthetic:
            d["_synthetic"] = True
        if reveal_key:
            d["api_key"] = self.api_key
        # 模型清单也落盘：重启后下拉立刻可用，不必等网络把清单再拉一遍
        if self.models:
            d["_models"] = self.models
            d["_models_at"] = self.models_at
        return d


class EndpointRegistry:
    """一组通道 + 按座位的分配策略。"""

    def __init__(self, path: str = DEFAULT_PATH,
                 fallback: Optional[dict] = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._items: List[Endpoint] = []
        self.assignment = "round_robin"     # round_robin | by_seat
        self.load(fallback)

    # ---------------- 载入 / 保存 ----------------

    def load(self, fallback: Optional[dict] = None) -> None:
        data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                data = {}

        items = data.get("endpoints") or []
        self.assignment = data.get("assignment", "round_robin")

        # 没有 endpoints.json 时，用 seats.json 里的 relay 段兜一个通道出来，
        # 保证老配置能继续跑
        if not items and fallback:
            relay_cfg = (fallback.get("relay") or {})
            if relay_cfg.get("base_url"):
                items = [{
                    "id": "default",
                    "name": "默认通道（来自 seats.json）",
                    "base_url": relay_cfg["base_url"],
                    "api_key_env": relay_cfg.get("api_key_env", "RELAY_API_KEY"),
                    "concurrency": (fallback.get("concurrency") or {}).get("global", 6),
                    "note": "自动生成，用户添加自己的通道后会自动移除",
                    "_synthetic": True,
                }]

        self._items = []
        for i, raw in enumerate(items):
            self._items.append(Endpoint(
                id=str(raw.get("id") or f"ep{i + 1}"),
                name=raw.get("name", ""),
                base_url=(raw.get("base_url") or "").rstrip("/"),
                api_key=raw.get("api_key", "") or "",
                api_key_env=raw.get("api_key_env", "") or "",
                concurrency=int(raw.get("concurrency", 4)),
                enabled=bool(raw.get("enabled", True)),
                note=raw.get("note", ""),
                synthetic=bool(raw.get("_synthetic", False)),
                models=list(raw.get("_models") or []),
                models_at=float(raw.get("_models_at") or 0.0),
            ))

    def save(self) -> None:
        with self._lock:
            payload = {
                "_comment": "多通道配置。api_key 明文落盘，注意不要提交到版本库。",
                "assignment": self.assignment,
                "endpoints": [e.to_json(reveal_key=True) for e in self._items],
            }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---------------- 查询 ----------------

    def list(self) -> List[Endpoint]:
        with self._lock:
            return list(self._items)

    def enabled(self) -> List[Endpoint]:
        return [e for e in self._items if e.enabled and e.base_url]

    def get(self, eid: str) -> Optional[Endpoint]:
        with self._lock:
            return next((e for e in self._items if e.id == eid), None)

    def by_seat(self, seat_no: int, model: str = "") -> Optional[Endpoint]:
        """按座位挑通道。

        **模型能力优先，再轮转摊并发。** 早期是纯轮转，结果把
        `gpt-5.6-sol` 分到了 deepseek 官方那条通道上，开局直接报
        "The supported API model names are deepseek-flash, ..." —— 座位整局哑火。
        所以先只保留"清单里确实有该模型"的通道（清单还没拉的通道不排除，
        否则新加的通道永远轮不到），再在其中按座位轮转。
        """
        pool = self.enabled()
        if not pool:
            return None
        if model:
            capable = [e for e in pool if (not e.models) or (model in e.models)]
            if capable:
                pool = capable
        return pool[(seat_no - 1) % len(pool)]

    def capable_endpoints(self, model: str) -> List[Endpoint]:
        """哪些通道能提供这个模型（清单未知的也算候选）。"""
        if is_deployment_id(model):
            return self.enabled()
        return [e for e in self.enabled() if (not e.models) or (model in e.models)]

    def check_bindings(self, seats: List) -> List[dict]:
        """开局前体检：座位绑的通道到底能不能跑这个模型。

        返回问题清单，空表示没问题。宁可开局前拦住，也不要跑起来才发现
        某个座位整局哑火。
        """
        problems: List[dict] = []
        for s in seats:
            if not s.endpoint:
                continue
            ep = self.get(s.endpoint)
            if ep is None:
                problems.append({"no": s.no, "model": s.model, "endpoint": s.endpoint,
                                 "reason": "通道不存在"})
            elif (ep.models and s.model not in ep.models
                  and not is_deployment_id(s.model)):
                problems.append({
                    "no": s.no, "model": s.model,
                    "endpoint": ep.name or ep.id,
                    "reason": f"该通道不提供这个模型（可选：{', '.join(ep.models[:4])}"
                              + ("…" if len(ep.models) > 4 else "") + "）",
                })
        return problems

    # ---------------- 增删改 ----------------

    def upsert(self, payload: dict) -> Endpoint:
        with self._lock:
            eid = str(payload.get("id") or "").strip()
            existing = next((e for e in self._items if e.id == eid), None) if eid else None
            if existing is None:
                eid = eid or self._next_id()
                existing = Endpoint(id=eid)
                self._items.append(existing)
                # 用户第一次添加自己的通道时，把自动生成的兜底通道摘掉，
                # 否则它会一直占着 1/N 的座位名额，继续用 seats.json 里那把 key。
                self._items = [e for e in self._items
                               if not e.synthetic or e is existing]
            existing.name = payload.get("name", existing.name) or existing.name
            if payload.get("base_url"):
                existing.base_url = payload["base_url"].rstrip("/")
            # 留空 = 不改 key；传 "-" = 清空
            if "api_key" in payload and payload["api_key"] not in (None, ""):
                existing.api_key = "" if payload["api_key"] == "-" else payload["api_key"]
            if "api_key_env" in payload:
                existing.api_key_env = payload.get("api_key_env") or ""
            if payload.get("concurrency") is not None:
                existing.concurrency = max(1, int(payload["concurrency"]))
                existing._sem = None           # 重建信号量
            if payload.get("enabled") is not None:
                existing.enabled = bool(payload["enabled"])
            if payload.get("note") is not None:
                existing.note = payload["note"]
        self.save()
        return existing

    def remove(self, eid: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [e for e in self._items if e.id != eid]
            changed = len(self._items) != before
        if changed:
            self.save()
        return changed

    def _next_id(self) -> str:
        used = {e.id for e in self._items}
        i = 1
        while f"ep{i}" in used:
            i += 1
        return f"ep{i}"

    # ---------------- 动态拉模型 ----------------

    def refresh_models(self, eid: str, timeout: float = 30.0) -> dict:
        ep = self.get(eid)
        if ep is None:
            return {"ok": False, "error": f"没有通道 {eid}"}
        if not ep.base_url:
            return {"ok": False, "error": "base_url 为空"}

        from .relay import http_post          # 延迟导入，避免循环依赖
        url = ep.base_url + "/models"
        headers = {"Authorization": f"Bearer {ep.resolve_key()}"}
        with self._lock:
            status, body, latency, err = http_post(url, headers, None, timeout, "auto")

        if status != 200 or not isinstance(body, dict):
            msg = err or (json.dumps(body, ensure_ascii=False)[:200] if body else "未知错误")
            ep.last_error = f"HTTP {status}: {msg}"
            return {"ok": False, "error": ep.last_error, "status": status}

        ids = []
        for m in body.get("data", []):
            mid = m.get("id") if isinstance(m, dict) else None
            if mid:
                ids.append(mid)
        ep.models = sorted(set(ids))
        ep.models_at = time.time()
        ep.last_error = ""
        self.save()          # 落盘，重启后下拉立刻可用
        return {"ok": True, "count": len(ep.models), "models": ep.models,
                "latency_s": round(latency, 2)}

    def refresh_all(self) -> dict:
        out = {}
        for ep in self.enabled():
            r = self.refresh_models(ep.id)
            out[ep.id] = r.get("count") if r.get("ok") else r.get("error")
        return out

    def model_catalog(self) -> dict:
        """所有通道能提供的模型，给座位里的下拉用。

        **不按时间过滤。** 之前这里加了个 300 秒 TTL，结果 中转站 A 的 112 个模型
        在缓存"过期"后被整个剔除，下拉里只剩另一条通道的 2 个 —— 看起来就像
        Gemini 3.8 flash 凭空消失了。模型清单只在手动/自动刷新时才变，
        缓存旧一点也远好过没有。
        """
        catalog: Dict[str, List[str]] = {}
        for ep in self.enabled():
            if ep.models:
                catalog[ep.id] = list(ep.models)
        return catalog

    def stale_endpoints(self, ttl: float = MODEL_CACHE_TTL) -> List[Endpoint]:
        """模型清单为空或太旧的通道，供后台自动刷新用。"""
        now = time.time()
        return [e for e in self.enabled()
                if not e.models or (now - e.models_at) > ttl]

    def snapshot(self) -> List[dict]:
        return [e.mask() for e in self.list()]
