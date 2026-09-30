"""`T-011` 契约测试：退货冲正（只冲减一次）。

关联：`REQ-013`、`REQ-028`；`AC-002`、`AC-017`。
覆盖契约：§3.11 退货冲正。
错误码用例：`MT-1001`（交易非 `paid`）、`MT-1003`（退货金额超过原单）、`MT-1005`（会话无效）、
`MT-1008`（参数校验失败）、`MT-1009`（交易不存在）。

> **幂等命中不是错误（本文件重点）**：契约 §3.11 写明重复提交返回 `200` + `replayed: true`
> **且金额不变**；`spec.md` §5 同义。故本文件断言 `200 + replayed`，**不**断言任何 4xx。
>
> **`AC-002` 的断言边界（如实标注）**：本组只断言契约面上能独立取证的部分 —— 退货后
> `refund_amount_cents` **等于**退掉的金额、`commission_amount_cents` **不增加**。
> 「佣金**同步减少**」的强断言需要先配置佣金口径（§3.24，属 `T-013`/`T-020`），
> 故此处只做不减断言，严格的"佣金减少"由 `T-020` 与 `T-030` 端到端负责 —— 不在这里假称已证。
"""

from __future__ import annotations

import pytest

from contract_support import (
    assert_endpoint_implemented,
    assert_error_response,
    bind_stall_session,
    create_priced_transaction,
    json_of,
    session_headers,
    today_iso,
)

STALL = "A-05"  # 本组专用摊位，降低跨组状态干扰
REFUND_FIELDS = {"refund_no", "transaction_status", "amount_cents", "replayed"}


def _stall_id(db_conn, stall_no: str = STALL) -> int:
    row = db_conn.execute("SELECT id FROM stall WHERE stall_no = ?", (stall_no,)).fetchone()
    assert row is not None, f"种子数据应有摊位 {stall_no}"
    return row["id"]


def _count(db_conn, sql: str, params: tuple = ()) -> int:
    return db_conn.execute(sql, params).fetchone()["n"]


def _refund(client, token: str, transaction_no: str, amount_cents: int, key: str):
    """按契约 §3.11 提交退货冲正。"""
    return client.post(
        f"/api/merchant/transactions/{transaction_no}/refund",
        json={"amount_cents": amount_cents},
        headers=session_headers(token, key),
    )


def _dashboard(client, token: str) -> dict:
    response = client.get(
        "/api/merchant/dashboard",
        query_string={"business_date": today_iso()},
        headers=session_headers(token),
    )
    assert response.status_code == 200, f"契约 §3.12 期望 200，实际 {response.status_code}"
    return json_of(response)


def _paid_transaction(client, token: str, *, tags: str) -> dict:
    """前置：创建一笔交易并现金收款（退货只在 `paid` 上发起）。"""
    created = create_priced_transaction(client, token, idempotency_key=f"t011-{tags}-txn")
    paid = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/payment",
        json={"method": "cash", "operator": "t011-cashier"},
        headers=session_headers(token, f"t011-{tags}-pay"),
    )
    assert paid.status_code == 200, f"前置：契约 §3.10 期望 200，实际 {paid.status_code}"
    return created


# ---------------------------------------------------------------------------
# §3.11 正常冲正（`REQ-013`、`AC-002`）
# ---------------------------------------------------------------------------


def test_refund_marks_transaction_refunded_and_reduces_aggregate(client, db_conn):
    """`AC-002`：退货后交易转 `refunded`，退货金额进入日聚合（金额口径同步减少）。"""
    token = bind_stall_session(client, STALL)
    created = _paid_transaction(client, token, tags="full")
    before = _dashboard(client, token)

    response = _refund(client, token, created["transaction_no"], created["total_amount_cents"], "t011-full-key")
    assert response.status_code == 200, f"契约 §3.11 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert set(payload) >= REFUND_FIELDS, f"契约 §3.11 响应缺字段：{sorted(REFUND_FIELDS - set(payload))}"
    assert payload["transaction_status"] == "refunded", f"退货后交易应为 refunded，实际 {payload['transaction_status']!r}"
    assert payload["amount_cents"] == created["total_amount_cents"], "退货金额应与请求一致"
    assert payload["replayed"] is False, f"首次退货不是重放（REQ-013），实际 {payload['replayed']!r}"
    assert isinstance(payload["refund_no"], str) and payload["refund_no"]

    stall_id = _stall_id(db_conn)
    transaction_id = db_conn.execute(
        'SELECT id FROM "transaction" WHERE transaction_no = ?', (created["transaction_no"],)
    ).fetchone()["id"]
    rows = db_conn.execute(
        "SELECT refund_no, amount_cents, stall_id, idempotency_key FROM refund WHERE transaction_id = ?",
        (transaction_id,),
    ).fetchall()
    assert len(rows) == 1, f"一次退货应恰好写一条 refund（REQ-013），实际 {len(rows)} 条"
    assert rows[0]["amount_cents"] == created["total_amount_cents"]
    assert rows[0]["stall_id"] == stall_id, "refund 必须记录操作摊位（REQ-013）"
    assert rows[0]["idempotency_key"] == "t011-full-key"
    assert rows[0]["refund_no"] == payload["refund_no"]

    after = _dashboard(client, token)
    assert after["refund_amount_cents"] - before["refund_amount_cents"] == created["total_amount_cents"], (
        f"退货金额未进入日聚合（AC-002）：{before['refund_amount_cents']} → {after['refund_amount_cents']}"
    )
    assert after["commission_amount_cents"] <= before["commission_amount_cents"], (
        f"退货后佣金不得增加（AC-002；本组未配置佣金口径，故只断言不减）："
        f"{before['commission_amount_cents']} → {after['commission_amount_cents']}"
    )


# ---------------------------------------------------------------------------
# §3.11 / §4 重复提交 → 200 + `replayed`（`REQ-013`、`AC-002`）
# ---------------------------------------------------------------------------


def test_repeat_refund_returns_200_replayed_and_does_not_double_deduct(client, db_conn):
    """`AC-002`/`REQ-013`：**同一笔退货重复提交 → 只冲减一次，金额与佣金不再变化**。"""
    token = bind_stall_session(client, STALL)
    created = _paid_transaction(client, token, tags="dup")
    key = "t011-dup-key"

    first = _refund(client, token, created["transaction_no"], created["total_amount_cents"], key)
    assert first.status_code == 200 and json_of(first)["replayed"] is False, "前置失败：首次退货"
    first_payload = json_of(first)
    after_first = _dashboard(client, token)

    transaction_id = db_conn.execute(
        'SELECT id FROM "transaction" WHERE transaction_no = ?', (created["transaction_no"],)
    ).fetchone()["id"]

    second = _refund(client, token, created["transaction_no"], created["total_amount_cents"], key)
    assert second.status_code == 200, (
        f"重复退货**不是错误**（契约 §3.11 / spec.md §5）：必须返回 200，实际 {second.status_code}"
    )
    payload = json_of(second)
    assert set(payload) >= REFUND_FIELDS, f"契约 §3.11 响应缺字段：{sorted(REFUND_FIELDS - set(payload))}"
    assert payload["replayed"] is True, f"重复提交必须标 replayed=true（REQ-013），实际 {payload['replayed']!r}"
    assert payload["amount_cents"] == first_payload["amount_cents"], "重复提交**金额不得变化**（REQ-013/AC-002）"
    assert payload["refund_no"] == first_payload["refund_no"], "重复提交应返回**首次结果**（契约 §1.3）"
    assert "error" not in payload, "幂等命中不得走统一错误响应（契约 §4 末注）"

    rows = _count(db_conn, "SELECT COUNT(*) AS n FROM refund WHERE transaction_id = ?", (transaction_id,))
    assert rows == 1, f"重复退货产生了多条冲正记录（REQ-013 只冲减一次）：{rows} 条"

    after_second = _dashboard(client, token)
    assert after_second["refund_amount_cents"] == after_first["refund_amount_cents"], "重复退货后金额不得再变化（AC-002）"
    assert after_second["commission_amount_cents"] == after_first["commission_amount_cents"], (
        "重复退货后佣金不得再变化（AC-002）"
    )

    audit = _count(
        db_conn,
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'refund_duplicate_hit' AND ref_table = 'refund'",
    )
    assert audit >= 1, "幂等命中必须写审计留痕（NFR-009 / data-model.md §4.1）"


def test_second_refund_with_new_key_on_refunded_transaction_mt_1001(client):
    """`refunded` 是终态（data-model.md §4.1）：换一个幂等键再退同一笔 → 409 `MT-1001`。"""
    token = bind_stall_session(client, STALL)
    created = _paid_transaction(client, token, tags="terminal")
    assert _refund(client, token, created["transaction_no"], created["total_amount_cents"], "t011-term-key-1").status_code == 200

    response = _refund(client, token, created["transaction_no"], created["total_amount_cents"], "t011-term-key-2")
    assert response.status_code == 409, f"契约 §4 期望 409(MT-1001)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1001")


# ---------------------------------------------------------------------------
# §4 `MT-1003` 超过原单金额（`REQ-028`、`AC-017`）
# ---------------------------------------------------------------------------


def test_refund_exceeding_original_mt_1003_and_creates_nothing(client, db_conn):
    """`AC-017`/`REQ-028`：退货金额 > 原单金额 → 422 `MT-1003`，**不创建 `refund`、不改交易状态**。"""
    token = bind_stall_session(client, STALL)
    created = _paid_transaction(client, token, tags="over")
    transaction_id = db_conn.execute(
        'SELECT id FROM "transaction" WHERE transaction_no = ?', (created["transaction_no"],)
    ).fetchone()["id"]

    response = _refund(client, token, created["transaction_no"], created["total_amount_cents"] + 1, "t011-over-key")
    assert response.status_code == 422, f"契约 §4 期望 422(MT-1003)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1003")

    rows = _count(db_conn, "SELECT COUNT(*) AS n FROM refund WHERE transaction_id = ?", (transaction_id,))
    assert rows == 0, f"越界退货不得创建 refund（REQ-028）：{rows} 条"
    status = db_conn.execute('SELECT status FROM "transaction" WHERE id = ?', (transaction_id,)).fetchone()["status"]
    assert status == "paid", f"被拒的退货不得改变交易状态（REQ-028），实际 {status!r}"


@pytest.mark.parametrize("amount_cents", [0, -1])
def test_refund_non_positive_amount_mt_1008(client, amount_cents):
    """契约 §3.11：`amount_cents` 必填且 > 0；0 与负数属参数校验失败。"""
    token = bind_stall_session(client, STALL)
    created = _paid_transaction(client, token, tags=f"badamt{amount_cents}")
    response = _refund(client, token, created["transaction_no"], amount_cents, f"t011-badamt-key-{amount_cents}")
    assert response.status_code == 422, f"契约 §3.11/§4 期望 422(MT-1008)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1008")


def test_refund_missing_amount_mt_1008(client):
    token = bind_stall_session(client, STALL)
    created = _paid_transaction(client, token, tags="noamt")
    response = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/refund",
        json={},
        headers=session_headers(token, "t011-noamt-key"),
    )
    assert response.status_code == 422, f"契约 §3.11 要求 amount_cents 必填，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1008")


# ---------------------------------------------------------------------------
# §4 `MT-1001` 未收款不可退货 / `MT-1005` / `MT-1009`
# ---------------------------------------------------------------------------


def test_refund_on_unpaid_transaction_mt_1001(client, db_conn):
    """契约 §3.11：交易非 `paid`（此处仍为 `priced`）→ 409 `MT-1001`，且不产生冲正。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t011-unpaid-txn")

    response = _refund(client, token, created["transaction_no"], created["total_amount_cents"], "t011-unpaid-key")
    assert response.status_code == 409, f"契约 §4 期望 409(MT-1001)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1001")

    transaction_id = db_conn.execute(
        'SELECT id FROM "transaction" WHERE transaction_no = ?', (created["transaction_no"],)
    ).fetchone()["id"]
    rows = _count(db_conn, "SELECT COUNT(*) AS n FROM refund WHERE transaction_id = ?", (transaction_id,))
    assert rows == 0, f"未收款交易不得产生冲正记录：{rows} 条"


def test_refund_requires_session_with_mt_1005(client):
    token = bind_stall_session(client, STALL)
    created = _paid_transaction(client, token, tags="nosess")
    response = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/refund",
        json={"amount_cents": created["total_amount_cents"]},
        headers={"Idempotency-Key": "t011-nosess-key"},
    )
    assert response.status_code == 401, f"契约 §1.6 期望 401(MT-1005)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1005")


def test_refund_requires_idempotency_key_mt_1008(client):
    token = bind_stall_session(client, STALL)
    created = _paid_transaction(client, token, tags="nokey")
    response = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/refund",
        json={"amount_cents": created["total_amount_cents"]},
        headers=session_headers(token),
    )
    assert response.status_code == 422, f"契约 §3.11 要求 Idempotency-Key 必填，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1008")


def test_refund_unknown_transaction_mt_1009(client):
    assert_endpoint_implemented(
        client.application, "POST", "/api/merchant/transactions/{transaction_no}/refund"
    )
    token = bind_stall_session(client, STALL)
    response = _refund(client, token, "ZZ-NOT-A-REAL-TXN", 100, "t011-unknown-key")
    assert response.status_code == 404, f"契约 §4 期望 404(MT-1009)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1009")
