"""演示游戏的**跨引擎**走查（Gecko）。

为什么单独一个文件、且必须单独一个文件：本仓库的 `browser` 夹具（`tests/conftest.py`）是
**Chromium 专用**且 `scope="module"`；Playwright 的同步 API **在同一个进程里只能有一个
上下文**（第二个 `sync_playwright()` 会报 "using Playwright Sync API inside the asyncio loop"）。
所以 Gecko 的走查要放在**不请求 `browser` 夹具**的模块里，各占一个上下文。

为什么值得跨引擎：悬停清单为了让 25 件商品一次看全，改用了 CSS **多列**（`column-count`）。
多列布局正是各引擎差异较大的地方 —— 在 Chromium 上实测过"多列 + 限高"会把放不下的内容
溢出到**新列**里、而新列在水平方向被裁掉（`scrollWidth 785 > clientWidth 213`，商品直接丢）。
这种坑在另一个内核上完全可能以另一种形式出现，**只在 Chromium 上验过不足以说明问题**。

缺浏览器内核时**跳过并说明"这不是通过，是没检查"**（与既有 `browser` 夹具同规则）。
安装：`python -m playwright install firefox`。
"""

from __future__ import annotations

import pytest

from e2e_support import active_products, bind_stall, snap

STALL = "A-01"


@pytest.fixture(scope="module")
def firefox():
    """Gecko 内核（本模块**唯一**的 Playwright 上下文）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - 取决于开发机环境
        pytest.skip("环境里没有 Playwright —— **这不是通过，是没检查**")
    with sync_playwright() as play:
        try:
            instance = play.firefox.launch()
        except Exception as error:  # pragma: no cover - 取决于开发机环境
            pytest.skip(
                f"Firefox 起不来（{error}）—— **这不是通过，是没检查**"
                "（安装：python -m playwright install firefox）"
            )
        yield instance
        instance.close()


def _open_game(page, server) -> None:
    page.goto(f"{server.base}/game/", wait_until="domcontentloaded")
    page.wait_for_function("() => window.GameApp && GameApp.state.world", timeout=30000)
    page.wait_for_function(
        "() => document.getElementById('dataBadge').className.includes('on')", timeout=30000
    )
    page.evaluate("window.scrollTo(0, 0)")


def test_gecko_keeps_integer_scaling_and_shows_the_whole_hover_list(
    firefox, live_server, shots
):
    """同一条不变量在 Gecko 上必须同样成立：**1:1 整数缩放** + **清单一件不少、零裁切**。"""
    token = bind_stall(live_server, STALL)
    expected = len(active_products(live_server, token))

    page = firefox.new_page(viewport={"width": 1280, "height": 900})
    page.set_default_timeout(20000)
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on(
        "console",
        lambda m: problems.append(f"console.{m.type}: {m.text}")
        if m.type == "error" and "Failed to load resource" not in m.text
        else None,
    )
    try:
        _open_game(page, live_server)

        info = page.evaluate(
            """() => { const c = document.getElementById('gameCanvas');
                       const r = c.getBoundingClientRect();
                       return { native: c.width, display: r.width,
                                scale: GameApp.state.scale, styleWidth: c.style.width,
                                overflowX: document.documentElement.scrollWidth
                                           - document.documentElement.clientWidth }; }"""
        )
        assert info["display"] == pytest.approx(info["native"], abs=0.01), (
            f"Gecko 上画布被分数比例缩放：显示 {info['display']} vs 后备 {info['native']}"
        )
        assert info["styleWidth"].endswith("px"), (
            f"Gecko 上显示宽度不该是百分比（{info['styleWidth']!r}）"
        )
        assert info["scale"] in (1, 2), f"Gecko 上放大倍数不是正整数：{info['scale']}"
        assert info["overflowX"] <= 0, f"Gecko 上出现横向溢出 {info['overflowX']}px"

        point = page.evaluate(
            """() => { const w = GameApp.state.world;
                       const pl = w.plots.find(p => p.stallNo === 'A-01');
                       const s = GameApp.state.scale;
                       const r = document.getElementById('gameCanvas').getBoundingClientRect();
                       return { x: r.left + (pl.pad.x + pl.pad.w / 2) * 16 * s,
                                y: r.top + (pl.pad.y + pl.pad.h / 2) * 16 * s }; }"""
        )
        page.mouse.move(point["x"], point["y"])
        page.wait_for_timeout(700)

        state = page.evaluate(
            """() => { const t = document.getElementById('tooltip');
                       const l = document.getElementById('tooltipList');
                       return { hidden: t.classList.contains('hide'),
                                rows: l.children.length,
                                cols: getComputedStyle(l).columnCount,
                                hClip: Math.max(0, l.scrollWidth - l.clientWidth),
                                vClip: Math.max(0, l.scrollHeight - l.clientHeight),
                                top: Math.round(t.getBoundingClientRect().top),
                                bottom: Math.round(t.getBoundingClientRect().bottom),
                                vh: window.innerHeight }; }"""
        )
        assert not state["hidden"], "Gecko 上悬停没弹出清单"
        assert state["rows"] == expected, (
            f"Gecko 上清单 {state['rows']} 件，服务端在售 {expected} 件 —— 必须逐项相等"
        )
        assert state["hClip"] == 0 and state["vClip"] == 0, (
            f"Gecko 上多列清单被裁切（横向 {state['hClip']}px / 纵向 {state['vClip']}px）"
            " —— 多列把内容溢出到新列并被裁掉，是 Chromium 上真实踩过的坑"
        )
        assert 0 <= state["top"] and state["bottom"] <= state["vh"], (
            f"Gecko 上清单跑到视口外（{state['top']}..{state['bottom']} / vh={state['vh']}）"
        )
        snap(page, shots, "game-gecko-hover")
        assert problems == [], f"Gecko 上出现前端错误：{problems}"
    finally:
        page.close()
