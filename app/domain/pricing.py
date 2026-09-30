"""计价、改价与抹零（`REQ-005`~`REQ-008`、`REQ-027`）。

契约 §3.6 创建交易并计价、§3.9 改价/抹零。本模块**不依赖 HTTP**。

三条口径（改动前先读，都是被测过的行为，不是随手写的）：

1. **计价取整**（`data-model.md` §0 / §7 注①）：`金额(分) = round(单价(分/公斤) × 重量(克) ÷ 1000)`，
   四舍五入到分。这里用 `(a + b//2) // b` 实现**四舍五入**，不用 Python 内置 `round`
   —— 后者是**银行家舍入**（`round(0.5) == 0`），与契约写的"四舍五入"不是同一件事。
2. **金额与重量一律整数**（分 / 克）：全程 `int`，**不引入浮点**（浮点会让对账等式时对时错）。
3. **抹零不是改价**（`REQ-008` / `AC-008`）：抹零只减总额、**不计入**标价一致率 ——
   落库时 `is_round_off = 1` 而 `price_changed = 0`；改价则相反。标价一致率只看 `price_changed`。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .. import TradeError
from ..db import now_iso
from .audit import write_audit

#: 单笔明细行数上限（契约 §3.6：`items` 长度 1~20）
MAX_ITEMS = 20
#: 重量上限：50 公斤（`REQ-027` / `MT-1002`；`data-model.md` §2.9 的 CHECK 同值）
MAX_WEIGHT_GRAMS = 50_000
#: 单笔改价需确认的幅度（`REQ-007` / `MT-1011`）：超过 50% 才要求回传确认，**不阻止**操作
CONFIRM_THRESHOLD_PERCENT = 50


def half_up_div(numerator: int, denominator: int) -> int:
    """整数四舍五入除法（不使用浮点）。"""
    return (numerator + denominator // 2) // denominator


def price_amount(unit_price_cents: int, weight_grams: int) -> int:
    """按单价（分/公斤）与重量（克）算金额（分）。"""
    return half_up_div(unit_price_cents * weight_grams, 1000)


def _parse_items(body) -> list[tuple[int, int]]:
    """校验 `items` 并返回 `[(product_id, weight_grams)]`（契约 §3.6）。"""
    items = body.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise TradeError(
            "MT-1008",
            f"`items` 必须是长度 1~{MAX_ITEMS} 的数组",
            {"field": "items", "max_items": MAX_ITEMS},
        )

    parsed: list[tuple[int, int]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise TradeError("MT-1008", f"`items[{index}]` 必须是对象", {"index": index})
        product_id = item.get("product_id")
        weight_grams = item.get("weight_grams")
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise TradeError("MT-1008", f"`items[{index}].product_id` 必须是整数", {"index": index})
        if not isinstance(weight_grams, int) or isinstance(weight_grams, bool):
            raise TradeError("MT-1008", f"`items[{index}].weight_grams` 必须是整数", {"index": index})
        if weight_grams <= 0 or weight_grams > MAX_WEIGHT_GRAMS:
            # REQ-027 / MT-1002：越界即拒绝，**不创建任何交易**（由调用方在写库前抛出保证）
            raise TradeError(
                "MT-1002",
                "重量超出允许范围",
                {"weight_grams": weight_grams, "max_grams": MAX_WEIGHT_GRAMS},
            )
        parsed.append((product_id, weight_grams))
    return parsed


def _item_fingerprint(conn: sqlite3.Connection, transaction_id: int) -> list[tuple[int, int]]:
    """交易明细指纹（用于判定"幂等键复用但请求体不同"，契约 §4 `MT-1012`(a)）。

    指纹取自**已落库的明细**而不是另存一份请求体副本：少存一份数据就少一个漂移点。
    """
    rows = conn.execute(
        "SELECT product_id, weight_grams FROM transaction_item WHERE transaction_id = ? "
        "ORDER BY product_id, weight_grams",
        (transaction_id,),
    ).fetchall()
    return [(row["product_id"], row["weight_grams"]) for row in rows]


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


def _next_transaction_no(conn: sqlite3.Connection, business_date: str) -> str:
    """生成交易号 `T-YYYYMMDD-NNNN`（按营业日流水号递增）。"""
    prefix = f"T-{business_date.replace('-', '')}"
    row = conn.execute(
        'SELECT transaction_no FROM "transaction" WHERE transaction_no LIKE ? '
        "ORDER BY transaction_no DESC LIMIT 1",
        (f"{prefix}-%",),
    ).fetchone()
    sequence = int(row["transaction_no"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}-{sequence:04d}"


def create_transaction(
    conn: sqlite3.Connection, stall: sqlite3.Row, body, idempotency_key: str | None
) -> tuple[dict, bool]:
    """创建交易并计价（契约 §3.6）；返回 `(响应体, 是否为幂等命中)`。"""
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")
    # 契约 §3.6：请求头 `Idempotency-Key` 必填（不传时取请求体字段，见 §2.8）
    key = idempotency_key or body.get("client_idempotency_key")
    if not isinstance(key, str) or not key or len(key) > 64:
        raise TradeError(
            "MT-1008", "缺少幂等键（请求头 Idempotency-Key，长度 ≤64）", {"header": "Idempotency-Key"}
        )

    parsed = _parse_items(body)
    fingerprint = sorted(parsed)
    stall_id = stall["stall_id"]

    # 幂等：同摊位同键 → 命中返回首次结果；键相同但请求体不同 → MT-1012（契约 §1.7 / §4）
    existing = conn.execute(
        'SELECT * FROM "transaction" WHERE stall_id = ? AND client_idempotency_key = ?',
        (stall_id, key),
    ).fetchone()
    if existing is not None:
        if _item_fingerprint(conn, existing["id"]) == fingerprint:
            return transaction_payload(conn, existing), True
        raise TradeError(
            "MT-1012",
            "幂等键已用于另一笔请求体不同的交易",
            {"idempotency_key": key, "transaction_no": existing["transaction_no"]},
        )

    business_date = date.today().isoformat()
    lines: list[tuple[int, int, int, int, int, int]] = []
    for product_id, weight_grams in parsed:
        product = conn.execute(
            "SELECT id, stall_id, category_id, status FROM product WHERE id = ?", (product_id,)
        ).fetchone()
        if product is None or product["status"] != "active":
            raise TradeError("MT-1009", f"商品不存在或已停用：{product_id}", {"product_id": product_id})
        if product["stall_id"] != stall_id:
            raise TradeError(
                "MT-1004", "不得为本摊位之外的商品计价", {"product_id": product_id}
            )
        price = conn.execute(
            "SELECT unit_price_cents FROM price_item WHERE stall_id = ? AND business_date = ? AND product_id = ?",
            (stall_id, business_date, product_id),
        ).fetchone()
        if price is None:
            # MT-1006：**不得以 0 元成交**（契约 §4 原文）
            raise TradeError(
                "MT-1006",
                "该商品当日没有价目表，请先设价",
                {"product_id": product_id, "business_date": business_date},
            )
        unit_price = int(price["unit_price_cents"])
        lines.append(
            (
                product_id,
                int(product["category_id"]),
                weight_grams,
                unit_price,
                unit_price,
                price_amount(unit_price, weight_grams),
            )
        )

    total = sum(line[5] for line in lines)
    transaction_no = _next_transaction_no(conn, business_date)
    cursor = conn.execute(
        """
        INSERT INTO "transaction"
            (transaction_no, stall_id, business_date, status, total_amount_cents,
             round_off_cents, origin, client_idempotency_key)
        VALUES (?, ?, ?, 'priced', ?, 0, 'online', ?)
        """,
        (transaction_no, stall_id, business_date, total, key),
    )
    transaction_id = int(cursor.lastrowid)
    for product_id, category_id, weight_grams, original, final, amount in lines:
        conn.execute(
            """
            INSERT INTO transaction_item
                (transaction_id, product_id, category_id, weight_grams, original_unit_price_cents,
                 final_unit_price_cents, amount_cents, price_changed, is_round_off)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (transaction_id, product_id, category_id, weight_grams, original, final, amount),
        )
    conn.commit()

    txn = conn.execute('SELECT * FROM "transaction" WHERE id = ?', (transaction_id,)).fetchone()
    return transaction_payload(conn, txn), False


def _recompute_total(conn: sqlite3.Connection, transaction_id: int) -> int:
    """总额 = 明细金额之和 − 抹零（抹零累计在 `transaction.round_off_cents`）。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS items_total FROM transaction_item WHERE transaction_id = ?",
        (transaction_id,),
    ).fetchone()
    round_off = conn.execute(
        'SELECT round_off_cents FROM "transaction" WHERE id = ?', (transaction_id,)
    ).fetchone()["round_off_cents"]
    total = max(0, int(row["items_total"]) - int(round_off))
    conn.execute(
        'UPDATE "transaction" SET total_amount_cents = ?, updated_at = ? WHERE id = ?',
        (total, now_iso(), transaction_id),
    )
    return total


def change_price(conn: sqlite3.Connection, stall: sqlite3.Row, transaction_no: str, body) -> dict:
    """改价或抹零并留痕（契约 §3.9）；返回 `{transaction_no, total_amount_cents, audit_event}`。"""
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")

    txn = find_transaction(conn, transaction_no)
    require_stall_scope(txn, stall["stall_id"])
    if txn["status"] != "priced":
        # MT-1001：已收款 / 已冲正的交易不允许再改价（`data-model.md` §4.1 流转表）
        raise TradeError(
            "MT-1001", "交易不在可改价状态", {"status": txn["status"], "transaction_no": transaction_no}
        )

    item_id = body.get("item_id")
    if not isinstance(item_id, int) or isinstance(item_id, bool):
        raise TradeError("MT-1008", "`item_id` 必须是整数", {"field": "item_id"})
    item = conn.execute(
        "SELECT * FROM transaction_item WHERE id = ? AND transaction_id = ?", (item_id, txn["id"])
    ).fetchone()
    if item is None:
        raise TradeError("MT-1009", f"明细行不存在：{item_id}", {"item_id": item_id})

    final_price = body.get("final_unit_price_cents")
    round_off = body.get("round_off_cents")
    if (final_price is None) == (round_off is None):
        # 契约 §3.9：两者**二选一**（都不给 → 无从改；都给 → 语义冲突）
        raise TradeError(
            "MT-1008",
            "`final_unit_price_cents` 与 `round_off_cents` 必须且只能给一个",
            {"field": ["final_unit_price_cents", "round_off_cents"]},
        )

    actor = f"stall-{stall['stall_no']}"
    if final_price is not None:
        if not isinstance(final_price, int) or isinstance(final_price, bool) or final_price < 0:
            raise TradeError("MT-1008", "`final_unit_price_cents` 必须是 ≥0 的整数", {"field": "final_unit_price_cents"})
        original = int(item["original_unit_price_cents"])
        if original > 0 and abs(final_price - original) * 100 > original * CONFIRM_THRESHOLD_PERCENT:
            if body.get("confirm_over_threshold") is not True:
                # MT-1011：只要求确认，**不阻止**（REQ-007；前端确认后带 true 重发即可）
                raise TradeError(
                    "MT-1011",
                    "单笔改价幅度超过 50%，请确认后重试",
                    {
                        "original_unit_price_cents": original,
                        "final_unit_price_cents": final_price,
                        "confirm_over_threshold": True,
                    },
                )
        amount = price_amount(final_price, int(item["weight_grams"]))
        conn.execute(
            "UPDATE transaction_item SET final_unit_price_cents = ?, amount_cents = ?, "
            "price_changed = 1, is_round_off = 0 WHERE id = ?",
            (final_price, amount, item_id),
        )
        payload = {
            "kind": "price_change",
            "item_id": item_id,
            "original_unit_price_cents": original,
            "final_unit_price_cents": final_price,
            "weight_grams": int(item["weight_grams"]),
            "amount_cents": amount,
        }
    else:
        if not isinstance(round_off, int) or isinstance(round_off, bool) or round_off < 0:
            raise TradeError("MT-1008", "`round_off_cents` 必须是 ≥0 的整数", {"field": "round_off_cents"})
        conn.execute(
            'UPDATE "transaction" SET round_off_cents = round_off_cents + ? WHERE id = ?',
            (round_off, txn["id"]),
        )
        # 抹零：只标记 is_round_off，**不置 price_changed**（REQ-008 / AC-008）
        conn.execute("UPDATE transaction_item SET is_round_off = 1 WHERE id = ?", (item_id,))
        payload = {"kind": "round_off", "item_id": item_id, "round_off_cents": round_off}

    total = _recompute_total(conn, txn["id"])
    write_audit(
        conn,
        event_type="price_change",
        ref_table="transaction_item",
        ref_id=item_id,
        payload=payload,
        actor=actor,
        stall_id=stall["stall_id"],
    )
    conn.commit()
    return {"transaction_no": transaction_no, "total_amount_cents": total, "audit_event": "price_change"}
