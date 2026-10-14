"""中转站客户端。对应 DESIGN.md 第 6.2 / 6.3 节。

相对原项目 lib/llm.py 补齐的六件事（全部来自实测，见 ../PROBE_FINDINGS.md）：

  1. **curl 回退** —— 某些沙箱里 Python 的 SSL 栈握手直接失败，curl 却能通
  2. **读 reasoning_content** —— 推理型模型会把额度花在推理上，content 返回空串
  3. **max_tokens 下限** —— 太小则推理型模型一个正文都不吐
  4. **temperature 锁定** —— 部分模型（如 kimi-k3）只接受特定值，传其它直接 400
  5. **重试 + 备用模型代打** —— 中转站个别模型时不时断联是常态
  6. **缓存命中统计** —— 记录 cached_tokens / prompt_tokens 用于监控
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .endpoints import is_deployment_id
from .protocol import THINK_RE
from .seats import Seat

_CURL = shutil.which("curl") or shutil.which("curl.exe")

# 直连一旦在连接层失败过（沙箱/企业代理最常见的症状是 SSL 握手被拦），
# 后续就不要再试了 —— 每次白等十几秒才回退 curl，整局下来全是浪费。
_URLLIB_DEAD = False

RETRY_STATUS = {0, 408, 409, 429, 500, 502, 503, 504, 524}
DEFAULT_FALLBACK: List[str] = []

# ★ 出站请求的默认 User-Agent（2026-10-11 加）。
#
# 实测：中转站 C（ep11）前半挂着 Cloudflare 的 bot 规则。不设 UA 时
# urllib 会自动带 `Python-urllib/3.13`，直接被 CF 按 UA 签名拦掉：
#
#     403  Server: cloudflare  body: 'error code: 1010\n'   (17 字节)
#
# CF 1010 = "banned your access based on your browser's signature"，
# 跟 key、余额、IP 信誉都无关 —— 同一个 IP、同一把 key，只把 UA 换成
# 浏览器串就 200。这条通道的 /models 和 /chat/completions 一起被拦，
# 表现出来就是「通道填好了但一个模型也拉不到、发言全失败」。
#
# 作用域刻意收在传输层（_post_urllib / _post_curl）：refresh_models 和
# chat 两条路都过这里。调用方显式传了 User-Agent 就不覆盖。
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# ★ 出站代理（2026-10-11 加）。
#
# 起因：中转站 C（ep11/ep12）和 relay-a.example（ep3）被**按主机名**在
# 网络层重置 —— 不是 key、不是余额、也不是 Cloudflare 的 bot 规则：
#
#     中转站 C:443        → curl (35) Recv failure: Connection was reset
#     同一 IP 换 SNI      → TLS 握手成功、HTTP 301     ← IP/端口没问题
#     中转站 C:80（明文） → 同样被 reset               ← 不只是 TLS 维度
#
# DEFAULT_UA 修的是"CF 回 403 error code: 1010"，对握手中的 RST 完全没用
# （RST 发生在任何 HTTP 字节之前，UA 根本发不出去）。这种情况只有走代理。
#
# 配置优先级：relay.proxy > 环境变量 > 不走代理。
# 代理是 Clash 之类的 mixed 端口时，规则由代理端决定，国内通道一般直连，
# 不做额外 no_proxy 处理（免得和代理端规则打架）。
PROXY_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                  "ALL_PROXY", "all_proxy")

_PROXY = ""          # 由 Relay.__init__ 按 config 设定；空串 = 直连


def set_proxy(url: Optional[str]) -> None:
    """设定出站代理（形如 ``http://127.0.0.1:7890``）。空值 = 直连。"""
    global _PROXY
    _PROXY = (url or "").strip()


def current_proxy() -> str:
    """当前生效的代理：显式配置优先，其次环境变量。"""
    if _PROXY:
        return _PROXY
    for k in PROXY_ENV_KEYS:
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return ""

# 各家网关的"推理内容"字段名。GLM 用的是 reasoning（不是 reasoning_content），
# 只读后者会让"额度全花在思维链上、正文为空"的响应看起来像彻底空响应。
REASONING_FIELDS = ("reasoning_content", "reasoning", "thinking", "thought", "analysis")

# 输出被 max_tokens 截断时最多把额度翻到多少。
# ★ 这个值原来写死 16384，2026-10-06 被真实对局证伪：
#   10 座局第 3 天，7 号 GLM 5.2 的思维链吃到 16380、正文只剩 3 个字符 `\n{"`，
#   撞上 16384 就不再翻倍，于是把半截 JSON 当成功收下 —— 那一回合直接"未发言"。
#   用同一份 messages 原样重放、把额度放到 32768：**16898 tokens 就正常 stop 了**，
#   正文 667 字符、解析零 issues。也就是说它只差 514 个 token 被判了死刑。
#
#   所以：上限太低会把"差一点就写完"的模型误杀。它现在可配（game.max_tokens_cap），
#   默认给到 32768 —— 绝大多数网关支持，而超出模型的真实输出上限时会被网关顶回来。
MAX_TOKENS_CAP = 32768

# ★ 上游**明确拒绝**这个请求时的 finish_reason（2026-10-06 加）。
#
# 实测：12 号 MiMo 在第 2 天投票时返回
#     finish_reason = "content_filter"
#     content = "The request was rejected because it was considered high risk"
# 生成了 827 tokens（719 是推理）之后被风控拦下。狼人杀 prompt 里全是
# "击杀/毒药/刀人/自曝"，撞风控是迟早的事。
#
# 老逻辑只认 `length`，于是这种响应被当成**成功**收下 → 正文解析不了 →
# 引擎兜底"本轮未发言"。而它其实应该立刻走代打：
# **同样的内容重试同一个模型只会被再拒一次**，换一家（不同厂商的风控）
# 才有机会。所以这一类直接换模型、不退避也不重试。
REJECT_FINISH_REASONS = ("content_filter", "content_filtered", "contentfilter",
                         "safety", "blocked", "prohibited", "risk")


@dataclass
class LLMResult:
    text: str = ""
    reasoning: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    model_used: str = ""
    substituted: bool = False
    attempts: int = 0
    latency_s: float = 0.0
    error: str = ""
    finish_reason: str = ""
    # 这次调用一共试过哪些模型（按顺序）。全部失败时要能说清"不是没代打，
    # 是代打的几个全挂了" —— 否则日志只剩一句"调用失败"，根本没法定位。
    models_tried: List[str] = field(default_factory=list)
    # 主模型失败的原因。成功代打之后 last_error 就被丢掉了，
    # 于是日志里只剩"主模型失败"——不知道它是超时、429 还是 400。
    primary_error: str = ""
    # ★ 每一次尝试的原始账目（含被丢弃的那些）。
    #
    # 以前只记"最终那次"的 usage，于是**翻倍重试烧掉的中间尝试完全不可见**：
    # 一次发言可能先花 8192 tokens 被截断、再花 16384 被截断、最后 32768 才写对，
    # 而日志里只有一个 32768 —— 那 24576 个 token 是白花的，账单上却查不到。
    # 记下来才能回答"这一局到底花在哪儿了"。
    attempts_detail: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def completion_tokens(self) -> int:
        return int(self.usage.get("completion_tokens") or 0)

    @property
    def reasoning_tokens(self) -> int:
        details = self.usage.get("completion_tokens_details") or {}
        return int(details.get("reasoning_tokens") or 0)

    @property
    def wasted_completion_tokens(self) -> int:
        """被丢弃的尝试烧掉的输出 token（不含最终交出去的那次）。"""
        total = sum(int(a.get("completion_tokens") or 0) for a in self.attempts_detail)
        return max(0, total - self.completion_tokens) if self.text else total

    @property
    def truncated(self) -> bool:
        """输出被 max_tokens 截断。这是 JSON 解析失败的头号原因。"""
        return self.finish_reason == "length"

    @property
    def prompt_tokens(self) -> int:
        return int(self.usage.get("prompt_tokens") or 0)

    @property
    def cached_tokens(self) -> int:
        details = self.usage.get("prompt_tokens_details") or {}
        v = details.get("cached_tokens")
        if v:
            return int(v)
        for key in ("prompt_cache_hit_tokens", "cache_read_input_tokens"):
            if self.usage.get(key):
                return int(self.usage[key])
        return 0

    @property
    def cache_ratio(self) -> float:
        pt = self.prompt_tokens
        return (self.cached_tokens / pt) if pt else 0.0


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _urlopen(req, timeout):
    """urllib 的唯一入口。

    ★ 单独抽成一个函数是为了**可测**：`selftest_transport.py` 直接替换它来
    检查请求头，比去 monkeypatch `urllib.request.urlopen` 干净。

    走代理时**必须**自己 build_opener —— 直接 urlopen 用的是默认 opener，
    它只认环境变量，`config` 里配的 proxy 会被默默忽略掉。
    """
    proxy = _PROXY
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(req, timeout=timeout)
    # 没配显式代理就走默认 urlopen：它自己认 HTTPS_PROXY 那套环境变量。
    return urllib.request.urlopen(req, timeout=timeout)


def _post_urllib(url, headers, data, timeout) -> Tuple[int, Any, float, Optional[str]]:
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    for k, v in headers.items():
        req.add_header(k, v)
    # urllib 默认会带 `Python-urllib/3.x`，那串 UA 会被 Cloudflare 直接
    # 按 bot 签名拦掉（error code: 1010）—— 见 DEFAULT_UA 的说明。
    if not req.get_header("User-agent"):
        req.add_header("User-Agent", DEFAULT_UA)
    started = time.monotonic()
    try:
        with _urlopen(req, timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            latency = time.monotonic() - started
            try:
                return resp.status, json.loads(raw), latency, None
            except json.JSONDecodeError:
                return resp.status, raw, latency, "响应不是合法 JSON"
    except urllib.error.HTTPError as e:
        latency = time.monotonic() - started
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
        return e.code, body, latency, None
    except Exception as e:
        return 0, None, time.monotonic() - started, f"{type(e).__name__}: {e}"


def _mk_tmpdir() -> str:
    """建一个**真的能写**的临时目录。

    ★ 不要用 `tempfile.mkdtemp()`，用手动 `os.makedirs`。

    依据是一次实测：在受限沙箱策略下，mkdtemp 建的目录**任何文件都写不进去**
    （PermissionError），而手动 makedirs 建的同样路径却完全正常：

        手动 os.makedirs 建子目录  -> ✅ 能写
        tempfile.mkdtemp 建子目录  -> ❌ EACCES
        直接在临时根下写文件        -> ✅ 能写

    这个坑很隐蔽：curl 回退要往临时目录写请求体，"建目录成功"会让人以为路径
    没问题，真正写 body 时才炸。表现出来就是"curl 回退不可用：写临时文件失败"，
    而那条路的**全部意义**就是"urllib 的 TLS 挂了还能救"
    （中转站 A 上 `SSL: UNEXPECTED_EOF_WHILE_READING` 就是这种情况）。

    makedirs 在宽松环境同样可用，所以这是**严格更可移植**的写法；
    探测时用真正要写的那个文件名，不用小文件糊弄过去。
    """
    for base in (os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "runs", "_curl_tmp"),
                 tempfile.gettempdir()):
        try:
            os.makedirs(base, exist_ok=True)
            d = os.path.join(base, "aiwolf_" + uuid.uuid4().hex[:10])
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, "body.json")      # 用真正要用的文件名探
            with open(probe, "wb") as f:
                f.write(b'{"probe":1}')
            os.remove(probe)
            return d
        except OSError:
            continue
    raise OSError("找不到任何可写的临时目录")


def _post_curl(url, headers, data, timeout, cancel=None) -> Tuple[int, Any, float, Optional[str]]:
    """用 curl 走一遍。支持被 cancel 事件中止（杀掉子进程）。

    沙箱或企业代理环境下，Python 的 SSL 栈经常直接握手失败，而 curl 走系统
    证书链能通。另外 curl 是子进程，可以随时杀掉——这是"强制结束"能立刻
    生效的关键（urllib 的在途请求没法取消）。
    """
    try:
        tmpdir = _mk_tmpdir()
    except OSError as e:
        return 0, None, 0.0, f"curl 回退不可用：没有可写的临时目录（{e}）"
    body_path = os.path.join(tmpdir, "body.json")
    out_path = os.path.join(tmpdir, "out.json")
    try:
        if data is not None:
            try:
                with open(body_path, "wb") as f:
                    f.write(data)
            except OSError as e:
                # 临时目录不可写（受限沙箱就是这样）—— 返回错误，**不要抛**。
                # 抛出去会把整局带走（实测死过 UnboundLocalError）。
                return 0, None, 0.0, f"curl 回退不可用：写临时文件失败 {e}"

        cmd = [_CURL, "-sS", "-A", DEFAULT_UA,
               "-m", str(int(timeout)),
               "--connect-timeout", "15",          # 网关不通时快速失败，别挂着
               "-o", out_path, "-w", "%{http_code}", url]
        # 代理也走 curl（ua/代理都在传输层，refresh_models 和 chat 两条路同享）
        proxy = current_proxy()
        if proxy:
            cmd += ["-x", proxy]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        if data is not None:
            cmd += ["--data-binary", "@" + body_path]

        started = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            return 0, None, time.monotonic() - started, f"curl 启动失败: {e}"

        deadline = started + timeout + 20
        killed = None
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                killed = "已被强制结束"
                proc.kill()
                break
            if time.monotonic() > deadline:
                killed = "超过总时限"
                proc.kill()
                break
            time.sleep(0.15)

        out, err = proc.communicate()
        latency = time.monotonic() - started
        if killed:
            return 0, None, latency, killed

        raw = ""
        if os.path.exists(out_path):
            with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        code_txt = (out or b"").decode("utf-8", "replace").strip()
        status = int(code_txt) if code_txt.isdigit() else 0
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = raw
        err_text = None
        if status == 0:
            err_text = (err or b"").decode("utf-8", "replace").strip() or "curl 未返回状态码"
        return status, body, latency, err_text
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _looks_like_timeout(status: int, error_text: str) -> bool:
    """是不是"连上了但一个字节都没回来"那种超时/挂起。

    这类失败重试同一个模型基本没用（实测 grok-4-5 挂满 240s 后，
    再试一次还是挂），有代打就该立刻换人。TCP 层面的 RST/10053 也算进
    —— 那种同样不是"等一下就好"。429 不算。
    """
    if status != 0:
        return False
    low = (error_text or "").lower()
    return any(k in low for k in ("timed out", "timeout", "10053", "10054",
                                  "connection aborted", "connection reset"))


def http_post(url, headers, data, timeout, transport="auto",
              cancel=None) -> Tuple[int, Any, float, Optional[str]]:
    """发请求。绝不抛异常。

    有 ``cancel`` 时**优先走 curl**：curl 是子进程，可以随时杀掉；
    urllib 的在途请求没法取消，会让"强制结束"干等几十秒。
    """
    global _URLLIB_DEAD

    if cancel is not None and cancel.is_set():
        return 0, None, 0.0, "已被强制结束"

    if (transport == "curl" or _URLLIB_DEAD
            or (cancel is not None and _CURL)):
        return _post_curl(url, headers, data, timeout, cancel)

    status, body, latency, err = _post_urllib(url, headers, data, timeout)
    if status == 0 and _CURL:
        if cancel is not None and cancel.is_set():
            return 0, None, latency, "已被强制结束"
        s2, b2, l2, e2 = _post_curl(url, headers, data, timeout, cancel)
        if s2 != 0:
            # ★ 只有 curl **真的成功了**，才认定"这个环境直连不可用"。
            # 老写法是"urllib 失败一次就永久拉黑它"，后果很严重：
            # 某个端点连不上（连接被拒）或一次网络抖动 → 整局所有座位都改走 curl
            # → 而 curl 回退要往临时目录写请求体，写不了就全盘皆输（实测踩过）。
            _URLLIB_DEAD = True
            return s2, b2, l2, e2
        return 0, None, latency, f"{err}；curl 回退同样失败: {e2}"
    return status, body, latency, err


def extract_text(body: Any) -> Tuple[str, str]:
    """返回 (content, reasoning)。content 为空时退回 reasoning。

    推理字段名各家不同，见 REASONING_FIELDS。漏读的后果很隐蔽：
    模型把额度全花在思维链上、content 返回空串时，会看起来像"彻底空响应"，
    于是被当成调用失败白白重试三次。

    ★ 2026-10-11 加：**思维链内联在 content 里的网关**也要拆。
    实测 8号 MiniMax（ep10）11/11 次调用都返回 `<think>…</think>` 后面再跟
    JSON，思维链根本没进 reasoning_content。不拆的代价是实打实的：解析链整段
    失败，而草稿里那份 JSON 模板会被按字段抢救抓去当答案 —— 8号第 4 天的投票
    理由就这样在全桌公开发成了 `...`（三个点）。详见 protocol.THINK_RE。
    """
    try:
        msg = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        # 少数网关用 legacy completions 结构
        try:
            return (body["choices"][0].get("text") or ""), ""
        except (KeyError, IndexError, TypeError):
            return "", ""
    if not isinstance(msg, dict):
        return "", ""

    content = msg.get("content") or ""
    reasoning = ""
    for field in REASONING_FIELDS:
        v = msg.get(field)
        if v:
            reasoning = v if isinstance(v, str) else str(v)
            break

    # 内联思维链：搬进 reasoning，content 只留标签外面的答案。
    # 只剥**成对标签**（THINK_RE），正文里正常出现的 "<think" 不会被误伤。
    if THINK_RE.search(content):
        inline = "\n".join(m.group(0) for m in THINK_RE.finditer(content)).strip()
        rest = THINK_RE.sub("", content).strip()
        if inline:
            reasoning = f"{inline}\n\n{reasoning}".strip() if reasoning else inline
            content = rest
    return content, reasoning


def finish_reason_of(body: Any) -> str:
    try:
        return body["choices"][0].get("finish_reason") or ""
    except (KeyError, IndexError, TypeError):
        return ""


# --------------------------------------------------------------------------- #
# Stub（离线自检用，不消耗额度）
# --------------------------------------------------------------------------- #

class StubBrain:
    """确定性假模型。用来在没有网络/不烧额度的情况下验证引擎与渲染。"""

    def __init__(self, seed: int = 7) -> None:
        self.rng = random.Random(seed)

    def respond(self, seat: Seat, phase: str, ctx: Dict[str, Any]) -> str:
        others = [n for n in ctx.get("targets", []) if n != seat.no]
        target = self.rng.choice(others) if others else None
        speech = self.rng.choice([
            f"我是{seat.no}号，{target}号的发言我觉得前后矛盾，先标一下。",
            f"{seat.no}号。今天信息太少，我先听，{target}号你多说说。",
            f"我{seat.no}号，暂时站{target}号是好人，出他我会投反对。",
        ])
        plan = {
            "DAY_SPEECH": (speech, {"type": "speak", "target": None}),
            "DAY_LAST_WORDS": (f"我{seat.no}号走了，{target}号你们盯一下。",
                               {"type": "speak", "target": None}),
            # 投票理由会进大桌 —— stub 也要走这条路径，否则离线跑不出这个环节
            "DAY_VOTE": (f"我{seat.no}号投{target}号：他刚才的发言最划水。",
                         {"type": "vote", "target": target}),
            "DAY_PK_SPEECH": (f"我{seat.no}号是好人，PK 台上我更该活下来。",
                              {"type": "speak", "target": None}),
            "DAY_VOTE_PK": (f"PK 里我票{target}号。", {"type": "vote", "target": target}),
            "NIGHT_WOLF": ("", {"type": "kill", "target": target}),
            "NIGHT_WOLF_CHAT": (f"我建议刀{target}号，发言稳的先处理。",
                                {"type": "chat", "target": None}),
            "NIGHT_SEER": ("", {"type": "check", "target": target}),
            "NIGHT_WITCH": ("", {"type": "pass", "target": None}),
            "NIGHT_GUARD": ("", {"type": "guard", "target": target}),
            "DAY_HUNTER": ("", {"type": "pass", "target": None}),
        }
        sp, act = plan.get(phase, (speech, {"type": "pass", "target": None}))
        return json.dumps({
            "thinking": f"（stub 内心独白，阶段 {phase}）",
            "speech": sp,
            "action": act,
            "confidence": round(self.rng.uniform(0.4, 0.9), 2),
        }, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Relay
# --------------------------------------------------------------------------- #

class Relay:
    def __init__(
        self,
        cfg: dict,
        *,
        api_key: Optional[str] = None,
        stub: bool = False,
        log_dir: Optional[str] = None,
        verbose: bool = True,
        registry=None,
    ) -> None:
        relay_cfg = cfg.get("relay", {})
        self.base_url = relay_cfg.get("base_url", "").rstrip("/")
        self.api_key = api_key or os.environ.get(relay_cfg.get("api_key_env", "RELAY_API_KEY"), "")
        self.transport = relay_cfg.get("http_transport", "auto")
        # 出站代理（见 PROXY_ENV_KEYS 的说明）。配了就用，没配就看环境变量。
        set_proxy(relay_cfg.get("proxy") or "")
        self.proxy = current_proxy()
        if self.proxy and verbose:
            # 显式打一行：挂没挂上代理是最容易"以为配了其实没生效"的地方。
            print(f"  [relay] 出站代理：{self.proxy}")
        self.timeout = int(cfg.get("game", {}).get("request_timeout_s", 600))
        self.max_tokens_floor = int(cfg.get("game", {}).get("max_tokens_floor", 512))
        # 截断后最多把输出额度翻到多少（见 MAX_TOKENS_CAP 上面的说明）
        self.max_tokens_cap = int(cfg.get("game", {}).get("max_tokens_cap", MAX_TOKENS_CAP))
        self.temperature_locks = cfg.get("temperature_locked_models", {}) or {}
        # 多通道：每个座位可以走不同的 base_url+key，各自维护并发预算
        self.registry = registry

        conc = cfg.get("concurrency", {}) or {}
        self.global_sem = threading.BoundedSemaphore(int(conc.get("global", 8)))
        self.seat_sems: Dict[str, threading.BoundedSemaphore] = {
            label: threading.BoundedSemaphore(int(n))
            for label, n in (conc.get("per_seat_overrides", {}) or {}).items()
        }

        self.stub = stub
        self.brain = StubBrain()
        self.verbose = verbose
        self.log_dir = log_dir
        # ★ 代打（fallback 模型替换）默认关闭。
        # 关闭时某个座位调用失败就是失败，会记进 events.jsonl 和 summary.json，
        # 便于 M2 阶段看清"到底哪家模型在断"。要开启就把 config 里的
        # fallback_enabled 置 true。
        self.fallback_enabled = bool(cfg.get("fallback_enabled", False))
        # 代打策略（都放在 config 的 game 段里，不用改代码）
        _game = cfg.get("game", {}) or {}
        self.max_attempts_per_model = max(1, int(_game.get("max_attempts_per_model", 3)))
        self.switch_model_on_timeout = bool(_game.get("switch_model_on_timeout", True))
        self.call_deadline_s = float(_game.get("call_deadline_s", 0) or 0)
        self._log_locks: Dict[int, threading.Lock] = {}
        self.stats: List[Dict[str, Any]] = []

        # 飞行中的请求计数。暂停只对"下一次调用"生效，拦不住已经在飞的请求——
        # 页面上要能看出到底是"停在闸门"还是"卡在 HTTP"，所以暴露这个。
        self._flight_lock = threading.Lock()
        self._inflight = 0
        self._inflight_label = ""
        # 外部（Web 端的"强制结束"）置位后，在途的 curl 子进程会被立刻杀掉。
        self.cancel_event: Optional[threading.Event] = None
        # 累计调用次数，给页面顶栏显示"API 调用"
        self.call_count = 0
        # 每个座位"实际需要多大输出预算"的记忆。重推理模型（GLM / Kimi 等）
        # 会把额度先花在思维链上，3072 根本不够；每次靠翻倍试错要重发整个
        # prompt，代价很大。记住上一次成功的预算，后续直接从这里起步。
        self._budget_hint: Dict[int, int] = {}

        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

    # ---------------- 运行状态（给 UI / 调试用） ----------------

    def inflight_snapshot(self) -> Dict[str, Any]:
        with self._flight_lock:
            return {"inflight": self._inflight, "label": self._inflight_label,
                    "calls": self.call_count}

    # ---------------- 内部工具 ----------------

    def _temperature(self, seat: Seat) -> Tuple[float, bool]:
        for model_name, locked in self.temperature_locks.items():
            if seat.model == model_name or seat.model.startswith(model_name):
                return float(locked), True
        return seat.temperature, False

    def _log(self, seat: Seat, record: Dict[str, Any]) -> None:
        if not self.log_dir:
            return
        path = os.path.join(self.log_dir, f"seat_{seat.no}.jsonl")
        lock = self._log_locks.setdefault(seat.no, threading.Lock())
        with lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _backoff_seconds(self, status: int, error_text: str, hard_fails: int) -> float:
        """退避时长。

        限流/并发上限要等得久得多 —— 按普通网络错误那样 1~4 秒重试，
        三次都会撞在同一堵墙上（实测 Grok 的 concurrency_limit_exceeded 就是这样）。
        """
        low = (error_text or "").lower()
        rate_limited = (
            status == 429
            or "concurrency" in low
            or "rate limit" in low
            or "too many requests" in low
            or "已达到并发上限" in (error_text or "")
        )
        if rate_limited:
            return 5.0 * hard_fails + random.random() * 2.0     # ~5s / ~10s / ~15s
        return min(2 ** hard_fails, 8) * (0.5 + random.random())

    def _acquire(self, seat: Seat):
        sem = self.seat_sems.get(seat.label)
        return sem

    def _deadline_hit(self, started_at: float) -> bool:
        """整次调用是否已经超过时限（0 = 不设限）。"""
        return bool(self.call_deadline_s) and (time.monotonic() - started_at) > self.call_deadline_s

    def _route(self, seat: Seat, model: str = "", endpoint_id: str = ""):
        """挑这条请求走哪条通道。返回 (base_url, api_key, semaphore, endpoint)。

        优先级：
          1. `endpoint_id`（代打条目里写死的 `模型@通道`）—— 说了就用它
          2. 座位绑定的通道
          3. 座位通道跑不了这个模型时，挑一条"清单里确实有它"的通道

        第 3 条是踩坑加的：1 号座绑在 deepseek 官方（只有 2 个模型），
        旧 fallback 写的是 `gemini-*`，代打必然二次失败。
        但**推理接入点（ep-*）不在任何通道的目录里**，别去瞎猜 —— 保持座位绑定。
        """
        if self.registry is None:
            return self.base_url, self.api_key, None, None

        ep = None
        if endpoint_id:
            ep = self.registry.get(endpoint_id)
        if ep is None and seat.endpoint:
            ep = self.registry.get(seat.endpoint)
        if (ep is not None and model and not endpoint_id
                and not is_deployment_id(model)
                and ep.models and model not in ep.models
                and hasattr(self.registry, "capable_endpoints")):
            alt = [e for e in self.registry.capable_endpoints(model)
                   if e.id != ep.id and (e.resolve_key() or self.api_key)]
            if alt:
                ep = alt[(seat.no - 1) % len(alt)]
        if ep is not None and ep.base_url:
            return ep.base_url, (ep.resolve_key() or self.api_key), ep.semaphore(), ep
        return self.base_url, self.api_key, None, None

    # ---------------- 主入口 ----------------

    def chat(
        self,
        seat: Seat,
        messages: List[dict],
        *,
        phase: str,
        day: int,
        ctx: Optional[Dict[str, Any]] = None,
        max_tokens: int = 1024,
        response_format: Optional[dict] = None,
    ) -> LLMResult:
        ctx = ctx or {}
        max_tokens = max(max_tokens, self.max_tokens_floor)

        if self.stub:
            started = time.monotonic()
            text = self.brain.respond(seat, phase, ctx)
            result = LLMResult(
                text=text, reasoning="", model_used=f"stub:{seat.model}",
                attempts=1, latency_s=time.monotonic() - started,
            )
            self._log(seat, {"day": day, "phase": phase, "stub": True,
                             "seat": seat.no, "label": seat.label,
                             "endpoint": seat.endpoint,
                             "model_used": result.model_used,
                             "latency_s": round(result.latency_s, 3),
                             "finish_reason": "stub", "truncated": False,
                             "prompt_tokens": 0, "cached_tokens": 0, "cache_ratio": 0.0,
                             "error": "", "raw": text, "messages": messages})
            self.stats.append({
                "seat": seat.no, "label": seat.label, "phase": phase,
                "model": result.model_used, "substituted": False,
                "latency_s": round(result.latency_s, 3),
                "prompt_tokens": 0, "cached_tokens": 0,
                "truncated": False, "error": "",
            })
            self.call_count += 1        # 离线模式也要计数，顶栏才不是一直 0
            return result

        temperature, locked = self._temperature(seat)
        # 代打链：(模型, 指定通道)。主模型永远走座位绑定的通道；
        # 代打条目可以写 `模型@通道` 来跨厂商（grok 在 ep3、豆包在 ep4）。
        chain: List[tuple] = [(seat.model, "")]
        if self.fallback_enabled:
            routes = getattr(seat, "fallback_endpoints", {}) or {}
            chain += [(m, routes.get(m, "")) for m in seat.fallback if m != seat.model]
        payload_common: Dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            payload_common["response_format"] = response_format
        # 按座位的额外请求参数（例如豆包/方舟的 {"thinking": {"type": "disabled"}}
        # —— 实测把狼队频道那一步从 71.0s 压到 2.7s）
        if getattr(seat, "extra_body", None):
            payload_common.update(seat.extra_body)

        last_error = ""
        primary_error = ""          # 主模型失败的最后一个原因（供代打时说明）
        attempt_no = 0
        call_started = time.monotonic()      # 整次调用（含代打切换）的时限基准
        # 每一次尝试的账目（含最终被丢弃的那些）—— 这局的 token 到底花在哪儿
        attempts_detail: List[Dict[str, Any]] = []

        for chain_idx, (model, ep_override) in enumerate(chain):
            if self._deadline_hit(call_started):
                last_error = (f"整次调用已超过 {self.call_deadline_s:.0f}s 上限，"
                              f"放弃剩余尝试（含代打）。最后一次错误：{last_error}")
                break
            base_url, api_key, ep_sem, ep = self._route(seat, model, ep_override)
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
            url = f"{base_url}/chat/completions"

            # 预算在遇到截断时会翻倍，所以用 while 而不是固定次数的 for。
            # 起步值用该座位上次成功的预算（见 _budget_hint），避免重复试错。
            budget = max(max_tokens, self._budget_hint.get(seat.no, 0))
            hard_fails = 0
            attempts_here = 0
            while attempts_here < 6:
                if self._deadline_hit(call_started):
                    last_error = (f"整次调用已超过 {self.call_deadline_s:.0f}s 上限，"
                                  f"放弃剩余尝试（含代打）。最后一次错误：{last_error}")
                    break
                attempts_here += 1
                attempt_no += 1
                payload = dict(payload_common, model=model, max_tokens=budget)
                data = json.dumps(payload).encode("utf-8")

                seat_sem = self._acquire(seat)
                if ep_sem:
                    ep_sem.acquire()
                if seat_sem:
                    seat_sem.acquire()
                self.global_sem.acquire()
                with self._flight_lock:
                    self._inflight += 1
                    self._inflight_label = (f"{seat.no}号 {seat.label} · {model} · {phase}"
                                            + (f" · {ep.id}" if ep else ""))
                # ★ 传输层**抛异常**时也必须兜住。
                # http_post 的约定是"绝不抛异常"，但只要它真抛了（沙箱拒绝联网、
                # curl 起不来、socket 层意外……），下面的 finally 里读 err 就会
                # UnboundLocalError —— 那会把**整局**带走（实测就是这么死的）。
                status, body, latency, err = 0, None, 0.0, ""
                try:
                    status, body, latency, err = http_post(
                        url, headers, data, self.timeout, self.transport,
                        cancel=self.cancel_event)
                except Exception as exc:                      # noqa: BLE001
                    err = f"传输层异常 {type(exc).__name__}: {exc}"
                finally:
                    with self._flight_lock:
                        self._inflight -= 1
                    self.global_sem.release()
                    if seat_sem:
                        seat_sem.release()
                    if ep_sem:
                        ep_sem.release()
                    if ep is not None:
                        if status == 200:
                            ep.ok_calls += 1
                        else:
                            ep.failed_calls += 1
                            ep.last_error = str(err or f"HTTP {status}")[:200]

                # ★ 记一次尝试的原始账目。
                # 位置放在"刚拿到响应"这里，而不是成功分支里 —— 被截断丢弃的、
                # 失败的那些同样要计费，它们才是账单里最容易被忽略的部分。
                _u = (body.get("usage") or {}) if isinstance(body, dict) else {}
                _d = (body.get("usage") or {}).get("completion_tokens_details") or {} \
                    if isinstance(body, dict) else {}
                att = {
                    "model": model,
                    "endpoint": (ep.id if ep else ""),
                    "max_tokens": budget,
                    "status": status,
                    "completion_tokens": int(_u.get("completion_tokens") or 0),
                    "reasoning_tokens": int(_d.get("reasoning_tokens") or 0),
                    "finish_reason": finish_reason_of(body) if isinstance(body, dict) else "",
                    "latency_s": round(latency, 2),
                    "outcome": "",
                    "error": str(err or "")[:200],
                }
                attempts_detail.append(att)

                if status == 200 and isinstance(body, dict):
                    content, reasoning = extract_text(body)
                    finish = finish_reason_of(body)

                    # ★ 上游风控拒绝：**立刻换下一个模型**，不要退避重试。
                    #   同样的 messages 发给同一个模型，只会被再拒一次；
                    #   换一家（不同厂商的风控策略不同）才有机会过。
                    if (finish or "").lower() in REJECT_FINISH_REASONS:
                        last_error = (f"上游拒绝了这个请求（finish_reason={finish}）："
                                      f"{(content or '')[:150]}")
                        is_last = chain_idx >= len(chain) - 1
                        att["outcome"] = ("upstream_rejected_last" if is_last
                                          else "upstream_rejected")
                        if self.verbose:
                            print(f"    [{seat.display}/{model}] {last_error}")
                        if not is_last:
                            # 换模型。primary_error 由下面循环外的统一逻辑记，
                            # 这里不重复设，免得两个地方各写一份。
                            break
                        # 已经是最后一个模型：还是把它交出去，让 engine 去兜底
                        # （正文多半解析不了，但至少留下了原始响应可以查）

                    if content.strip() or reasoning.strip():
                        # ★ 只要是 `finish_reason=length` 就翻倍重试，
                        # **不能只在"正文为空"时才重试**。
                        #
                        # 两种截断都是废的：
                        #   · 正文空 —— 额度全花在思维链上（GLM 常这样）
                        #   · 正文非空但被腰斩 —— 半截 JSON / 半句话。
                        #     老逻辑会当成功收下，然后解析降级链去抢救半截 JSON，
                        #     结果是动作丢失、甚至抢救出一个模型根本没投的动作。
                        if finish == "length" and budget < self.max_tokens_cap:
                            att["outcome"] = "truncated_retry"
                            budget = min(budget * 2, self.max_tokens_cap)
                            if self.verbose:
                                why = "额度全花在推理上" if not content.strip() else "正文被截断"
                                print(f"    [{seat.display}/{model}] {why}，"
                                      f"预算提到 {budget} 重试")
                            continue
                        # ★ 额度已经顶到上限、还是 `length`：这个模型的输出就是半截的。
                        # 还有代打就换人 —— 半截 JSON 走解析降级链最可能的结局是
                        # "未发言"，白白丢掉一整回合；换一个思维链短的同族/异族模型
                        # 大概率一次就出完整 JSON。（2026-10-06 起的新行为）
                        if finish == "length" and chain_idx < len(chain) - 1:
                            r_tok = ((body.get("usage") or {})
                                     .get("completion_tokens_details") or {}).get("reasoning_tokens")
                            last_error = (f"输出在 {budget} tokens（已到上限）处仍被截断"
                                          + (f"，其中思维链 {r_tok} tokens" if r_tok else "")
                                          + "；半截输出不可用，换下一个模型")
                            if self.verbose:
                                print(f"    [{seat.display}/{model}] {last_error}")
                            att["outcome"] = "truncated_at_cap_switch_model"
                            # 记住"这个座位在这个阶段就是需要大额度"，
                            # 否则下一次调用又要从 3072 逐级翻倍白白烧几分钟。
                            self._budget_hint[seat.no] = budget
                            hard_fails += 1
                            break
                        att["outcome"] = ("accepted_truncated" if finish == "length"
                                          else "ok")
                        result = LLMResult(
                            text=content, reasoning=reasoning,
                            usage=body.get("usage") or {},
                            model_used=model,
                            substituted=(model != seat.model),
                            attempts=attempt_no,
                            latency_s=latency,
                            finish_reason=finish,
                            # 试过哪几个（含成功的这个）、主模型为什么失败
                            models_tried=[m for m, _ in chain[:chain_idx + 1]],
                            primary_error=primary_error if model != seat.model else "",
                            attempts_detail=attempts_detail,
                        )
                        self._record(seat, day, phase, messages, result, temperature, locked,
                                     endpoint=(ep.id if ep else ""))
                        # 记住这次用了多大预算，下次直接从这里起步
                        self._budget_hint[seat.no] = budget
                        return result

                    att["outcome"] = "empty"
                    last_error = "响应 200 但正文与推理都为空"
                else:
                    att["outcome"] = "http_error"
                    last_error = err or json.dumps(body, ensure_ascii=False)[:300] \
                        if body is not None else (err or "未知错误")

                hard_fails += 1
                if hard_fails >= self.max_attempts_per_model:
                    break
                # ★ 超时不再死磕同一个模型：上游挂起（连上了但一个字节都不回）
                # 不会因为等 2 秒就恢复，而死磕一次就是 240s。有代打就立刻换人。
                # 429 / 5xx 仍然照常重试（那些是会自愈的）。
                if (self.switch_model_on_timeout and self.fallback_enabled
                        and model != chain[-1][0]
                        and _looks_like_timeout(status, last_error)):
                    if self.verbose:
                        print(f"    [{seat.display}/{model}] 超时，直接换下一个模型")
                    break
                sleep_for = self._backoff_seconds(status, str(last_error), hard_fails)
                if self.verbose:
                    tag = f"[{seat.display}/{model}]"
                    print(f"    {tag} 第{hard_fails}次失败({status})：{str(last_error)[:110]}"
                          f"，{sleep_for:.1f}s 后重试")
                # 被强制结束时不再等待重试，立刻退出
                if self.cancel_event is not None and self.cancel_event.is_set():
                    break
                time.sleep(sleep_for)

            if self.verbose:
                if self.fallback_enabled:
                    print(f"    [{seat.display}] 模型 {model} 多次失败，切换下一个")
                else:
                    print(f"    [{seat.display}] 模型 {model} 多次失败"
                          f"（代打已关闭，本回合放弃）")
            if model == seat.model and not primary_error:
                primary_error = str(last_error)[:300]

        result = LLMResult(model_used="", attempts=attempt_no, error=last_error,
                           models_tried=[m for m, _ in chain],
                           primary_error=primary_error or str(last_error)[:300],
                           attempts_detail=attempts_detail)
        self._record(seat, day, phase, messages, result, temperature, locked)
        return result

    def _record(self, seat, day, phase, messages, result: LLMResult,
                temperature: float, locked: bool, endpoint: str = "") -> None:
        entry = {
            "day": day,
            "phase": phase,
            "seat": seat.no,
            "label": seat.label,
            "endpoint": endpoint or seat.endpoint,
            "requested_model": seat.model,
            "model_used": result.model_used,
            "models_tried": result.models_tried,
            "primary_error": result.primary_error,
            "substituted": result.substituted,
            "temperature": temperature,
            "temperature_locked": locked,
            "attempts": result.attempts,
            "latency_s": round(result.latency_s, 2),
            "finish_reason": result.finish_reason,
            "truncated": result.truncated,
            "prompt_tokens": result.prompt_tokens,
            "cached_tokens": result.cached_tokens,
            "cache_ratio": round(result.cache_ratio, 4),
            "usage": result.usage,
            "error": result.error,
            # 每一次尝试的账目（含被截断丢弃、失败的那些）。
            # 只记最终 usage 的话，翻倍重试烧掉的中间尝试在账单上是隐形的。
            "attempts_detail": result.attempts_detail,
            "completion_tokens": result.completion_tokens,
            "reasoning_tokens": result.reasoning_tokens,
            "wasted_completion_tokens": result.wasted_completion_tokens,
            "raw": result.text,
            "reasoning": result.reasoning[:2000],
            "messages": messages,
        }
        self._log(seat, entry)
        self.call_count += 1
        self.stats.append({
            "seat": seat.no, "label": seat.label, "phase": phase,
            "model": result.model_used, "substituted": result.substituted,
            # ★ attempts 必须带上：summary 的 calls_retried / attempts_total 靠它，
            # 漏了就会永远显示"0 次重试"，看起来像一遍过。
            "attempts": result.attempts,
            "models_tried": result.models_tried,
            "latency_s": round(result.latency_s, 2),
            "prompt_tokens": result.prompt_tokens,
            "cached_tokens": result.cached_tokens,
            "truncated": result.truncated,
            "error": result.error,
            # 成本口径：summary 的 completion_tokens_total /
            # reasoning_tokens_total / wasted_completion_tokens_total 靠这三个.
            "completion_tokens": result.completion_tokens,
            "reasoning_tokens": result.reasoning_tokens,
            "wasted_completion_tokens": result.wasted_completion_tokens,
        })
