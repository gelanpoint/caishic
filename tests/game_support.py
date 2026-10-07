"""演示游戏走查的共用助手（真浏览器）。

单独成模块的原因：界面层用例分布在 `tests/e2e/test_demo_game_browser.py`（Chromium）与
`tests/e2e/test_demo_game_cross_engine.py`（Gecko）里，若把助手复制两份，
"取点方式"一改就会出现两处不一致 —— 而取点方式恰恰是这类用例最容易写错的地方
（写错会算出 `NaN`，而 Playwright 收到 `NaN` 会**直接崩掉 driver**，不是普通断言失败）。

命名跟随仓库既有约定（`tests/e2e_support.py` / `tests/contract/contract_support.py`）。
"""

from __future__ import annotations

from typing import Any

#: 走查用的两个摊位：一个蔬菜摊、一个肉类摊（后者用来防"只对第一个摊位有效"）。
VEG_STALL = "A-01"
MEAT_STALL = "A-06"


def open_game(page: Any, server: Any) -> None:
    """打开游戏页并等到**地图与数据都就绪**（否则后面点什么都点空）。"""
    page.goto(f"{server.base}/game/", wait_until="domcontentloaded")
    page.wait_for_function("() => window.GameApp && GameApp.state.world", timeout=30000)
    page.wait_for_function(
        "() => document.getElementById('dataBadge').className.includes('on')", timeout=30000
    )
    page.evaluate("window.scrollTo(0, 0)")


def plot_center(page: Any, stall_no: str, what: str = "pad") -> tuple[float, float]:
    """把摊位的 `pad`（铺位）/ `scale`（智能秤）中心换算成**视口坐标**。

    不写死像素：世界坐标取自页面自己构建的 `state.world`，画布位置与当前整数倍也从 DOM 读 ——
    断点或缩放一变，用例跟着走，不会因为布局调整而假红。
    """
    box = page.evaluate(
        """([stallNo, what]) => {
            const w = GameApp.state.world;
            const plot = w.plots.find(p => p.stallNo === stallNo);
            const part = plot[what];
            const scale = GameApp.state.scale;
            const rect = document.getElementById('gameCanvas').getBoundingClientRect();
            /* `pad` 有 w/h；`scale`/`boss` 只有一个格子（无 w/h）—— 少了这个兜底会算出 NaN，
               而 Playwright 收到 NaN 会**直接崩掉 driver**（不是普通断言失败）。 */
            const pw = part.w === undefined ? 1 : part.w;
            const ph = part.h === undefined ? 1 : part.h;
            return { x: rect.left + (part.x + pw / 2) * 16 * scale,
                     y: rect.top + (part.y + ph / 2) * 16 * scale };
        }""",
        [stall_no, what],
    )
    return box["x"], box["y"]


def hover(page: Any, x: float, y: float) -> None:
    page.mouse.move(x, y)
    page.wait_for_timeout(400)


def watch_errors(page: Any) -> list[str]:
    """收集**真异常**（`pageerror` + `console.error`）；返回列表本身，随时可读。

    被 `page.route(...).abort()` 拦掉的请求会让浏览器自己往控制台写一条
    `Failed to load resource` —— 那是**我们主动造成的网络失败**，不是前端缺陷。
    故按前缀剔除，避免把"故意的故障注入"误判成"页面报错"。
    """
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on(
        "console",
        lambda m: problems.append(f"console.{m.type}: {m.text}")
        if m.type == "error" and "Failed to load resource" not in m.text
        else None,
    )
    return problems
