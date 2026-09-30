"""日聚合、按需重算、结算单与对账等式（`REQ-018`~`REQ-020`；契约 §3.26~§3.29）。

本模块是**快照与单据的落地处**（写 `daily_aggregate` / `settlement`），
而"一天一个摊位的数怎么算"的唯一实现在 `metrics.stall_day_facts()` —— **本模块只调用它**，
不自己再写一遍聚合 SQL。这样 §3.25/§3.12 的现场汇总与 §3.26 的快照**不可能算出两个数**
（`T-020` 交接的那条纪律）。

===========================================================================
§3.29 对账等式的口径（**规格级判断，改动前务必读完**）
===========================================================================
契约 §3.29 要求核验「订单总额 = 支付流水 = 分账明细」，且 `data-model.md` §1 末注写明
**由 `transaction` / `payment` / `transaction_item` 现场计算、不落表**。契约没有写"算哪些交易"，
这个口径决定了这个等式是**有意义的报警**还是**永远亮的红灯**：

- **口径 = 已结算交易**（`status IN ('paid','refunded')`，即"钱真的收过"）。
  理由一：等式若把在途（`priced` 未收款）也算进去，那么**任何一笔正在等付款的交易都会让市场显示
  "不平"** —— 对账页将长期是红的，真正的记账错误反而淹在里面，等于没有对账。
  理由二：`分账明细`（分账基数）是"已经收到的钱怎么分"，没收到的钱没有可分之物。
  理由三：与 §3.30 现金占比的口径分工一致 —— 那里分母明确是"当日**全部**走秤笔数"（含在途），
  说明**契约在需要"全部"的地方是会写出来的**；§3.29 没写"全部"，而它要的是一个**恒等式**。
  `refunded` 一并计入：钱收过（`payment` 有成功流水）、也分过账，退货是**另一条流**（冲减在
  §2.15 的 `refund_amount_cents`），把它排除才会让"支付流水有、分账没有"这种真实不一致消失。
- `order_total_cents` = 已结算交易的 `total_amount_cents` 之和；
- `payment_total_cents` = 这些交易的**成功**支付流水 `amount_cents` 之和；
- `split_total_cents` = 这些交易的**分账基数** = `SUM(transaction_item.amount_cents) − SUM(transaction.round_off_cents)`。
  **抹零必须扣掉**：`transaction.total_amount_cents = 明细之和 − round_off_cents`
  （`pricing._recompute_total`），而抹零不属于可分账金额；不扣的话**每一笔抹零交易都会永久显示
  "不平"**，那正是"账没错、报警常亮"的典型坏味道。
- `balanced` = 三者完全相等；`diff_cents` = 三者最大值 − 最小值
  （取"离平衡最远的那条腿"，比只报 `order − payment` 更能指向问题）。

**这条口径已由测试双向锁死**：`test_admin::test_reconciliation_equation_balanced`（有已收款交易 →
三线相等）与 `test_offline::test_sync_backfills_...`（两笔 `priced` 未收款补传交易 → `balanced=True`）。
两者在"只算已结算"下**同时成立**，且与契约不冲突 —— 故 **`test_offline:211` 的期望是对的，
无需改测试、也无需改契约**（详见本轮报告）。
"""

from __future__ import annotations

import sqlite3

from .. import TradeError
from ..db import now_iso
from .catalog import STALL_NO_MAX_LEN, parse_business_date
from .commission import find_effective_rule
from .metrics import recompute_stall_credit, stall_day_facts

#: 对账等式的口径常量（改它等于改对账语义，先读模块 docstring）
SETTLED_STATUSES = ("paid", "refunded")


def _active_stall_ids(conn: sqlite3.Connection, stall_no: str | None) -> tuple[list[int], str | None]:
    """聚合目标摊位：不传 `stall_no` → 全部在营摊位；传了但查不到 → `MT-1009`。"""
    if stall_no is None:
        rows = conn.execute("SELECT id FROM stall WHERE status = 'active' ORDER BY id").fetchall()
        return [int(row["id"]) for row in rows], None
    if not isinstance(stall_no, str) or not 1 <= len(stall_no) <= STALL_NO_MAX_LEN:
        raise TradeError(
            "MT-1008",
            f"`stall_no` 必须是长度 1~{STALL_NO_MAX_LEN} 的字符串",
            {"field": "stall_no", "max_length": STALL_NO_MAX_LEN},
        )
    row = conn.execute("SELECT id FROM stall WHERE stall_no = ?", (stall_no,)).fetchone()
    if row is None:
        raise TradeError("MT-1009", f"摊位不存在：{stall_no}", {"stall_no": stall_no})
    return [int(row["id"])], stall_no


# ---------------------------------------------------------------------------
# §3.26 日终聚合与按需重算
# ---------------------------------------------------------------------------


def aggregate_day(conn: sqlite3.Connection, body) -> dict:
    """契约 §3.26：日终聚合 / 退货冲正后按需重算 → 200 `{business_date, stalls_aggregated, revision}`。

    - 该营业日**必须有一条生效佣金口径**，否则 `MT-1013`（409）——
      佣金算不出来就不该产出一份"看起来完整"的聚合快照（契约 §4 明示）。
      注意"无口径"与"佣金为 0"是两件事，判断用 `find_effective_rule()` 的事实，不靠结果值反推。
    - 重算**生成新 `revision` 并保留旧行**（`REQ-018` / `Q-10` 可追溯、不可物理删除）；
      写入前把该摊位该日的旧行 `is_current` 置 0，保证"有且仅有一条 `is_current = 1`"（§2.15）。
    - 响应 `revision` = **本次写入各摊位版本的较大者**（各摊位各自递增，通常一致；
      只聚合单摊位时它可能与别的摊位不同，取最大值即"这次结算到第几版"）。
    """
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")
    day = parse_business_date(body.get("business_date"))
    stall_ids, _ = _active_stall_ids(conn, body.get("stall_no"))

    if find_effective_rule(conn, day) is None:
        raise TradeError(
            "MT-1013",
            f"该营业日没有任何生效的佣金口径：{day}",
            {"business_date": day, "hint": "先调用 §3.24 配置佣金口径"},
        )

    revision = 0
    for stall_id in stall_ids:
        facts = stall_day_facts(conn, stall_id, day)
        previous = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS r FROM daily_aggregate WHERE stall_id = ? AND business_date = ?",
            (stall_id, day),
        ).fetchone()["r"]
        next_revision = int(previous) + 1
        revision = max(revision, next_revision)
        conn.execute(
            "UPDATE daily_aggregate SET is_current = 0 WHERE stall_id = ? AND business_date = ?",
            (stall_id, day),
        )
        conn.execute(
            """
            INSERT INTO daily_aggregate
                (stall_id, business_date, revision, is_current, txn_count, gross_amount_cents,
                 refund_amount_cents, commission_amount_cents, recomputed_at)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                stall_id,
                day,
                next_revision,
                facts["txn_count"],
                facts["gross_amount_cents"],
                facts["refund_amount_cents"],
                facts["commission_amount_cents"],
                now_iso(),
            ),
        )
        # §2.17：信用档案随日聚合一并重算（同一口径，见 metrics.recompute_stall_credit）
        recompute_stall_credit(conn, stall_id, day)

    conn.commit()
    return {"business_date": day, "stalls_aggregated": len(stall_ids), "revision": revision}


# ---------------------------------------------------------------------------
# §3.27 生成结算单 / §3.28 查询结算单
# ---------------------------------------------------------------------------


def _period_days(period_start: str, period_end: str) -> list[str]:
    from datetime import date, timedelta

    start, end = date.fromisoformat(period_start), date.fromisoformat(period_end)
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def create_settlement(conn: sqlite3.Connection, body) -> dict:
    """契约 §3.27：依据**当前有效**的日聚合生成结算单 → 201。

    - 期内每个营业日都必须有 `is_current = 1` 的日聚合，缺一天就 `MT-1010`（409）——
      "先做日终聚合再结算"是契约明写的处置建议（§4）。
    - 金额 = 期内各日聚合之和（`data-model.md` §2.16）；**不现场重算**，
      否则结算单就不再是"依据日聚合生成"的了（`AC-023` 要求两者一致）。
    - 同一（摊位，期间）再次生成 → `version + 1`，**旧版本保留**（`Q-10`）。
    """
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")
    stall_no = body.get("stall_no")
    if not isinstance(stall_no, str) or not 1 <= len(stall_no) <= STALL_NO_MAX_LEN:
        raise TradeError(
            "MT-1008",
            f"`stall_no` 必须是长度 1~{STALL_NO_MAX_LEN} 的字符串",
            {"field": "stall_no", "max_length": STALL_NO_MAX_LEN},
        )
    period_start = parse_business_date(body.get("period_start"), field="period_start")
    period_end = parse_business_date(body.get("period_end"), field="period_end")
    if period_end < period_start:
        raise TradeError(
            "MT-1008",
            "`period_end` 必须 ≥ `period_start`",
            {"field": "period_end", "period_start": period_start},
        )

    stall = conn.execute("SELECT id FROM stall WHERE stall_no = ?", (stall_no,)).fetchone()
    if stall is None:
        raise TradeError("MT-1009", f"摊位不存在：{stall_no}", {"stall_no": stall_no})
    stall_id = int(stall["id"])

    missing = [
        day
        for day in _period_days(period_start, period_end)
        if conn.execute(
            "SELECT 1 FROM daily_aggregate WHERE stall_id = ? AND business_date = ? AND is_current = 1",
            (stall_id, day),
        ).fetchone()
        is None
    ]
    if missing:
        raise TradeError(
            "MT-1010",
            f"期内存在未聚合的营业日：{'、'.join(missing[:5])}",
            {"period_start": period_start, "period_end": period_end, "missing_days": missing},
        )

    totals = conn.execute(
        """
        SELECT COALESCE(SUM(gross_amount_cents), 0) AS gross_amount_cents,
               COALESCE(SUM(commission_amount_cents), 0) AS commission_amount_cents
        FROM daily_aggregate
        WHERE stall_id = ? AND business_date BETWEEN ? AND ? AND is_current = 1
        """,
        (stall_id, period_start, period_end),
    ).fetchone()

    version = (
        int(
            conn.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS v FROM settlement
                WHERE stall_id = ? AND period_start = ? AND period_end = ?
                """,
                (stall_id, period_start, period_end),
            ).fetchone()["v"]
        )
        + 1
    )
    settlement_no = f"ST-{stall_no}-{period_start.replace('-', '')}-{period_end.replace('-', '')}-{version}"
    # `settlement_no` 有长度 ≤32 的约束（§2.16）：超长即说明上游取值未被约束，直接拒绝而不是截断
    # （截断会造出两个不同摊位指向同一单号的假唯一性）
    if len(settlement_no) > 32:
        raise TradeError(
            "MT-1008", f"结算单号超长（{len(settlement_no)}>32），请检查摊位号格式", {"settlement_no": settlement_no}
        )

    conn.execute(
        """
        INSERT INTO settlement
            (settlement_no, stall_id, period_start, period_end, gross_amount_cents,
             commission_amount_cents, version, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            settlement_no,
            stall_id,
            period_start,
            period_end,
            int(totals["gross_amount_cents"]),
            int(totals["commission_amount_cents"]),
            version,
            now_iso(),
        ),
    )
    conn.commit()
    return {
        "settlement_no": settlement_no,
        "version": version,
        "gross_amount_cents": int(totals["gross_amount_cents"]),
        "commission_amount_cents": int(totals["commission_amount_cents"]),
    }


def list_settlements(
    conn: sqlite3.Connection,
    *,
    stall_no: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> list[dict]:
    """契约 §3.28：查询结算单（**含全部 `version`，旧版本不删**）。"""
    where: list[str] = []
    params: list = []
    if stall_no is not None:
        where.append("s.stall_no = ?")
        params.append(stall_no)
    if period_start is not None:
        where.append("st.period_end >= ?")
        params.append(parse_business_date(period_start, field="period_start"))
    if period_end is not None:
        where.append("st.period_start <= ?")
        params.append(parse_business_date(period_end, field="period_end"))
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    rows = conn.execute(
        f"""
        SELECT st.settlement_no, s.stall_no, st.period_start, st.period_end,
               st.gross_amount_cents, st.commission_amount_cents, st.version, st.generated_at
        FROM settlement st
        JOIN stall s ON s.id = st.stall_id
        {clause}
        ORDER BY st.period_start, st.period_end, st.version
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# §3.29 对账等式（口径见模块 docstring）
# ---------------------------------------------------------------------------


def reconcile(conn: sqlite3.Connection, *, business_date: str | None, stall_no: str | None = None) -> dict:
    """契约 §3.29：现场计算「订单总额 = 支付流水 = 分账明细」。

    只算**已结算交易**（见模块 docstring 的三条理由）；`diff_cents` 取三条腿的最大−最小。
    """
    if business_date is None:
        raise TradeError("MT-1008", "`business_date` 必填", {"field": "business_date"})
    day = parse_business_date(business_date)

    scope = ""
    params: list = [*SETTLED_STATUSES, day]
    if stall_no is not None:
        row = conn.execute("SELECT id FROM stall WHERE stall_no = ?", (stall_no,)).fetchone()
        if row is None:
            raise TradeError("MT-1009", f"摊位不存在：{stall_no}", {"stall_no": stall_no})
        scope = "AND t.stall_id = ?"
        params.append(int(row["id"]))
    placeholders = ", ".join("?" for _ in SETTLED_STATUSES)

    order_total = int(
        conn.execute(
            f"""
            SELECT COALESCE(SUM(t.total_amount_cents), 0) AS v FROM "transaction" t
            WHERE t.status IN ({placeholders}) AND t.business_date = ? {scope}
            """,
            tuple(params),
        ).fetchone()["v"]
    )
    payment_total = int(
        conn.execute(
            f"""
            SELECT COALESCE(SUM(p.amount_cents), 0) AS v
            FROM payment p JOIN "transaction" t ON t.id = p.transaction_id
            WHERE t.status IN ({placeholders}) AND t.business_date = ? AND p.status = 'success' {scope}
            """,
            tuple(params),
        ).fetchone()["v"]
    )
    # 分账基数 = 明细之和 − 抹零累计（抹零不属于可分账金额，见模块 docstring）
    items_total = int(
        conn.execute(
            f"""
            SELECT COALESCE(SUM(ti.amount_cents), 0) AS v
            FROM transaction_item ti JOIN "transaction" t ON t.id = ti.transaction_id
            WHERE t.status IN ({placeholders}) AND t.business_date = ? {scope}
            """,
            tuple(params),
        ).fetchone()["v"]
    )
    round_off = int(
        conn.execute(
            f"""
            SELECT COALESCE(SUM(t.round_off_cents), 0) AS v FROM "transaction" t
            WHERE t.status IN ({placeholders}) AND t.business_date = ? {scope}
            """,
            tuple(params),
        ).fetchone()["v"]
    )
    split_total = items_total - round_off

    diff = max(order_total, payment_total, split_total) - min(order_total, payment_total, split_total)
    return {
        "business_date": day,
        "order_total_cents": order_total,
        "payment_total_cents": payment_total,
        "split_total_cents": split_total,
        "balanced": diff == 0,
        "diff_cents": diff,
    }
