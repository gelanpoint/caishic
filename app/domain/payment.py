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

from .. import TradeError, clock
from ..db import now_iso
from .audit import write_audit
from .numbering import insert_payment_row
from .transactions import find_transaction, require_stall_scope

#: 操作人长度上限（契约 §3.10：`operator` ≤32）
OPERATOR_MAX_LEN = 32
#: 收款流水号长度上限（`data-model.md` §2.10）
PAYMENT_NO_MAX_LEN = 32


def has_success_payment(conn: sqlite3.Connection, transaction_id: int) -> bool:
    """该交易是否已有一条**成功**流水（`REQ-026`：只允许一条交易记录与一次佣金）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = ? AND status = 'success'",
        (transaction_id,),
    ).fetchone()
    return int(row["n"]) > 0


def _write_confirmed_audit(
    conn: sqlite3.Connection, txn: sqlite3.Row, payment_no: str, method: str, confirmed_at: str, actor: str
) -> None:
    """交易转 `paid` 时写一条 `transaction_confirmed` 留痕（`REQ-054`、中台页事件表的数据源）。

    为什么写在**领域层**而不是演示端点里：中台页要实时显示"确认（上传）"这件事，而"交易转 `paid`"
    只有两个落点 —— 现金确认（§3.10）与收款码回调成功（§3.17）。在某个端点里补记就会漏掉另一条路径，
    于是"中台看到的事件"与"账上的事实"分叉。
    """
    write_audit(
        conn,
        event_type="transaction_confirmed",
        ref_table="transaction",
        ref_id=int(txn["id"]),
        payload={
            "transaction_no": txn["transaction_no"],
            "payment_no": payment_no,
            "method": method,
            "received_amount_cents": int(txn["total_amount_cents"]),
        },
        actor=actor,
        stall_id=int(txn["stall_id"]),
        occurred_at=confirmed_at,
    )


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

    business_date = txn["business_date"] or clock.today_iso()

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
        payment_no = insert_payment_row(
            conn,
            business_date=business_date,
            transaction_id=int(txn["id"]),
            method="cash",
            amount_cents=int(txn["total_amount_cents"]),
            status="success",
            confirmed_at=confirmed_at,
            operator=operator,
        )
        conn.execute(
            'UPDATE "transaction" SET status = ?, received_amount_cents = ?, updated_at = ? WHERE id = ?',
            ("paid", txn["total_amount_cents"], confirmed_at, txn["id"]),
        )
        _write_confirmed_audit(conn, txn, payment_no, "cash", confirmed_at, operator)
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
    payment_no = insert_payment_row(
        conn,
        business_date=business_date,
        transaction_id=int(txn["id"]),
        method="qr",
        amount_cents=int(txn["total_amount_cents"]),
        status="pending",
        confirmed_at=None,
        operator=body.get("operator"),
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
    if transaction_status == "cancelled":
        # 取消优先（契约 §3.35「与迟到回调的关系」硬要求）：取消后该笔是**终态**，
        # 收款码回调无论 success / failed / timeout 都**不得**把交易改回 `paid` / `payment_failed`。
        # 若不拦这里：调用方被告知"取消成功"，账上却被回调重新计入 —— `REQ-055`「账上等同于未发生」
        # 与 `RL-9`（不得静默丢弃/静默反悔）双破。
        # 回调本身**照常落 `payment_callback_log`**（上面那行，`RL-9`：回调不得被静默丢弃）；
        # 支付单状态**不改**（留一条 `success` 支付单会污染 `usage_metrics.cash_txns` 这类
        # `payment JOIN transaction` 且按流水取数的口径），也**不写** `transaction_confirmed` 留痕。
        conn.commit()
        return (
            {
                "payment_no": payment_no,
                "result": result,
                "is_duplicate": False,
                "transaction_status": "cancelled",
            },
            200,
        )
    if result == "success" and payment["status"] == "pending":
        confirmed_at = now_iso()
        conn.execute(
            "UPDATE payment SET status = 'success', callback_no = ?, confirmed_at = ? WHERE id = ?",
            (callback_no, confirmed_at, payment["id"]),
        )
        conn.execute(
            'UPDATE "transaction" SET status = ?, received_amount_cents = ?, updated_at = ? WHERE id = ?',
            ("paid", payment["amount_cents"], confirmed_at, payment["transaction_id"]),
        )
        confirmed_txn = conn.execute(
            'SELECT * FROM "transaction" WHERE id = ?', (payment["transaction_id"],)
        ).fetchone()
        _write_confirmed_audit(conn, confirmed_txn, payment_no, "qr", confirmed_at, "mock")
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
