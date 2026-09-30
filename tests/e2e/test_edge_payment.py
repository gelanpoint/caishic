"""`T-031` 端到端验证（二之二）：**支付回调异常与界面错误面**（真实服务进程 + 真实库 + 真实浏览器）。

覆盖：

| # | 场景 | 判据 | 关联 |
| --- | --- | --- | --- |
| 1 | 支付回调重复送达 | 200 + `is_duplicate`，交易/流水/对账全不变 | `REQ-029`、`AC-010` |
| 2 | 回调失败或超时后改记现金 | 只有一条交易、只成功一次、对账等式成立 | `REQ-026`、`AC-009` |
| 3 | 界面拿到**真**错误码 | 界面必须显示、不得假装成功 | `REQ-030` 的"确定性反馈" |

第 3 条为什么放在这里：它是**界面真能触发**的服务端错误（收款后按钮仍在，再点一次就是真实的
重复收款请求），**不需要伪造任何响应**。契约把"重复现金收款"钉在 `MT-1001`
（`Q-18` 裁定：状态机本身就是幂等保护，重复收款不新增流水），所以这条同时验证了两件事：
服务端按契约拒绝、界面把拒绝**摊开给摊主看**（绝不显示"收款成功"）。

**"不重复记账"的证据是四路一起给的**（只数流水条数会漏掉"状态被改坏"这类失败）：
流水条数、交易条数、交易状态与实收金额、**对账等式三条腿**（重复前后逐字段相等）。
"""

from __future__ import annotations

import time

import pytest

from e2e_support import (
    active_products,
    bind_stall,
    count,
    create_priced,
    evidence_key,
    new_page,
    pay_transaction,
    reconciliation,
    session_headers,
    snap,
)


def _success_payments(live_server, txn_no: str) -> int:
    conn = live_server.connect_db()
    try:
        return count(
            conn,
            "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = "
            '(SELECT id FROM "transaction" WHERE transaction_no = ?) AND status = \'success\'',
            (txn_no,),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 场景 1：支付回调重复送达（AC-010 / REQ-029）
# ---------------------------------------------------------------------------


def test_repeated_payment_callback_is_idempotent_ac_010(live_server):
    """`AC-010`：同一次回调重复送达 → 交易与佣金**不重复记账**。"""
    stall = "A-03"
    token = bind_stall(live_server, stall)
    product = active_products(live_server, token)[0]
    txn = create_priced(live_server, token, product["id"], evidence_key("cb-price"))
    txn_no = txn["transaction_no"]
    status, qr = pay_transaction(
        live_server, token, txn_no, {"method": "qr", "operator": "e2e31"}, evidence_key("cb-qr")
    )
    assert status == 202, f"契约 §3.10 收款码期望 202，实际 {status}：{qr}"

    callback_no = f"CB-{time.time_ns()}"
    callback = {"callback_no": callback_no, "payment_no": qr["payment_no"], "result": "success"}
    status, first = live_server.api("POST", "/api/mock/payment/callback", callback)
    assert status == 200 and first["is_duplicate"] is False, f"首次回调应 200 且非重复：{first}"

    recon_before = reconciliation(live_server, stall)
    conn = live_server.connect_db()
    try:
        row_before = conn.execute(
            'SELECT status, received_amount_cents FROM "transaction" WHERE transaction_no = ?', (txn_no,)
        ).fetchone()
    finally:
        conn.close()

    # **同一份请求体**再送一次
    status, again = live_server.api("POST", "/api/mock/payment/callback", callback)
    assert status == 200, f"契约 §1.3/§3.17：重复回调必须仍是 200，实际 {status}：{again}"
    assert again["is_duplicate"] is True, f"重复回调必须标记 is_duplicate：{again}"
    assert again["result"] == "success", f"契约 §1.3：重复请求返回**首次结果**，实际 {again['result']}"
    assert again["transaction_status"] == "paid"

    conn = live_server.connect_db()
    try:
        total_pay = count(
            conn,
            "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = "
            '(SELECT id FROM "transaction" WHERE transaction_no = ?)',
            (txn_no,),
        )
        success_pay = count(
            conn,
            "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = "
            '(SELECT id FROM "transaction" WHERE transaction_no = ?) AND status = \'success\'',
            (txn_no,),
        )
        txns = count(
            conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE transaction_no = ?', (txn_no,)
        )
        row_after = conn.execute(
            'SELECT status, received_amount_cents FROM "transaction" WHERE transaction_no = ?', (txn_no,)
        ).fetchone()
        logs = [row["is_duplicate"] for row in conn.execute(
            "SELECT is_duplicate FROM payment_callback_log WHERE callback_no = ? ORDER BY id", (callback_no,)
        ).fetchall()]
        dup_audit = count(
            conn,
            "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'payment_callback_duplicate_hit' "
            'AND ref_id = (SELECT id FROM "transaction" WHERE transaction_no = ?)',
            (txn_no,),
        )
    finally:
        conn.close()

    assert txns == 1, f"重复回调不得产生第二条交易：{txns}"
    assert total_pay == 1, f"重复回调不得新增支付流水：{total_pay}"
    assert success_pay == 1, f"成功流水只允许一条（只计一次佣金）：{success_pay}"
    assert (row_after["status"], row_after["received_amount_cents"]) == (
        row_before["status"], row_before["received_amount_cents"]
    ), "重复回调不得改动交易状态与实收金额"
    assert logs == [0, 1], f"回调日志应记录两次到达并标出命中，实际 {logs}"
    assert dup_audit == 1, "幂等命中必须留痕（`payment_callback_duplicate_hit`）"
    recon_after = reconciliation(live_server, stall)
    assert recon_after == recon_before, f"重复回调不得改变对账三条腿：{recon_before} → {recon_after}"
    print(f"[边界] 回调 {callback_no} 第二次送达 → 200 is_duplicate=True；"
          f"交易 {txns} / 流水 {total_pay} / 成功 {success_pay} / 对账差 {recon_after['diff_cents']} 分")


# ---------------------------------------------------------------------------
# 场景 2：回调失败或超时 → 改记现金，仍只有一条交易、只成功一次（AC-009 / REQ-026）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("result", ["failed", "timeout"])
def test_failed_callback_then_cash_keeps_single_transaction_ac_009(live_server, result):
    """`AC-009`：回调失败/超时后摊主改记现金 → 该笔**只有一条交易记录、只计一次佣金**。"""
    stall = "A-07" if result == "failed" else "A-08"
    token = bind_stall(live_server, stall)
    product = active_products(live_server, token)[0]
    txn = create_priced(live_server, token, product["id"], evidence_key(f"cb9-{result}-price"))
    txn_no = txn["transaction_no"]
    status, qr = pay_transaction(
        live_server, token, txn_no, {"method": "qr", "operator": "e2e31"}, evidence_key(f"cb9-{result}-qr")
    )
    assert status == 202, f"收款码期望 202，实际 {status}：{qr}"

    status, cb = live_server.api(
        "POST", "/api/mock/payment/callback",
        {"callback_no": f"CB9-{result}-{time.time_ns()}", "payment_no": qr["payment_no"], "result": result},
    )
    assert status == 200 and cb["transaction_status"] == "payment_failed", (
        f"回调 {result} 后交易应为 payment_failed：{cb}"
    )
    assert _success_payments(live_server, txn_no) == 0, f"{result} 不得被当成收款成功（REQ-026）"

    status, cash = pay_transaction(
        live_server, token, txn_no, {"method": "cash", "operator": "e2e31"}, evidence_key(f"cb9-{result}-cash")
    )
    assert status == 200, f"改记现金期望 200，实际 {status}：{cash}"

    conn = live_server.connect_db()
    try:
        txns = count(conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE transaction_no = ?', (txn_no,))
        total_pay = count(
            conn,
            "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = "
            '(SELECT id FROM "transaction" WHERE transaction_no = ?)',
            (txn_no,),
        )
        row = conn.execute(
            'SELECT status, received_amount_cents, total_amount_cents FROM "transaction" WHERE transaction_no = ?',
            (txn_no,),
        ).fetchone()
    finally:
        conn.close()

    assert txns == 1, f"回调 {result} + 改记现金 **只能有一条交易记录**：{txns}"
    assert _success_payments(live_server, txn_no) == 1, "成功流水只能有一条（只计一次佣金）"
    assert total_pay == 2, f"流水应含一条 {result} + 一条现金成功，实际 {total_pay}"
    assert row["status"] == "paid" and row["received_amount_cents"] == row["total_amount_cents"], (
        f"改记现金后应为 paid 且实收=应收：{dict(row)}"
    )

    recon = reconciliation(live_server, stall)
    assert recon["balanced"] and recon["diff_cents"] == 0, f"对账三条腿必须相等（失败流水不得计入）：{recon}"
    assert recon["payment_total_cents"] == row["total_amount_cents"], (
        f"支付流水腿只算成功的那一条：{recon}"
    )
    print(f"[边界] 回调 {result} → 改记现金：交易 1 条、成功流水 1 条、对账差 {recon['diff_cents']} 分")


# ---------------------------------------------------------------------------
# 场景 3：界面拿到**真**错误码时必须显示，不得假装成功（AC-006 的"确定性反馈"）
# ---------------------------------------------------------------------------


def test_ui_surfaces_real_mt_1001_instead_of_pretending_success(live_server, browser, shots):
    """界面错误面：界面**自己走完**一笔收款后再点一次收款 → 服务端按 `MT-1001` 拒绝 → 界面必须显示。

    全程真实：真 Chromium、真 HTTP、真库；**没有伪造任何响应**。断言两头都查 ——
    页面把错误摊开给摊主看（`#payErr` 含 `MT-1001`）、且**不得**出现"收款成功"，
    库里那条成功流水也仍然只有一条。
    """
    page = new_page(browser)
    page.goto(f"{live_server.base}/scale/")
    page.click("#defaultStall")
    page.wait_for_selector(".tile")

    # 界面自己的完整链路：选品 → 计价 → 现金收款
    page.locator(".tile").first.click()
    page.click("#checkout")
    page.wait_for_function("document.querySelector('#txnNo').textContent.trim() !== '—'")
    txn_no = page.inner_text("#txnNo").strip()
    page.click("#payCash")
    page.wait_for_selector("#afterSale:not(.hide)")
    assert "现金收款成功" in page.inner_text("body"), "前置：第一笔现金收款应成功"

    # 再点一次同一笔的收款（界面真能触发；契约按 MT-1001 拒绝重复收款）
    # 先等**上一次成功的提示条**自己淡出，再点：否则断言"页面里没有收款成功字样"会撞上
    # 上一条合法提示的残留 —— 那是**断言写得不精确**（把两次动作的提示混在一起），不是缺陷。
    page.wait_for_function("document.querySelector('#toast').classList.contains('hide')")
    page.click("#payCash")
    page.wait_for_function("document.querySelector('#payErr').textContent.trim() !== ''")
    shown = page.inner_text("#payErr").strip()
    snap(page, shots, "edge-1-mt1001-shown")
    assert "MT-1001" in shown, f"界面必须把服务端错误码显示出来（而不是静默）：{shown!r}"

    # 被拒的收款**不得再弹任何提示条**（`show()`/`toast()` 只有成功路径才调用）——
    # 这比"页面文本里没有'收款成功'"精确：它证明被拒那次**没有制造新的成功假象**。
    toast_hidden = page.evaluate("document.querySelector('#toast').classList.contains('hide')")
    assert toast_hidden, f"被拒绝的收款不得弹出新的提示（最坏的一种失败）：{page.inner_text('#toast')!r}"
    assert _success_payments(live_server, txn_no) == 1, "界面重复点收款不得多记一笔流水"
    print(f"[边界] 界面重复点现金收款 → 页面显示 {shown!r}；库中仍只有 1 条成功流水")
    page.close()
