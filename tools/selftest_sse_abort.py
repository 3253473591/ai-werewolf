#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证"客户端半路断开"不再刷 traceback —— **带正面对照**。

一个"不再报错"的测试，如果不能证明"旧写法确实会报错"，就什么都证明不了。

⚠️ 试过纯 socket 复现（裸 close / RST / 半截请求行 / 半截请求头，五种），
**裸 ThreadingHTTPServer 一次都没报**。真实触发场景是 keep-alive 长连接上的
第二次读取（浏览器关掉 SSE 那条连接之后服务器还在读下一行）。既然端到端
不稳定复现，就在**我实际改的那一层**做对照：`handle_error()`。

  旧写法 ThreadingHTTPServer.handle_error  -> 往 stderr 打一整页 traceback
  新写法 QuietThreadingHTTPServer          -> 静默（对方走了不是事故）
"""
import io
import os
import sys
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILURES = []


def check(name, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def fire(server_obj):
    """在 except 块里调 handle_error —— 它靠 sys.exc_info() 拿异常类型。"""
    buf = io.StringIO()
    with redirect_stderr(buf):
        try:
            raise ConnectionAbortedError(
                10053, "你的主机中的软件中止了一个已建立的连接。")
        except ConnectionAbortedError:
            server_obj.handle_error(None, ("127.0.0.1", 52905))
    return buf.getvalue()


def main() -> int:
    from server import QuietThreadingHTTPServer

    print("\n[1] 正面对照：旧写法必须会打 traceback（否则这测试没意义）")
    old = ThreadingHTTPServer.__new__(ThreadingHTTPServer)
    old_out = fire(old)
    check("旧的 ThreadingHTTPServer 会打 traceback",
          "ConnectionAbortedError" in old_out or "10053" in old_out,
          old_out.strip().splitlines()[-1][:70] if old_out.strip() else "（没打）")

    print("\n[2] 修补后：同一个异常必须静默")
    new = QuietThreadingHTTPServer.__new__(QuietThreadingHTTPServer)
    new_out = fire(new)
    check("修补后不再打 traceback", not new_out.strip(),
          repr(new_out[:80]))

    print("\n[3] 别把真错误也一起吞了")
    buf = io.StringIO()
    with redirect_stderr(buf):
        try:
            raise ValueError("这个是真的坏了")
        except ValueError:
            new.handle_error(None, ("127.0.0.1", 1))
    check("非连接类异常照常打印", "ValueError" in buf.getvalue(),
          buf.getvalue().strip().splitlines()[-1][:60])

    print("\n[4] Handler 里的兜底也还在")
    from server import Handler
    check("Handler 覆盖了 handle_one_request",
          Handler.handle_one_request is not __import__(
              "http.server", fromlist=["BaseHTTPRequestHandler"]
          ).BaseHTTPRequestHandler.handle_one_request)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：{FAILURES}")
        return 1
    print("✅ 正面对照会报错、修补后不报、真错误照样报 —— 这条修复是真的")
    return 0


if __name__ == "__main__":
    sys.exit(main())
