#!/usr/bin/env python3
"""演示游戏美术资源生成器（俯视角像素风，`REQ-044` / `REQ-048`）。

产出（全部是仓库内本地文件，`AC-037`）：`app/static/game/sprites/manifest.json`（冻结的接口
契约，引擎按**名字**取图）+ 6 张精灵表 PNG。

**本文件是产物与 manifest 的唯一来源**：布局表 `SHEETS` 同时决定 PNG 的实际尺寸与 manifest
里的像素矩形，两者不可能漂移。可复算做法、24 色板逐色用途、格式取舍见
`docs/game/美术资源方案.md`（本文只留数据与机械校验，不写第二份说明）。

用法：`python scripts/gen_game_art.py [--out DIR] [--check]`
  —— 无参数生成；`--out` 生成到别处；`--check` 自检（重生成逐字节比对 + 不变量），不写文件。
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from game_art_base import (  # noqa: E402  （与脚本同目录，Python 自动把脚本目录放进 sys.path）
    C, DIGITS, GUTTER, MANIFEST_VERSION, PALETTE, TILE_PX, Canvas, png_bytes, png_read)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "app" / "static" / "game" / "sprites"
#: 五类摊位：(精灵名, 棚顶主色, 条纹色, 货物形状)。顺序 = `V/F/M/A/杂货`。
#: **谁属于哪类由服务端 `category.code` 前缀推导**（V 蔬菜 / F 水果 / M 肉 / A 水产 /
#: 无单一主营 ⇒ 杂货），前端**不得硬编码** —— 这里只定义"长什么样"。
#: 定义在 `SHEETS` **之前**：`SHEETS` 要按它列出精灵名。
STALL_KINDS = (
    ("stall_veg", C["cloth_light"], C["leaf"], "leaf"),
    ("stall_fruit", C["cloth_boss"], C["fruit_yellow"], "round"),
    ("stall_meat", C["cloth_light"], C["fruit_red"], "slab"),
    ("stall_fish", C["cloth_light"], C["water"], "fish"),
    ("stall_grocery", C["cloth_admin"], C["cloth_light"], "mixed"),
)

#: 精灵表：(表名, 格宽, 格高, 列数, 行数, ((精灵名, 宽, 高), ...))，行优先；格内精灵落在格
#: 左上角 + (GUTTER, GUTTER)，表尺寸 = 格数 × (格 + 2×GUTTER)。
SHEETS = (
    ("terrain", 16, 16, 4, 1, (("tile_floor", 16, 16), ("tile_path", 16, 16),
                               ("tile_grass", 16, 16), ("tile_water", 16, 16))),
    ("stalls", 32, 32, 3, 3, tuple((f"stall_{i}", 32, 32) for i in range(4))
     + tuple((name, 32, 32) for name, _, _, _ in STALL_KINDS)),
    ("scales", 24, 24, 2, 1, (("scale_idle", 24, 24), ("scale_active", 24, 24))),
    ("people", 16, 24, 4, 3, tuple((f"person_{r}_{d}", 16, 24) for r in
                                   ("boss", "customer", "admin")
                                   for d in ("down", "up", "left", "right"))),
    ("items", 16, 16, 4, 2, tuple((f"item_{i}", 16, 16) for i in range(8))),
    ("props", 32, 32, 2, 1, (("prop_computer", 32, 32), ("prop_crate", 16, 16))),
)
#: 最小集精灵名（任务书冻结，`--check` 机械比对，缺一个即红）。**不得改名、不得少。**
REQUIRED_SPRITES = tuple(
    "tile_floor tile_path tile_grass tile_water stall_0 stall_1 stall_2 stall_3 "
    "scale_idle scale_active prop_computer prop_crate item_0 item_1 item_2 item_3 "
    "item_4 item_5 item_6 item_7".split()) + tuple(
    f"person_{r}_{d}" for r in ("boss", "customer", "admin")
    for d in ("down", "up", "left", "right"))




def tile_floor(c):
    c.rect(0, 0, 16, 16, C["floor_a"])         # 16px 大砖：只在右/下留砖缝
    c.rect(15, 0, 1, 16, C["path"]); c.rect(0, 15, 16, 1, C["path"])
    for x, y in ((3, 4), (9, 11), (12, 6), (6, 9)): c.put(x, y, C["path"])   # 砂粒质感
def tile_path(c):
    c.rect(0, 0, 16, 16, C["path"])
    for v in (7, 15): c.rect(v, 0, 1, 16, C["floor_a"]); c.rect(0, v, 16, 1, C["floor_a"])
    for x, y in ((2, 3), (5, 10), (11, 4), (13, 12)): c.put(x, y, C["root_brown"])
def tile_grass(c):
    c.rect(0, 0, 16, 16, C["grass"])
    for x, y in ((2, 4), (6, 10), (11, 5), (13, 12), (4, 13), (9, 2)):
        c.rect(x, y, 1, 3, C["leaf_dark"]); c.put(x, y - 1, C["leaf_dark"])   # 草叶
    for x, y in ((3, 5), (12, 6), (7, 11), (14, 3)): c.put(x, y, C["leaf"])
def tile_water(c):
    c.rect(0, 0, 16, 16, C["water"])
    c.rect(0, 15, 16, 1, C["steel_dark"]); c.rect(15, 0, 1, 16, C["steel_dark"])
    for x, y, n in ((2, 3, 5), (9, 7, 4), (4, 11, 6), (11, 13, 3)): c.rect(x, y, n, 1, C["steel"])


#: 4 个摊位：(棚顶主色, 棚顶条纹色, ((货位 x, y, 色), ...)) —— 棚顶与货物一起变，四个一眼可分
STALL_VARIANTS = (
    (C["cloth_boss"], C["cloth_light"], ((7, 12, C["fruit_red"]), (13, 12, C["fruit_red"]),
                                         (19, 12, C["fruit_red"]), (25, 12, C["fruit_red"]))),
    (C["cloth_light"], C["leaf"], ((7, 12, C["leaf"]), (13, 12, C["leaf_dark"]),
                                   (19, 12, C["leaf"]), (25, 12, C["leaf_dark"]))),
    (C["cloth_boss"], C["fruit_yellow"], ((7, 12, C["fruit_orange"]), (13, 12, C["fruit_yellow"]),
                                          (19, 12, C["fruit_orange"]), (25, 12, C["fruit_yellow"]))),
    (C["cloth_admin"], C["cloth_light"], ((7, 12, C["fruit_purple"]), (13, 12, C["root_brown"]),
                                          (19, 12, C["fruit_purple"]), (25, 12, C["root_brown"]))),
)


def draw_stall(c, a, b, goods):
    c.rect(0, 1, 32, 9, a)                     # 棚顶（y=0 留给 outline 描边）
    for i in range(4, 32, 8): c.rect(i, 1, 4, 9, b)   # 竖条纹
    c.rect(2, 10, 28, 6, C["ink_soft"])        # 棚下背板
    for x, y, col in goods: c.ellipse(x, y, 2, 2, col)   # 台面货物
    c.rect(1, 16, 30, 11, C["wood"])           # 货台
    for x in (7, 15, 23): c.rect(x, 16, 1, 11, C["root_brown"])   # 木纹
    c.rect(1, 16, 30, 1, C["cloth_light"])     # 台面高光
    c.rect(4, 19, 11, 6, C["cloth_light"])     # 价签
    c.rect(6, 21, 7, 1, C["ink"]); c.rect(6, 23, 5, 1, C["ink"])
    c.rect(3, 27, 3, 4, C["root_brown"]); c.rect(26, 27, 3, 4, C["root_brown"])   # 支腿
    c.outline(C["ink"])


# --- 按主营品类区分的摊位（`REQ-051` / `AC-041`） -------------------------------
#: 台面货物按品类换**形状**，不只换颜色 —— 只换色在小尺寸下几类会糊成一团，
#: 形状差异（叶 / 圆 / 厚片 / 长条 / 方箱）才是"一眼能分"的关键。
#: **只用 24 色板内的颜色**（`check()` 机械核验色板封闭）。
def _goods_leaf(c, x, y, i):
    c.ellipse(x, y + 1, 2, 2, C["leaf"] if i % 2 == 0 else C["leaf_dark"])
    c.rect(x - 1, y - 2, 3, 2, C["leaf_dark"])            # 菜叶


def _goods_round(c, x, y, i):
    c.ellipse(x, y, 2, 2, (C["fruit_red"], C["fruit_orange"],
                           C["fruit_yellow"], C["fruit_purple"])[i % 4])


def _goods_slab(c, x, y, i):
    c.ellipse(x, y, 3, 2, C["fruit_red"])                  # 肉块
    c.rect(x - 2, y, 5, 1, C["cloth_light"])               # 肥瘦纹路


def _goods_fish(c, x, y, i):
    c.ellipse(x, y, 3, 1, C["steel"])                      # 鱼身（细长）
    c.put(x + 3, y, C["steel_dark"]); c.put(x - 3, y, C["steel_dark"])   # 尾
    c.put(x + 1, y - 1, C["ink"])                          # 眼睛


def _goods_mixed(c, x, y, i):
    c.rect(x - 2, y - 1, 4, 3, C["root_brown"] if i % 2 == 0 else C["cloth_admin"])
    c.rect(x - 2, y - 1, 4, 1, C["fruit_yellow"])          # 箱盖


GOODS_DRAW = {"leaf": _goods_leaf, "round": _goods_round, "slab": _goods_slab,
              "fish": _goods_fish, "mixed": _goods_mixed}


def draw_stall_kind(c, a, b, kind):
    """品类摊位：棚顶配色 + 台面货物形状一起换，与 `draw_stall` 同版式（高度/支腿一致）。"""
    c.rect(0, 1, 32, 9, a)
    for i in range(4, 32, 8): c.rect(i, 1, 4, 9, b)
    c.rect(2, 10, 28, 6, C["ink_soft"])
    for i, x in enumerate((7, 13, 19, 25)): GOODS_DRAW[kind](c, x, 12, i)
    c.rect(1, 16, 30, 11, C["wood"])
    for x in (7, 15, 23): c.rect(x, 16, 1, 11, C["root_brown"])
    c.rect(1, 16, 30, 1, C["cloth_light"])
    c.rect(4, 19, 11, 6, C["cloth_light"])     # 价签
    c.rect(6, 21, 7, 1, C["ink"]); c.rect(6, 23, 5, 1, C["ink"])
    c.rect(3, 27, 3, 4, C["root_brown"]); c.rect(26, 27, 3, 4, C["root_brown"])
    c.outline(C["ink"])


def draw_scale(c, active):
    c.rect(4, 2, 16, 9, C["steel_dark"])       # 显示屏头
    c.rect(4, 2, 16, 1, C["steel"])
    c.rect(6, 4, 12, 6, C["screen_on"] if active else C["ink_soft"])
    if active:                                 # 屏上读数 "25"
        for i, ch in enumerate("25"):
            for j, mask in enumerate(DIGITS[int(ch)]):
                for k in range(3):
                    if mask & (4 >> k): c.put(7 + i * 4 + k, 5 + j, C["ink"])
    else:                                      # 待机 "--"
        c.rect(7, 7, 3, 1, C["steel"]); c.rect(12, 7, 3, 1, C["steel"])
    c.rect(10, 10, 4, 8, C["steel_dark"])      # 立柱
    c.rect(10, 10, 1, 8, C["steel"])
    c.rect(2, 17, 20, 4, C["steel"])           # 秤台
    c.rect(2, 20, 20, 1, C["steel_dark"])
    c.rect(3, 21, 3, 2, C["ink_soft"]); c.rect(18, 21, 3, 2, C["ink_soft"])   # 支脚
    if active:                                 # 台上放着待称商品 —— active 一眼可辨
        c.ellipse(16, 15, 3, 2, C["fruit_red"]); c.put(16, 12, C["leaf_dark"])
    c.outline(C["ink"])


def _boss_extra(c):
    c.rect(4, 11, 8, 7, C["cloth_light"])      # 围裙
    c.rect(4, 11, 8, 1, C["skin_shade"])
def _customer_extra(c):
    c.rect(12, 15, 3, 5, C["cloth_light"]); c.rect(12, 17, 3, 1, C["root_brown"])   # 购物袋
def _admin_extra(c):
    c.rect(1, 14, 3, 5, C["cloth_light"]); c.rect(1, 16, 3, 1, C["steel_dark"])   # 记录板


#: 身份 -> (发色, 上衣色, 附加装饰)。三身份靠 发色 + 上衣色 + 手持物 三重区分。
ROLES = {"boss": (C["hair_dark"], C["cloth_boss"], _boss_extra),
         "customer": (C["hair_dark"], C["cloth_cust"], _customer_extra),
         "admin": (C["steel"], C["cloth_admin"], _admin_extra)}


def draw_person(hair, shirt, direction, extra):
    c = Canvas(16, 24)
    if direction == "up":                      # 背面：整头头发，无五官
        c.rect(4, 1, 8, 9, hair); c.rect(5, 9, 6, 2, C["skin_shade"])
    else:
        c.rect(4, 2, 8, 8, C["skin"]); c.rect(4, 1, 8, 3, hair)
        c.rect(4, 4, 1, 3, hair); c.rect(11, 4, 1, 3, hair)
        if direction == "down":                # 正面：两眼 + 嘴
            c.put(6, 6, C["ink"]); c.put(9, 6, C["ink"]); c.rect(7, 8, 2, 1, C["skin_shade"])
        else:                                  # 侧面（朝左；right 由镜像得到）
            c.rect(4, 1, 8, 4, hair); c.rect(7, 1, 5, 6, hair)   # 后脑侧头发更厚
            c.put(5, 6, C["ink"]); c.put(4, 8, C["skin_shade"])  # 单眼与鼻口都偏向朝向侧
    c.rect(3, 10, 10, 8, shirt)                # 身体
    c.rect(2, 11, 1, 6, shirt); c.rect(13, 11, 1, 6, shirt)     # 手臂
    c.put(2, 17, C["skin"]); c.put(13, 17, C["skin"])           # 手
    c.rect(4, 18, 3, 5, C["ink_soft"]); c.rect(9, 18, 3, 5, C["ink_soft"])   # 腿
    c.rect(4, 23, 3, 1, C["ink"]); c.rect(9, 23, 3, 1, C["ink"])             # 脚
    extra(c)
    if direction == "right": c = c.mirror()
    c.outline(C["ink"])
    return c


def draw_item(c, k):
    if k == 0:                                 # 叶菜（一扎）
        c.ellipse(8, 9, 5, 4, C["leaf"]); c.ellipse(5, 6, 2, 3, C["leaf"])
        c.ellipse(11, 6, 2, 3, C["leaf"]); c.rect(8, 3, 1, 3, C["leaf_dark"])
        c.rect(7, 12, 3, 1, C["cloth_light"])
    elif k == 1:                               # 番茄
        c.ellipse(8, 9, 4, 4, C["fruit_red"]); c.rect(5, 5, 7, 1, C["leaf_dark"])
        c.rect(8, 4, 1, 2, C["leaf_dark"]); c.put(6, 8, C["cloth_light"])
    elif k == 2:                               # 胡萝卜（尖朝下）
        for i in range(9):
            c.rect(8 - (4 - i // 3), 4 + i, 1 + 2 * (4 - i // 3), 1, C["fruit_orange"])
        c.rect(8, 1, 1, 4, C["leaf_dark"]); c.put(6, 2, C["leaf_dark"]); c.put(10, 2, C["leaf_dark"])
    elif k == 3:                               # 茄子
        c.ellipse(8, 9, 4, 5, C["fruit_purple"]); c.rect(6, 4, 5, 1, C["leaf_dark"])
        c.rect(8, 2, 1, 3, C["leaf_dark"]); c.put(6, 8, C["cloth_light"])
    elif k == 4:                               # 玉米
        c.ellipse(8, 9, 4, 5, C["fruit_yellow"])
        for y in range(6, 13, 2):
            for x in range(6, 11, 2): c.put(x, y, C["root_brown"])
        c.rect(4, 8, 2, 5, C["leaf_dark"]); c.rect(11, 8, 2, 5, C["leaf_dark"])
    elif k == 5:                               # 土豆
        c.ellipse(8, 9, 5, 4, C["root_brown"]); c.put(6, 6, C["wood"])
        for x, y in ((6, 7), (10, 9), (7, 11), (11, 6)): c.put(x, y, C["ink_soft"])
    elif k == 6:                               # 白萝卜（尖朝下）
        c.ellipse(8, 8, 4, 4, C["cloth_light"])
        for i in range(5):
            c.rect(8 - (3 - i), 12 + i, 1 + 2 * (3 - i), 1, C["cloth_light"])
        c.rect(8, 2, 1, 3, C["leaf_dark"]); c.put(6, 3, C["leaf_dark"]); c.put(10, 3, C["leaf_dark"])
    else:                                      # 蘑菇
        c.ellipse(8, 7, 6, 3, C["root_brown"]); c.rect(6, 8, 4, 5, C["cloth_light"])
        for x, y in ((5, 6), (9, 5), (11, 7)): c.put(x, y, C["wood"])
    c.outline(C["ink"])


def prop_computer(c):                          # 管理员桌上的电脑 32x32
    c.rect(6, 3, 24, 18, C["steel_dark"]); c.rect(8, 5, 20, 14, C["ink_soft"])   # 显示器
    for i, y in enumerate((7, 9, 11, 13, 15)):
        c.rect(10, y, (14, 10, 16, 8, 12)[i], 1, C["screen_on"])   # 终端文字行
    c.rect(16, 21, 4, 5, C["steel_dark"]); c.rect(12, 26, 12, 2, C["steel_dark"])   # 支架/底座
    c.rect(8, 28, 20, 3, C["steel_dark"]); c.rect(8, 28, 20, 1, C["steel"])         # 键盘
    for x in range(10, 27, 3): c.put(x, 29, C["ink_soft"])
    c.outline(C["ink"])
def prop_crate(c):                             # 菜筐 16x16
    c.rect(1, 6, 14, 9, C["root_brown"])
    for y in (6, 9, 12): c.rect(1, y, 14, 1, C["wood"])
    c.rect(0, 7, 1, 3, C["ink_soft"]); c.rect(15, 7, 1, 3, C["ink_soft"])   # 提手缺口
    c.ellipse(5, 4, 2, 2, C["leaf"]); c.ellipse(9, 3, 2, 2, C["fruit_red"])
    c.ellipse(12, 5, 2, 2, C["fruit_orange"])   # 筐口露出的菜
    c.outline(C["ink"])


def _mk(fn, w, h, *a):
    def make():
        c = Canvas(w, h); fn(c, *a); return c
    return make


SPRITES = {"tile_floor": _mk(tile_floor, 16, 16), "tile_path": _mk(tile_path, 16, 16),
           "tile_grass": _mk(tile_grass, 16, 16), "tile_water": _mk(tile_water, 16, 16),
           "scale_idle": _mk(draw_scale, 24, 24, False),
           "scale_active": _mk(draw_scale, 24, 24, True),
           "prop_computer": _mk(prop_computer, 32, 32), "prop_crate": _mk(prop_crate, 16, 16)}
for _i in range(4):
    SPRITES[f"stall_{_i}"] = _mk(draw_stall, 32, 32, *STALL_VARIANTS[_i])
for _name, _a, _b, _kind in STALL_KINDS:
    SPRITES[_name] = _mk(draw_stall_kind, 32, 32, _a, _b, _kind)
for _i in range(8):
    SPRITES[f"item_{_i}"] = _mk(draw_item, 16, 16, _i)
for _r, (_hair, _shirt, _extra) in ROLES.items():
    for _d in ("down", "up", "left", "right"):
        SPRITES[f"person_{_r}_{_d}"] = (
            lambda hair, shirt, d, e: lambda: draw_person(hair, shirt, d, e))(
                _hair, _shirt, _d, _extra)


def build(out_dir: Path) -> dict:
    """生成全部 PNG 与 manifest.json，返回 manifest 对象。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"version": MANIFEST_VERSION, "tile_px": TILE_PX, "sheets": {}, "sprites": {}}
    for sheet, cw, ch, cols, rows, entries in SHEETS:
        sw, sh = cols * (cw + 2 * GUTTER), rows * (ch + 2 * GUTTER)
        canvas = Canvas(sw, sh)
        for i, (name, w, h) in enumerate(entries):
            art = SPRITES[name]()
            assert (art.w, art.h) == (w, h) and w <= cw and h <= ch, \
                f"{name} 画成 {art.w}x{art.h}，布局表写 {w}x{h}（格 {cw}x{ch}）"
            x = (i % cols) * (cw + 2 * GUTTER) + GUTTER
            y = (i // cols) * (ch + 2 * GUTTER) + GUTTER
            canvas.blit(art, x, y)
            manifest["sprites"][name] = {"sheet": sheet, "x": x, "y": y, "w": w, "h": h}
        fname = f"{sheet}.png"
        (out_dir / fname).write_bytes(png_bytes(canvas))
        manifest["sheets"][sheet] = {"file": fname, "w": sw, "h": sh}
    (out_dir / "manifest.json").write_bytes(
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return manifest
def check(out_dir: Path, rebuild: bool = True) -> list:
    """自检：重生成后逐字节比对 + 矩形在表内 + PNG 尺寸与 manifest 一致 + 24 色板封闭 + 最小集无缺。"""
    errs = []
    if rebuild:                                # 「删掉产物重跑逐字节还原」的机械版
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "sprites"
            build(fresh)
            old = {q.name for q in out_dir.iterdir() if q.is_file()}
            new = {q.name for q in fresh.iterdir() if q.is_file()}
            errs += [f"缺失产物：{n}" for n in sorted(new - old)]
            errs += [f"多余产物：{n}" for n in sorted(old - new)]
            errs += [f"字节不一致：{n}" for n in sorted(new & old)
                     if (out_dir / n).read_bytes() != (fresh / n).read_bytes()]
    m = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    sheets, allowed = m["sheets"], set(PALETTE.values())
    for name, s in sorted(m["sprites"].items()):
        if s["sheet"] not in sheets:
            errs.append(f"{name}: sheet `{s['sheet']}` 不在 sheets 里")
        else:
            sh = sheets[s["sheet"]]
            if min(s["x"], s["y"]) < 0 or s["x"] + s["w"] > sh["w"] or s["y"] + s["h"] > sh["h"]:
                errs.append(f"{name}: 矩形 ({s['x']},{s['y']},{s['w']},{s['h']}) 越出 "
                            f"{s['sheet']} {sh['w']}x{sh['h']}")
    missing = sorted(set(REQUIRED_SPRITES) - set(m["sprites"]))
    if missing: errs.append(f"最小集精灵缺失 {len(missing)} 个：{missing}")
    # 品类摊位（`REQ-051`）与最小集同等待遇：**缺一个即红** —— 否则"摊位按品类区分"
    # 会在缺图时静默退化成同一张图，而页面上只是"看起来都差不多"，没人会发现。
    missing_kind = sorted({name for name, _, _, _ in STALL_KINDS} - set(m["sprites"]))
    if missing_kind: errs.append(f"品类摊位精灵缺失 {len(missing_kind)} 个：{missing_kind}")
    for sheet, info in sorted(sheets.items()):
        w, h, rows = png_read(out_dir / info["file"])
        if (w, h) != (info["w"], info["h"]):
            errs.append(f"{sheet}: manifest 写 {info['w']}x{info['h']}，PNG 实际 {w}x{h}")
        for y, row in enumerate(rows):
            for x in range(w):
                r, g, b, a = row[x * 4:x * 4 + 4]
                if a and a != 255:
                    errs.append(f"{info['file']}:({x},{y}) alpha={a}（只允许 0/255）")
                elif a and (r, g, b) not in allowed:
                    errs.append(f"{info['file']}:({x},{y}) #{r:02X}{g:02X}{b:02X} 不在 24 色板内")
    return errs
def main(argv=None) -> int:
    if not getattr(sys.stdout, "isatty", lambda: True)():    # 被重定向时强制 UTF-8：与 locale /
        getattr(sys.stdout, "reconfigure", lambda **kw: None)(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="演示游戏像素美术资源生成器（REQ-048）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--check", action="store_true", help="自检：重生成逐字节比对 + 不变量校验")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if args.check:
        errs = check(out)
        print("\n".join(errs) if errs else f"[check] OK：{len(SPRITES)} 个精灵 / 重生成逐字节一致 / "
              f"矩形全在表内 / 24 色板封闭 / 最小集无缺")
        return 1 if errs else 0
    manifest = build(out)
    print(f"[gen_game_art] {len(manifest['sprites'])} 个精灵 / {len(manifest['sheets'])} 张表"
          f" → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
