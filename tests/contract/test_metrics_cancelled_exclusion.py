"""`metrics.py` 里 `status <> 'cancelled'` 五处排除的**灵敏度**用例（`task-27` 的 RL-4 补件）。

`RL-4` 评审指出：把 `metrics.py` 里那五处 `AND t.status <> 'cancelled'` 删掉，
**当时没有任何用例会变红** —— 即"取消不计入一切聚合"这条 `REQ-055` 的核心语义**无人看守**。
本文件专治这一条，做法是**断言具体数值**（不是 `>= 0`），并**同时算出"不过滤时会是多少"**，
证明这条排除是**承重**的：

| 站点 | 函数 | 断言 |
| --- | --- | --- |
| `metrics.py:113` | `stall_day_facts` | `txn_count` / `gross_amount_cents` / `received_amount_cents` 只含已收款 |
| `metrics.py:56` | `price_consistency_bp` | 已取消交易的 `price_changed` 明细不得进分母 |
| `metrics.py:153` | `recompute_stall_credit` | 落库的 `item_count` 只含未取消交易 |
| `metrics.py:343/360` | `usage_metrics` | 只有"已取消交易"的摊位**不得**算作"有交易" |

直接调**领域函数**（不经端点）是为了让判据紧贴那五行；数据仍用真端点造（§3.6/§3.10/§3.35）。
"""

from __future__ import annotations

import pytest

from contract_support import bind_stall_session, create_priced_transaction, today_iso
from demo_console_support import (
    add_product,
    cancel,
    ensure_rule,
    half_up,
    pay_cash,
    registered,
    suffix,
    usage_metrics,
)

from app.domain import metrics  # noqa: E402  （仓根已在 sys.path 上：contract_support 的垫片）


def _fixture(client, db_conn, *, weights=(1000, 2400)):
    """造一笔**已收款** + 一笔**已取消**（金额不同）；返回 `(stall_id, day, paid_txn, cancelled_txn)`。"""
    day = today_iso()
    ensure_rule(client, day)
    merchant, _ = registered(client)
    token = bind_stall_session(client, merchant["stall_no"])
    add_product(client, token, price_cents=500)

    paid = create_priced_transaction(client, token, idempotency_key=f"m-ok-{suffix()}",
                                     weights_grams=[weights[0]])
    assert pay_cash(client, token, paid["transaction_no"], f"m-okp-{suffix()}").status_code == 200
    doomed = create_priced_transaction(client, token, idempotency_key=f"m-cx-{suffix()}",
                                       weights_grams=[weights[1]])
    assert cancel(client, doomed["transaction_no"], f"m-cxk-{suffix()}").status_code == 200

    stall_id = db_conn.execute(
        'SELECT stall_id FROM "transaction" WHERE transaction_no = ?', (paid["transaction_no"],)
    ).fetchone()["stall_id"]
    return stall_id, day, paid, doomed


def _naive(db_conn, stall_id: int, day: str) -> dict:
    """**不过滤**地算一遍（即"实现被改坏之后"的样子）—— 用来证明排除是承重的。"""
    return db_conn.execute(
        'SELECT COUNT(*) AS n, COALESCE(SUM(total_amount_cents), 0) AS gross,'
        " COALESCE(SUM(received_amount_cents), 0) AS received"
        ' FROM "transaction" WHERE stall_id = ? AND business_date = ?',
        (stall_id, day),
    ).fetchone()


def test_stall_day_facts_excludes_the_cancelled_transaction(client, db_conn):
    """`metrics.py:113`：日聚合的笔数与金额**只含未取消交易**（断言具体数值）。"""
    stall_id, day, paid, _ = _fixture(client, db_conn)
    facts = metrics.stall_day_facts(db_conn, stall_id, day)
    naive = _naive(db_conn, stall_id, day)

    assert facts["txn_count"] == 1, f"日聚合笔数只应含已收款那笔，实际 {facts['txn_count']}"
    assert facts["gross_amount_cents"] == int(paid["total_amount_cents"]), (
        f"交易总额只应含未取消交易：期望 {paid['total_amount_cents']}，实际 {facts['gross_amount_cents']}"
    )
    assert facts["received_amount_cents"] == int(paid["total_amount_cents"]), (
        f"实收只应含已收款那笔：实际 {facts['received_amount_cents']}"
    )
    # 承重证明：不过滤时笔数与总额都**不同** ⇒ 删掉那行 WHERE 条件，上面三条必红
    assert int(naive["n"]) == 2 and int(naive["gross"]) > facts["gross_amount_cents"], (
        f"前置/承重证明失败：不过滤时应有 2 笔且总额更大，实际 {dict(naive)}"
    )


def test_price_consistency_and_credit_exclude_cancelled_items(client, db_conn):
    """`metrics.py:56` 与 `:153`：已取消交易的明细不得进标价一致率的分母，也不得进信用档案。"""
    stall_id, day, paid, doomed = _fixture(client, db_conn)
    # 前置：把**已取消**那笔的明细标成"价格被改过" ⇒ 若它进了分母，一致率必低于 10000
    doomed_id = db_conn.execute('SELECT id FROM "transaction" WHERE transaction_no = ?',
                                (doomed["transaction_no"],)).fetchone()["id"]
    db_conn.execute("UPDATE transaction_item SET price_changed = 1 WHERE transaction_id = ?",
                    (doomed_id,))
    db_conn.commit()

    bp = metrics.price_consistency_bp(db_conn, stall_id, day)
    assert bp == 10000, (
        f"只有未取消交易的明细参与计算，且它们都没改过价 ⇒ 一致率必须是 10000，实际 {bp}"
        "（若已取消那笔进了分母，这里会 < 10000）"
    )
    items = db_conn.execute(
        "SELECT COUNT(*) AS n FROM transaction_item ti JOIN \"transaction\" t ON t.id = ti.transaction_id"
        " WHERE t.stall_id = ? AND t.business_date = ? AND t.status <> 'cancelled'", (stall_id, day)
    ).fetchone()["n"]
    assert items > 0, "前置：未取消交易必须有明细，否则一致率取的是'无明细'分支"
    naive_items = db_conn.execute(
        "SELECT COUNT(*) AS n FROM transaction_item ti JOIN \"transaction\" t ON t.id = ti.transaction_id"
        " WHERE t.stall_id = ? AND t.business_date = ?", (stall_id, day)
    ).fetchone()["n"]
    assert naive_items > items, f"承重证明失败：不过滤时明细数应更多（{naive_items} vs {items}）"
    naive_bp = half_up((naive_items - 1) * 10_000, naive_items)
    assert naive_bp < 10000, f"不过滤时一致率应低于 10000（{naive_bp}）⇒ 这条排除是可观测的"

    metrics.recompute_stall_credit(db_conn, stall_id, day)
    db_conn.commit()
    credit = db_conn.execute(
        "SELECT item_count, price_consistency_bp FROM stall_credit WHERE stall_id = ?", (stall_id,)
    ).fetchone()
    assert int(credit["item_count"]) == items, (
        f"信用档案的明细数必须只含未取消交易：期望 {items}，实际 {credit['item_count']}"
    )
    assert int(credit["price_consistency_bp"]) == 10000, (
        f"信用档案的一致率必须与 §3.18 同口径（10000），实际 {credit['price_consistency_bp']}"
    )


def test_usage_metrics_does_not_count_a_stall_whose_only_transaction_is_cancelled(client, db_conn):
    """`metrics.py:343/360`：只有"已取消交易"的摊位**不得**算作"有交易的摊位"。"""
    day = today_iso()
    ensure_rule(client, day)
    merchant, _ = registered(client)
    token = bind_stall_session(client, merchant["stall_no"])
    add_product(client, token)

    base = usage_metrics(client, day)["stall_usage_numerator"]
    txn = create_priced_transaction(client, token, idempotency_key=f"u-{suffix()}")
    assert usage_metrics(client, day)["stall_usage_numerator"] == base + 1, (
        "前置：只计价（未取消）时该摊位应计入使用率"
    )
    assert cancel(client, txn["transaction_no"], f"u-ck-{suffix()}").status_code == 200
    after = usage_metrics(client, day)
    assert after["stall_usage_numerator"] == base, (
        f"已取消交易不得让摊位计入使用率：期望 {base}，实际 {after['stall_usage_numerator']}"
        "（若 `status <> 'cancelled'` 被删，这里会 = base+1）"
    )
    assert after["stall_usage_denominator"] == usage_metrics(client, day)["stall_usage_denominator"], (
        "分母（在营摊位数）不随交易增减"
    )
    # 直接查库交叉核对：该摊位当日**没有**未取消交易
    stall_id = db_conn.execute("SELECT id FROM stall WHERE stall_no = ?",
                               (merchant["stall_no"],)).fetchone()["id"]
    live = db_conn.execute(
        'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ? AND business_date = ?'
        " AND status <> 'cancelled'", (stall_id, day)
    ).fetchone()["n"]
    assert live == 0, f"该摊位当日不应有未取消交易，实际 {live}"


def test_sensitivity_flipping_status_back_to_paid_changes_every_metric(client, db_conn):
    """**灵敏度负例**：把那笔的 `status` 从 `cancelled` 改回 `paid`（等价于"过滤条件失效"）
    ⇒ 日聚合/标价一致率/信用档案**必须全部变化**。

    这条不碰 `app/**`：它证明上面几条判据**真的盯着 `status`**，而不是"恰好那个数就是 1"。
    改完当场还原，不留脏数据。
    """
    stall_id, day, _, doomed = _fixture(client, db_conn)
    doomed_id = db_conn.execute('SELECT id FROM "transaction" WHERE transaction_no = ?',
                                (doomed["transaction_no"],)).fetchone()["id"]
    db_conn.execute("UPDATE transaction_item SET price_changed = 1 WHERE transaction_id = ?",
                    (doomed_id,))
    db_conn.commit()

    facts_before = metrics.stall_day_facts(db_conn, stall_id, day)
    bp_before = metrics.price_consistency_bp(db_conn, stall_id, day)
    metrics.recompute_stall_credit(db_conn, stall_id, day)
    db_conn.commit()
    credit_before = db_conn.execute("SELECT item_count FROM stall_credit WHERE stall_id = ?",
                                    (stall_id,)).fetchone()["item_count"]
    assert bp_before == 10000, f"前置：过滤生效时一致率应为 10000，实际 {bp_before}"

    try:
        db_conn.execute('UPDATE "transaction" SET status = \'paid\' WHERE id = ?', (doomed_id,))
        db_conn.commit()
        facts_after = metrics.stall_day_facts(db_conn, stall_id, day)
        bp_after = metrics.price_consistency_bp(db_conn, stall_id, day)
        metrics.recompute_stall_credit(db_conn, stall_id, day)
        db_conn.commit()
        credit_after = db_conn.execute("SELECT item_count FROM stall_credit WHERE stall_id = ?",
                                       (stall_id,)).fetchone()["item_count"]
    finally:
        db_conn.execute('UPDATE "transaction" SET status = \'cancelled\' WHERE id = ?', (doomed_id,))
        db_conn.commit()

    assert facts_after["txn_count"] == facts_before["txn_count"] + 1, (
        f"状态改回 paid 后日聚合笔数必须 +1：{facts_before['txn_count']} → {facts_after['txn_count']}"
    )
    assert facts_after["gross_amount_cents"] > facts_before["gross_amount_cents"], (
        "状态改回 paid 后交易总额必须变大 —— 否则这条判据根本没盯着 status"
    )
    assert bp_after < bp_before, (
        f"状态改回 paid 后（那笔明细 price_changed=1）一致率必须下降：{bp_before} → {bp_after}"
    )
    assert int(credit_after) > int(credit_before), (
        f"信用档案明细数必须随之变大：{credit_before} → {credit_after}"
    )
