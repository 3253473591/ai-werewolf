#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闸门单测 —— 证明暂停真的会阻塞，单步真的只放行指定的次数。

一个"看起来暂停了但线程还在跑"的闸门是危险的：调试时你以为停住了，
实际上额度已经烧掉了。所以这里用真实线程去验。

用法：python tools/selftest_gate.py
"""
from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiwolf.gate import GameGate  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    print("\n[1] 运行态：wait() 直接放行")
    gate = GameGate()
    t0 = time.monotonic()
    for _ in range(20):
        gate.wait("x")
    check("未暂停时不阻塞", time.monotonic() - t0 < 0.05)

    print("\n[2] 暂停：对局线程必须停住")
    gate = GameGate()
    passes = []
    stop = threading.Event()

    def worker():
        i = 0
        while not stop.is_set():
            gate.wait(label=f"call {i}")
            i += 1
            passes.append(i)
            time.sleep(0.01)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    time.sleep(0.25)
    gate.pause()
    time.sleep(0.15)                 # 让可能已经越过闸门的那一次跑完
    n0 = len(passes)
    time.sleep(0.5)
    n1 = len(passes)
    check("暂停后不再放行", n1 - n0 == 0, f"暂停后新增 {n1 - n0} 次")
    check("闸门报告 waiting>0", gate.snapshot()["waiting"] > 0,
          f"waiting={gate.snapshot()['waiting']}")

    print("\n[3] 继续：恢复放行")
    gate.resume()
    time.sleep(0.4)
    n2 = len(passes)
    check("继续后重新放行", n2 - n1 > 5, f"恢复后新增 {n2 - n1} 次")

    print("\n[4] 单步：只放行指定的次数")
    gate.pause()
    time.sleep(0.2)
    n3 = len(passes)
    gate.step(3)
    time.sleep(0.6)
    n4 = len(passes)
    check("step(3) 恰好放行 3 次", n4 - n3 == 3, f"实际放行 {n4 - n3} 次")
    check("放行后重新阻塞", gate.snapshot()["waiting"] > 0)

    print("\n[5] 单步可以连续叠加")
    gate.step(1)
    gate.step(1)
    time.sleep(0.4)
    n5 = len(passes)
    check("两次 step(1) 共放行 2 次", n5 - n4 == 2, f"实际放行 {n5 - n4} 次")

    print("\n[6] toggle / 状态快照")
    gate.resume()
    time.sleep(0.1)
    snap = gate.snapshot()
    check("resume 后 paused=False", snap["paused"] is False)
    gate.toggle()
    check("toggle 后 paused=True", gate.snapshot()["paused"] is True)
    gate.toggle()
    check("再 toggle 后 paused=False", gate.snapshot()["paused"] is False)

    stop.set()
    gate.resume()
    time.sleep(0.3)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：{FAILURES}")
        return 1
    print("✅ 闸门语义全部正确（暂停真的阻塞，单步真的只放行 N 次）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
