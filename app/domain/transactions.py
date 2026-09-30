"""交易的读端点：列表（§3.7）与详情（§3.8），以及**组装函数** `transaction_payload()`（`REQ-021`、`REQ-031`）。

> **为什么单独成文件**：`Q-16` 的按语义拆分 —— 原 `app/domain/pricing.py` 到 444 行
> （超 `quality-gates` §1.2 的 400 行阈值），而它同时装着**计价/改价的写路径**与**交易的读端点**。

**组装函数只有一个**：`transaction_payload()` 既服务 §3.6 的响应（`create_transaction` 复用，
在 `pricing.py`），也服务 §3.8 的详情。若把读端点搬去 `app/api/`，同一个交易行就会被两处各组装一次 ——
那是漂移的种子，故读写分离时**组装函数跟着读侧走，写侧 import 它**（`pricing` → `transactions` 单向依赖）。

三条纪律：

1. **摊位边界在 SQL 里**（`REQ-032` / `AC-021`）：列表与详情都带 `stall_id = 会话绑定摊位`，
   不从请求里接受摊位参数；越权读 → `MT-1004`（403），**先判存在再判归属**（见 `transaction_detail`）。
2. **`total` 不受 `limit` 影响**（§3.7）：分页对象的 `total` 若也被截断，调用方就不知道"还有多少没取到"。
3. **不发明错误形态**（`RL-1`）：§3.7 声明的可能错误只有 `MT-1005`/`MT-1004`，故 `limit` 超上限是
   **夹到上限**而不是 422；只有无法解释为整数才用通用码 `MT-1008`（`_parse_limit`）。
"""

from __future__ import annotations

import sqlite3

from .. import TradeError
from .catalog import parse_business_date


def transaction_payload(conn: sqlite3.Connection, txn: sqlite3.Row) -> dict:
    """把交易行与明细组装成契约 §3.6 / §3.8 的响应体。"""
    items = conn.execute(
        """
        SELECT id, product_id, category_id, weight_grams, original_unit_price_cents,
               final_unit_price_cents, amount_cents, price_changed, is_round_off
        FROM transaction_item WHERE transaction_id = ? ORDER BY id
        """,
        (txn["id"],),
    ).fetchall()
    return {
        "transaction_no": txn["transaction_no"],
        "status": txn["status"],
        "business_date": txn["business_date"],
        "total_amount_cents": txn["total_amount_cents"],
        "received_amount_cents": txn["received_amount_cents"],
        "round_off_cents": txn["round_off_cents"],
        "origin": txn["origin"],
        "created_at": txn["created_at"],
        "items": [dict(row) for row in items],
    }


def find_transaction(conn: sqlite3.Connection, transaction_no: str) -> sqlite3.Row:
    """按交易号取交易；不存在 → `MT-1009`。"""
    txn = conn.execute(
        'SELECT * FROM "transaction" WHERE transaction_no = ?', (transaction_no,)
    ).fetchone()
    if txn is None:
        raise TradeError("MT-1009", f"交易不存在：{transaction_no}", {"transaction_no": transaction_no})
    return txn


def require_stall_scope(txn: sqlite3.Row, stall_id: int) -> None:
    """秤端数据边界（`REQ-032` / `AC-021` / `MT-1004`）：非本摊位的数据一律 403。"""
    if txn["stall_id"] != stall_id:
        raise TradeError(
            "MT-1004",
            "不得访问非本摊位的交易数据",
            {"transaction_no": txn["transaction_no"]},
        )


# ---------------------------------------------------------------------------
# §3.7 交易列表 / §3.8 交易详情（秤端读端点；写侧在上面）
# ---------------------------------------------------------------------------


#: 契约 §3.7：`limit` 默认 50、上限 200。
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


def _parse_limit(raw) -> int:
    """解析契约 §3.7 的 `limit`。

    规则（**只有一条**，免得两个边界各判一次）：`limit` 是"返回条数上限"，
    故可解释为整数时**夹到 `[1, MAX_LIST_LIMIT]`** —— 契约写的是"上限 200"（约束返回条数），
    不是"超过 200 就拒绝"；且 §3.7 声明的可能错误只有 `MT-1005`/`MT-1004`，
    在此发明 422 就等于在契约之外新增错误形态（`RL-1`）。
    无法解释为整数（`limit=abc`）才按通用码 `MT-1008` 拒绝。
    """
    if raw is None or raw == "":
        return DEFAULT_LIST_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise TradeError("MT-1008", "`limit` 必须是整数", {"field": "limit"}) from exc
    return max(1, min(value, MAX_LIST_LIMIT))


def list_transactions(conn: sqlite3.Connection, stall_id: int, business_date, limit_raw=None) -> dict:
    """本摊位交易列表（契约 §3.7）→ `{"total": n, "items": [...]}`。

    `total` 是**过滤后的总条数**（不受 `limit` 影响）：分页对象里 `total` 若也被 `limit` 截断，
    调用方就无法知道"还有多少没取到"，分页就失去意义。
    摊位过滤（`REQ-032` / `AC-021`）与 `limit` 都在 SQL 层完成，不先全表取回再在 Python 里切。
    """
    filters = ["stall_id = ?"]
    params: list = [stall_id]
    if business_date is not None:
        filters.append("business_date = ?")
        params.append(parse_business_date(business_date))

    where = " AND ".join(filters)
    total = int(
        conn.execute(f'SELECT COUNT(*) AS n FROM "transaction" WHERE {where}', params).fetchone()["n"]
    )
    rows = conn.execute(
        f'SELECT * FROM "transaction" WHERE {where} ORDER BY id DESC LIMIT ?',
        [*params, _parse_limit(limit_raw)],
    ).fetchall()
    return {"total": total, "items": [transaction_payload(conn, row) for row in rows]}


def transaction_detail(conn: sqlite3.Connection, stall_id: int, transaction_no: str) -> dict:
    """交易详情与凭证数据（契约 §3.8）→ §2.8 顶层 + `items` + `payments` + `printable: false`。

    顺序有语义：**先判存在（`MT-1009` 404）再判归属（`MT-1004` 403）** ——
    颠倒过来会让"不存在的交易号"回 403，而它其实连存在性都不该被确认（`T-008`/`T-009` 用例锁定两者）。
    """
    txn = find_transaction(conn, transaction_no)
    require_stall_scope(txn, stall_id)

    payments = conn.execute(
        """
        SELECT payment_no, method, amount_cents, status, callback_no, confirmed_at, operator, created_at
        FROM payment WHERE transaction_id = ? ORDER BY id
        """,
        (txn["id"],),
    ).fetchall()
    payload = transaction_payload(conn, txn)
    payload["payments"] = [dict(row) for row in payments]
    # 契约 §3.8：固定字段，明示不打印（REQ-012 只要求屏幕展示）
    payload["printable"] = False
    return payload
