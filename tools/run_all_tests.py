#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑全部自测。

    python tools/run_all_tests.py

每一项都是"会失败"的检查 —— 一个从不失败的测试等于没有测试。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")

SUITES = [
    ("胜负判定", "selftest_rules.py", "parity / side 两套规则，覆盖 3-10 人"),
    ("玩法规则", "selftest_gameplay.py", "投票理由 / 狼队表决 / 猎人开枪 / PK / 自曝 / 落盘"),
    ("代打", "selftest_fallback.py", "换人时机 / 标记 substituted / 时限 / 跨通道"),
    ("抽卡分配", "selftest_draw.py", "卡方检验分布均匀、同 seed 可复现"),
    ("对局闸门", "selftest_gate.py", "暂停真的阻塞、单步只放行 N 次"),
    ("信息隔离", "selftest_invariants.py", "审计能抓泄漏；且真故障只进上帝账本、不进公开公告"),
    ("响应解析", "selftest_transport.py", "推理字段名、中文动作词、截断抢救"),
    ("多通道", "selftest_endpoints.py", "落盘 / 重启读回 / key 掩码 / 轮转分配"),
    ("局后感言", "selftest_reflect.py", "身份来源 / 不泄漏模型名 / 不污染对局记录 / 续跑"),
    ("断连噪声", "selftest_sse_abort.py", "客户端半路断开不打 traceback，但真错误照打"),
    ("表情派生", "selftest_emotion.py", "只读 events / 不改记录 / 可复现 / 分句无损（供 galgame 消费）"),
]

# 前端用 Node + 假 DOM 跑，浏览器打不开时的替代验证
NODE_SUITES = [
    ("前端渲染", "selftest_frontend.js", "SSE 消息渲染 + 结算动画，无运行时错误"),
]


def run_suite(cmd: list, env: dict):
    """跑一个套件，返回 (returncode, output)。

    ★ 受限环境（沙箱、某些 CI）里**不允许创建管道**，`capture_output=True` 会直接
    抛 `PermissionError: [WinError 5] 拒绝访问`（实测过）。那种情况下退回**继承
    stdio**：拿不到过滤后的输出，但测试是真的跑了，退出码照样准 —— 总比整个
    测试入口崩掉强。这也解释了为什么以前这个脚本在本地好好的、在别处一跑就死。
    """
    argv = [sys.executable, "-u", cmd] if cmd.endswith(".py") else [sys.executable, cmd]
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (PermissionError, OSError):
        print("  ⚠ 这个环境不允许创建管道，退回直连输出（过滤会失效，退出码仍然准）")
        proc = subprocess.run(argv, env=env)
        return proc.returncode, ""


def main() -> int:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    results = []

    for name, script, desc in SUITES:
        print("=" * 70)
        print(f"▶ {name}  —— {desc}")
        print("=" * 70)
        code, output = run_suite(os.path.join(TOOLS, script), env)
        for line in output.rstrip().splitlines():
            if line.strip().startswith(("✅", "❌", "  ✅", "  ❌", "[", "▶")):
                print(line)
        results.append((name, code == 0))
        print()

    node = shutil.which("node")
    for name, script, desc in NODE_SUITES:
        print("=" * 70)
        print(f"▶ {name}  —— {desc}")
        print("=" * 70)
        if not node:
            print("  ⏭  没装 node，跳过")
            print()
            continue
        try:
            proc = subprocess.run([node, os.path.join(TOOLS, script)], env=env,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace")
            code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except (PermissionError, OSError):
            print("  ⚠ 这个环境不允许创建管道，退回直连输出")
            code, output = subprocess.run(
                [node, os.path.join(TOOLS, script)], env=env).returncode, ""
        for line in output.rstrip().splitlines():
            if line.strip().startswith(("✅", "❌", "  ✅", "  ❌")):
                print(line)
        results.append((name, code == 0))
        print()

    print("=" * 70)
    print("汇总")
    print("=" * 70)
    failed = 0
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
        failed += 0 if ok else 1
    print()
    if failed:
        print(f"❌ {failed}/{len(results)} 个测试套件未通过")
        return 1
    print(f"✅ 全部 {len(results)} 个测试套件通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
