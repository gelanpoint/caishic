"""收款与支付回调（`REQ-009`~`REQ-012`、`REQ-026`、`REQ-029`）。

契约 §3.10 确认收款、§3.17 支付回调（Mock）。

**现金与收款码共用同一张 `payment` 表**（`REQ-010` / `data-model.md` §2.10）——
`method` 区分二者，不为现金另建一张表：另建就等于把"同一件事"记两处，对账时必然要再写一次合并逻辑。

幂等语义（契约 §1.7）：回调重复送达 → **返回 200 + `is_duplicate: true`，不重复记账**。
这里"命中"的判定用 `payment.callback_no` 上的唯一索引（数据库层兜底），
而不是"先查再插"——先查再插在并发下会漏。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .. import TradeError
from ..db import now_iso
from .audit import write_audit
from .pricing import find_transaction, require_stall_scope

#: 操作人长度上限（契约 §3.10：`operator` ≤32）
OPERATOR_MAX_LEN = 32
#: 收款流水号长度上限（`data-model.md` §2.10）
PAYMENT_NO_MAX_LEN = 32


def _next_payment_no(conn: sqlite3.Connection, business_date: str) -> str:
    """生成收款流水号 `PAY-YYYYMMDD-NNNNNN`。"""
    prefix = f"PAY-{business_date.replace('-', '')}"
    row = conn.execute(
        "SELECT payment_no FROM payment WHERE payment_no LIKE ? ORDER BY payment_no DESC LIMIT 1",
        (f"{prefix}-%",),
    ).fetchone()
    sequence = int(row["payment_no"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}-{sequence:06d}"


def has_success_payment(conn: sqlite3.Connection, transaction_id: int) -> bool:
    """该交易是否已有一条**成功**流水（`REQ-026`：只允许一条交易记录与一次佣金）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = ? AND status = 'success'",
        (transaction_id,),
    ).fetchone()
    return int(row["n"]) > 0


def pay_transaction(
    conn: sqlite3.Connection, stall: sqlite3.Row, transaction_no: str, body, idempotency_key: str | None
) -> tuple[dict, int]:
    """确认收款；返回 `(响应体, HTTP 状态)`（契约 §3.10：现金 200 / 收款码 202）。"""
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")

    txn = find_transaction(conn, transaction_no)
    require_stall_scope(txn, stall["stall_id"])

    method = body.get("method")
    if method not in {"cash", "qr"}:
        raise TradeError("MT-1008", "`method` 必须是 cash 或 qr", {"field": "method"})
    if not idempotency_key:
        raise TradeError(
            "MT-1008", "缺少幂等键（请求头 Idempotency-Key，长度 ≤64）", {"header": "Idempotency-Key"}
        )
    if len(idempotency_key) > 64:
        raise TradeError("MT-1008", "幂等键长度必须 ≤64", {"header": "Idempotency-Key"})

    # 可收款状态：priced（首收）或 payment_failed（改记现金 / 重试，见 data-model.md §4.1 流转表）
    if txn["status"] not in {"priced", "payment_failed"}:
        raise TradeError(
            "MT-1001", "交易不在可收款状态", {"status": txn["status"], "transaction_no": transaction_no}
        )

    business_date = txn["business_date"] or date.today().isoformat()
    payment_no = _next_payment_no(conn, business_date)

    if method == "cash":
        operator = body.get("operator")
        if not isinstance(operator, str) or not 1 <= len(operator) <= OPERATOR_MAX_LEN:
            raise TradeError(
                "MT-1008",
                f"现金收款必须提供 `operator`（长度 1~{OPERATOR_MAX_LEN}）",
                {"field": "operator"},
            )
        if has_success_payment(conn, txn["id"]):
            raise TradeError(
                "MT-1001", "该交易已有成功流水，不得重复收款", {"transaction_no": transaction_no}
            )
        confirmed_at = now_iso()
        conn.execute(
            """
            INSERT INTO payment (payment_no, transaction_id, method, amount_cents, status, confirmed_at, operator)
            VALUES (?, ?, 'cash', ?, 'success', ?, ?)
            """,
            (payment_no, txn["id"], txn["total_amount_cents"], confirmed_at, operator),
        )
        conn.execute(
            'UPDATE "transaction" SET status = ?, received_amount_cents = ?, updated_at = ? WHERE id = ?',
            ("paid", txn["total_amount_cents"], confirmed_at, txn["id"]),
        )
        conn.commit()
        return (
            {
                "payment_no": payment_no,
                "method": "cash",
                "status": "success",
                "confirmed_at": confirmed_at,
                "transaction_status": "paid",
            },
            200,
        )

    # 收款码：置 pending，等 §3.17 回调驱动交易转 paid
    conn.execute(
        "INSERT INTO payment (payment_no, transaction_id, method, amount_cents, status, operator) "
        "VALUES (?, ?, 'qr', ?, 'pending', ?)",
        (payment_no, txn["id"], txn["total_amount_cents"], body.get("operator")),
    )
    conn.commit()
    return (
        {
            "payment_no": payment_no,
            "method": "qr",
            "status": "pending",
            "qr_payload": f"mtpay://pay/{payment_no}",
            # 收款标识只回**脱敏值**（REQ-024 / NFR-012）；库里存的就是掩码形态
            "receiver_token_masked": stall["payment_receiver_token"],
        },
        202,
    )


def apply_payment_callback(conn: sqlite3.Connection, body) -> tuple[dict, int]:
    """支付回调（契约 §3.17，Mock）：`success` 驱动交易转 `paid`；重复送达幂等。"""
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")

    callback_no = body.get("callback_no")
    payment_no = body.get("payment_no")
    result = body.get("result")
    if not isinstance(callback_no, str) or not 1 <= len(callback_no) <= 32:
        raise TradeError("MT-1008", "`callback_no` 必须是长度 1~32 的字符串", {"field": "callback_no"})
    if not isinstance(payment_no, str) or not payment_no:
        raise TradeError("MT-1008", "`payment_no` 必填", {"field": "payment_no"})
    if result not in {"success", "failed", "timeout"}:
        raise TradeError("MT-1008", "`result` 必须是 success / failed / timeout", {"field": "result"})

    payment = conn.execute("SELECT * FROM payment WHERE payment_no = ?", (payment_no,)).fetchone()
    if payment is None:
        raise TradeError("MT-1009", f"支付单号不存在：{payment_no}", {"payment_no": payment_no})

    # 幂等命中：同一 callback_no 已到达过 → 返回首次语义 + 标记，**不重复记账**（REQ-029 / AC-010）
    seen = conn.execute(
        "SELECT * FROM payment_callback_log WHERE callback_no = ?", (callback_no,)
    ).fetchone()
    if seen is not None:
        # `data-model.md` §2.11：`payment_callback_log` 是「**每次**回调的到达记录与幂等命中标记」——
        # 故命中这一次**也要落一行**（`is_duplicate = 1`）。只在首次落库的话，
        # "回调被重复送达了几次"这个事实就丢了，而它正是 AC-010 要留的证据。
        conn.execute(
            "INSERT INTO payment_callback_log (callback_no, payment_id, result, is_duplicate) VALUES (?, ?, ?, 1)",
            (callback_no, payment["id"], result),
        )
        write_audit(
            conn,
            event_type="payment_callback_duplicate_hit",
            # 指向**交易**而不是支付流水：本条留痕要回答的问题是
            # "这笔交易的资金有没有被重复记账"（AC-010），交易才是那个被保护的对象。
            ref_table="transaction",
            ref_id=payment["transaction_id"],
            payload={
                "callback_no": callback_no,
                "payment_no": payment_no,
                "result": result,
                "first_result": seen["result"],
            },
            actor="mock",
        )
        conn.commit()
        return (
            # 字段**恰好**契约 §3.17 列出的四个（多一个就是契约之外的字段，`RL-1`）：
            # `callback_no` 是请求入参，不属于响应契约。
            {
                "payment_no": payment_no,
                "result": seen["result"],  # 契约 §1.3：重复请求返回**首次结果**
                "is_duplicate": True,
                "transaction_status": _transaction_status(conn, payment["transaction_id"]),
            },
            200,
        )

    conn.execute(
        "INSERT INTO payment_callback_log (callback_no, payment_id, result, is_duplicate) VALUES (?, ?, ?, 0)",
        (callback_no, payment["id"], result),
    )
    transaction_status = _transaction_status(conn, payment["transaction_id"])
    if result == "success" and payment["status"] == "pending":
        conn.execute(
            "UPDATE payment SET status = 'success', callback_no = ?, confirmed_at = ? WHERE id = ?",
            (callback_no, now_iso(), payment["id"]),
        )
        conn.execute(
            'UPDATE "transaction" SET status = ?, received_amount_cents = ?, updated_at = ? WHERE id = ?',
            ("paid", payment["amount_cents"], now_iso(), payment["transaction_id"]),
        )
        transaction_status = "paid"
    elif result in {"failed", "timeout"} and payment["status"] == "pending":
        conn.execute("UPDATE payment SET status = ?, callback_no = ? WHERE id = ?", (result, callback_no, payment["id"]))
        conn.execute(
            'UPDATE "transaction" SET status = ?, updated_at = ? WHERE id = ?',
            ("payment_failed", now_iso(), payment["transaction_id"]),
        )
        transaction_status = "payment_failed"

    conn.commit()
    return (
        # 同上：响应字段恰好契约 §3.17 的四个（`callback_no` 是入参，不进响应体）。
        {
            "payment_no": payment_no,
            "result": result,
            "is_duplicate": False,
            "transaction_status": transaction_status,
        },
        200,
    )


def _transaction_status(conn: sqlite3.Connection, transaction_id: int) -> str:
    row = conn.execute('SELECT status FROM "transaction" WHERE id = ?', (transaction_id,)).fetchone()
    return row["status"] if row is not None else ""
