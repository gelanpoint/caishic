"""`AC-045` 取消族的**可失败版** + 两条 RL-4 评审要求的新回归（`task-27` 补件）。

## 为什么把取消族搬到这里

原来那条 `test_cancel_restores_payable_...` 取消的是一笔 **`priced`（从未收款）** 的交易 ——
它**本来就不在** `payables` 的分子里，所以 `after == before` 与被取消交易**怎么计数无关**，
给的是"已经验过取消不污染应缴"的**假象**（`RL-4` 评审 P3-b 抓出）。本文件改成：

- 先造**已收款**基线（两笔，金额不同）；
- 再取消**另一笔**；
- 断言 `paid_txn_count` / `received_amount_cents` / `payable_cents` **恰等于"只算已收款"的独立复算值**；
- 并**直接查库**证明"不过滤时笔数与金额都不同" ⇒ 这条排除是**承重**的，删掉它必红。

## 另两条（`mid` 正在修，故先红后绿）

- **取消 vs 迟到回调**：`method=qr`（`pending`）→ §3.35 取消 → 再投递 `success`/`failed`/`timeout`
  回调 ⇒ 交易**仍 `cancelled`**、实收**未被写**、无 `transaction_confirmed` 留痕、`payment` 状态不变、
  `payment_callback_log` **有**该回调（`RL-9` 不得静默丢弃）、使用率不含它。
- **应缴 vs 日聚合同基数**：一笔正常收款 + 一笔收款后全额退货 ⇒ §3.36 的 `commission_cents`
  与 §3.12/§2.15 的 `commission_amount_cents` 差额 ≤1 分（逐笔取整 vs 合计取整的固有差）。
"""

from __future__ import annotations

import pytest

from contract_support import (
    assert_error_response,
    assert_exact_keys,
    bind_stall_session,
    create_priced_transaction,
    json_of,
    today_iso,
)
from demo_console_support import (
    add_product,
    callback,
    cancel,
    dashboard,
    effective_rate_bp,
    ensure_rule,
    half_up,
    hub,
    pay_cash,
    pay_qr,
    payable_of,
    refund,
    registered,
    suffix,
    usage_metrics,
)


def _setup(client, *, price_cents: int = 480):
    """注册商家 + 建会话 + 铺一件可计价商品；返回 `(merchant, token, stall_id)`。"""
    merchant, _ = registered(client)
    token = bind_stall_session(client, merchant["stall_no"])
    add_product(client, token, price_cents=price_cents)
    return merchant, token


def _paid_rows(db_conn, stall_id: int, day: str) -> list:
    return db_conn.execute(
        'SELECT received_amount_cents FROM "transaction"'
        " WHERE stall_id = ? AND business_date = ? AND status = 'paid'",
        (stall_id, day),
    ).fetchall()


# --------------------------------------------------------------------------- #
# `AC-045` ①②③：可失败版（已收款基线 + 独立复算 + 直接查库）
# --------------------------------------------------------------------------- #


def test_cancel_is_excluded_from_every_payable_sum_and_leaves_exactly_one_trail(client, db_conn):
    """`AC-045` ①②③：取消的那笔**不进任何金额/笔数**，且留痕恰好一条、重复取消只生效一次。"""
    day = today_iso()
    ensure_rule(client, day)
    rate_bp = effective_rate_bp(db_conn, day)
    merchant, token = _setup(client)

    # 基线：两笔**已收款**（重量不同 ⇒ 金额不同），保证分子非空、且不是 0 对 0
    for weight in (800, 1600):
        txn = create_priced_transaction(client, token, idempotency_key=f"base{weight}-{suffix()}",
                                        weights_grams=[weight])
        assert pay_cash(client, token, txn["transaction_no"], f"bp{weight}-{suffix()}").status_code == 200

    # 第三笔：只计价，随后取消
    doomed = create_priced_transaction(client, token, idempotency_key=f"doom-{suffix()}",
                                       weights_grams=[2400])
    key = f"cancel-key-{suffix()}"
    response = cancel(client, doomed["transaction_no"], key)
    assert response.status_code == 200, (
        f"契约 §3.35 期望 200，实际 {response.status_code}：{response.get_data(as_text=True)[:200]}"
    )
    payload = json_of(response)
    assert_exact_keys(payload, {"transaction_no", "status", "cancelled_at", "is_duplicate"},
                      "§3.35 取消响应")
    assert payload["status"] == "cancelled" and payload["is_duplicate"] is False

    row = db_conn.execute('SELECT id, status FROM "transaction" WHERE transaction_no = ?',
                          (doomed["transaction_no"],)).fetchone()
    assert row["status"] == "cancelled", "库里状态必须转 cancelled（不是 DELETE 掉）"
    stall_id = db_conn.execute("SELECT stall_id FROM \"transaction\" WHERE id = ?",
                               (row["id"],)).fetchone()["stall_id"]

    # 独立复算：只取 status='paid'
    paid = _paid_rows(db_conn, stall_id, day)
    assert len(paid) == 2, f"前置：应有 2 笔已收款，实际 {len(paid)}"
    my_received = sum(int(item["received_amount_cents"]) for item in paid)
    my_commission = sum(half_up(int(item["received_amount_cents"]) * rate_bp, 10000) for item in paid)

    view = payable_of(hub(client, day), merchant["merchant_id"])
    assert view is not None, "中台必须给出该商家应缴行"
    assert view["paid_txn_count"] == 2, f"被取消那笔不得计入笔数：{view['paid_txn_count']}"
    assert view["received_amount_cents"] == my_received, (
        f"实收必须只含已收款：期望 {my_received}，实际 {view['received_amount_cents']}"
    )
    assert view["payable_cents"] == my_commission, (
        f"应缴必须只含已收款：期望 {my_commission}，实际 {view['payable_cents']}"
    )

    # 直接查库：**不过滤**时笔数与金额都不同 ⇒ 证明这条排除是承重的（删掉它，上面两条必红）
    naive = db_conn.execute(
        'SELECT COUNT(*) AS n, COALESCE(SUM(total_amount_cents), 0) AS gross'
        ' FROM "transaction" WHERE stall_id = ? AND business_date = ?',
        (stall_id, day),
    ).fetchone()
    assert int(naive["n"]) == 3, f"不过滤时应有 3 笔（含已取消），实际 {naive['n']}"
    assert int(naive["gross"]) > my_received, (
        f"不过滤时总额必须更大（已取消那笔有金额）：{naive['gross']} vs 已收款 {my_received}"
    )

    trails = db_conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'transaction_cancelled' AND ref_id = ?",
        (row["id"],),
    ).fetchone()["n"]
    assert trails == 1, f"取消动作必须留痕**恰好一条**（NFR-009），实际 {trails}"

    repeat = cancel(client, doomed["transaction_no"], key)
    assert repeat.status_code == 200, "重复取消必须仍 200"
    assert json_of(repeat)["is_duplicate"] is True, "重复取消必须标记 is_duplicate"
    trails_again = db_conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'transaction_cancelled' AND ref_id = ?",
        (row["id"],),
    ).fetchone()["n"]
    assert trails_again == 1, f"重复取消**不得重复留痕**，实际 {trails_again} 条"


def test_cancel_a_paid_transaction_is_rejected_mt_1001(client, db_conn):
    """`AC-045` ④ 负例：对**已确认支付**的交易取消 ⇒ `MT-1001`(409)，且状态不变、不留痕。"""
    merchant, token = _setup(client)
    txn = create_priced_transaction(client, token, idempotency_key=f"paid-{suffix()}")
    assert pay_cash(client, token, txn["transaction_no"], f"pay-{suffix()}").status_code == 200

    payload = assert_error_response(cancel(client, txn["transaction_no"], f"k-{suffix()}"), "MT-1001")
    assert payload["error"]["code"] == "MT-1001"
    row = db_conn.execute('SELECT id, status FROM "transaction" WHERE transaction_no = ?',
                          (txn["transaction_no"],)).fetchone()
    assert row["status"] == "paid", "被拒的取消不得改动库里的状态"
    trails = db_conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'transaction_cancelled' AND ref_id = ?",
        (row["id"],),
    ).fetchone()["n"]
    assert trails == 0, "被拒的取消不得留痕（否则等于静默撤销）"


def test_cancel_unknown_transaction_and_bad_key(client):
    """契约 §3.35：交易不存在 ⇒ `MT-1009`(404)；幂等键缺失/超长 ⇒ `MT-1008`(422)。"""
    assert_error_response(cancel(client, "NO-SUCH-TXN-0001", f"k-{suffix()}"), "MT-1009")
    assert_error_response(cancel(client, "NO-SUCH-TXN-0001", None), "MT-1008")
    assert_error_response(cancel(client, "NO-SUCH-TXN-0001", "k" * 65), "MT-1008")


# --------------------------------------------------------------------------- #
# 回归：取消 vs 迟到回调（`mid` 正在修）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("result", ["success", "failed", "timeout"])
def test_late_callback_after_cancel_never_confirms(client, db_conn, result):
    """取消后**迟到**的回调不得把交易"救活"：状态、实收、留痕、流水、回调日志逐项钉住。"""
    day = today_iso()
    merchant, token = _setup(client)
    base_numerator = usage_metrics(client, day)["stall_usage_numerator"]

    txn = create_priced_transaction(client, token, idempotency_key=f"late-{suffix()}")
    assert usage_metrics(client, day)["stall_usage_numerator"] == base_numerator + 1, (
        "前置：只计价（未取消）时该摊位应计入使用率"
    )
    qr = pay_qr(client, token, txn["transaction_no"], f"qr-{suffix()}")
    assert qr.status_code == 202, f"§3.10 `method=qr` 期望 202，实际 {qr.status_code}"
    payment_no = json_of(qr)["payment_no"]

    assert cancel(client, txn["transaction_no"], f"ck-{suffix()}").status_code == 200
    row = db_conn.execute(
        'SELECT id, status, received_amount_cents FROM "transaction" WHERE transaction_no = ?',
        (txn["transaction_no"],),
    ).fetchone()
    assert row["status"] == "cancelled", "取消后库里必须是 cancelled"
    payment_status_before = db_conn.execute(
        "SELECT status FROM payment WHERE payment_no = ?", (payment_no,)
    ).fetchone()["status"]

    callback_no = f"CB-{suffix()}"
    response = callback(client, callback_no, payment_no, result)
    assert response.status_code == 200, (
        f"§3.17 回调期望 200（**不得**因为交易已取消就丢弃），实际 {response.status_code}："
        f"{response.get_data(as_text=True)[:200]}"
    )

    after = db_conn.execute(
        'SELECT status, received_amount_cents FROM "transaction" WHERE transaction_no = ?',
        (txn["transaction_no"],),
    ).fetchone()
    assert after["status"] == "cancelled", (
        f"迟到的 `{result}` 回调不得把已取消交易改回其它状态，实际 {after['status']}"
    )
    assert not after["received_amount_cents"], (
        f"已取消交易不得被写入实收，实际 {after['received_amount_cents']!r}"
    )
    confirmed = db_conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'transaction_confirmed' AND ref_id = ?",
        (row["id"],),
    ).fetchone()["n"]
    assert confirmed == 0, f"已取消交易不得留下 `transaction_confirmed` 留痕，实际 {confirmed} 条"
    payment_status_after = db_conn.execute(
        "SELECT status FROM payment WHERE payment_no = ?", (payment_no,)
    ).fetchone()["status"]
    assert payment_status_after == payment_status_before, (
        f"`payment` 状态不得因迟到回调改变：{payment_status_before} → {payment_status_after}"
    )
    logged = db_conn.execute(
        "SELECT COUNT(*) AS n FROM payment_callback_log WHERE callback_no = ?", (callback_no,)
    ).fetchone()["n"]
    assert logged == 1, f"回调必须落 `payment_callback_log`（RL-9 不得静默丢弃），实际 {logged} 条"
    assert usage_metrics(client, day)["stall_usage_numerator"] == base_numerator, (
        "已取消交易不得计入使用率（过滤被删时这里会 = base+1 ⇒ 红）"
    )


# --------------------------------------------------------------------------- #
# 回归：应缴（逐笔） vs 日聚合（合计）同基数
# --------------------------------------------------------------------------- #


def test_hub_commission_and_daily_aggregate_share_the_same_base(client, db_conn):
    """一笔正常收款 + 一笔收款后**全额退货** ⇒ §3.36 与 §3.12/§2.15 的佣金差额 ≤1 分。"""
    day = today_iso()
    ensure_rule(client, day)
    merchant, token = _setup(client, price_cents=500)

    normal = create_priced_transaction(client, token, idempotency_key=f"ok-{suffix()}",
                                       weights_grams=[1000])
    assert pay_cash(client, token, normal["transaction_no"], f"okp-{suffix()}").status_code == 200

    refunded = create_priced_transaction(client, token, idempotency_key=f"rf-{suffix()}",
                                        weights_grams=[1500])
    assert pay_cash(client, token, refunded["transaction_no"], f"rfp-{suffix()}").status_code == 200
    amount = int(refunded["total_amount_cents"])
    response = refund(client, token, refunded["transaction_no"], amount, f"rfr-{suffix()}")
    assert response.status_code == 200, (
        f"§3.11 退货期望 200，实际 {response.status_code}：{response.get_data(as_text=True)[:200]}"
    )

    hub_row = payable_of(hub(client, day), merchant["merchant_id"])
    board = dashboard(client, token, day)
    assert hub_row is not None, "中台必须给出应缴行"
    delta = abs(int(hub_row["commission_cents"]) - int(board["commission_amount_cents"]))
    assert delta <= 1, (
        "两条路径必须同基数（逐笔取整 vs 合计取整只允许差 1 分）："
        f"§3.36 commission_cents={hub_row['commission_cents']} "
        f"vs §3.12 commission_amount_cents={board['commission_amount_cents']}（差 {delta}）"
    )
