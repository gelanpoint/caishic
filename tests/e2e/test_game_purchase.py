"""顾客购买行为真实落账（`REQ-052` / `AC-042`，`T-GAME-08`）。

**为什么要有这组**：演示游戏原先**完全没有购买行为** —— 顾客只是走来走去，
于是管理端看板恒为 `txn_count = 0` / `gross_amount_cents = 0`，演示时"后台看不到数据"。

**纪律（这几条才是本组的重点）**：
1. 成交必须走**既有**端点（契约 §3.6 创建计价 + §3.10 确认收款）—— **不得新增端点**
   （`NFR-016`），**不得绕过 `app/domain/**` 直接改库**（`REQ-044`）；
2. 看板数字必须**随之增加**（API 与库里两处都能对上）；
3. 失败必须**明确记录**、计数，**绝不静默丢弃**（`RL-9`）。
"""

from __future__ import annotations

from e2e_support import new_page, snap
from game_support import open_game

PURCHASES_JS = "GameApp.state.purchases"

#: 打开页面后等到"顾客至少买成一笔"，最多等 `timeout` 毫秒。
WAIT_JS = """(ms) => new Promise((resolve) => {
  const t0 = Date.now();
  const tick = () => {
    if (GameApp.state.purchases.ok > 0) { resolve(GameApp.state.purchases); return; }
    if (Date.now() - t0 > ms) { resolve(GameApp.state.purchases); return; }
    setTimeout(tick, 200);
  };
  tick();
})"""


def _dashboard(server) -> dict:
    status, body = server.api("GET", "/api/admin/dashboard?business_date=2026-10-07")
    assert status == 200, f"看板接口返回 {status}：{body}"
    return body


def test_customers_buy_through_existing_endpoints_and_the_dashboard_moves(
    browser, live_server, shots
):
    """`AC-042`：顾客购买 ⇒ 看板笔数与金额增加，且库里能查到对应成交。"""
    before = _dashboard(live_server)["market"]

    page = new_page(browser)
    try:
        open_game(page, live_server)
        stats = page.evaluate(WAIT_JS, 40000)
        assert stats["ok"] > 0, (
            f"40 秒内没有顾客买成任何一笔（stats={stats}）—— "
            "演示游戏没有产生购买行为，管理端看板就永远是 0"
        )
        assert stats["failed"] == 0, (
            f"有 {stats['failed']} 笔买单失败，最后一条：{stats['lastError']} "
            "—— 失败必须明确记录（RL-9），这里直接判红"
        )
        snap(page, shots, "game-purchases")

        after = _dashboard(live_server)["market"]
        assert after["txn_count"] > before["txn_count"], (
            f"顾客买了 {stats['ok']} 笔，但看板笔数没变（{before['txn_count']} → "
            f"{after['txn_count']}）—— 购买没有真正落账"
        )
        assert after["gross_amount_cents"] > before["gross_amount_cents"], (
            f"看板交易总额没变（{before['gross_amount_cents']} → "
            f"{after['gross_amount_cents']}）"
        )

        with live_server.connect_db() as conn:
            # `transaction` 是 SQLite 保留字，必须加双引号 —— 不加会直接语法错误
            paid = conn.execute(
                'SELECT COUNT(*) FROM "transaction" WHERE status = \'paid\''
            ).fetchone()[0]
            payments = conn.execute("SELECT COUNT(*) FROM payment").fetchone()[0]
        assert paid >= stats["ok"], (
            f"库里 `paid` 成交 {paid} 笔，少于前端报成功的 {stats['ok']} 笔"
        )
        assert payments >= stats["ok"], (
            f"库里支付流水 {payments} 条，少于前端报成功的 {stats['ok']} 笔"
            " —— 只创建未收款不算成交"
        )
    finally:
        page.close()


def test_buying_uses_only_pre_existing_endpoints(live_server):
    """`NFR-016`：演示游戏的买单**不得新增端点** —— 用到的路径必须都在契约里已登记。

    判据是**机械的**：把 `api.js` 里出现的所有接口路径抠出来，逐个比对契约文档。
    多出任何一条没登记的路由 ⇒ 判红。
    """
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    js = (repo / "app" / "static" / "game" / "js" / "api.js").read_text(encoding="utf-8")
    used = set(re.findall(r'"(/api/[^"?]+)', js))
    # 路径里的变量段（如 /transactions/<no>/payment）在契约里是 {transaction_no}
    contract = (repo / "specs" / "market-trade-flow" / "contracts" / "rest-api.md").read_text(
        encoding="utf-8"
    )
    for path in sorted(used):
        head = re.sub(r"/(transaction|stall|product)", "/{param}", path)
        assert path in contract or head in contract or path.rstrip("/") in contract, (
            f"演示游戏调用了契约里没有的路径 {path} —— REQ-044/NFR-016 禁止新增账口"
        )
