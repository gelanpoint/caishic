"""`T-010` 契约测试：收款与支付回调（含 Mock 回调）。

关联：`REQ-009`、`REQ-010`、`REQ-011`、`REQ-026`、`REQ-029`；`AC-004`、`AC-009`、`AC-010`。
覆盖契约：§3.10 确认收款（现金 / 收款码）、§3.17 模拟支付回调。
错误码用例：`MT-1001`（状态不允许收款）、`MT-1005`（会话无效）、`MT-1008`（参数校验失败）、
`MT-1009`（支付单号不存在）。

> **幂等命中不是错误（本文件的重点断言）**：契约 §4 末注与 `spec.md` §5 都写明 —— 支付回调**重复送达**
> 必须返回 **200 + `is_duplicate: true`** 且**不重复记账**。因此本文件断言的是 `200` 与
> `is_duplicate`，而**不是**某个 4xx 错误码；写成错误码就与需求矛盾（`REQ-029`、`AC-010`）。

> **`AC-004` 的等价性口径**：现金与收款码都写**同一张** `payment` 表（`data-model.md` §2.10），以 `method` 区分。
> 本文件对两条路径断言**同样的落库结果**（`status = success`、`amount_cents` 相等、交易转 `paid`、各计一次），
> 两侧日聚合/佣金的完整等价由 `T-030` 端到端验证。
"""

from __future__ import annotations

import pytest

from conftest import (
    assert_endpoint_implemented,
    assert_error_response,
    assert_exact_keys,
    bind_stall_session,
    create_priced_transaction,
    json_of,
    session_headers,
)

STALL = "A-04"  # 本组专用摊位，降低跨组状态干扰
CASH_FIELDS = {"payment_no", "method", "status", "confirmed_at", "transaction_status"}
QR_FIELDS = {"payment_no", "method", "status", "qr_payload", "receiver_token_masked"}
CALLBACK_FIELDS = {"payment_no", "result", "is_duplicate", "transaction_status"}


def _stall_id(db_conn, stall_no: str = STALL) -> int:
    row = db_conn.execute("SELECT id FROM stall WHERE stall_no = ?", (stall_no,)).fetchone()
    assert row is not None, f"种子数据应有摊位 {stall_no}"
    return row["id"]


def _count(db_conn, sql: str, params: tuple = ()) -> int:
    return db_conn.execute(sql, params).fetchone()["n"]


def _pay_cash(client, token: str, transaction_no: str, key: str, operator: str = "t010-cashier"):
    """按契约 §3.10 走现金收款。"""
    return client.post(
        f"/api/merchant/transactions/{transaction_no}/payment",
        json={"method": "cash", "operator": operator},
        headers=session_headers(token, key),
    )


def _pay_qr(client, token: str, transaction_no: str, key: str):
    """按契约 §3.10 生成收款码（202 + `status = pending`）。"""
    return client.post(
        f"/api/merchant/transactions/{transaction_no}/payment",
        json={"method": "qr"},
        headers=session_headers(token, key),
    )


def _callback(client, callback_no: str, payment_no: str, result: str):
    """按契约 §3.17 送达一次支付回调。"""
    return client.post(
        "/api/mock/payment/callback",
        json={"callback_no": callback_no, "payment_no": payment_no, "result": result},
    )


# ---------------------------------------------------------------------------
# §3.10 现金收款（`REQ-010`、`AC-004`）
# ---------------------------------------------------------------------------


def test_cash_payment_writes_single_success_flow(client, db_conn):
    """`REQ-010`：现金在**同一张支付流水表**落一条记录，含确认时间与操作人；交易转 `paid`。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t010-cash-txn-1")

    response = _pay_cash(client, token, created["transaction_no"], "t010-cash-pay-1")
    assert response.status_code == 200, f"契约 §3.10 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert_exact_keys(payload, CASH_FIELDS, "§3.10 现金收款响应")
    assert payload["method"] == "cash" and payload["status"] == "success"
    assert payload["transaction_status"] == "paid", f"现金收款后交易应为 paid，实际 {payload['transaction_status']!r}"
    assert isinstance(payload["confirmed_at"], str) and payload["confirmed_at"].strip()
    assert isinstance(payload["payment_no"], str) and payload["payment_no"]

    transaction_id = db_conn.execute(
        'SELECT id FROM "transaction" WHERE transaction_no = ?', (created["transaction_no"],)
    ).fetchone()["id"]
    rows = db_conn.execute(
        "SELECT payment_no, method, status, amount_cents, confirmed_at, operator "
        "FROM payment WHERE transaction_id = ?",
        (transaction_id,),
    ).fetchall()
    assert len(rows) == 1, f"现金收款应恰好写一条支付流水（REQ-010），实际 {len(rows)} 条"
    row = rows[0]
    assert row["method"] == "cash" and row["status"] == "success"
    assert row["payment_no"] == payload["payment_no"]
    assert row["amount_cents"] == created["total_amount_cents"], (
        f"现金流水金额必须等于应收金额（data-model.md §2.10）：{row['amount_cents']} vs {created['total_amount_cents']}"
    )
    assert row["confirmed_at"] and row["operator"] == "t010-cashier", "现金流水必须含确认时间与操作人（REQ-010）"

    status = db_conn.execute('SELECT status, received_amount_cents FROM "transaction" WHERE id = ?', (transaction_id,)).fetchone()
    assert status["status"] == "paid"
    assert status["received_amount_cents"] == created["total_amount_cents"], "支付成功后应写入实收金额（REQ-017 计佣基数）"


def test_cash_payment_requires_operator_mt_1008(client):
    """契约 §3.10：`method = cash` 时 `operator` 条件必填 → 缺失即 422 `MT-1008`。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t010-cash-txn-2")
    response = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/payment",
        json={"method": "cash"},
        headers=session_headers(token, "t010-cash-pay-2"),
    )
    assert response.status_code == 422, f"契约 §3.10/§4 期望 422，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1008")


def test_payment_invalid_method_and_missing_session(client):
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t010-cash-txn-3")

    bad_method = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/payment",
        json={"method": "wechat", "operator": "x"},
        headers=session_headers(token, "t010-cash-pay-3"),
    )
    assert bad_method.status_code == 422, f"契约 §3.10 枚举 cash/qr，实际 {bad_method.status_code}"
    assert_error_response(bad_method, expected_code="MT-1008")

    no_session = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/payment",
        json={"method": "cash", "operator": "x"},
        headers={"Idempotency-Key": "t010-cash-pay-4"},
    )
    assert no_session.status_code == 401, f"契约 §1.6 期望 401，实际 {no_session.status_code}"
    assert_error_response(no_session, expected_code="MT-1005")


def test_payment_on_unknown_transaction_mt_1009(client):
    assert_endpoint_implemented(client.application, "POST", "/api/merchant/transactions/{transaction_no}/payment")
    token = bind_stall_session(client, STALL)
    response = _pay_cash(client, token, "ZZ-NOT-A-REAL-TXN", "t010-cash-pay-5")
    assert response.status_code == 404, f"契约 §4 期望 404(MT-1009)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1009")


# ---------------------------------------------------------------------------
# §3.10 非法流转 `MT-1001`（`REQ-012`/`REQ-026`）
# ---------------------------------------------------------------------------


def test_second_payment_on_paid_transaction_mt_1001(client, db_conn):
    """`MT-1001`：已 `paid` 的交易不允许再次收款（状态机 §4.1 无 `paid → paid` 流转）。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t010-illegal-txn-1")
    first = _pay_cash(client, token, created["transaction_no"], "t010-illegal-pay-1")
    assert first.status_code == 200, f"前置失败：契约 §3.10 期望 200，实际 {first.status_code}"

    transaction_id = db_conn.execute(
        'SELECT id FROM "transaction" WHERE transaction_no = ?', (created["transaction_no"],)
    ).fetchone()["id"]
    before = _count(db_conn, "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = ?", (transaction_id,))

    second = _pay_cash(client, token, created["transaction_no"], "t010-illegal-pay-2")
    assert second.status_code == 409, f"契约 §4 期望 409(MT-1001)，实际 {second.status_code}"
    assert_error_response(second, expected_code="MT-1001")

    after = _count(db_conn, "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = ?", (transaction_id,))
    assert after == before, f"非法流转不得新增支付流水（REQ-026）：{before} → {after}"


def test_payment_on_refunded_transaction_mt_1001(client):
    """`refunded` 是终态（data-model.md §4.1）：对其收款必须被拒。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t010-illegal-txn-2")
    paid = _pay_cash(client, token, created["transaction_no"], "t010-illegal-pay-3")
    assert paid.status_code == 200, f"前置失败：{paid.status_code}"
    refunded = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/refund",
        json={"amount_cents": created["total_amount_cents"]},
        headers=session_headers(token, "t010-illegal-refund-1"),
    )
    assert refunded.status_code == 200, f"前置：契约 §3.11 期望 200，实际 {refunded.status_code}"

    response = _pay_cash(client, token, created["transaction_no"], "t010-illegal-pay-4")
    assert response.status_code == 409, f"契约 §4 期望 409(MT-1001)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1001")


# ---------------------------------------------------------------------------
# §3.10 收款码 + §3.17 回调（`REQ-011`、`AC-004`）
# ---------------------------------------------------------------------------


def test_qr_payment_returns_202_pending_and_masks_receiver(client, db_conn):
    """契约 §3.10：`method = qr` → 202 `pending`，收款标识只回**脱敏值**（`REQ-024`）。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t010-qr-txn-1")

    response = _pay_qr(client, token, created["transaction_no"], "t010-qr-pay-1")
    assert response.status_code == 202, f"契约 §3.10 期望 202，实际 {response.status_code}"
    payload = json_of(response)
    assert_exact_keys(payload, QR_FIELDS, "§3.10 收款码响应")
    assert payload["method"] == "qr" and payload["status"] == "pending"
    assert isinstance(payload["qr_payload"], str) and payload["qr_payload"].strip()

    raw_token = db_conn.execute(
        "SELECT payment_receiver_token FROM stall WHERE stall_no = ?", (STALL,)
    ).fetchone()["payment_receiver_token"]
    masked = payload["receiver_token_masked"]
    assert isinstance(masked, str) and "*" in masked, f"收款标识必须是脱敏值（REQ-024），实际 {masked!r}"
    # 落库的也必须已是掩码形态（data-model.md §0「只存脱敏值」；`0001_init.sql` 用 CHECK 强制 `%****%`）
    assert "****" in raw_token, f"库里的收款标识未脱敏（REQ-024 / NFR-012）：{raw_token!r}"

    row = db_conn.execute(
        'SELECT p.status, p.callback_no FROM payment p JOIN "transaction" t ON t.id = p.transaction_id '
        "WHERE t.transaction_no = ?",
        (created["transaction_no"],),
    ).fetchone()
    assert row is not None and row["status"] == "pending", f"收款码支付流水应为 pending，实际 {row and row['status']!r}"


def test_callback_success_completes_payment_and_transaction(client, db_conn):
    """`REQ-011`：回调 `success` → 交易转 `paid`，支付流水转 `success` 并写入回调单号。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t010-cb-txn-1")
    qr = json_of(_pay_qr(client, token, created["transaction_no"], "t010-cb-pay-1"))

    response = _callback(client, "t010-cb-no-1", qr["payment_no"], "success")
    assert response.status_code == 200, f"契约 §3.17 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert_exact_keys(payload, CALLBACK_FIELDS, "§3.17 回调响应")
    assert payload["is_duplicate"] is False, f"首次回调不是幂等命中（REQ-029），实际 {payload['is_duplicate']!r}"
    assert payload["result"] == "success"
    assert payload["transaction_status"] == "paid", f"回调成功后交易应为 paid，实际 {payload['transaction_status']!r}"
    assert payload["payment_no"] == qr["payment_no"]

    row = db_conn.execute(
        "SELECT status, callback_no, amount_cents FROM payment WHERE payment_no = ?", (qr["payment_no"],)
    ).fetchone()
    assert row["status"] == "success" and row["callback_no"] == "t010-cb-no-1", f"回调未落库：{dict(row)!r}"
    assert row["amount_cents"] == created["total_amount_cents"]


@pytest.mark.parametrize("result", ["failed", "timeout"])
def test_callback_failure_keeps_transaction_retryable(client, db_conn, result):
    """契约 §3.17 / `REQ-011`：`failed` / `timeout` 都记录，交易转 `payment_failed`（可重试或改记现金）。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key=f"t010-cbfail-txn-{result}")
    qr = json_of(_pay_qr(client, token, created["transaction_no"], f"t010-cbfail-pay-{result}"))

    response = _callback(client, f"t010-cbfail-no-{result}", qr["payment_no"], result)
    assert response.status_code == 200, f"契约 §3.17 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert payload["result"] == result and payload["is_duplicate"] is False
    assert payload["transaction_status"] == "payment_failed", (
        f"回调 {result} 后交易应转 payment_failed（data-model.md §4.1），实际 {payload['transaction_status']!r}"
    )
    row = db_conn.execute("SELECT status FROM payment WHERE payment_no = ?", (qr["payment_no"],)).fetchone()
    assert row["status"] == result, f"支付流水状态应为 {result}，实际 {row['status']!r}"


def test_failed_callback_then_cash_records_one_transaction_and_one_success(client, db_conn):
    """`AC-009`/`REQ-026`：回调失败后改记现金 → **只有一条交易记录、只计一次**（失败流水保留作留痕）。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t010-retry-txn-1")
    qr = json_of(_pay_qr(client, token, created["transaction_no"], "t010-retry-pay-1"))
    failed = _callback(client, "t010-retry-no-1", qr["payment_no"], "timeout")
    assert failed.status_code == 200 and json_of(failed)["transaction_status"] == "payment_failed"

    retried = _pay_cash(client, token, created["transaction_no"], "t010-retry-pay-2")
    assert retried.status_code == 200, f"改记现金应被接受（REQ-026），实际 {retried.status_code}"
    assert json_of(retried)["transaction_status"] == "paid"

    transaction_id = db_conn.execute(
        'SELECT id FROM "transaction" WHERE transaction_no = ?', (created["transaction_no"],)
    ).fetchone()["id"]
    txn_rows = _count(
        db_conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ? AND client_idempotency_key = ?',
        (_stall_id(db_conn), "t010-retry-txn-1"),
    )
    assert txn_rows == 1, f"REQ-026：只允许一条交易记录，实际 {txn_rows} 条"
    success_rows = _count(
        db_conn, "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = ? AND status = 'success'", (transaction_id,)
    )
    assert success_rows == 1, f"REQ-026：只允许一条成功支付流水，实际 {success_rows} 条"
    failed_rows = _count(
        db_conn,
        "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = ? AND status IN ('failed','timeout')",
        (transaction_id,),
    )
    assert failed_rows >= 1, "失败的支付流水应保留作留痕（data-model.md §4.2）"


# ---------------------------------------------------------------------------
# §1.7 / §4 幂等命中：重复回调 → 200 + `is_duplicate`（`REQ-029`、`AC-010`）
# ---------------------------------------------------------------------------


def test_duplicate_callback_returns_200_is_duplicate_and_does_not_rebook(client, db_conn):
    """`AC-010`：同一次回调重复送达 → **200 + `is_duplicate: true`**，交易与佣金**不重复记账**。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t010-dup-txn-1")
    qr = json_of(_pay_qr(client, token, created["transaction_no"], "t010-dup-pay-1"))
    callback_no = "t010-dup-no-1"

    first = _callback(client, callback_no, qr["payment_no"], "success")
    assert first.status_code == 200 and json_of(first)["is_duplicate"] is False, "前置失败：首次回调"

    transaction_id = db_conn.execute(
        'SELECT id FROM "transaction" WHERE transaction_no = ?', (created["transaction_no"],)
    ).fetchone()["id"]
    payments_before = _count(db_conn, "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = ?", (transaction_id,))
    received_before = db_conn.execute(
        'SELECT received_amount_cents FROM "transaction" WHERE id = ?', (transaction_id,)
    ).fetchone()["received_amount_cents"]

    second = _callback(client, callback_no, qr["payment_no"], "success")
    assert second.status_code == 200, (
        f"重复回调**不是错误**（契约 §4 末注 / spec.md §5）：必须返回 200，实际 {second.status_code}"
    )
    payload = json_of(second)
    assert_exact_keys(payload, CALLBACK_FIELDS, "§3.17 重复回调响应")
    assert payload["is_duplicate"] is True, f"重复回调必须标记 is_duplicate=true（REQ-029），实际 {payload['is_duplicate']!r}"
    assert payload["payment_no"] == qr["payment_no"]
    assert payload["transaction_status"] == "paid", "重复回调不得改变交易终态"

    payments_after = _count(db_conn, "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = ?", (transaction_id,))
    assert payments_after == payments_before == 1, f"重复回调产生了重复记账（AC-010）：{payments_before} → {payments_after}"
    received_after = db_conn.execute(
        'SELECT received_amount_cents FROM "transaction" WHERE id = ?', (transaction_id,)
    ).fetchone()["received_amount_cents"]
    assert received_after == received_before == created["total_amount_cents"], "重复回调不得叠加实收金额"

    logs = db_conn.execute(
        "SELECT is_duplicate FROM payment_callback_log WHERE callback_no = ? ORDER BY id", (callback_no,)
    ).fetchall()
    assert len(logs) == 2, f"两次送达都应留痕（data-model.md §2.11），实际 {len(logs)} 条"
    assert [row["is_duplicate"] for row in logs] == [0, 1], f"第二次必须标 is_duplicate=1，实际 {[r['is_duplicate'] for r in logs]}"

    audit = _count(
        db_conn,
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'payment_callback_duplicate_hit' AND ref_id = ?",
        (transaction_id,),
    )
    assert audit >= 1, "幂等命中必须写审计留痕（NFR-009：只增不改的证据链）"


def test_duplicate_callback_is_not_an_error_envelope(client):
    """同一断言的**显式形态**：重复回调的响应体里不得出现任何 `error` 结构。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t010-dup-txn-2")
    qr = json_of(_pay_qr(client, token, created["transaction_no"], "t010-dup-pay-2"))
    _callback(client, "t010-dup-no-2", qr["payment_no"], "success")

    repeat = _callback(client, "t010-dup-no-2", qr["payment_no"], "success")
    assert repeat.status_code == 200
    assert "error" not in json_of(repeat), "幂等命中不得走统一错误响应（契约 §4 末注）"


def test_callback_unknown_payment_mt_1009_and_invalid_result_mt_1008(client):
    unknown = _callback(client, "t010-bad-no-1", "ZZ-NOT-A-REAL-PAYMENT", "success")
    assert unknown.status_code == 404, f"契约 §3.17/§4 期望 404(MT-1009)，实际 {unknown.status_code}"
    assert_error_response(unknown, expected_code="MT-1009")

    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t010-bad-txn-1")
    qr = json_of(_pay_qr(client, token, created["transaction_no"], "t010-bad-pay-1"))
    invalid = _callback(client, "t010-bad-no-2", qr["payment_no"], "refunded")
    assert invalid.status_code == 422, f"契约 §3.17 枚举 success/failed/timeout，实际 {invalid.status_code}"
    assert_error_response(invalid, expected_code="MT-1008")


# ---------------------------------------------------------------------------
# `AC-004`：现金与收款码两条路径的落库等价（同表、同金额、各一次）
# ---------------------------------------------------------------------------


def test_cash_and_qr_are_booked_equivalently(client, db_conn):
    """`AC-004`：两笔等额交易分别走现金与收款码 → 支付流水、金额与记账次数**完全等价**。"""
    token = bind_stall_session(client, STALL)
    cash_txn = create_priced_transaction(client, token, idempotency_key="t010-equiv-txn-cash", weights_grams=[1000])
    qr_txn = create_priced_transaction(client, token, idempotency_key="t010-equiv-txn-qr", weights_grams=[1000])
    assert cash_txn["total_amount_cents"] == qr_txn["total_amount_cents"], "前置：两笔应等额"

    assert _pay_cash(client, token, cash_txn["transaction_no"], "t010-equiv-pay-cash").status_code == 200
    qr = json_of(_pay_qr(client, token, qr_txn["transaction_no"], "t010-equiv-pay-qr"))
    assert _callback(client, "t010-equiv-no-1", qr["payment_no"], "success").status_code == 200

    rows = db_conn.execute(
        "SELECT t.transaction_no AS txn, p.method, p.status, p.amount_cents, p.confirmed_at "
        'FROM payment p JOIN "transaction" t ON t.id = p.transaction_id '
        "WHERE t.transaction_no IN (?, ?) ORDER BY p.method",
        (cash_txn["transaction_no"], qr_txn["transaction_no"]),
    ).fetchall()
    assert len(rows) == 2, f"两笔交易各应有一条成功支付流水（AC-004 同表），实际 {len(rows)} 条"
    methods = sorted(row["method"] for row in rows)
    assert methods == ["cash", "qr"], f"两条路径都写同一张 payment 表并以 method 区分，实际 {methods}"
    amounts = {row["amount_cents"] for row in rows}
    assert amounts == {cash_txn["total_amount_cents"]}, f"两条路径金额应一致（AC-004），实际 {amounts}"
    assert all(row["status"] == "success" for row in rows), f"两条路径都应落 success，实际 {[r['status'] for r in rows]}"
    assert all(row["confirmed_at"] for row in rows), "现金由 confirmed_at 表达到账，收款码回调后也应有确认时间"

    statuses = db_conn.execute(
        'SELECT status FROM "transaction" WHERE transaction_no IN (?, ?)',
        (cash_txn["transaction_no"], qr_txn["transaction_no"]),
    ).fetchall()
    assert {row["status"] for row in statuses} == {"paid"}, f"两条路径都应转 paid，实际 {[r['status'] for r in statuses]}"
