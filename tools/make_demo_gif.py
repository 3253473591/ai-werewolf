"""把 run_text.py --stub 的输出渲染成终端风动画 GIF。

用法：
    python tools/make_demo_gif.py <日志文件> <输出GIF> [--cols 100] [--rows 34]

只依赖 Pillow。中文字体优先用 msyh.ttc，回退 simhei.ttf。
渲染前会做脱敏：去掉本机绝对路径、把中转站名换成代称。
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\simhei.ttf",   # 等宽 CJK（中英宽度比 2.0），黑体可读性好
    r"C:\Windows\Fonts\simsun.ttc",   # 同样是等宽 CJK
    r"C:\Windows\Fonts\msyh.ttc",     # 非等宽，仅兜底
]


def cell_width(text: str) -> int:
    """按终端列宽计长：CJK / 全角算 2 列，其余算 1 列。"""
    import unicodedata

    n = 0
    for ch in text:
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def wrap_cells(text: str, cols: int) -> list[str]:
    """按显示列宽折行（不是按字符数）。"""
    out: list[str] = []
    for ln in text.splitlines():
        cur = ""
        w = 0
        for ch in ln:
            cw = 2 if __import__("unicodedata").east_asian_width(ch) in ("W", "F") else 1
            if w + cw > cols:
                out.append(cur)
                cur, w = ch, cw
            else:
                cur += ch
                w += cw
        out.append(cur)
    return out

# 脱敏规则（顺序敏感）
SCRUB = [
    (re.compile(r"[A-Za-z]:\\[^\s]*runs\\[^\s]*"), "<runs>"),
    (re.compile(r"[A-Za-z]:\\[^\s]*ai-werewolf[^\s]*"), "<repo>"),
    (re.compile(r"http://127\.0\.0\.1:\d+"), "<proxy>"),
    (re.compile(r"liaobots\d*"), "relay-a"),
    (re.compile(r"lingkeai|cc88app"), "relay-b"),
    (re.compile(r"usa0\.top"), "relay-c"),
    # 官方通道名也一并抹掉，保持画面里只有「座位 / 模型 / 通道编号」
    (re.compile(r"[（(](deepseek|GLM|Kimi|千问|Minimax|火山方舟)[^）)]*[）)]"), ""),
]

# 字体（simhei/simsun）没有这些字形，替换成等价纯文本，避免显示成空框
CHAR_MAP = {
    "✅": "OK",
    "🐺": "[狼]",
    "█": "#",
    "├": "+",
    "└": "+",
    "│": "|",
    "─": "-",
    "→": "->",
    "←": "<-",
}


def map_chars(text: str) -> str:
    for a, b in CHAR_MAP.items():
        text = text.replace(a, b)
    return text

# 配色（近终端）
BG = (13, 17, 23)
FG = (201, 209, 217)
DIM = (110, 118, 129)
GREEN = (63, 185, 80)
CYAN = (86, 182, 194)
YELLOW = (210, 168, 71)
MAGENTA = (188, 140, 255)
RED = (248, 81, 73)
BLUE = (88, 166, 255)
WHITE = (240, 246, 252)


def pick_font() -> str:
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    raise SystemExit("找不到可用的中文字体")


def colorize(line: str):
    """按内容给一行分配颜色。返回 (text, color)。"""
    s = line.rstrip("\n")
    t = s.strip()
    if not t:
        return s, FG
    if t.startswith("==") or t.startswith("--"):
        return s, BLUE
    if "✅" in s:
        return s, GREEN
    if t.startswith("🐺") or "狼 人 阵 营" in s or "█" in s:
        return s, RED
    if t.startswith("[小桌]") or t.startswith("[私密"):
        return s, MAGENTA
    if t.startswith("[大桌]"):
        return s, FG
    if re.match(r"^\d+号\s", t) and ("狼人" in s or "村民" in s or "预言家" in s):
        return s, YELLOW
    if "泄漏" in s or "审计" in s:
        return s, CYAN
    if t.startswith("结果：") or t.startswith("调用 "):
        return s, WHITE
    return s, FG


def scrub(text: str) -> str:
    for pat, rep in SCRUB:
        text = pat.sub(rep, text)
    return text


def read_log(path: str) -> str:
    """读日志，自动探测编码。

    日志可能来自不同渠道：直接重定向是 UTF-8，经 PowerShell 捕获常变成
    UTF-16LE（带 BOM），老环境还可能是 GBK。这里按 BOM 优先、再逐个试。
    """
    with open(path, "rb") as fh:
        raw = fh.read()

    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le").lstrip("\ufeff")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be").lstrip("\ufeff")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")

    for enc in ("utf-8", "gbk", "utf-16-le"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        # 解码后若含大量替换符或控制符，视为猜错
        bad = sum(1 for ch in text if ch == "\ufffd")
        if bad == 0:
            return text
    return raw.decode("utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile")
    ap.add_argument("outfile")
    ap.add_argument("--cols", type=int, default=104)
    ap.add_argument("--rows", type=int, default=34)
    ap.add_argument("--font-size", type=int, default=17)
    ap.add_argument("--line-height", type=int, default=22)
    ap.add_argument("--step", type=int, default=10, help="每帧滚动多少行")
    ap.add_argument("--hold-frames", type=int, default=40, help="末尾定格帧数")
    ap.add_argument("--duration", type=int, default=90, help="每帧毫秒")
    ap.add_argument("--max-colors", type=int, default=64, help="调色板颜色数，越小文件越小")
    args = ap.parse_args()

    raw = read_log(args.logfile)
    raw = map_chars(scrub(raw))

    # 折行：按显示列宽（中文算 2 列）
    lines = wrap_cells(raw, args.cols)

    font_path = pick_font()
    font = ImageFont.truetype(font_path, args.font_size)
    pad = 14
    # 等宽 CJK 字体下，一个"列"= 半个中文宽度
    char_w = font.getlength("号") / 2
    img_w = int(char_w * args.cols) + pad * 2
    img_h = args.line_height * args.rows + pad * 2

    visible = args.rows
    total = len(lines)
    # 跨步滚动：每帧前进 step 行，保证首尾都在
    offsets = list(range(0, max(1, total - visible + 1), max(1, args.step)))
    if offsets[-1] != total - visible:
        offsets.append(max(0, total - visible))

    frames: list[Image.Image] = []
    for off in offsets:
        img = Image.new("RGB", (img_w, img_h), BG)
        d = ImageDraw.Draw(img)
        # 终端标题栏
        d.rectangle([0, 0, img_w, pad + 6], fill=(22, 27, 34))
        d.text((pad, 3), "run_text.py --stub --seats 9    （离线模式，不消耗额度）", font=font, fill=DIM)
        window = lines[off : off + visible]
        y = pad + 16
        for ln in window:
            text, col = colorize(ln)
            d.text((pad, y), text, font=font, fill=col)
            y += args.line_height
        # 量化调色板，GIF 体积能压掉一大截
        frames.append(img.quantize(colors=args.max_colors, method=Image.MEDIANCUT))

    # 末尾定格
    last = frames[-1]
    for _ in range(args.hold_frames):
        frames.append(last.copy())

    os.makedirs(os.path.dirname(os.path.abspath(args.outfile)), exist_ok=True)
    frames[0].save(
        args.outfile,
        save_all=True,
        append_images=frames[1:],
        duration=args.duration,
        loop=0,
        optimize=True,
        disposal=1,
    )
    size_kb = os.path.getsize(args.outfile) / 1024
    print(f"日志 {total} 行 -> 滚动 {len(offsets)} 步 + 定格 {args.hold_frames} -> {args.outfile}")
    print(f"尺寸 {img_w}x{img_h}  大小 {size_kb:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
