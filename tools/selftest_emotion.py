#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表情派生层的自测 —— 重点是**边界**，不是分类准确率。

为什么边界比准确率重要：这个派生文件是给 `werewolf-galgame` 消费的，
一旦它开始写回对局记录，或者依赖了不该依赖的东西（model 名、时间戳），
污染的是**权威对局记录**和整个复盘链。

钉四条：
  1. 只读 events.jsonl（**不许读 seat_*.jsonl** —— 那份含 model 名）；
  2. **绝不修改** events.jsonl / seat_*.jsonl / summary.json；
  3. 同输入两次派生**字节级一致**（无时间戳、无随机、无 dict 顺序依赖）；
  4. 分句**无损** —— 拼回去必须和原文逐字节相同（否则 galgame 播放会丢字）。

用法：python tools/selftest_emotion.py
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.derive_emotion import (  # noqa: E402
    EMOTIONS, OUT_FILE, classify, derive, split_utterances)

FAILURES = []

# 用仓库里真实跑过的一局当夹具（比手搓 fixture 有说服力：
# 里面的文本是真模型写出来的，长度/标点/换行都真实）
RUN = os.path.join(ROOT, "runs", "20261006_220858_12seat_web")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    if not os.path.exists(os.path.join(RUN, "events.jsonl")):
        print(f"⏭  夹具不存在，跳过：{RUN}")
        return 0

    print("\n[1] 分句必须无损（丢字会直接毁掉 galgame 的台词显示）")
    evs = [json.loads(l) for l in open(os.path.join(RUN, "events.jsonl"),
                                       encoding="utf-8") if l.strip()]
    speeches = [e for e in evs if e.get("kind") == "speech"]
    bad = [e["seq"] for e in speeches
           if "".join(split_utterances(e.get("text") or "")) != (e.get("text") or "")]
    check(f"{len(speeches)} 条发言分句后拼回原文逐字节相同", not bad, f"不一致的 seq: {bad[:5]}")

    empty = [e["seq"] for e in speeches
             if (e.get("text") or "").strip() and not split_utterances(e.get("text"))]
    check("有内容的发言不会切出 0 句", not empty, str(empty[:5]))

    print("\n[2] 只读 events.jsonl：不许读 seat_*.jsonl（那份含 model 名）")
    # ★ 用 AST 看**真实字符串常量**，而不是在源码里做子串匹配 ——
    #   第一版就是子串匹配，结果被自己注释里正当出现的 `seat_*.jsonl` 绊倒。
    #   要禁的是"代码真的去读那个文件"，不是"不许提它"。
    import ast
    src_path = os.path.join(ROOT, "tools", "derive_emotion.py")
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    seat_literals = [n.value for n in ast.walk(tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)
                     and n.value.startswith("seat_")]
    check("源码里没有以 seat_ 开头的字符串常量（即真的没去读它）",
          not seat_literals, f"发现：{seat_literals}")
    # 更强的一刀：把 seat_*.jsonl 的 mtime 全记下来，跑两次派生，看有没有被碰
    seat_files = [os.path.join(RUN, f) for f in os.listdir(RUN)
                  if f.startswith("seat_") and f.endswith(".jsonl")]
    before = {p: os.stat(p).st_mtime_ns for p in seat_files}

    print("\n[3] 绝不修改权威对局记录")
    guarded = ["events.jsonl", "summary.json"] + \
        [os.path.basename(p) for p in seat_files]
    stat_before = {f: os.stat(os.path.join(RUN, f)).st_mtime_ns for f in guarded}

    p1 = derive(RUN)
    p2 = derive(RUN)

    stat_after = {f: os.stat(os.path.join(RUN, f)).st_mtime_ns for f in guarded}
    touched = [f for f in guarded if stat_before[f] != stat_after[f]]
    check(f"{len(guarded)} 个权威文件一个都没被改", not touched, str(touched))
    check("派生过程也没碰 seat_*.jsonl 的 mtime",
          all(os.stat(p).st_mtime_ns == before[p] for p in seat_files))

    print("\n[4] 同输入两次派生必须字节级一致（可复现）")
    a = json.dumps(p1, ensure_ascii=False, sort_keys=False)
    b = json.dumps(p2, ensure_ascii=False, sort_keys=False)
    check("两次派生结果完全相同", a == b)
    check("派生文件里不含时间戳字段",
          not any(k in a for k in ("_generated_at", "_timestamp", "time.time")))

    print("\n[5] 产出结构（galgame 要按这个消费）")
    check("emotion 取值全部在枚举内",
          all(u["emotion"] in EMOTIONS
              for l in p1["lines"] for u in l["utterances"]))
    check("每条 utterance 都带 text 与 emotion",
          all(("text" in u and "emotion" in u)
              for l in p1["lines"] for u in l["utterances"]))
    check("带 _source_events_sha（用来判断要不要重生）",
          bool(p1.get("_source_events_sha")))
    check("neutral/natural 的映射写在文件里（素材文件名是 -natural）",
          (p1.get("_asset_map") or {}).get("neutral") == "natural")
    check("cast 覆盖了所有出过声的座位",
          all(str(l["seat"]) in p1["cast"] for l in p1["lines"] if l["seat"]))
    # 逐座位用到的表情，必须和 lines 里实际出现的一致
    for no, c in p1["cast"].items():
        seen = {u["emotion"] for l in p1["lines"]
                if str(l["seat"]) == no for u in l["utterances"]}
        if set(c["emotions_used"]) != seen:
            check(f"{no}号 cast 与实际标签一致", False,
                  f"cast={c['emotions_used']} 实际={sorted(seen)}")
            break
    else:
        check("每个座位的 cast 都与逐句标签一致", True)

    print("\n[6] 分类器：锚点不许把『游戏术语』和『否定结构』当情绪")
    # 这几条都是实测抓到的误判，钉成回归
    cases = [
        # (文本, 阶段, 不该是哪个情绪, 说明)
        ("平安夜我认可，刀口在6、女巫救了。", "DAY_SHERIFF_RUN", "sorrow",
         "『我认可』是同意，不是认栽（v2 误判 45 条）"),
        ("真预言家夜里只能知道自己验了2号，不可能知道刀口。", "DAY_SHERIFF_VOTE", "shock",
         "『不可能』是逻辑论证，不是惊讶（v2 误判 33 条）"),
        ("白痴翻牌不死，但从此失去投票权。", "DAY_SPEECH", "shock",
         "『翻牌』是游戏机制术语，不是惊讶"),
        ("这不是狼能凭空编出来的信息链。", "DAY_SPEECH", "shock",
         "『这不』是否定结构，不是惊讶"),
        ("10号遗言专门点了5号零成本中立。", "DAY_SPEECH", "sorrow",
         "讨论**别人的**遗言，不是自己在告别"),
        ("所以我反而觉得可能是6号在利用平安夜倒打一耙。", "DAY_SHERIFF_RUN", "wry",
         "『倒打一耙』是指控，不是阴阳（v2 误判）"),
    ]
    for text, phase, wrong, why in cases:
        got = classify(text, phase)[0]
        check(f"不该判成 {wrong}：{why}", got != wrong, f"实际={got}")

    # 正面对照：真的该判出来的也要判出来（别把守卫修成摆设）
    positives = [
        ("我出局了，但我死得不服。", "DAY_LAST_WORDS", "sorrow"),
        ("没想到4号居然是狼。", "DAY_SPEECH", "shock"),
        ("我不确定1号是不是狼，先听一圈。", "DAY_SPEECH", "worry"),
        ("你今天的解释和你的票完全矛盾。", "DAY_SPEECH", "accuse"),
    ]
    for text, phase, want in positives:
        got = classify(text, phase)[0]
        check(f"该判成 {want} 的要判出来", got == want, f"实际={got}")

    print("\n[7] 遗言的结构信号（渲染端用它切立绘/退场演出）")
    lw = [l for l in p1["lines"] if l["phase"] == "DAY_LAST_WORDS"]
    check("夹具里有遗言发言可测", bool(lw), f"{len(lw)} 条")
    if lw:
        ok = all(l["utterances"][0].get("farewell") and l["utterances"][-1].get("farewell")
                 for l in lw if l["utterances"])
        check("每条遗言的首句与末句都带 farewell 标记", ok)
        mid = [u.get("farewell") for l in lw for u in l["utterances"][1:-1]]
        check("遗言中间的战术分析不被打成 farewell（内容判断保持诚实）",
              not any(mid), f"共 {len(mid)} 句中间句")
    non_lw = [u.get("farewell") for l in p1["lines"]
              if l["phase"] != "DAY_LAST_WORDS" for u in l["utterances"]]
    check("非遗言阶段没有任何 farewell 标记", not any(non_lw))

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：{FAILURES}")
        return 1
    print("✅ 表情派生层边界全部通过（只读 events / 不改记录 / 可复现 / 分句无损）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
