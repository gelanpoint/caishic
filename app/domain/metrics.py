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

from .. import TradeError, clock
from ..db import now_iso
from .catalog import parse_business_date
from .commission import commission_of, find_effective_rule
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

    **佣金不再是恒 0**：`T-020` 落地后改为按实收金额真实折算，且与 §3.26 日聚合快照
    **调用同一个 `stall_day_facts()`** —— 两条路径不可能算出两个数。
    无生效口径时看板给 0（契约 §3.12 未声明 `MT-1013`，**不得报错**，见模块 docstring 第 1 条）。
    """
    day = parse_business_date(business_date if business_date is not None else clock.today_iso())
    facts = stall_day_facts(conn, stall_id, day)
    return {
        "business_date": day,
        "txn_count": facts["txn_count"],
        "gross_amount_cents": facts["gross_amount_cents"],
        "refund_amount_cents": facts["refund_amount_cents"],
        "commission_amount_cents": facts["commission_amount_cents"],
        "price_consistency_bp": price_consistency_bp(conn, stall_id, day),
    }


def stall_day_facts(conn: sqlite3.Connection, stall_id: int, business_date: str) -> dict:
    """一个摊位一个营业日的**聚合口径唯一实现**（`data-model.md` §2.15）。

    §3.12 商户看板、§3.25 市场方看板、§3.26 日聚合快照**都走这里** ——
    这是 `T-020` 交接要求的那条纪律："快照口径与现场汇总口径必须对齐，否则同一块看板会经两条路径
    算出两个数"。**要改口径只改这一处**（改前先看 `data-model.md` §2.15 的字段语义）。

    - `txn_count` / `gross_amount_cents`：该日**全部**交易（含在途 `priced`）—— 口径同 §2.15"交易总额"；
    - `refund_amount_cents`：经原单营业日归集（`refund` 表没有营业日字段，见模块 docstring 第 2 条）；
    - `received_amount_cents`：实收合计（`transaction.received_amount_cents`，收款时才写入）；
    - `commission_amount_cents`：**按实收金额**折算（`REQ-017`），费率取该营业日生效的口径；
      无生效口径 → 0（调用方若必须报错，先用 `find_effective_rule()` 自己判断）。
    """
    totals = conn.execute(
        """
        SELECT COUNT(*) AS txn_count,
               COALESCE(SUM(total_amount_cents), 0) AS gross_amount_cents,
               COALESCE(SUM(received_amount_cents), 0) AS received_amount_cents
        FROM "transaction" WHERE stall_id = ? AND business_date = ?
        """,
        (stall_id, business_date),
    ).fetchone()
    refunds = conn.execute(
        """
        SELECT COALESCE(SUM(r.amount_cents), 0) AS refund_amount_cents
        FROM refund r
        JOIN "transaction" t ON t.id = r.transaction_id
        WHERE r.stall_id = ? AND t.business_date = ?
        """,
        (stall_id, business_date),
    ).fetchone()

    received = int(totals["received_amount_cents"])
    rule = find_effective_rule(conn, business_date)
    return {
        "txn_count": int(totals["txn_count"]),
        "gross_amount_cents": int(totals["gross_amount_cents"]),
        "refund_amount_cents": int(refunds["refund_amount_cents"]),
        "received_amount_cents": received,
        "commission_amount_cents": commission_of(received, rule),
        "commission_rule_id": int(rule["id"]) if rule is not None else None,
    }


def recompute_stall_credit(conn: sqlite3.Connection, stall_id: int, business_date: str) -> None:
    """按营业日重算摊位信用档案（`data-model.md` §2.17："随日聚合一并重算"）。

    `stall_credit` 每个摊位只有一行（`ux_stall_credit`），故本期把它写成"最近一次被聚合的那一天"
    的档案；`period_start`/`period_end` 同时落该日，使"这份数说的是哪一天"有据可查。
    口径（`item_count` / `price_changed_count` / `price_consistency_bp`）与 §3.18 现场计算
    **共用 `price_consistency_bp()` 一个实现**。
    """
    counts = conn.execute(
        """
        SELECT COUNT(*) AS item_count,
               COALESCE(SUM(ti.price_changed), 0) AS price_changed_count
        FROM transaction_item ti
        JOIN "transaction" t ON t.id = ti.transaction_id
        WHERE t.stall_id = ? AND t.business_date = ?
        """,
        (stall_id, business_date),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO stall_credit
            (stall_id, period_start, period_end, item_count, price_changed_count, price_consistency_bp, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (stall_id) DO UPDATE SET
            period_start = excluded.period_start,
            period_end = excluded.period_end,
            item_count = excluded.item_count,
            price_changed_count = excluded.price_changed_count,
            price_consistency_bp = excluded.price_consistency_bp,
            computed_at = excluded.computed_at
        """,
        (
            stall_id,
            business_date,
            business_date,
            int(counts["item_count"]),
            int(counts["price_changed_count"]),
            price_consistency_bp(conn, stall_id, business_date),
            now_iso(),
        ),
    )


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
        "price_consistency_bp": price_consistency_bp(conn, int(stall["id"]), clock.today_iso()),
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


# ---------------------------------------------------------------------------
# §3.25 市场方看板（`REQ-021`）
# ---------------------------------------------------------------------------


def market_dashboard(conn: sqlite3.Connection, business_date: str | None = None) -> dict:
    """契约 §3.25：全部摊位汇总 + 排行。

    - 汇总口径 = `data-model.md` §2.15，**与 §3.12 商户看板共用 `stall_day_facts()`**；
    - `stalls` 的元素给出 `stall_no` 与 §2.15 的四个口径字段。**契约把该数组写成 `[...]` 未逐字段钉死**
      （`说明` 只要求"全部摊位汇总 + 排行"），故这里取"能唯一标识摊位 + 能核对汇总"的最小集合，
      不塞展示用装饰字段（`RL-1`；要加 `stall_name` 之类需先改契约）；
    - **排行 = 按交易总额降序**（其次 `stall_no` 升序保证顺序稳定），写在这里免得后来者猜；
    - 覆盖**全部在营摊位**（含当日零交易者）：市场方要看的正是"谁没开秤"，只列有数据的摊位
      会把"整摊没营业"这个最重要的事实藏起来。
    """
    day = parse_business_date(business_date if business_date is not None else clock.today_iso())
    stalls = conn.execute("SELECT id, stall_no FROM stall WHERE status = 'active' ORDER BY stall_no").fetchall()

    rows: list[dict] = []
    for stall in stalls:
        facts = stall_day_facts(conn, int(stall["id"]), day)
        rows.append(
            {
                "stall_no": stall["stall_no"],
                "txn_count": facts["txn_count"],
                "gross_amount_cents": facts["gross_amount_cents"],
                "refund_amount_cents": facts["refund_amount_cents"],
                "commission_amount_cents": facts["commission_amount_cents"],
            }
        )
    rows.sort(key=lambda row: (-row["gross_amount_cents"], row["stall_no"]))

    return {
        "business_date": day,
        "market": {
            "txn_count": sum(row["txn_count"] for row in rows),
            "gross_amount_cents": sum(row["gross_amount_cents"] for row in rows),
            "commission_amount_cents": sum(row["commission_amount_cents"] for row in rows),
        },
        "stalls": rows,
    }


# ---------------------------------------------------------------------------
# §3.30 三个使用率指标（`REQ-022` 修订版、`AC-005`；口径 = `data-model.md` §5.1）
# ---------------------------------------------------------------------------


def ratio_bp(numerator: int, denominator: int) -> int:
    """万分比整数（0~10000）。分母为 0 → 0（**不发明数值**：没有分母就没有比率）。"""
    if denominator <= 0:
        return 0
    return half_up_div(numerator * 10_000, denominator)


def usage_metrics(conn: sqlite3.Connection, business_date: str | None) -> dict:
    """契约 §3.30：三个使用率指标，**每个都随响应返回分子与分母**（`AC-005` 的"第三方可逐一核对"）。

    `business_date` **必填**（缺 → `MT-1008`；契约 §3.30 明示"单日口径，逐项指标的分子分母都在同一
    营业日内取数"）。

    §5.1 的三条口径逐一照抄，**不做任何"估算"**；特别是**已废弃的第四项**
    （市场口径「日均智能秤交易占比」）的分母是外部基准（`Q-15`），**禁止编造** ——
    故本函数**只返回三个 `*_bp`**，连字段名都不给它留位置。
    """
    if business_date is None:
        raise TradeError("MT-1008", "`business_date` 必填（单日口径）", {"field": "business_date"})
    day = parse_business_date(business_date)

    active_stalls = int(conn.execute("SELECT COUNT(*) AS n FROM stall WHERE status = 'active'").fetchone()["n"])
    transaction_stalls = int(
        conn.execute(
            'SELECT COUNT(DISTINCT stall_id) AS n FROM "transaction" WHERE business_date = ?', (day,)
        ).fetchone()["n"]
    )
    cash_txns = int(
        conn.execute(
            """
            SELECT COUNT(*) AS n FROM payment p
            JOIN "transaction" t ON t.id = p.transaction_id
            WHERE t.business_date = ? AND p.method = 'cash' AND p.status = 'success'
            """,
            (day,),
        ).fetchone()["n"]
    )
    all_txns = int(
        conn.execute('SELECT COUNT(*) AS n FROM "transaction" WHERE business_date = ?', (day,)).fetchone()["n"]
    )
    # 「当日价目表完整且已更新」= 该摊位全部在售商品当日都有 price_item（source 为 manual / copied 均算）
    maintained = int(
        conn.execute(
            """
            SELECT COUNT(*) AS n FROM stall s
            WHERE s.status = 'active' AND NOT EXISTS (
                SELECT 1 FROM product p
                WHERE p.stall_id = s.id AND p.status = 'active' AND NOT EXISTS (
                    SELECT 1 FROM price_item pi
                    WHERE pi.stall_id = s.id AND pi.product_id = p.id AND pi.business_date = ?))
            """,
            (day,),
        ).fetchone()["n"]
    )

    return {
        "business_date": day,
        "stall_usage_bp": ratio_bp(transaction_stalls, active_stalls),
        "stall_usage_numerator": transaction_stalls,
        "stall_usage_denominator": active_stalls,
        "cash_txn_share_bp": ratio_bp(cash_txns, all_txns),
        "cash_txn_numerator": cash_txns,
        "cash_txn_denominator": all_txns,
        "price_list_maintenance_bp": ratio_bp(maintained, active_stalls),
        "price_list_numerator": maintained,
        "price_list_denominator": active_stalls,
    }
