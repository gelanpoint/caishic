"""面向展示的只读投影与派生口径（`REQ-021`、`REQ-023`；契约 §3.12 / §3.18 / §3.19，
口径见 `data-model.md` §2.15 / §2.17）。

本模块**不落表**、**不依赖 HTTP**：所有数字都从事实表（`transaction` / `transaction_item` /
`refund` / `stall` / `product`）现场算出 —— `data-model.md` §5.1 的"派生指标不落表"纪律同样适用：
多存一份派生值，就多一个"库里是 3 笔、接口说 2 笔"的漂移点。

> **本模块的归口（如实说明）**：`plan.md` §4 把 `app/domain/metrics.py` 描述为"使用率指标的派生计算"。
> 这里同时承载 §3.18/§3.19 的**顾客可见只读投影**，理由是两者同源：§3.18 的核心字段
> `price_consistency_bp` 就是本模块的口径，§3.19 的凭证视图与看板同属"按事实表现场投影"，
> 放在一起才能保证同一口径只有一处实现。若负责人更愿意按展示对象分文件，正确做法是
> **先改 `plan.md` §4** 登记 `app/domain/customer.py` 再迁 —— 不自行新建白名单外的文件。

两处必须明说的边界（免得后来者把这当成最终形态）：

1. **`commission_amount_cents` 当前恒为 0**：佣金口径的匹配与按实收金额计算属 `T-020`
   （`app/domain/commission.py`）。本期尚无任何 `commission_rule` 可匹配，故"无口径 → 不计佣"
   是**当下的真实值**，不是占位 0。且契约 §3.12 声明的可能错误只有 `MT-1005`，
   **没有** `MT-1013`（无生效佣金口径）→ 看板不得因为"没配佣金口径"而报错。
   `T-020` 落地后**必须把这里改成读佣金口径**，而不是在别处再算一遍（一个口径只准有一处实现）。
2. **退货额按"原单营业日"归集**：`refund` 表只有 `refunded_at`（时刻），没有营业日字段，
   故经原单 `transaction.business_date` 归集 —— 与 `daily_aggregate.refund_amount_cents`
   的口径一致（`data-model.md` §2.15：同一摊位同一营业日的冲减额）。
   **`T-020` 生成 `daily_aggregate` 快照后，必须让快照口径与本模块的现场汇总口径对齐**，
   否则同一块看板会经两条路径算出两个数。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .. import TradeError
from ..db import now_iso
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


# ---------------------------------------------------------------------------
# §3.18 顾客可见的摊位信息与信用指标（`REQ-008` / `REQ-023` / `AC-011`）
# ---------------------------------------------------------------------------


def customer_stall_profile(conn: sqlite3.Connection, stall_no: str) -> dict:
    """契约 §3.18：`{stall_no, stall_name, in_business, price_consistency_bp, computed_at}`。

    字段**恰好这五个**：`T-014` 按白名单逐一相等断言，多一个就是"没有采集来源的空壳字段"——
    那正是绵阳"扫过好几次空码之后再也不信这个标签了"的成因（`discovery.md` D-08）。

    - 未知摊位号 → `MT-1009`；**停用摊位不算未知**：它照样能被看到，只是 `in_business = false`
      （顾客有权知道"这个摊今天没营业"，这比一个 404 有用得多）；
    - `price_consistency_bp` 用**当日**口径现场计算（复用本模块唯一实现）；
    - `computed_at` 是**本次计算时刻**（`data-model.md` §2.17 的 `computed_at` 语义）。
      始终现场算而不读 `stall_credit` 快照：快照由 `T-020` 随日聚合重算，
      若这里读快照、看板（§3.12）读事实表，同一指标就会出现两个值。
      `T-020` 落地后两处一起改为读快照（一个口径一处实现）。
    """
    stall = conn.execute(
        "SELECT id, stall_no, name, status FROM stall WHERE stall_no = ?", (stall_no,)
    ).fetchone()
    if stall is None:
        raise TradeError("MT-1009", f"摊位不存在：{stall_no}", {"stall_no": stall_no})

    return {
        "stall_no": stall["stall_no"],
        "stall_name": stall["name"] or stall["stall_no"],
        "in_business": stall["status"] == "active",
        "price_consistency_bp": price_consistency_bp(conn, int(stall["id"]), date.today().isoformat()),
        "computed_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# §3.19 顾客扫码页数据（`REQ-023` / `AC-011`）
# ---------------------------------------------------------------------------


def customer_receipt(conn: sqlite3.Connection, transaction_no: str) -> dict:
    """契约 §3.19：`{transaction_no, status, total_amount_cents, items[{name, weight_grams, amount_cents}], paid_at}`。

    同样**只给这五个字段**，明细元素只给三个字段 —— 顾客页给的是**凭证**，不是内部数据视图：
    `transaction_item.id` / `payment_no` / 操作人这类字段一个都不放（`AC-011` / `AC-012`）。

    - 未知交易号 → `MT-1009`；
    - `paid_at` 取该交易**成功流水**的 `confirmed_at`（现金与收款码同源，`REQ-010`）；
      未收款的交易该字段为 `null` —— 契约 §3.19 只声明 `MT-1009` 一种错误，
      没有"未收款不得查看"这一条，故**不发明**拒绝语义（该边界在 `T-014` 文件里也如实标注了）。
    """
    txn = conn.execute(
        'SELECT * FROM "transaction" WHERE transaction_no = ?', (transaction_no,)
    ).fetchone()
    if txn is None:
        raise TradeError("MT-1009", f"交易不存在：{transaction_no}", {"transaction_no": transaction_no})

    items = conn.execute(
        """
        SELECT p.name AS name, ti.weight_grams AS weight_grams, ti.amount_cents AS amount_cents
        FROM transaction_item ti
        JOIN product p ON p.id = ti.product_id
        WHERE ti.transaction_id = ?
        ORDER BY ti.id
        """,
        (txn["id"],),
    ).fetchall()
    paid = conn.execute(
        "SELECT confirmed_at FROM payment WHERE transaction_id = ? AND status = 'success' ORDER BY id LIMIT 1",
        (txn["id"],),
    ).fetchone()

    return {
        "transaction_no": txn["transaction_no"],
        "status": txn["status"],
        "total_amount_cents": int(txn["total_amount_cents"]),
        "items": [
            {
                "name": row["name"],
                "weight_grams": int(row["weight_grams"]),
                "amount_cents": int(row["amount_cents"]),
            }
            for row in items
        ],
        "paid_at": paid["confirmed_at"] if paid is not None else None,
    }
