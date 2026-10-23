"""生成 static/favicon.ico 与 static/favicon.svg。

为什么手搓而不是引入 Pillow / cairosvg：
    这个小项目刻意保持零额外依赖（requirements.txt 里只有服务运行必需的几个包），
    为了一个图标把 Pillow 拉进来不划算。

做法：
    * 矢量形状（王冠 + 底座）自己用 4x 超采样做多边形/圆角矩形的覆盖判定，
      得到抗锯齿的覆盖率再混色 —— 小图标常见的 ray casting 覆盖
    * PNG 用 zlib + struct 手写（只有 IHDR / IDAT / IEND 三个块）
    * ICO 容器里直接嵌 PNG 条目（PNG-in-ICO，现代浏览器都支持），
      一份 ICO 里装 16 / 32 / 48 三个尺寸

图标本身和界面上的品牌标识是同一个造型：
    深底 #16161C + 薰衣草强调色 #B48CFF。改动配色请先改这里的常量。

用法：
    python tools/make_favicon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

# ---------------------------------------------------------------------------
# 配色（与 static/theme.css 保持一致）
# ---------------------------------------------------------------------------
BG = (22, 22, 28)        # 卡底 #16161C
FG = (180, 140, 255)     # 王冠 #B48CFF

# 形状定义在 24 x 24 的坐标系里，渲染时按目标尺寸缩放
CROWN = [(3.5, 19.0), (20.5, 19.0), (18.6, 8.6), (16.0, 12.4),
         (12.0, 5.2), (8.0, 12.4), (5.4, 8.6)]
BASE = (3.0, 19.6, 21.0, 21.2, 0.7)     # x0, y0, x1, y1, 圆角
CARD = (1.0, 1.0, 23.0, 23.0, 4.6)      # 外框（带圆角）

SIZES = (16, 32, 48)
SUPERSAMPLE = 4


# ---------------------------------------------------------------------------
# 几何：点在多边形 / 圆角矩形内
# ---------------------------------------------------------------------------
def in_polygon(px: float, py: float, pts: list[tuple[float, float]]) -> bool:
    """射线法判定点是否在多边形内（射线向右，统计交点奇偶）。"""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > py) != (yj > py):
            x_cross = xi + (py - yi) / (yj - yi) * (xj - xi)
            if px < x_cross:
                inside = not inside
        j = i
    return inside


def in_rounded_rect(px: float, py: float, rect: tuple[float, float, float, float, float]) -> bool:
    x0, y0, x1, y1, r = rect
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    # 把点夹到"去掉四个角"后的中心矩形里，超出 r 的部分就是角上的圆弧判定
    cx = min(max(px, x0 + r), x1 - r)
    cy = min(max(py, y0 + r), y1 - r)
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy <= r * r


def in_artwork(px: float, py: float) -> bool:
    return in_polygon(px, py, CROWN) or in_rounded_rect(px, py, BASE)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render(size: int) -> bytes:
    """渲染一张 RGBA 位图，返回未过滤的裸像素数据（每行前面不放 filter 字节）。"""
    ss = SUPERSAMPLE
    steps = [((s + 0.5) / ss) for s in range(ss)]
    raw = bytearray()

    for y in range(size):
        row = bytearray()
        for x in range(size):
            art_hits = card_hits = 0
            for dy in steps:
                for dx in steps:
                    px = (x + dx) / size * 24
                    py = (y + dy) / size * 24
                    if not in_rounded_rect(px, py, CARD):
                        continue
                    card_hits += 1
                    if in_artwork(px, py):
                        art_hits += 1
            if card_hits == 0:
                row += b"\x00\x00\x00\x00"
                continue
            total = ss * ss
            # 先算前景在卡片内部的覆盖率，再混色；卡片自身边缘用 alpha 表达
            ratio = (art_hits / card_hits) if art_hits else 0.0
            r = int(BG[0] * (1 - ratio) + FG[0] * ratio)
            g = int(BG[1] * (1 - ratio) + FG[1] * ratio)
            b = int(BG[2] * (1 - ratio) + FG[2] * ratio)
            a = int(255 * card_hits / total)
            row += bytes((r, g, b, a))
        raw += row
    return bytes(raw)


# ---------------------------------------------------------------------------
# PNG 编码
# ---------------------------------------------------------------------------
def png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_png(size: int, raw: bytes) -> bytes:
    """把裸像素包成 PNG。每行前加 filter type 0（None）。"""
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)   # 8bit RGBA
    stride = size * 4
    scanlines = b"".join(
        b"\x00" + raw[i * stride:(i + 1) * stride] for i in range(size)
    )
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(scanlines, 9))
            + png_chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# ICO 容器
# ---------------------------------------------------------------------------
def encode_ico(entries: list[tuple[int, bytes]]) -> bytes:
    """entries: [(size, png_bytes), ...]，按尺寸升序写入。"""
    count = len(entries)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    directory = b""
    images = b""
    for size, data in entries:
        # width/height 写 0 表示 256；这里最大 48，正常写即可
        w = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset
        )
        images += data
        offset += len(data)
    return header + directory + images


# ---------------------------------------------------------------------------
# 手写 SVG（给支持 SVG favicon 的浏览器，矢量在任何缩放都清晰）
# ---------------------------------------------------------------------------
SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">
  <rect x="1.33" y="1.33" width="29.33" height="29.33" rx="6.13" fill="#16161C"/>
  <path d="M4.67 25.33 H27.33 L24.8 11.47 L21.33 16.53 L16 6.93 L10.67 16.53 L7.2 11.47 Z" fill="#B48CFF"/>
  <rect x="4" y="26.13" width="24" height="2.13" rx="0.93" fill="#B48CFF"/>
</svg>
"""


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    static_dir = root / "static"
    static_dir.mkdir(exist_ok=True)

    ico_path = static_dir / "favicon.ico"
    svg_path = static_dir / "favicon.svg"

    entries = [(size, encode_png(size, render(size))) for size in SIZES]
    ico_path.write_bytes(encode_ico(entries))
    svg_path.write_text(SVG, encoding="utf-8")

    print(f"写好了 {ico_path}（{ico_path.stat().st_size} 字节，含 {SIZES} 三个尺寸）")
    print(f"写好了 {svg_path}（{svg_path.stat().st_size} 字节）")


if __name__ == "__main__":
    main()
