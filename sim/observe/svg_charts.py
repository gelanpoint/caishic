"""**内联 SVG 图元原语**（`T-SIM-09`；`docs/sim-design.md` §8 `T-SIM-09` 交付物）。

## 这份报告为什么必须"纯静态"

演示机**可能就是没有外网**（`AGENTS.md` §3 硬要求 4、`REQ-025`、`AC-013`）。一个外链
在开发机上"看起来没问题"，到现场会让页面卡住等超时，而观众只会看到"报告坏了"。
故这里的每一个图元都是**纯函数**：只吐 SVG 标记字符串，**不引用任何资源** ——
无网络地址、无外部字体、无包管理器产物目录、无脚本。`T-SIM-09` 验收① 用
`tests/contract/test_no_external_assets.py::scan_static_text()` 扫这份产物（不另写一套正则）。

## 为什么拆成两个文件（`quality-gates.md` §1.2 的 400 行门禁）

* `svg_charts.py`（本文件）：**纯函数、无 I/O、无数据口径** —— 可被灵敏度负例直接喂；
* `svg_report.py`：**读产物 → 选数据 → 排版**。

## 数据纪律（本项目硬约束）

**图元不接受"结论"，只接受数值与 `None`。** 值为 `None` 一律画成显式的「未生成」灰条，
**绝不画成 0、也不省略这一行** —— 缺产物时最危险的不是报错，而是图看起来是齐的。
"""

from __future__ import annotations

#: 调色板（全部为字面色值，**没有渐变、没有图案填充** ⇒ 离线渲染一致）
PALETTE = {
    "ink": "#1f2933", "muted": "#7b8794", "bar": "#2f6f9f", "bar_alt": "#6aa1c9",
    "good": "#2f7d55", "bad": "#a83232", "warn": "#9a6a12", "line": "#2f6f9f",
    "panel": "#f4f6f8", "edge": "#d6dde3", "miss": "#c9d2d9",
}

#: 缺数据时的**唯一**占位词。全报告只此一处，测试据此断言（不许各处各写一个近义词）
NOT_GENERATED = "未生成"

_FONT = "system-ui, sans-serif"


def esc(text) -> str:
    """XML 转义。**必须过这一道**：臂名里有 `①`、`&`，裸写会让整页解析失败。"""
    out = str(text)
    for old, new in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;")):
        out = out.replace(old, new)
    return "".join(ch for ch in out if ch >= " " or ch in "\t")


def pct(value) -> str:
    return NOT_GENERATED if value is None else f"{value * 100:.2f}%"


def svg(width: int, height: int, title: str, body: str) -> str:
    """一张图的骨架。`title` 同时进 `<title>` 与 `role="img"` ⇒ 读屏与检索都能拿到。

    ⚠️ **刻意不写 `xmlns`**：本模块只产出**内联在 HTML5 里**的 `<svg>`，HTML 解析器会自动
    把它放进 SVG 命名空间，不需要声明。而 `xmlns="http://www.w3.org/2000/svg"` 是一个
    **字面外链**（浏览器不会去请求它，但 `scan_static_text` 判的是**字面**）——
    写了就会让 `T-SIM-09` 验收① 判红。这一条是实测踩出来的，不是洁癖：
    独立 `.svg` 文件才需要 `xmlns`，本项目不产出独立 `.svg`。
    """
    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" role="img" aria-label="{esc(title)}" font-family="{_FONT}">'
            f"<title>{esc(title)}</title>{body}</svg>")


def text(x, y, content, *, size=13, fill=None, anchor="start", weight="normal") -> str:
    color = fill or PALETTE["ink"]
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" text-anchor="{anchor}" '
            f'font-weight="{weight}">{esc(content)}</text>')


def bar_chart(rows, *, width=900, row_h=32, label_w=330, bar_w=440, max_value=1.0,
              formatter=pct, missing=NOT_GENERATED) -> str:
    """横向条形图。`rows` = `[(标签, 值 | None, 附注 | None)]`。

    值为 `None` ⇒ 画灰色占位条并写上 `missing`：**行仍然在**，读者一眼看到"这一项没数据"。
    `max_value` 让不同量纲的图共用一套几何（走秤率 0~1、等待天数 0~100…）。
    """
    height = row_h * len(rows) + 26
    parts = [text(4, 16, f"横轴 0 ~ {formatter(max_value)}（按每行自己的量纲）", size=11, fill=PALETTE["muted"])]
    for index, row in enumerate(rows):
        label, value, note = (list(row) + [None, None])[:3]
        y = 26 + index * row_h
        parts.append(text(4, y + row_h * 0.66, label, size=12, fill=PALETTE["ink"]))
        frame = (f'<rect x="{label_w}" y="{y + 3}" width="{bar_w}" height="{row_h - 12}" rx="3" '
                 f'fill="{PALETTE["panel"]}" stroke="{PALETTE["edge"]}"/>')
        parts.append(frame)
        if value is None:
            parts.append(text(label_w + 8, y + row_h * 0.66, missing, size=12, fill=PALETTE["bad"], weight="bold"))
            parts.append(text(label_w + bar_w + 10, y + row_h * 0.66, "（本项未生成，不填 0）",
                              size=11, fill=PALETTE["muted"]))
            continue
        #: ⚠️ 局部变量**不能**叫 `width`：它与本函数的 `width` 参数同名。第一版就写成 `width = ...`，
        #: 于是循环结束后 `width` 被改成**最后一根条**的像素宽，再被 `svg()` 当成画布宽 ——
        #: 实测生成 `width="351.58"` 而 `viewBox` 也是 351.58，整张图只剩最左边 21px 露在框外，
        #: 看上去"每行只有一个小方块"。**浏览器截图是把它抓出来的，纯扫描查不出来**
        #: （标签里既没有外链也没有非法属性）。
        bar_px = max(1.0, bar_w * min(abs(float(value)) / (max_value or 1.0), 1.0))
        color = PALETTE["bar"] if index % 2 == 0 else PALETTE["bar_alt"]
        parts.append(f'<rect x="{label_w}" y="{y + 3}" width="{bar_px:.1f}" height="{row_h - 12}" rx="3" fill="{color}"/>')
        parts.append(text(label_w + bar_w + 10, y + row_h * 0.66,
                          formatter(value) + (f"　{note}" if note else ""), size=11, fill=PALETTE["muted"]))
    return svg(width, height, f"条形图：{len(rows)} 行", "".join(parts))


def line_chart(values, *, title="按期序列", width=760, height=190, y_max=1.0, y_label="") -> str:
    """单序列折线。`values` 为 `None` 或不足 2 点 ⇒ 直接给 `未生成` 面板（不画空图假装有数据）。"""
    points = [v for v in (values or []) if isinstance(v, (int, float))]
    if len(points) < 2:
        return svg(width, height, title, text(12, 40, f"{NOT_GENERATED}：序列不足 2 个点", size=13,
                                               fill=PALETTE["bad"], weight="bold"))
    left, right, top, bottom = 52, width - 12, 16, height - 34
    span = (y_max - 0.0) or 1.0
    step = (right - left) / (len(points) - 1)
    xy = [(left + index * step, bottom - (value / span) * (bottom - top)) for index, value in enumerate(points)]
    body = [f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="{PALETTE["edge"]}"/>',
            f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="{PALETTE["edge"]}"/>',
            text(left - 6, top + 6, pct(y_max), size=10, fill=PALETTE["muted"], anchor="end"),
            text(left - 6, bottom + 4, pct(0.0), size=10, fill=PALETTE["muted"], anchor="end"),
            '<polyline fill="none" stroke="' + PALETTE["line"] + '" stroke-width="2" points="'
            + " ".join(f"{x:.1f},{y:.1f}" for x, y in xy) + '"/>']
    body += [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{PALETTE["line"]}"/>' for x, y in xy]
    body += [text(left, height - 12, f"第 1 期", size=10, fill=PALETTE["muted"]),
             text(right, height - 12, f"第 {len(points)} 期", size=10, fill=PALETTE["muted"], anchor="end"),
             text(left, top - 4, y_label or title, size=11, fill=PALETTE["muted"])]
    return svg(width, height, title, "".join(body))


def badge(content: str, tone: str = "ink") -> str:
    """三态/四类的小圆角标签。**tone 是有限的几个键**，不是任意样式串（避免把 CSS 注入当成 API）。"""
    color = PALETTE.get(tone, PALETTE["ink"])
    width = 34 + 13 * len(content)
    return svg(width, 26, f"标记：{content}",
               f'<rect x="0" y="0" width="{width}" height="24" rx="12" fill="{PALETTE["panel"]}" '
               f'stroke="{color}"/><text x="{width / 2:.0f}" y="17" font-size="12" fill="{color}" '
               f'text-anchor="middle" font-weight="bold">{esc(content)}</text>')


def panel(title: str, lines, *, width=900, missing_reason: str | None = None) -> str:
    """一个带标题的说明面板。`missing_reason` 非空 ⇒ 渲染成醒目的「未生成」块。"""
    rows = list(lines or [])
    height = 40 + 20 * (len(rows) + 1) + (34 if missing_reason else 0)
    body = [text(12, 24, title, size=14, weight="bold"),
            f'<line x1="8" y1="32" x2="{width - 8}" y2="32" stroke="{PALETTE["edge"]}"/>']
    for index, line in enumerate(rows):
        body.append(text(16, 56 + index * 20, line, size=12, fill=PALETTE["ink"]))
    if missing_reason:
        y = 56 + len(rows) * 20
        body.append(f'<rect x="10" y="{y - 14}" width="{width - 20}" height="30" rx="4" fill="#fbeaea" '
                    f'stroke="{PALETTE["bad"]}"/>')
        body.append(text(20, y + 6, f"{NOT_GENERATED}：{missing_reason}", size=12,
                         fill=PALETTE["bad"], weight="bold"))
    return svg(width, height, title, "".join(body))