"""演示游戏的**视角隔离 / 降级路径 / 零报错**走查（`T-GAME-03`）。

与 `test_demo_game_browser.py` 分开的原因：那一个盯的是**渲染与操作的量化事实**
（显示尺寸、裁切像素、能否命中），这一个盯的是**"什么能做、什么不能做"以及坏环境下会不会崩**。
两者的失败含义不同，混在一起会让"红了"不好归因。

覆盖的三条都是页面**自己写在界面上的承诺**，故必须真的去撞一遍：
1. `AC-034`：顾客 / 管理员视角**只读** —— 点智能秤必须被拒，且面板里**一个写入口都没有**；
2. 美术资源缺失时"**降级为色块 + 名字标签，页面照常可用**"；
3. 整条链路**零前端错误**（含刷新）。
"""

from __future__ import annotations

import pytest

from e2e_support import new_page, snap
from game_support import VEG_STALL, hover, open_game, plot_center, watch_errors

STALL = VEG_STALL


@pytest.mark.parametrize("view,label", [("customer", "顾客"), ("admin", "管理员")])
def test_non_merchant_views_refuse_writes(browser, live_server, view, label):
    """顾客 / 管理员视角点智能秤 ⇒ 明确拒绝，且面板里**一个写入口都没有**。"""
    page = new_page(browser)
    try:
        open_game(page, live_server)
        page.click(f"#tab-{view}")
        page.wait_for_timeout(300)
        page.mouse.click(*plot_center(page, STALL, "scale"))
        page.wait_for_timeout(600)

        assert page.locator("#opsBody .op-input").count() == 0, f"{label}视角不该有设价输入框"
        assert page.locator("#opsBody button", has_text="下架").count() == 0, (
            f"{label}视角不该有下架按钮"
        )
        log = page.locator("#logList").inner_text()
        assert f"当前是{label}视角" in log, (
            f"{label}视角点秤应留下拒绝写操作的记录，实际日志：{log[:200]}"
        )
    finally:
        page.close()


def test_missing_art_degrades_to_blocks_and_stays_usable(browser, live_server, shots):
    """页面自称的降级承诺必须真的成立：拦掉精灵 ⇒ 色块 + 名字标签、功能照常、**零报错**。"""
    page = new_page(browser)
    problems = watch_errors(page)
    try:
        page.route("**/static/game/sprites/**", lambda route: route.abort())
        open_game(page, live_server)

        badge = page.locator("#artBadge")
        assert "降级" in badge.inner_text(), f"美术徽章应报降级，实际 {badge.inner_text()!r}"
        assert "warn" in (badge.get_attribute("class") or ""), "降级时徽章应是 warn 态"

        # 地图仍在画、数据仍可用、悬停仍能弹出清单
        assert page.evaluate("() => document.getElementById('gameCanvas').width") == 1024
        assert "数据就绪" in page.locator("#dataBadge").inner_text()
        hover(page, *plot_center(page, STALL, "pad"))
        title = page.locator("#tooltipTitle").inner_text()
        assert "在售" in title, f"降级后悬停仍应可用，实际标题 {title!r}"

        snap(page, shots, "game-art-missing-fallback")
        assert problems == [], f"降级路径出现前端错误：{problems}"
    finally:
        page.close()


def test_no_console_errors_during_a_full_walkthrough(browser, live_server):
    """整条走查链路（加载 → 悬停 → 点秤 → 切三视角 → 刷新）**零前端错误**。"""
    page = new_page(browser)
    problems = watch_errors(page)
    try:
        open_game(page, live_server)
        hover(page, *plot_center(page, STALL, "pad"))
        page.mouse.click(*plot_center(page, STALL, "scale"))
        page.wait_for_selector("#opsBody .op-input", timeout=10000)
        for view in ("customer", "admin", "merchant"):
            page.click(f"#tab-{view}")
            page.wait_for_timeout(250)
        page.evaluate("window.scrollTo(0, 0)")
        page.click("#refreshBtn")
        page.wait_for_function(
            "() => document.getElementById('dataBadge').className.includes('on')", timeout=20000
        )
        assert problems == [], f"走查期间出现前端错误：{problems}"
    finally:
        page.close()
