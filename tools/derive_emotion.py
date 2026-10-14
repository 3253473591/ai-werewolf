#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给一局的发言派生「表情差分」标签，产出 `emotion.json`（给 werewolf-galgame 用）。

## 为什么是派生文件，而不是写进 events.jsonl

1. **历史局能用。** events.jsonl 里信息已经全了，表情可以事后算；写进权威记录的话
   已跑完的 20+ 局永远没有表情，除非重跑（一局 40 分钟 / 140 万 token）。
2. **素材还没做全。** 目前只有 豆包 有 6 张差分，其他角色只有基图。
   派生层可以随时重生 —— 等你补齐了素材，**老局重新派生一遍就有表情**，一局都不用重跑。
3. **表情是演出决策，不是事实。** 同一句"我验了3号是狼"可以演成拍桌也可以演成冷静。
   固化进案卷之后想换演绎就得重跑。

## 判据：按「言语行为」判，不按感情色彩判

实测这局的发言几乎全是**论证**（"关键不在于谁先跳，而在于……"），
不是抒情。所以情绪标签取的是**这句话主要在干什么**：

    accuse  指控、查杀、归票要出人、点名要人解释
    shock   死讯/翻牌/被查杀等冲击下的反应；"没想到/竟然"
    sorrow  遗言、认栽、出局告别
    worry   不确定、保留判断、劝人谨慎、"我再听听"
    wry     阴阳、自嘲、退让、"我嘛…"
    neutral 陈述、复述票、自动生成的 `N号 投给 X号`

## 边界（硬约束）

* **只读 events.jsonl**，绝不读 `seat_*.jsonl` —— 那份含 model 名，
  读了就违反"不给座位贴模型画像"这条原则。
* **绝不修改 events.jsonl / seat_*.jsonl / summary.json**：它们是权威对局记录。
* **不改 render.py / 引擎**：表情是表现层的事，一旦进了共享前缀就会打死
  `assert_deterministic` / `check_shared_prefix` 那两个命根子断言。
* 同一份输入派生两次必须**字节级一致**（无时间戳、无随机）。

用法：
    python tools/derive_emotion.py runs/20261006_220858_12seat_web
    python tools/derive_emotion.py runs/xxx --stdout     # 不落盘，打到屏幕
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCHEMA = "werewolf-emotion/2"
OUT_FILE = "emotion.json"

# ★ 命名：语义上用 neutral；素材文件名是 `豆包-natural.png`。
#   差一个字母（neutral vs natural）是这类系统最典型的静默错配，
#   所以把映射写在这里一处，渲染端别自己拼字符串。
EMOTIONS = ("neutral", "accuse", "worry", "shock", "sorrow", "wry")

# 引擎自动生成的文案：没有任何情绪，别硬套
AUTO_VOTE_RE = re.compile(r"^\d+号\s*投给\s*\d+号$")

SEAT_RE = re.compile(r"\d+号")

# 分句。**必须无损**：用 findall 显式带走标点与换行，
# 纯空白并入前一段（否则 `…’\n\n我明确说` 会丢掉那两个换行）。
SEG_RE = re.compile(r"[^。！？!?…\n]*(?:[。！？!?…]+|\n+|$)")


def split_utterances(text: str) -> list[str]:
    """把一个座位的一次发言切成若干句。**拼接回来必须和原文逐字节相同。**"""
    out: list[str] = []
    for s in SEG_RE.findall(text or ""):
        if not s:
            continue
        if out and (not s.strip() or not out[-1].strip()):
            out[-1] += s            # 纯空白并入相邻段，别单独成句
        else:
            out.append(s)
    return out

# --------------------------------------------------------------------------- #
# 判据：按「言语行为」判，不按感情色彩判
# --------------------------------------------------------------------------- #
#
# 踩过的坑（按发生顺序，别再重复）：
#
# v1：按整条发言打标 -> 一条 300~800 字，一个标签把整段独白压平，等于没用。
# v2：词表太松，抽检时抓到三类**致命误判**（全部是"词在，情绪不在"）：
#       · `我认` 命中 `我认可` / `我认6号的预言家面` —— 那是**同意**，
#         结果 45 条 sorrow 里几乎没有一条是真的认栽；
#       · `不可能` 命中 `不可能知道刀口` —— 那是**逻辑论证**，不是惊讶，
#         33 条 shock 几乎全是假的；
#       · `倒打一耙` 被判成 wry —— 那是指控，不是阴阳。
#     教训：单个词在论证型文本里几乎不可靠，**必须用不会撞车的锚点**。
#
# v3（现在）：只留高精度锚点；宁可退回 neutral，也不给错表情。
#   六个表情里本来就有中性 —— 错表情比没表情更伤演出。

# 高压指控：主语直指某人（v1 里最缺的就是这些）
ACCUSE_STRONG = [
    "你是狼", "你是悍跳", "你必是", "你必为", "你才", "你的问题", "你的漏洞",
    "你在骗", "你说谎", "你在演", "别倒打", "别再无脑", "别跟", "你别",
    "坐实", "必是悍跳", "就是狼", "狼面极大", "狼面", "查杀", "悍跳狼",
    "投死真", "带节奏", "抗推", "冲锋狼", "倒钩",
]
# 弱指控：只有配上"方向"才算数（见 classify 里的判定）
ACCUSE_WEAK = ["矛盾", "解释", "动机", "站边", "逻辑不自", "回避", "漏洞"]

# ★ 高精度锚点。凡是"在论证里也会自然出现"的词一律不要。
#
# v3 又抓到两类同源的错判（都是把**游戏术语/否定结构**当情绪词）：
#   · `遗言` 命中"10号遗言专门点了5号" —— 那是**讨论别人的遗言**，不是自己哀悼，
#     27 条 sorrow 里一半是这么来的。现在：遗言**只在 DAY_LAST_WORDS 阶段**算 sorrow，
#     或者句子里出现明确的告别语（我走了 / 认栽 / 带进棺材 …）。
#   · `这不` 命中"这不是狼能凭空编出来的" —— 那是**否定判断**，不是惊讶；
#     `翻牌` 命中"白痴翻牌不死" —— 那是**游戏机制术语**。两条锚点全删。
#
# 结论：这个文本里**术语密度极高**，锚点必须挑"只有当人在表达情绪时才会说"的词。
WORRY_WORDS = [
    "不确定", "没把握", "我再听", "先听", "保留", "倾向",
    "别急", "需要观察", "有待", "存疑", "谨慎", "再盘", "暂时",
    "我倾向", "未必", "还不好说", "再看看",
]
# 只放"自己正在告别"的词。**不放 `遗言`** —— 那个靠阶段判定。
SORROW_WORDS = [
    "我走了", "认栽", "告别", "带进棺材", "对不起",
    "我的锅", "背锅", "死得不服", "我扛", "我认输", "我认栽",
    "愿赌服输", "好运",
]
Wry_WORDS = [
    "我嘛", "随你", "无所谓", "懒得", "随便", "呵呵", "你开心", "行吧",
    "陪你演", "贴花", "小红花", "装老好人",
    "替我说句公道话", "少往我头上", "说风凉话", "别装",
]
# 惊讶：需要**高精度锚点**，或至少两个中等锚点（见 classify 里的阈值）。
SHOCK_STRONG = ["没想到", "竟然", "居然", "我压根没", "我拿头", "太离谱"]
SHOCK_WEAK = ["怎么会", "什么情况", "打脸", "不可能吧", "这也"]


def _hits(text: str, words: list[str], w: int) -> int:
    return sum(w for kw in words if kw in text)


def classify(text: str, phase: str) -> tuple[str, float, list[str]]:
    """给**一个分句**判表情。返回 (emotion, confidence, 命中明细)。纯函数。"""
    t = (text or "").strip()
    if not t:
        return "neutral", 1.0, []

    why: dict[str, list[str]] = {}

    def hit(words: list[str], weight: int, label: str) -> int:
        got = [kw for kw in words if kw in t]
        if got:
            why[label] = why.get(label, []) + got
        return weight * len(got)

    scores = {
        "accuse": hit(ACCUSE_STRONG, 3, "accuse"),
        "worry": hit(WORRY_WORDS, 2, "worry"),
        "sorrow": hit(SORROW_WORDS, 3, "sorrow"),
        "wry": hit(Wry_WORDS, 3, "wry"),
    }
    # 惊讶：高精度锚点 1 个就够；中等锚点要**两个**才够 ——
    # 单个中等词在论证型文本里误报太多（"怎么会有人知道刀口"其实是在论证）。
    strong = [kw for kw in SHOCK_STRONG if kw in t]
    weak = [kw for kw in SHOCK_WEAK if kw in t]
    shock = 3 * len(strong) + (2 if len(weak) >= 2 else 0)
    scores["shock"] = shock
    if strong or len(weak) >= 2:
        why["shock"] = strong + weak

    # 弱指控词：只有在**同句里点了名**（你 / N号 / 别）才算指控证据。
    # 否则"能解释站边变化的人"这种通用表述会把全场都判成 accuse。
    weak_acc = [kw for kw in ACCUSE_WEAK if kw in t]
    directed = ("你" in t) or bool(SEAT_RE.search(t)) or ("别" in t)
    if weak_acc and directed:
        scores["accuse"] += len(weak_acc)
        why.setdefault("accuse", []).extend(weak_acc)

    # 阶段偏置
    if phase == "DAY_LAST_WORDS":
        # ★ 唯一让 `遗言` 生效的地方：**真的是自己在说遗言**。
        #   后置位讨论"10号遗言点了我"时还在 DAY_SPEECH，不会吃到这个偏置。
        scores["sorrow"] += 3
        why.setdefault("sorrow", []).append("遗言阶段+3")
    elif phase in ("DAY_VOTE", "DAY_VOTE_PK"):
        scores["accuse"] += 1
        why.setdefault("accuse", []).append("投票阶段+1")

    best_score = max(scores.values())
    if best_score <= 0:
        return "neutral", 1.0, []
    # 平手按 EMOTIONS 顺序取，保证确定性
    best = max(scores.items(), key=lambda kv: (kv[1], -EMOTIONS.index(kv[0])))[0]
    total = sum(scores.values())
    conf = round(min(1.0, scores[best] / total), 3)
    return best, conf, why.get(best, [])


def _load_events(run: str) -> list[dict]:
    p = os.path.join(run, "events.jsonl")
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def derive(run: str) -> dict:
    evp = os.path.join(run, "events.jsonl")
    evs = _load_events(run)
    summary = {}
    try:
        summary = json.load(open(os.path.join(run, "summary.json"), encoding="utf-8"))
    except (OSError, ValueError):
        pass
    roles = {int(k): v for k, v in (summary.get("roles_cn") or {}).items()}

    lines = []
    for e in evs:
        if e.get("kind") != "speech":
            continue
        text = e.get("text") or ""
        auto = bool(AUTO_VOTE_RE.match(text.strip()))
        # ★ 逐句打标：galgame 播放时逐句推进、表情随语气变
        segs = [s for s in split_utterances(text) if s.strip()]
        is_last_words = e.get("phase") == "DAY_LAST_WORDS"
        utts = []
        for i, seg in enumerate(segs):
            if auto:
                emo, conf, hits = "neutral", 1.0, []
            else:
                emo, conf, hits = classify(seg, e.get("phase", ""))
            # ★ 遗言的**首句与末句**是戏剧节拍：整段遗言里夹着大量战术分析
            #   （实测 5号 说"猎人女巫若还在场，明天先不要盲毒"、8号 说"证据很硬"），
            #   按内容判成 neutral 是诚实的，但渲染端需要知道"这里该切遗言立绘、
            #   该走退场演出"。所以给结构信号，不改内容判断 —— 两者别混。
            farewell = is_last_words and (i == 0 or i == len(segs) - 1)
            utts.append({
                "text": seg,
                "emotion": emo,
                "confidence": conf,
                **({"farewell": True} if farewell else {}),
                **({"why": hits} if hits else {}),
            })
        lines.append({
            "seq": e["seq"],
            "day": e["day"],
            "phase": e["phase"],
            "channel": e["channel"],
            "seat": e.get("actor"),
            "auto": auto,
            "chars": len(text),
            "n_utterances": len(utts),
            "utterances": utts,
        })

    # 逐座位的"这局用到了哪些表情" —— 渲染端据此知道要准备哪些差分
    used: dict[str, list[str]] = {}
    for ln in lines:
        if ln["seat"] is None:
            continue
        bucket = used.setdefault(str(ln["seat"]), [])
        for u in ln["utterances"]:
            if u["emotion"] not in bucket:
                bucket.append(u["emotion"])

    return {
        "_schema": SCHEMA,
        "run_dir": os.path.basename(run.rstrip("/\\")),
        "days": summary.get("days"),
        "winner": summary.get("winner"),
        # 用来判断"要不要重新派生"：events.jsonl 变了就重生
        "_source_events_sha": _sha(evp),
        "_generator": "tools/derive_emotion.py",
        "_emotions": list(EMOTIONS),
        "_note": (
            "派生文件，可随时重生。events.jsonl / seat_*.jsonl / summary.json 未改动。"
            "表情打在**分句**上（一句话里语气会变，整条发言一个标签等于没用）。"
            "命名用 neutral；素材文件名是 -natural，映射见 _asset_map。"
        ),
        "_asset_map": {"neutral": "natural"},
        # 全量逐句标签（按 seq 升序，播放顺序）
        "lines": lines,
        # 逐座位汇总：这局需要哪些差分（缺素材的渲染端自己回落基图）
        "cast": {
            no: {"role_cn": roles.get(int(no), ""), "emotions_used": emos}
            for no, emos in sorted(used.items(), key=lambda kv: int(kv[0]))
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="派生局内表情差分（不改进对局记录）")
    ap.add_argument("run")
    ap.add_argument("--stdout", action="store_true", help="不落盘，只打印")
    args = ap.parse_args()

    run = args.run if os.path.isabs(args.run) else os.path.join(ROOT, args.run)
    if not os.path.exists(os.path.join(run, "events.jsonl")):
        print(f"❌ 没有 events.jsonl：{run}")
        return 2

    payload = derive(run)
    out = os.path.join(run, OUT_FILE)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.stdout:
        print(text)
        return 0
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, out)

    from collections import Counter
    utts = [u for l in payload["lines"] for u in l["utterances"]]
    dist = Counter(u["emotion"] for u in utts)
    print(f"已写入 {out}")
    print(f"  逐句标签 {len(utts)} 条（来自 {len(payload['lines'])} 条发言；"
          f"其中引擎自动文案 {sum(1 for l in payload['lines'] if l['auto'])} 条按 neutral）")
    print("  情绪分布: " + "  ".join(f"{k}={v}" for k, v in dist.most_common()))
    print("  逐座位用到的差分:")
    for no, c in payload["cast"].items():
        print(f"    {no:>2}号 {c['role_cn']:<4} {c['emotions_used']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
