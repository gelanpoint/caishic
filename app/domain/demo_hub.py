"""中台视图：事件表与各商家应缴（契约 §3.36；`REQ-056` / `AC-046`）。

两条口径纪律：

1. **不另写一份佣金口径**：费率取 `commission.find_effective_rule()`、折算用 `commission_of()`
   —— 与 §2.15 日聚合是同一对函数。无生效口径 ⇒ 佣金 0，**不报错**（§3.36 只声明 `MT-1008`；
   这条与 `metrics.stall_day_facts` 的无口径行为一致，与 §3.26 日聚合的 `MT-1013` 不同，
   因为那两个端点的契约各自写了不同的处置）。
2. **取数口径 = `status IN ('paid', 'refunded')`**（契约 §3.36 明文）：即「**收过款的、且未被取消的**」。
   `refunded` 的钱**确实收过**（退货只改状态、**不冲减** `received_amount_cents`，见 §3.12），
   与 `data-model.md` §5.1「均只统计**非 `cancelled`** 的交易」同口径；在途未收款的 `priced`
   与 `payment_failed` 不计入。取消**不是 DELETE** —— 行还在、留痕还在，只是不再进任何金额口径
   （"账上等同于未发生"，`REQ-055` / `AC-046`）。
"""

from __future__ import annotations

import sqlite3

from .commission import commission_of, find_effective_rule


def payables_for_day(conn: sqlite3.Connection, business_date: str) -> list[dict]:
    """各商家该营业日的应缴（契约 §3.36）。

    `payable_cents` = 当日 `status IN ('paid', 'refunded')` 交易**逐笔**按佣金口径折算后求和
    （§3.36 / `AC-046` ① 明文"逐笔计算的佣金之和"；取数口径见模块 docstring 第 2 条）。
    `commission_cents` 与 `payable_cents` **同值**：本演示里"应缴"就是佣金，
    契约没有给出两个不同口径，拆成两个数就是自己发明语义（`RL-1`）。
    与 §2.15 日聚合的 `commission_amount_cents`（**对总额折算一次**）在费率非整除时
    **允许相差 1 分**（§3.36 口径注明文），不为了对齐而改任何一边。
    """
    rule = find_effective_rule(conn, business_date)
    stalls = conn.execute(
        """
        SELECT m.id AS merchant_id, m.name AS merchant_name, s.id AS stall_id, s.stall_no
        FROM merchant m
        JOIN stall s ON s.merchant_id = m.id
        ORDER BY m.id, s.id
        """
    ).fetchall()

    payables: list[dict] = []
    for row in stalls:
        collected = conn.execute(
            'SELECT received_amount_cents FROM "transaction"'
            " WHERE stall_id = ? AND business_date = ? AND status IN ('paid', 'refunded')",
            (row["stall_id"], business_date),
        ).fetchall()
        amounts = [int(item["received_amount_cents"] or 0) for item in collected]
        commission = sum(commission_of(amount, rule) for amount in amounts)
        payables.append(
            {
                "merchant_id": int(row["merchant_id"]),
                "merchant_name": row["merchant_name"],
                "stall_no": row["stall_no"],
                # 契约字段名是 `paid_txn_count`（"已确认笔数"）；按 §3.36 的取数口径，
                # 它数的是 `paid + refunded` —— 退货的钱确实收过（只改状态、不冲减实收）。
                "paid_txn_count": len(amounts),
                "received_amount_cents": sum(amounts),
                "commission_cents": commission,
                "payable_cents": commission,
            }
        )
    return payables


def hub_events(conn: sqlite3.Connection, business_date: str) -> list[dict]:
    """该营业日的事件表（契约 §3.36；数据源 = `audit_log` 只增不改的留痕）。

    为什么按 `occurred_at` 的**日期部分**筛：`audit_log` 没有 `business_date` 列
    （`data-model.md` §2.18 就是那 8 个字段），而留痕本身就是"什么时候发生的"。
    排序照契约：`occurred_at` 升序、`event_id` 升序。
    交易号/金额经 `ref_table = 'transaction'` 回查 —— 非交易类留痕（如补传、佣金口径变更）这两项为 `null`。
    """
    rows = conn.execute(
        """
        SELECT l.id AS event_id, l.occurred_at, l.event_type, l.actor,
               s.stall_no, m.name AS merchant_name,
               t.transaction_no, t.total_amount_cents
        FROM audit_log l
        LEFT JOIN stall s ON s.id = l.stall_id
        LEFT JOIN merchant m ON m.id = s.merchant_id
        LEFT JOIN "transaction" t ON l.ref_table = 'transaction' AND t.id = l.ref_id
        WHERE substr(l.occurred_at, 1, 10) = ?
        ORDER BY l.occurred_at ASC, l.id ASC
        """,
        (business_date,),
    ).fetchall()
    return [
        {
            "event_id": int(row["event_id"]),
            "occurred_at": row["occurred_at"],
            "event_type": row["event_type"],
            "stall_no": row["stall_no"],
            "merchant_name": row["merchant_name"],
            "transaction_no": row["transaction_no"],
            "amount_cents": row["total_amount_cents"],
            "actor": row["actor"],
        }
        for row in rows
    ]
