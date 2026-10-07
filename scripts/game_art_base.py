"""演示游戏像素美术资源生成器 · **基础层**（调色板 / 画布 / PNG 读写）。

从 `gen_game_art.py` 拆出来的，原因只有一个：**单文件 ≤400 行**（`quality-gates.md` §1.2，
适用范围含 `*.py`）。加完"按品类区分的五套摊位"之后 `gen_game_art.py` 涨到 467 行越了线，
故把与"画什么"无关的基础设施挪到这里 —— 拆的是**层**（画布/IO vs 图形定义），
不是按行数硬切。`gen_game_art.py` 仍是唯一入口（CLI 与 `--check` 行为不变）。

**色彩唯一来源仍是这里的 24 色板**：`--check` 机械验证每个不透明像素都落在板内。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

MANIFEST_VERSION = 1
TILE_PX = 16
#: 每个精灵四周的透明隔离带（像素）：防止画布缩放/双线性采样把相邻精灵的边缘吃进来。
GUTTER = 1
#: 全部 24 色（**唯一色彩来源**；逐色用途表在 docs/game/美术资源方案.md §3）。`--check` 机械
#: 验证 PNG 每个不透明像素都落在这 24 色内。
PALETTE = dict(zip(
    "ink ink_soft skin skin_shade hair_dark cloth_boss cloth_cust cloth_admin cloth_light "
    "floor_a path grass leaf leaf_dark water steel steel_dark screen_on wood root_brown "
    "fruit_red fruit_orange fruit_purple fruit_yellow".split(),
    ((0x24, 0x1A, 0x2E), (0x4A, 0x35, 0x50), (0xFF, 0xD9, 0xA6), (0xE0, 0xA8, 0x70),
     (0x4A, 0x2C, 0x1A), (0xE8, 0x56, 0x3F), (0x3F, 0x7F, 0xD8), (0x2F, 0xA6, 0xA0),
     (0xF5, 0xE6, 0xC8), (0xE9, 0xD9, 0xBC), (0xC2, 0xA8, 0x7F), (0x7C, 0xBF, 0x4A),
     (0x4F, 0xA8, 0x3C), (0x2E, 0x7A, 0x28), (0x4F, 0xA8, 0xD8), (0xC7, 0xCD, 0xD6),
     (0x8A, 0x92, 0x9E), (0x6B, 0xE0, 0x7A), (0xA9, 0x74, 0x3F), (0x8B, 0x5A, 0x2B),
     (0xE0, 0x39, 0x2B), (0xF0, 0x8A, 0x24), (0x7A, 0x4F, 0xB5), (0xF5, 0xC5, 0x42))))
C = {name: rgb + (255,) for name, rgb in PALETTE.items()}


def _chunk(tag, body):
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)


def png_bytes(canvas: "Canvas") -> bytes:
    """手写 PNG（8bit RGBA）：签名 + IHDR + IDAT + IEND。**不写任何带时间/环境的 chunk**。"""
    raw = bytearray()
    for y in range(canvas.h):
        raw.append(0)  # filter 0 (None)：固定，不用自适应滤波（其结果依赖启发式，会漂）
        raw += canvas.px[y * canvas.w * 4:(y + 1) * canvas.w * 4]
    # zlib 参数逐个显式写死（level/method/wbits/memLevel/strategy），不吃默认值
    co = zlib.compressobj(9, zlib.DEFLATED, 15, 8, zlib.Z_DEFAULT_STRATEGY)
    idat = co.compress(bytes(raw)) + co.flush()
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", canvas.w, canvas.h, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))
def png_read(path: Path):
    """读回 PNG → (w, h, [扫描线 bytes])。只接受本脚本写出的形态（filter 恒为 0）。"""
    blob = path.read_bytes()
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} 不是 PNG"
    pos, idat, ihdr = 8, b"", None
    while pos < len(blob):
        ln = struct.unpack(">I", blob[pos:pos + 4])[0]
        tag, body = blob[pos + 4:pos + 8], blob[pos + 8:pos + 8 + ln]
        if tag == b"IHDR": ihdr = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT": idat += body
        pos += 12 + ln
    assert ihdr and ihdr[2] == 8 and ihdr[3] == 6, f"{path.name} 不是 8bit RGBA"
    w, h, raw, rows, p = ihdr[0], ihdr[1], zlib.decompress(idat), [], 0
    for _ in range(h):
        assert raw[p] == 0, f"{path.name} 出现 filter {raw[p]}（本脚本只写 filter 0）"
        p += 1
        rows.append(raw[p:p + w * 4])
        p += w * 4
    return w, h, rows
class Canvas:
    """最小像素画布：RGBA bytearray + 够用的图元。像素画靠图元组合，不手抄点阵。"""
    def __init__(self, w, h):
        self.w, self.h, self.px = w, h, bytearray(w * h * 4)   # 全透明
    def put(self, x, y, c):
        if c is not None and 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 4
            self.px[i:i + 4] = bytes(c)
    def rect(self, x, y, w, h, c):
        for j in range(y, y + h):
            for i in range(x, x + w): self.put(i, j, c)
    def ellipse(self, cx, cy, rx, ry, c):
        for dy in range(-ry, ry + 1):
            for dx in range(-rx, rx + 1):
                if rx * rx * ry * ry and dx * dx * ry * ry + dy * dy * rx * rx <= rx * rx * ry * ry:
                    self.put(cx + dx, cy + dy, c)
    def outline(self, c):
        """给非透明像素的 4 邻域空白补一圈描边（先采样后写，避免描边自我扩散）。"""
        add = [(x, y) for y in range(self.h) for x in range(self.w)
               if not self.px[(y * self.w + x) * 4 + 3]
               and any(0 <= x + dx < self.w and 0 <= y + dy < self.h
                       and self.px[((y + dy) * self.w + x + dx) * 4 + 3]
                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))]
        for x, y in add: self.put(x, y, c)
    def mirror(self):
        m = Canvas(self.w, self.h)
        for y in range(self.h):
            for x in range(self.w):
                i = (y * self.w + x) * 4
                m.put(self.w - 1 - x, y, tuple(self.px[i:i + 4]))
        return m
    def blit(self, src, dx, dy):
        for y in range(src.h):
            for x in range(src.w):
                i = (y * src.w + x) * 4
                if src.px[i + 3]: self.put(dx + x, dy + y, tuple(src.px[i:i + 4]))
#: 3x5 点阵数字（每行 3 bit，bit2 = 最左）。秤屏读数用。
DIGITS = {0: (7, 5, 5, 5, 7), 1: (2, 6, 2, 2, 7), 2: (7, 1, 7, 4, 7), 3: (7, 1, 7, 1, 7),
          4: (5, 5, 7, 1, 1), 5: (7, 4, 7, 1, 7), 6: (7, 4, 7, 5, 7), 7: (7, 1, 1, 2, 2),
          8: (7, 5, 7, 5, 7), 9: (7, 5, 7, 1, 7)}
