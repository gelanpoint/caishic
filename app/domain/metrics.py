"""商户端看板的派生计算（`REQ-021`；契约 §3.12，口径见 `data-model.md` §2.15 / §2.17）。

本模块**不落表**、**不依赖 HTTP**：所有数字都从事实表（`transaction` / `transaction_item` / `refund`）
现场算出 —— `data-model.md` §5.1 的"派生指标不落表"纪律对看板同样适用：
多存一份派生值，就多一个"库里是 3 笔、接口说 2 笔"的漂移点。

两处必须明说的边界（免得后来者把这当成最终形态）：

1. **`commission_amount_cents` 当前恒为 0**：佣金口径的匹配与按实收金额计算属 `T-020`
   （`app/domain/commission.py`）。本期尚无任何 `commission_rule` 可匹配，故"无口径 → 不计佣"
   是**当下的真实值**，不是占位 0。且契约 §3.12 声明的可能错误只有 `MT-1005`，
   **没有** `MT-1013`（无生效佣金口径）→ 看板不得因为"没配佣金口径"而报错。
   `T-020` 落地后**必须把这里改成读佣金口径**，而不是在别处再算一遍（一个口径只准有一处实现）。
2. **退货额按"原单营业日"归集**：`refund` 表只有 `refunded_at`（时刻），没有营业日字段，
   故经原单 `transaction.business_date` 归集 —— 与 `daily_aggregate.refund_amount_cents`
   的口径一致（`data-model.md` §2.15：同一摊位同一营业日的冲减额）。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .catalog import parse_business_date
from .pricing import half_up_div

#: 无明细时的一致率（`data-model.md` §2.17：`item_count = 0` 时取 10000）
NO_ITEM_CONSISTENCY_BP = 10_000


def price_consistency_bp(conn: sqlite3.Connection, stall_id: int, business_date: str) -> int:
    """标价一致率（万分比 0~10000），口径 = `data-model.md` §2.17。

    `round((item_count − price_changed_count) ÷ item_count × 10000)`；无明细取 10000。
    **抹零不计入**（`REQ-008` / `AC-008`）：公式只看 `price_changed`，`is_round_off` 不参与 ——
    这正是 `AC-008` 要的行为，改动前先看那条断言。
    取整复用 `pricing.half_up_div`（与计价同一套四舍五入），不在本文件再写一个除法。
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS item_count,
               COALESCE(SUM(ti.price_changed), 0) AS price_changed_count
        FROM transaction_item ti
        JOIN "transaction" t ON t.id = ti.transaction_id
        WHERE t.stall_id = ? AND t.business_date = ?
        """,
        (stall_id, business_date),
    ).fetchone()

    item_count = int(row["item_count"])
    if item_count == 0:
        return NO_ITEM_CONSISTENCY_BP
    unchanged = item_count - int(row["price_changed_count"])
    return half_up_div(unchanged * 10_000, item_count)


def stall_daily_dashboard(
    conn: sqlite3.Connection, stall_id: int, business_date: str | None = None
) -> dict:
    """契约 §3.12 商户端看板：`{business_date, txn_count, gross_amount_cents, refund_amount_cents,
    commission_amount_cents, price_consistency_bp}`。

    字段**恰好这六个**（`T-008` 的用例按字段白名单断言）：契约 §3.12 没有的字段一个都不加。
    金额一律整数「分」（`data-model.md` §0），全程 `int`、不引入浮点。
    """
    day = parse_business_date(business_date if business_date is not None else date.today().isoformat())

    totals = conn.execute(
        """
        SELECT COUNT(*) AS txn_count,
               COALESCE(SUM(total_amount_cents), 0) AS gross_amount_cents
        FROM "transaction" WHERE stall_id = ? AND business_date = ?
        """,
        (stall_id, day),
    ).fetchone()
    refunds = conn.execute(
        """
        SELECT COALESCE(SUM(r.amount_cents), 0) AS refund_amount_cents
        FROM refund r
        JOIN "transaction" t ON t.id = r.transaction_id
        WHERE r.stall_id = ? AND t.business_date = ?
        """,
        (stall_id, day),
    ).fetchone()

    return {
        "business_date": day,
        "txn_count": int(totals["txn_count"]),
        "gross_amount_cents": int(totals["gross_amount_cents"]),
        "refund_amount_cents": int(refunds["refund_amount_cents"]),
        # 见模块 docstring 第 1 条：T-020 落地前无佣金口径可匹配
        "commission_amount_cents": 0,
        "price_consistency_bp": price_consistency_bp(conn, stall_id, day),
    }
