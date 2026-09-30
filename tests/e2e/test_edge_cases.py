"""`T-031` 端到端验证（二之一）：**输入与权限边界**（真实服务进程 + 真实库）。

覆盖：

| # | 场景 | 错误码 | 关联 |
| --- | --- | --- | --- |
| 1 | 重量越界（0 / >50 kg） | `MT-1002` 422 | `REQ-027`、`AC-017` |
| 2 | 退货金额超过原单（含**反向对照**：不超则必须成功） | `MT-1003` 422 | `REQ-028`、`AC-017` |
| 3 | 越权访问他人交易 / 会话无效或伪造 | `MT-1004` 403 / `MT-1005` 401 | `REQ-032`、`AC-021` |

**每个场景都断言"没有产生错误数据"**（`tasks.md` `T-031` 验收方式原文）：动作前后用**直连库**的
行数与金额核对 —— 行数不变、不新增半条记录、被拒的请求不得改动任何既有数据。
只断言"返回了错误码"是不够的：报错但库里多了一行，才是真正危险的那种失败。

**能力边界（如实说明）**：秤端 UI **产生不出**越界重量（预设 0.5~3 kg，"读秤"注入的是当前值），
这是 UI 的正确设计（不让摊主输错），不是覆盖缺口 —— 故场景 1/2 按**接口边界**验证
（真进程、真 HTTP、真库）；界面侧的**错误面**由 `test_edge_payment.py` 结尾用界面真能触发的
服务端错误单独验证。

> 按语义拆分说明（`Q-16` 的先例：单文件 ≤400 行，拆则按**语义**拆）：
> `test_edge_cases.py`（输入与权限）/ `test_edge_payment.py`（支付回调异常）/ `test_edge_offline.py`（离线异常）。
> 三个文件共用 `tests/conftest.py` 的夹具与助手（`create_priced` / `assert_api_error` /
> `reconciliation` / `stall_id` …），**不各写一套**。
"""

from __future__ import annotations

import pytest

from conftest import (
    active_products,
    assert_api_error,
    bind_stall,
    count,
    create_priced,
    create_transaction,
    evidence_key,
    pay_transaction,
    reconciliation,
    session_headers,
    stall_id,
)


# ---------------------------------------------------------------------------
# 场景 1：重量越界 → MT-1002，且**不创建任何交易**（AC-017 / REQ-027）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("weight_grams", "why"),
    [(0, "0 克"), (50001, "刚过 50 kg 上限"), (200000, "远超上限")],
)
def test_weight_out_of_range_mt_1002_creates_nothing(live_server, weight_grams, why):
    """`REQ-027`：重量 ≤0 或 >50 kg 必须**拒绝计价并提示**，且"不创建任何交易"。"""
    stall = "A-01"
    _id = stall_id(live_server, stall)
    token = bind_stall(live_server, stall)
    product = active_products(live_server, token)[0]
    key = evidence_key(f"weight-{weight_grams}")

    conn = live_server.connect_db()
    try:
        txns_before = count(conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?', (_id,))
        items_before = count(conn, "SELECT COUNT(*) AS n FROM transaction_item")
    finally:
        conn.close()

    status, payload = create_transaction(
        live_server, token, [{"product_id": product["id"], "weight_grams": weight_grams}], key
    )
    error = assert_api_error(status, payload, "MT-1002", 422, f"重量越界（{why}）")

    conn = live_server.connect_db()
    try:
        txns_after = count(conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?', (_id,))
        items_after = count(conn, "SELECT COUNT(*) AS n FROM transaction_item")
        by_key = count(
            conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE client_idempotency_key = ?', (key,)
        )
    finally:
        conn.close()

    assert txns_after == txns_before, f"越界计价**不得**创建交易：{txns_before} → {txns_after}"
    assert items_after == items_before, f"越界计价不得留下明细行：{items_before} → {items_after}"
    assert by_key == 0, "越界的那次请求不得占用幂等键（不得留下半条记录）"
    print(f"[边界] 重量 {weight_grams}g（{why}）→ {error['code']} 422，交易数不变（{txns_before}）")


# ---------------------------------------------------------------------------
# 场景 2：退货超原单 → MT-1003；**反向对照**：不超原单必须成功（AC-017 / REQ-028）
# ---------------------------------------------------------------------------


def test_refund_over_original_mt_1003_creates_no_refund(live_server):
    """`REQ-028`：退货金额不得大于原单；被拒时不得写 `refund`、不得改交易状态与实收。

    结尾的**反向对照**是必须的：只证明"超了会被拒"证明不了"没超就能退" ——
    一个永远返回 `MT-1003` 的实现也能让前半段变绿（`AGENTS.md`：检查必须能失败，也必须能通过）。
    """
    stall = "A-02"
    stall_db_id = stall_id(live_server, stall)
    token = bind_stall(live_server, stall)
    product = active_products(live_server, token)[0]
    txn = create_priced(live_server, token, product["id"], evidence_key("refund-price"))
    txn_no = txn["transaction_no"]
    status, paid = pay_transaction(
        live_server, token, txn_no, {"method": "cash", "operator": "e2e31"}, evidence_key("refund-pay")
    )
    assert status == 200, f"契约 §3.10 现金收款期望 200，实际 {status}：{paid}"

    recon_before = reconciliation(live_server, stall)

    over = txn["total_amount_cents"] + 1
    status, payload = live_server.api(
        "POST", f"/api/merchant/transactions/{txn_no}/refund",
        {"amount_cents": over}, session_headers(token, evidence_key("refund-over")),
    )
    assert_api_error(status, payload, "MT-1003", 422, "退货超原单")

    conn = live_server.connect_db()
    try:
        refunds = count(
            conn,
            'SELECT COUNT(*) AS n FROM refund WHERE transaction_id = '
            '(SELECT id FROM "transaction" WHERE transaction_no = ?)',
            (txn_no,),
        )
        row = conn.execute(
            'SELECT status, received_amount_cents, total_amount_cents FROM "transaction" '
            "WHERE stall_id = ? AND transaction_no = ?",
            (stall_db_id, txn_no),
        ).fetchone()
    finally:
        conn.close()

    assert refunds == 0, "被拒的退货不得留下 `refund` 行"
    assert row["status"] == "paid", f"被拒的退货不得改交易状态：{row['status']}"
    assert row["received_amount_cents"] == txn["total_amount_cents"], "被拒的退货不得改实收金额"
    assert reconciliation(live_server, stall) == recon_before, "被拒的退货不得改变对账三条腿"

    # 反向对照：不超原单时必须真的成功
    status, ok = live_server.api(
        "POST", f"/api/merchant/transactions/{txn_no}/refund",
        {"amount_cents": txn["total_amount_cents"]}, session_headers(token, evidence_key("refund-ok")),
    )
    assert status == 200, f"契约 §3.11 不超原单的退货期望 200，实际 {status}：{ok}"
    assert ok["transaction_status"] == "refunded", f"退货后交易应为 refunded：{ok}"
    print(f"[边界] 退货 {over} 分 > 原单 {txn['total_amount_cents']} 分 → MT-1003（零副作用）；"
          f"改回原单金额 → 200 {ok['transaction_status']}")


# ---------------------------------------------------------------------------
# 场景 3：越权访问 / 会话无效（AC-021 / REQ-032）
# ---------------------------------------------------------------------------


def test_cross_stall_access_mt_1004_and_invalid_session_mt_1005_ac_021(live_server):
    """`AC-021`：摊主请求非本摊位数据必须被拒绝，且**不得回带对方的业务数据**。

    `detail` 里回带**请求方自己给出的** `transaction_no` **不算泄漏**（那是入参的回显，
    且能让调用方知道是哪一次请求失败）—— 本条断言的是**对方的业务字段一个都不出现**
    （金额、明细、状态、营业日等）。
    """
    owner, intruder = "A-01", "A-06"
    owner_token = bind_stall(live_server, owner)
    intruder_token = bind_stall(live_server, intruder)
    product = active_products(live_server, owner_token)[0]
    txn = create_priced(live_server, owner_token, product["id"], evidence_key("scope-price"))
    txn_no = txn["transaction_no"]

    # ① 读别人家的交易详情 → 403 MT-1004
    status, payload = live_server.api(
        "GET", f"/api/merchant/transactions/{txn_no}", headers=session_headers(intruder_token)
    )
    assert_api_error(status, payload, "MT-1004", 403, "越权读他人交易详情")
    leaked = [field for field in
              ("total_amount_cents", "received_amount_cents", "items", "business_date", "created_at")
              if field in str(payload)]
    assert not leaked, f"越权响应回带了对方的业务字段：{leaked} → {payload}"

    # ② 改别人家的价 → 403 MT-1004（写路径同样守住）
    status, payload = live_server.api(
        "POST", f"/api/merchant/transactions/{txn_no}/price-change",
        {"item_id": txn["items"][0]["id"], "final_unit_price_cents": 1},
        session_headers(intruder_token, evidence_key("scope-pricechange")),
    )
    assert_api_error(status, payload, "MT-1004", 403, "越权改他人交易价格")

    # ③ 会话缺失 / 伪造 → 401 MT-1005
    status, payload = live_server.api("GET", "/api/merchant/transactions")
    assert_api_error(status, payload, "MT-1005", 401, "缺会话头读交易列表")
    status, payload = live_server.api(
        "GET", "/api/merchant/transactions", headers={"X-Stall-Session": "not-a-real-token"}
    )
    assert_api_error(status, payload, "MT-1005", 401, "伪造会话 Token")

    # ④ **没有产生错误数据**：两次被拒都不得改动原交易、不得写留痕
    conn = live_server.connect_db()
    try:
        row = conn.execute(
            'SELECT status, total_amount_cents, received_amount_cents FROM "transaction" WHERE transaction_no = ?',
            (txn_no,),
        ).fetchone()
        price_audits = count(
            conn,
            "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'price_change' AND ref_id = "
            '(SELECT id FROM "transaction" WHERE transaction_no = ?)',
            (txn_no,),
        )
    finally:
        conn.close()
    assert row["status"] == "priced" and row["total_amount_cents"] == txn["total_amount_cents"], (
        f"越权改价不得改动任何数据：{dict(row)}"
    )
    assert row["received_amount_cents"] in (None, 0), f"未收款的交易不得有实收金额：{dict(row)}"
    assert price_audits == 0, "越权请求不得写改价留痕"
    print("[边界] 越权读/写 → MT-1004 403（无业务字段泄漏）；缺/伪会话 → MT-1005 401；原交易零改动")
