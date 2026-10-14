#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓一个 URL 的原始响应，落盘 + 打印摘要。用于看中转站文档 / 探针。

    python tools/fetch_url.py https://relay-b.example/apidoc --out runs/_probe/apidoc.html
"""
from __future__ import annotations

import argparse
import gzip
import io
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", default="")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--head", action="store_true", help="只看响应头")
    ap.add_argument("--max", type=int, default=20000, help="打印正文的前 N 字")
    ap.add_argument("--insecure", action="store_true", help="跳过证书校验")
    args = ap.parse_args()

    req = urllib.request.Request(
        args.url, method="HEAD" if args.head else "GET",
        headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
    ctx = ssl.create_default_context()
    if args.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=args.timeout, context=ctx) as r:
            raw = r.read()
            lat = time.monotonic() - started
            enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            body = raw.decode("utf-8", "replace")
            print(f"HTTP {r.status}  {lat:.2f}s  {len(raw)} bytes  {r.headers.get('Content-Type')}")
            print("--- 响应头 ---")
            for k, v in r.headers.items():
                print(f"  {k}: {v}")
            if args.out:
                path = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(body)
                print(f"\n已写入 {path}（{len(body)} 字符）")
            if not args.head:
                print(f"\n--- 正文前 {args.max} 字 ---")
                print(body[:args.max])
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {e.reason}")
        try:
            print(e.read().decode("utf-8", "replace")[:2000])
        except Exception:                                     # noqa: BLE001
            pass
        return 1
    except Exception as e:                                    # noqa: BLE001
        print(f"❌ {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
