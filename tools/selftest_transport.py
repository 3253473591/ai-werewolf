#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""响应解析单测 —— 覆盖"7号被跳过"那次事故的三个根因。

事故现场：GLM 返回 HTTP 200，`content` 为空串，内容全在 `reasoning` 字段里，
`finish_reason=length`（3072 额度全花在自己的思维链上）。当时的代码：

  1. 只读 `reasoning_content`，读不到 GLM 的 `reasoning` -> 看起来像彻底空响应
  2. 不管 `finish_reason=length`，把同一请求重发三次 -> 三次一模一样地失败
  3. 失败只记成"上帝可见"，大桌上就是凭空少了一个人

用法：python tools/selftest_transport.py
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiwolf.protocol import parse_decision  # noqa: E402
from aiwolf.relay import extract_text, finish_reason_of  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    print("\n[1] 推理字段：各家网关用的名字不一样")
    cases = [
        ("GLM",        {"content": "", "reasoning": "Let me analyze..."}),
        ("DeepSeek",   {"content": "", "reasoning_content": "我们需要回答"}),
        ("Kimi/其它",  {"content": "", "thinking": "分析中"}),
        ("thought",    {"content": "", "thought": "思考"}),
        ("analysis",   {"content": "", "analysis": "分析"}),
    ]
    for label, msg in cases:
        body = {"choices": [{"finish_reason": "length", "message": dict(msg, role="assistant")}]}
        c, r = extract_text(body)
        check(f"{label} 的推理字段能读到", bool(r.strip()), f"content={c!r} reasoning={r[:22]!r}")

    print("\n[2] 正文优先，正文存在时不被推理盖掉")
    body = {"choices": [{"message": {"content": '{"speech":"hi"}', "reasoning": "很长的推理"}}]}
    c, r = extract_text(body)
    check("content 非空时原样返回", c == '{"speech":"hi"}', repr(c))

    print("\n[3] legacy completions 结构（少数网关）")
    c, r = extract_text({"choices": [{"finish_reason": "stop", "text": "hello"}]})
    check("能读 choices[0].text", c == "hello", repr(c))

    print("\n[4] finish_reason 必须能读出来（截断判定靠它）")
    check("length", finish_reason_of(
        {"choices": [{"finish_reason": "length"}]}) == "length")
    check("缺失时返回空串而不是抛异常", finish_reason_of({}) == "")

    print("\n[5] 被截断的 JSON 要能按字段抢救")
    truncated = ('{"thinking": "好长好长的推理", "speech": "我是7号，我认'
                 '为6号的发言有问题", "action": {"type": "speak", "target": nul')
    d = parse_decision(truncated, "DAY_SPEECH")
    check("能从截断 JSON 里救出 speech", "我是7号" in d.speech, repr(d.speech[:30]))
    check("标记为 json_salvaged", "json_salvaged" in d.issues, str(d.issues))

    print("\n[6] 完全解析不出来时，绝不能把原始文本当成发言")
    d = parse_decision("我思考了很久但是没有输出 JSON", "DAY_SPEECH")
    check("speech 为空（不污染大桌）", d.speech == "", repr(d.speech))
    check("标记为 unparsed", "unparsed" in d.issues, str(d.issues))

    print("\n[7] 动作校验：非法动作降级为 pass 并留痕")
    d = parse_decision('{"speech":"x","action":{"type":"乱写","target":3}}', "DAY_VOTE")
    check("非法动作类型降级", d.action.type == "pass", d.action.type)
    check("留了 action_invalid 痕迹", any("action_invalid" in i for i in d.issues), str(d.issues))

    d = parse_decision('{"speech":"x","action":{"type":"vote","target":null}}', "DAY_VOTE")
    check("缺目标时降级为 pass", d.action.type == "pass", d.action.type)

    print("\n[8] 中文动作词必须能识别（Claude 实测会返回「击杀」）")
    zh_cases = [
        ("NIGHT_WOLF", "击杀", "kill"),
        ("NIGHT_WOLF", "刀", "kill"),
        ("NIGHT_WOLF", "杀2号", "kill"),
        ("NIGHT_SEER", "查验", "check"),
        ("NIGHT_SEER", "验人", "check"),
        ("NIGHT_GUARD", "守护", "guard"),
        ("NIGHT_WITCH", "解药", "save"),
        ("NIGHT_WITCH", "毒杀", "poison"),
        ("DAY_VOTE", "投票", "vote"),
        ("DAY_VOTE", "放逐", "vote"),
        ("DAY_HUNTER", "开枪", "shoot"),
    ]
    for phase, raw, want in zh_cases:
        d = parse_decision(json.dumps({"speech": "", "action": {"type": raw, "target": 2}},
                                      ensure_ascii=False), phase)
        got = d.action.type if want != "kill" or d.action.target else d.action.type
        target_ok = d.action.target == 2
        ok = (d.action.type == want) and (target_ok or want == "save")
        check(f"「{raw}」-> {want}", ok,
              f"得到 {d.action.type}/{d.action.target} issues={d.issues}")

    print("\n[9] 「毒杀」不能被误判成 kill")
    d = parse_decision('{"action":{"type":"毒杀","target":3}}', "NIGHT_WITCH")
    check("毒杀 -> poison", d.action.type == "poison", d.action.type)

    print("\n[10] 值里有未转义的引号（实测 Claude 就是这么写的）")
    # 线上事故现场：Claude 在发言里写了 非常"完美"—— 但没有转义，
    # 整段 JSON 非法。旧的抢救逻辑按"下一个引号"切，发言被腰斩成半句
    # （"…警徽流设计得非常"），症状看起来像 max_tokens 截断，其实不是。
    broken = (
        '{"thinking": "I am the real prophet.", '
        '"speech": "3号发言，底牌预言家。第二，1号的警徽流设计得非常"完美"——先验5号再验3号。'
        '第三，1号锁定"藏身位"验4号，这个逻辑无法证伪。今天的票我投1号。", '
        '"action": {"type": "speak", "target": null}, "confidence": 0.92}'
    )
    import json as _json
    try:
        _json.loads(broken)
        check("样本确实是非法 JSON（前提成立）", False, "居然能解析，样本失效了")
    except ValueError:
        check("样本确实是非法 JSON（前提成立）", True)

    d = parse_decision(broken, "DAY_SPEECH")
    check("整段发言被完整救回来（不是半句）",
          d.speech.endswith("今天的票我投1号。"), repr(d.speech[-20:]))
    check("发言里的引号保留", '"完美"' in d.speech, repr(d.speech[:40]))
    # 关键：必须越过第一个未转义引号。切在那里就只有 25 字
    check("越过了第一个未转义的引号", len(d.speech) > 40, f"len={len(d.speech)}")
    check("confidence 也救回来了", abs(d.confidence - 0.92) < 1e-6, str(d.confidence))
    check("标记为 json_salvaged", "json_salvaged" in d.issues, str(d.issues))

    print("\n[11] 真被截断时，仍然只救到有的部分（不能凭空变长）")
    cut = ('{"thinking": "推理中", "speech": "我是7号，我认')
    d = parse_decision(cut, "DAY_SPEECH")
    check("救出截断处的发言", d.speech == "我是7号，我认", repr(d.speech))
    check("标记为 json_salvaged", "json_salvaged" in d.issues, str(d.issues))

    print("\n[12] 中文动作词在抢救路径上也要能识别")
    broken2 = ('{"speech": "我提议"先"刀2号", '
               '"action": {"type": "击杀", "target": 2}, "confidence": 0.8}')
    d = parse_decision(broken2, "NIGHT_WOLF")
    check("抢救出的 speech 完整", d.speech.endswith("刀2号"), repr(d.speech))
    check("抢救出的「击杀」映射成 kill", d.action.type == "kill", d.action.type)
    check("target 保留", d.action.target == 2, str(d.action.target))

    print("\n[12.5] 思维链内联在正文里（MiniMax 风格）—— 必须拿答案，不能拿草稿")
    # 线上事故现场（12 座局第 4 天，8号 MiniMax / ep10，11/11 次调用都是这个形状）：
    #   正文 = "<think>…思维链…</think>\n\n{答案 JSON}"
    # 三个连环坑：
    #   ① 整段解析在第 0 个字符就失败（"<"）；
    #   ② 括号恢复抓到草稿里的集合写法 {1,12}（不是 JSON）；
    #   ③ 按字段抢救的正则在整段上匹配，先命中**草稿里的 JSON 模板** ——
    #      于是 "speech": "..." 被当成答案发出去，全桌看到的投票理由就是三个点。
    draft = '{"thinking": "草稿", "speech": "...", "action": {"type": "vote", "target": 12}}'
    answer = ('{"thinking": "正式推理", "speech": "12号两天独投6号做深水身份，'
              '投票12号。", "action": {"type": "vote", "target": 12}, "confidence": 0.85}')
    inline = (f"<think>Let me analyze. 若6是狼，剩1狼在{{1,12}}；"
              f"我本来打算写 {draft} </think>\n\n{answer}")
    d = parse_decision(inline, "DAY_VOTE")
    check("拿的是答案不是草稿（不是三个点）", d.speech.startswith("12号两天独投6号"),
          repr(d.speech[:30]))
    check("草稿里的占位符绝不能进公开频道", d.speech != "...", repr(d.speech))
    check("动作也从答案里取", d.action.type == "vote" and d.action.target == 12,
          f"{d.action.type}/{d.action.target}")
    check("答案 JSON 本身合法 -> 零降级（不是 json_salvaged）",
          "json_salvaged" not in d.issues, str(d.issues))
    check("thinking 取的是答案那份", d.thinking == "正式推理", repr(d.thinking))

    # 变体：<thinking> / <reasoning> 也要认；只剥成对标签，正文里的 "<think" 不动
    d2 = parse_decision("<reasoning>草稿</reasoning>\n" + answer, "DAY_VOTE")
    check("<reasoning> 变体也认", d2.speech.startswith("12号两天独投6号"), repr(d2.speech[:20]))
    d3 = parse_decision('{"speech": "我上一条说 3号<think 是狼", '
                        '"action": {"type": "speak", "target": null}}', "DAY_SPEECH")
    check("正文里正常出现的 \"<think\" 不被误伤", "<think" in d3.speech, repr(d3.speech))
    # 反面：整段都是草稿（没有标签外的答案）时，不许把答案丢掉
    d4 = parse_decision("<think>只有草稿</think>", "DAY_SPEECH")
    check("整段都是思维链时不崩（退化成空发言，不是异常）",
          isinstance(d4.speech, str), repr(d4.speech))

    print("\n[12.6] 传输层就把内联思维链拆出来（根因修复）")
    from aiwolf.relay import extract_text as _extract
    body = {"choices": [{"message": {"content": inline}}]}
    content, reasoning = _extract(body)
    check("content 只剩答案（解析器根本看不到思维链）",
          content.strip().startswith("{") and "<think>" not in content, content[:40])
    check("思维链搬进了 reasoning（证据没丢）",
          "<think>" in reasoning and "Let me analyze" in reasoning, reasoning[:40])
    body2 = {"choices": [{"message": {"content": "{\"speech\": \"干净输出\"}",
                                      "reasoning_content": "原生推理"}}]}
    c2, r2 = _extract(body2)
    check("没有内联标签时一个字都不动",
          c2 == '{"speech": "干净输出"}' and r2 == "原生推理", f"{c2!r}/{r2!r}")
    body3 = {"choices": [{"message": {"content": "<think>只有草稿</think>"}}]}
    c3, r3 = _extract(body3)
    check("只有思维链时 content 为空、reasoning 有值（引擎据此走网关风格回退）",
          c3.strip() == "" and "只有草稿" in r3, f"{c3!r}/{r3!r}")


    print("\n[13] 出站请求必须带浏览器 UA（Cloudflare 1010 回归）")
    # 线上事故现场：中转站 C（ep11）前面挂着 Cloudflare 的 bot 规则，
    # urllib 默认的 `Python-urllib/3.x` 会被按 UA 签名拦掉：
    #     403  Server: cloudflare  body: 'error code: 1010\n'   (17 字节)
    # /models 和 /chat/completions 一起 403 —— 通道填好了却一个模型都拉不到。
    # 同一个 IP、同一把 key，只换 UA 就 200，所以这条必须钉死在单测里。
    import urllib.request as _u
    from aiwolf import relay as _relay

    captured = {}

    class _FakeResp:
        status = 200

        def read(self):
            return b'{"data": []}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None, **kw):
        captured["req"] = req
        return _FakeResp()

    real_urlopen = _relay._urlopen
    _relay._urlopen = _fake_urlopen
    try:
        _relay._post_urllib("https://example.invalid/v1/models", {}, None, 5)
    finally:
        _relay._urlopen = real_urlopen

    ua = captured["req"].get_header("User-agent") or ""
    check("没传 UA 时自动补上浏览器 UA", ua == _relay.DEFAULT_UA, repr(ua))
    check("绝不是 Python-urllib", "Python-urllib" not in ua, repr(ua))

    captured.clear()
    _relay._urlopen = _fake_urlopen
    try:
        _relay._post_urllib("https://example.invalid/v1/models",
                            {"User-Agent": "curl/8.5.0"}, None, 5)
    finally:
        _relay._urlopen = real_urlopen
    check("调用方显式传的 UA 不被覆盖",
          captured["req"].get_header("User-agent") == "curl/8.5.0",
          repr(captured["req"].get_header("User-agent")))

    # ---- 出站代理（7890）：配置优先级 + 两条传输层都吃这个设置 ----
    # 背景：中转站 C / relay-a.example 被按主机名在 TLS 握手阶段 RST，
    # UA 救不了（RST 在任何 HTTP 字节之前），只能走代理。
    import os as _os

    saved_env = {k: _os.environ.get(k) for k in _relay.PROXY_ENV_KEYS}
    for k in _relay.PROXY_ENV_KEYS:
        _os.environ.pop(k, None)
    try:
        _relay.set_proxy("http://127.0.0.1:7890")
        check("配了 proxy 就生效", _relay.current_proxy() == "http://127.0.0.1:7890",
              _relay.current_proxy())
        _os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1080"
        check("配置优先于环境变量（config 说了算）",
              _relay.current_proxy() == "http://127.0.0.1:7890", _relay.current_proxy())
        _relay.set_proxy("")
        check("没配 proxy 时回落到环境变量",
              _relay.current_proxy() == "http://127.0.0.1:1080", _relay.current_proxy())
        _os.environ.pop("HTTPS_PROXY", None)
        check("都没有时不走代理（直连）", _relay.current_proxy() == "", _relay.current_proxy())

        # curl 那条路必须显式带 -x，否则配了代理也只有 urllib 生效
        _relay.set_proxy("http://127.0.0.1:7890")
        seen = {}
        real_popen = _relay.subprocess.Popen

        class _P:
            returncode = 0

            def __init__(self, cmd, **kw):
                seen["cmd"] = cmd

            def poll(self):
                return 0

            def communicate(self):
                return b"000", b""

            def kill(self):
                pass

        _relay.subprocess.Popen = _P
        try:
            _relay._post_curl("https://中转站 C/v1/models", {}, None, 5)
        finally:
            _relay.subprocess.Popen = real_popen
        cmd = seen.get("cmd") or []
        check("curl 也带上代理（-x）", "-x" in cmd and "http://127.0.0.1:7890" in cmd,
              " ".join(cmd[:9]))
        check("代理参数在 -x 的位置上（不是被当成 URL）",
              cmd[cmd.index("-x") + 1] == "http://127.0.0.1:7890" if "-x" in cmd else False,
              str(cmd[-3:]))
    finally:
        _relay.set_proxy("")
        for k, v in saved_env.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：{FAILURES}")
        return 1
    print("✅ 响应解析全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
