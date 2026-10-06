"""§3.6 收款确认 / §3.7 退货申请的契约测试（`T-SCALE-03`）。

覆盖：`REQ-009`、`REQ-010`、`REQ-013`、`REQ-042`、`REQ-043`；`AC-002`、`AC-033`；
错误码 `MT-1001` / `MT-1003` / `MT-2001` / `MT-2002`。

三条要点（都要求**核对库里的状态**，不只看响应字段）：
1. `qr_payload` 必须指向**中台的顾客页**（秤端不承载顾客页、也不得指向自己）—— `AC-033`；
2. 现金写**同一张支付流水表**（不另立现金表）—— `REQ-010`；
3. 退货冲正与**佣金扣减全部由中台计算**，重复申请**只冲减一次** —— `REQ-042`、`NFR-015`。
"""

from __future__ import annotations

import uuid

from scale_provision import OTHER_STALL_NO, active_device_token  # 先导入：它会把 tests/contract 放进 sys.path
from contract_support import bind_stall_session, create_priced_transaction
from scale_support import (
    REFUND_KEYS,
    SETTLE_CASH_KEYS,
    SETTLE_QR_KEYS,
    assert_exact_keys,
    assert_narrow_body,
    assert_scale_error_response,
    audit_rows,
    json_of,
    payment_rows,
    refund,
    refund_rows,
    report_transaction,
    settle,
    sqlite_tables,
    today_iso,
    transaction_row,
)


def _key() -> str:
    return f"SC-SETTLE-{uuid.uuid4().hex[:12]}"


def _token(client, db_conn) -> str:
    """数据面要求设备**已激活**（未激活 → `MT-2001`），故前置走一次 §3.1 激活。"""
    return active_device_token(client, db_conn)


def _priced(client, db_conn, *, weight_grams: int = 780) -> tuple[str, int]:
    """经 §3.5 上报造一笔 `priced` 交易，返回 `(交易号, 库里入账金额)`。"""
    response, _ = report_transaction(
        client, _token(client, db_conn), db_conn, idempotency_key=_key(), weight_grams=weight_grams
    )
    assert response.status_code == 201, (
        f"前置失败：§3.5 未新建交易（{response.status_code}）：{response.get_data(as_text=True)[:300]}"
    )
    transaction_no = json_of(response)["transaction_no"]
    return transaction_no, int(transaction_row(db_conn, transaction_no)["total_amount_cents"])


def _insert_commission_rule(db_conn, *, rate_bp: int = 200) -> int:
    """直连库插入一条**全品类、长期生效**的佣金口径（契约测试的"布置前置条件"用法）。

    `T-020` 的日聚合/结算/看板都按 `commission.find_effective_rule(conn, business_date)`
    （`category_tier=None`）取口径，故这里插 NULL 档位以保证有口径可依。
    """
    cursor = db_conn.execute(
        "INSERT INTO commission_rule (pay_object, rate_bp, category_tier, effective_from, effective_to, created_at) "
        "VALUES ('merchant', ?, NULL, '2000-01-01', NULL, '2000-01-01 00:00:00')",
        (rate_bp,),
    )
    db_conn.commit()
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# §3.6 收款确认
# ---------------------------------------------------------------------------


def test_settle_qr_payload_points_to_the_midplatform_customer_page(client, db_conn):
    """`AC-033` / `REQ-043`：`qr_payload` 指向**中台**顾客页，不指向秤端（秤端不承载顾客页）。"""
    token = _token(client, db_conn)
    transaction_no, _ = _priced(client, db_conn)
    response = settle(client, token, transaction_no, "qr", idempotency_key=_key())
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    payload = json_of(response)
    assert_narrow_body(payload, SETTLE_QR_KEYS, "§3.6 收款码响应")
    assert payload["transaction_no"] == transaction_no
    assert payload["status"] == "priced", "取回收款码不改变交易状态，等顾客扫码回调"
    qr_payload = payload["qr_payload"]
    assert isinstance(qr_payload, str) and qr_payload.startswith("http"), qr_payload
    assert "/customer/" in qr_payload, f"收款码必须指向中台顾客页，实际 {qr_payload!r}"
    assert transaction_no in qr_payload, f"顾客页地址必须能定位到本笔，实际 {qr_payload!r}"
    assert "/scale/" not in qr_payload, f"收款码不得指向秤端自己，实际 {qr_payload!r}"
    assert not qr_payload.startswith("mtpay://"), f"不得复用主契约的 Mock 支付串，实际 {qr_payload!r}"


def test_settle_qr_is_idempotent_and_does_not_create_a_second_payment(client, db_conn):
    """§1.1：收款确认按幂等键去重 —— 同键重复取码**不重复记账**（`REQ-026`）。"""
    token = _token(client, db_conn)
    transaction_no, _ = _priced(client, db_conn)
    key = _key()
    first = settle(client, token, transaction_no, "qr", idempotency_key=key)
    assert first.status_code == 200, first.get_data(as_text=True)[:300]
    second = settle(client, token, transaction_no, "qr", idempotency_key=key)
    assert second.status_code == 200, second.get_data(as_text=True)[:300]
    rows = payment_rows(db_conn, transaction_no)
    assert len(rows) == 1, f"同键重复取码不得新建流水，实际 {len(rows)} 条"
    assert rows[0]["method"] == "qr"


def test_settle_cash_writes_to_the_same_payment_table(client, db_conn):
    """`REQ-010`：现金写**同一张支付流水表**（`method='cash'`），不另立现金表。"""
    token = _token(client, db_conn)
    transaction_no, amount = _priced(client, db_conn)
    response = settle(client, token, transaction_no, "cash", idempotency_key=_key())
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    payload = json_of(response)
    assert_narrow_body(payload, SETTLE_CASH_KEYS, "§3.6 现金收款响应")
    assert payload["status"] == "paid"
    assert payload["paid_at"], "现金收款必须给出收款时刻"
    rows = payment_rows(db_conn, transaction_no)
    assert len(rows) == 1, f"应恰好一条支付流水，实际 {len(rows)} 条"
    assert rows[0]["method"] == "cash" and rows[0]["status"] == "success"
    assert int(rows[0]["amount_cents"]) == amount
    assert transaction_row(db_conn, transaction_no)["status"] == "paid"
    cash_tables = sorted(name for name in sqlite_tables(db_conn) if "cash" in name.lower())
    assert not cash_tables, f"不得为现金另立表（REQ-010），发现：{cash_tables}"


def test_settle_without_token_returns_mt2001(client, db_conn):
    """§4 `MT-2001`：收款确认要求设备令牌。"""
    transaction_no, _ = _priced(client, db_conn)
    response = settle(client, None, transaction_no, "cash", idempotency_key=_key())
    assert response.status_code == 401, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2001")


def test_settle_a_transaction_of_another_stall_returns_mt2002(client, db_conn):
    """§4 `MT-2002` / `REQ-032`：交易号不属于本摊位 ⇒ 403，且**不得产生支付流水**。

    真造"别的摊位的交易"：走主契约的摊主会话（§3.2/§3.6）在 `A-02` 建一笔已计价交易。
    """
    other_session = bind_stall_session(client, OTHER_STALL_NO)
    other_txn = create_priced_transaction(client, other_session, idempotency_key=_key())["transaction_no"]
    response = settle(client, _token(client, db_conn), other_txn, "cash", idempotency_key=_key())
    assert response.status_code == 403, (
        f"§4 MT-2002 应为 403，实际 {response.status_code}：{response.get_data(as_text=True)[:300]}"
    )
    assert_scale_error_response(response, "MT-2002")
    assert payment_rows(db_conn, other_txn) == [], "越权收款不得留下流水"


def test_settle_a_refunded_transaction_returns_mt1001(client, db_conn):
    """§4 `MT-1001`：状态机不允许（`refunded` 是终态）⇒ 409。"""
    token = _token(client, db_conn)
    transaction_no, amount = _priced(client, db_conn)
    assert settle(client, token, transaction_no, "cash", idempotency_key=_key()).status_code == 200
    assert refund(client, token, transaction_no, amount, idempotency_key=_key()).status_code == 201
    response = settle(client, token, transaction_no, "cash", idempotency_key=_key())
    assert response.status_code == 409, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-1001")


# ---------------------------------------------------------------------------
# §3.7 退货申请
# ---------------------------------------------------------------------------


def test_refund_reverses_once_and_commission_is_computed_by_the_midplatform(client, db_conn):
    """`AC-002` / `REQ-042` / `NFR-015`：冲正与**佣金扣减**全由中台算，同键重复只冲减一次。

    金额取 `weight_grams=50000`（50 公斤上限）⇒ 入账金额足够大，**任何 ≥1 万分比的费率**
    都会算出非零佣金 —— 于是"佣金由中台计算"这条断言不是恒真的 0 == 0。
    """
    from app.domain.commission import commission_of, find_effective_rule

    token = _token(client, db_conn)
    transaction_no, amount = _priced(client, db_conn, weight_grams=50_000)
    assert settle(client, token, transaction_no, "cash", idempotency_key=_key()).status_code == 200
    rule_id = _insert_commission_rule(db_conn)
    try:
        rule = find_effective_rule(db_conn, today_iso())
        assert rule is not None, "布置前置条件失败：插入口径后仍无生效佣金口径"
        expected_delta = -commission_of(amount, rule)
        assert expected_delta < 0, f"50 公斤 × 单价应算出非零佣金，实际 {expected_delta}（amount={amount}）"
        before_audit = len(audit_rows(db_conn, "refund_applied"))

        key = _key()
        first = refund(client, token, transaction_no, amount, idempotency_key=key)
        assert first.status_code == 201, (
            f"§3.7 退货申请应回 201，实际 {first.status_code}：{first.get_data(as_text=True)[:300]}"
        )
        payload = json_of(first)
        # §3.7 响应**契约明文要求** `commission_delta_cents`，故只能用字段白名单；
        # "禁成本/佣金字段"的窄体扫描是 §3.3/§3.4 的判据，用在这里会与契约直接打架
        # （本缺陷由 mid 评审指出）。
        assert_exact_keys(payload, REFUND_KEYS, "§3.7 退货响应")
        assert payload["status"] == "refunded"
        assert int(payload["refund_amount_cents"]) == amount
        assert int(payload["commission_delta_cents"]) == expected_delta, (
            "佣金扣减必须由中台按生效口径计算（REQ-042 / NFR-015）"
        )
        assert transaction_row(db_conn, transaction_no)["status"] == "refunded"

        second = refund(client, token, transaction_no, amount, idempotency_key=key)
        assert second.status_code == 201, second.get_data(as_text=True)[:300]
        assert int(json_of(second)["refund_amount_cents"]) == amount, "重复申请返回首次结果"
        assert len(refund_rows(db_conn, transaction_no)) == 1, "同键重复申请**只冲减一次**"
        assert len(audit_rows(db_conn, "refund_applied")) == before_audit + 1, "冲正只留一次痕"
    finally:
        db_conn.execute("DELETE FROM commission_rule WHERE id = ?", (rule_id,))
        db_conn.commit()


def test_refund_exceeding_the_original_amount_returns_mt1003(client, db_conn):
    """§4 `MT-1003`：退货金额超过原单 ⇒ 422，且**不得留下冲正行**（`REQ-028`）。"""
    token = _token(client, db_conn)
    transaction_no, amount = _priced(client, db_conn)
    assert settle(client, token, transaction_no, "cash", idempotency_key=_key()).status_code == 200
    response = refund(client, token, transaction_no, amount + 1, idempotency_key=_key())
    assert response.status_code == 422, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-1003")
    assert refund_rows(db_conn, transaction_no) == [], "越界退货不得留下冲正行"


def test_refund_without_token_returns_mt2001(client, db_conn):
    """§4 `MT-2001`：退货申请要求设备令牌。"""
    token = _token(client, db_conn)
    transaction_no, amount = _priced(client, db_conn)
    assert settle(client, token, transaction_no, "cash", idempotency_key=_key()).status_code == 200
    response = refund(client, None, transaction_no, amount, idempotency_key=_key())
    assert response.status_code == 401, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2001")
