"""演示游戏的**界面层**真浏览器走查：缩放 / 悬停 / 点秤操作（`T-GAME-03`）。

为什么要单独一组：`tests/e2e/test_demo_game.py` 验的是**数据层**（Node 载入真 `api.js`
打真服务），`test_no_external_assets.py` 验的是**零 CDN**。它们**都碰不到**
canvas 绘制、DOM 布局、鼠标悬停、点击与面板行为 —— 而这正是演示的全部看点。

本文件把界面层补上，并把两个**"不报错、不白屏、pytest 全绿"的真缺陷**钉成回归
（都是先由真浏览器实玩发现、再修的）：

1. **画布被 CSS 按分数比例缩放** —— 后备像素 1024、显示交给 `width:100%`，容器一窄就被
   浏览器按 0.8613 缩。像素风当场糊掉（实测 1280 窗口下 39.5% 的水平游程变成奇数）。
2. **悬停清单有一半商品够不着** —— 25 件自然高 480px，而清单上限 240px，且浮层是
   `pointer-events:none`，用户连滚都滚不了。

两者的共同点是：**功能"看起来"正常、没有任何报错**，只有真的量像素 / 真的点一下才会露。
故断言都落在**可量化的界面事实**上（显示尺寸 vs 后备像素、裁切像素数、能否命中），
不是"页面上有这么个元素"。视角只读 / 降级 / 零报错另见 `test_demo_game_views.py`。
"""

from __future__ import annotations

import pytest

from e2e_support import active_products, bind_stall, new_page, snap
from game_support import MEAT_STALL, VEG_STALL, hover, open_game, plot_center, watch_errors

STALL = VEG_STALL


# --------------------------------------------------------------------------- #
# 缺陷 1：分数比例缩放（像素风糊掉的根因）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "width,height",
    [(1920, 1080), (1500, 900), (1366, 768), (1280, 800), (1100, 900), (1024, 768), (900, 700)],
)
def test_canvas_is_never_scaled_fractionally(browser, live_server, width, height):
    """**核心不变量**：画布显示尺寸 == 后备像素尺寸，且放大倍数是正整数。

    只要这条破了，浏览器就会按分数比例重采样 —— 像素被抻成长短不一，观感直接崩，
    但页面**不报错、不白屏**，所以必须在这里量出来。
    """
    page = new_page(browser)
    try:
        page.set_viewport_size({"width": width, "height": height})
        open_game(page, live_server)
        info = page.evaluate(
            """() => {
                const c = document.getElementById('gameCanvas');
                const r = c.getBoundingClientRect();
                return { native: c.width, display: r.width, scale: GameApp.state.scale,
                         styleWidth: c.style.width,
                         overflowX: document.documentElement.scrollWidth
                                    - document.documentElement.clientWidth };
            }"""
        )
        assert info["display"] == pytest.approx(info["native"], abs=0.01), (
            f"{width}x{height}：显示尺寸 {info['display']} != 后备像素 {info['native']} "
            f"⇒ 浏览器会按 {info['display']/info['native']:.4f} 分数比例缩放，像素会糊"
        )
        assert info["styleWidth"].endswith("px"), (
            f"{width}x{height}：显示宽度不该是百分比（{info['styleWidth']!r}）"
        )
        assert info["scale"] in (1, 2) and isinstance(info["scale"], int), (
            f"{width}x{height}：放大倍数必须是正整数，实际 {info['scale']!r}"
        )
        assert info["native"] == 512 * info["scale"], (
            f"{width}x{height}：后备像素 {info['native']} != 512 × {info['scale']}"
        )
        assert info["overflowX"] <= 0, f"{width}x{height}：出现横向溢出 {info['overflowX']}px"
    finally:
        page.close()


def test_narrow_viewport_falls_back_to_one_x_not_fractional(browser, live_server):
    """放不下 2 倍时退到 1 倍（宁可地图小、也不缩糊）—— 退让方向必须是整数档位。"""
    page = new_page(browser)
    try:
        page.set_viewport_size({"width": 900, "height": 700})
        open_game(page, live_server)
        scale = page.evaluate("() => GameApp.state.scale")
        assert scale == 1, f"窄视口应退到 1 倍，实际 {scale}"
    finally:
        page.close()


# --------------------------------------------------------------------------- #
# 缺陷 2 + `AC-035`：悬停清单必须**全部可见**
# --------------------------------------------------------------------------- #


def test_hover_lists_every_product_without_clipping(browser, live_server, shots):
    """`AC-035` 的界面层：悬停弹出清单，且**每一件都能看到**（零横向/纵向裁切）。"""
    token = bind_stall(live_server, STALL)
    expected = len(active_products(live_server, token))

    page = new_page(browser)
    problems = watch_errors(page)
    try:
        open_game(page, live_server)
        hover(page, *plot_center(page, STALL, "pad"))
        state = page.evaluate(
            """() => {
                const tip = document.getElementById('tooltip');
                const list = document.getElementById('tooltipList');
                return { hidden: tip.classList.contains('hide'),
                         rows: list.children.length,
                         hClip: Math.max(0, list.scrollWidth - list.clientWidth),
                         vClip: Math.max(0, list.scrollHeight - list.clientHeight),
                         bottom: tip.getBoundingClientRect().bottom,
                         top: tip.getBoundingClientRect().top,
                         vh: window.innerHeight };
            }"""
        )
        assert not state["hidden"], "悬停摊位后清单没弹出"
        assert state["rows"] == expected, (
            f"清单 {state['rows']} 件，服务端在售 {expected} 件 —— 两者必须逐项相等（AC-035）"
        )
        assert state["hClip"] == 0 and state["vClip"] == 0, (
            f"清单被裁切（横向 {state['hClip']}px / 纵向 {state['vClip']}px）"
            " ⇒ 有商品用户永远看不到（浮层是 pointer-events:none，滚不动）"
        )
        assert 0 <= state["top"] and state["bottom"] <= state["vh"], (
            f"清单跑到视口外（top={state['top']}, bottom={state['bottom']}, vh={state['vh']}）"
        )
        snap(page, shots, "game-hover-list")
        assert problems == [], f"悬停期间出现前端错误：{problems}"
    finally:
        page.close()


# --------------------------------------------------------------------------- #
# 缺陷 3 + `AC-036`：点秤 → 调价 / 上下架（走真端点 + 查库）
# --------------------------------------------------------------------------- #


def test_clicking_scale_opens_ops_and_price_change_persists(browser, live_server, shots):
    """`AC-036` 的界面层：点智能秤 → 面板出现 → 键入新价 → 提交 → **查库确认真的变了**。"""
    token = bind_stall(live_server, STALL)
    product = active_products(live_server, token)[0]      # §3.5 只有 id/name，不含价格
    product_id, name = product["id"], product["name"]
    new_cents = 1234

    def _db_price() -> int | None:
        with live_server.connect_db() as conn:
            row = conn.execute(
                "SELECT unit_price_cents FROM price_item WHERE product_id = ?"
                " AND business_date = (SELECT MAX(business_date) FROM price_item)",
                (product_id,),
            ).fetchone()
        return row[0] if row else None

    old_cents = _db_price()
    assert old_cents is not None and old_cents != new_cents, (
        f"前置条件不成立：库里 {name} 的价格是 {old_cents}，无法与 {new_cents} 形成区分"
    )

    page = new_page(browser)
    problems = watch_errors(page)
    try:
        open_game(page, live_server)
        page.mouse.click(*plot_center(page, STALL, "scale"))
        page.wait_for_selector("#opsBody .op-input", timeout=10000)

        first = page.locator("#opsBody .op-row").first
        assert first.locator(".op-name").inner_text() == name, (
            "面板第一行应与服务端在售清单首项一致"
        )
        first.locator(".op-input").fill(f"{new_cents / 100:.2f}")
        first.locator("button.primary").click()
        page.wait_for_function(
            "() => /成功/.test(document.querySelector('#opsBody .result').textContent)",
            timeout=15000,
        )
        snap(page, shots, "game-price-changed")

        assert _db_price() == new_cents, (
            f"界面报成功，但库里 {name} 仍是 {_db_price()} 分（期望 {new_cents}）"
        )
        assert problems == [], f"调价期间出现前端错误：{problems}"
    finally:
        page.close()


def test_off_shelf_entry_is_visible_and_clickable(browser, live_server, shots):
    """**缺陷 3 的回归**：下架后「上架」入口必须**排在整个价目表之前**，且点得到、点了能恢复。

    原先它被追加在 24 行之后 ⇒ 落在视口外，`elementFromPoint` 取不到 ——
    功能上"有"、实际"够不着"，是最容易被漏掉的一类缺陷。
    这里断的是**顺序**（结构事实）而不是某个瞬时像素位置：顺序对了，用户第一眼就能看到。
    """
    token = bind_stall(live_server, STALL)
    product_id = active_products(live_server, token)[0]["id"]

    page = new_page(browser)
    try:
        open_game(page, live_server)
        page.mouse.click(*plot_center(page, STALL, "scale"))
        page.wait_for_selector("#opsBody .op-row button.ghost", timeout=10000)
        page.locator("#opsBody .op-row button.ghost").first.click()
        page.wait_for_function(
            "() => /下架成功/.test(document.querySelector('#opsBody .result').textContent)",
            timeout=15000,
        )
        snap(page, shots, "game-shelf-off")

        order = page.evaluate(
            """() => {
                const kids = [...document.getElementById('opsBody').children];
                return {
                    head: kids.findIndex(e => e.classList.contains('op-head')),
                    firstPriceRow: kids.findIndex(e => e.querySelector && e.querySelector('.op-input')),
                    hasEntry: kids.some(e => [...e.querySelectorAll('button')]
                                              .some(b => b.textContent === '上架')),
                };
            }"""
        )
        assert order["hasEntry"], "下架后没有出现「上架」入口"
        assert order["head"] != -1, "下架后没有「本次会话内已下架」分组"
        assert 0 <= order["head"] < order["firstPriceRow"], (
            f"「上架」分组排在整个价目表之后（head={order['head']}，"
            f"首个在售行={order['firstPriceRow']}）—— 用户要滚过 24 行才看得到，"
            "正是实玩抓到的那个缺陷"
        )

        entry = page.locator("#opsBody button", has_text="上架").first
        assert entry.is_visible(), "「上架」入口不可见"
        entry.click()
        page.wait_for_function(
            "() => /上架成功/.test(document.querySelector('#opsBody .result').textContent)",
            timeout=15000,
        )
        with live_server.connect_db() as conn:
            status = conn.execute(
                "SELECT status FROM product WHERE id = ?", (product_id,)
            ).fetchone()
        assert status is not None and status[0] == "active", (
            f"点「上架」后库里 status 仍是 {status}（期望 active）"
        )
    finally:
        page.close()


def test_operations_work_on_a_second_stall_not_just_the_first(browser, live_server):
    """换个摊位（肉类摊）同样能操作 —— 防"只对第一个摊位有效"的硬编码。"""
    page = new_page(browser)
    try:
        open_game(page, live_server)
        page.mouse.click(*plot_center(page, MEAT_STALL, "scale"))
        page.wait_for_selector("#opsBody .op-input", timeout=10000)
        title = page.locator("#opsTitle").inner_text()
        assert MEAT_STALL in title, f"面板标题应指向 {MEAT_STALL}，实际 {title!r}"

        names = page.locator("#opsBody .op-name").all_inner_texts()
        token = bind_stall(live_server, MEAT_STALL)
        expected = [p["name"] for p in active_products(live_server, token)]
        assert names == expected, (
            f"{MEAT_STALL} 面板清单与服务端不一致：{names[:5]}… vs {expected[:5]}…"
        )
    finally:
        page.close()
