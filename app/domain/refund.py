"""退货冲正（`REQ-013`、`REQ-028`；契约 §3.11）。

本模块**不依赖 HTTP**：错误一律抛 `TradeError(契约错误码, ...)`，由统一处理器转成响应。

三条口径（改动前先读，都是被测过的行为）：

1. **只冲减一次**：`refund(transaction_id, idempotency_key)` 的联合唯一索引 `ux_refund_idem`
   是最后一道闸门（`data-model.md` §2.12）。**幂等命中不是错误**（`spec.md` §5 / 契约 §3.11）：
   重复提交返回 **200 + `replayed: true`** 且金额不变 —— 写成 4xx 就与需求矛盾。
2. **幂等判定必须早于状态判定**：首次退货成功后交易已转 `refunded`（**终态**，`data-model.md` §4.1）。
   若先判"必须 `paid`"再判幂等，重复提交就会撞 `MT-1001` 而不是返回首次结果 ——
   这正是"重复退货必须 200"最容易写错的地方。判定顺序：
   存在(`MT-1009`) → 归属(`MT-1004`) → 参数(`MT-1008`) → **幂等命中** → 状态(`MT-1001`) → 金额越界(`MT-1003`)。
3. **不在这里写日聚合**：`daily_aggregate` 快照的重算属 `T-020`（`app/domain/settlement.py`），
   本模块只写**事实行**（`refund`）+ 交易状态 + 留痕。看板（§3.12）按事实行现场汇总退货额，
   因此 `AC-002` 的可核对性不依赖快照是否已生成。
"""

from __future__ import annotations

import sqlite3

from .. import TradeError
from ..db import now_iso
from .audit import write_audit
from .pricing import find_transaction, require_stall_scope

#: 退货单号长度上限（`data-model.md` §2.12）
REFUND_NO_MAX_LEN = 32
#: 幂等键长度上限（契约 §3.11 / `data-model.md` §2.12）
IDEMPOTENCY_KEY_MAX_LEN = 64


def _next_refund_no(conn: sqlite3.Connection, business_date: str) -> str:
    """生成退货单号 `RF-YYYYMMDD-NNNN`（按营业日递增，长度 ≤32）。

    与交易号 `T-YYYYMMDD-NNNN`、支付流水号 `PAY-YYYYMMDD-NNNNNN` 同一套命名法 ——
    单号前缀本身就说明了它属于哪条链路，现场排错不用回表查。
    """
    prefix = f"RF-{business_date.replace('-', '')}"
    row = conn.execute(
        "SELECT refund_no FROM refund WHERE refund_no LIKE ? ORDER BY refund_no DESC LIMIT 1",
        (f"{prefix}-%",),
    ).fetchone()
    sequence = int(row["refund_no"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}-{sequence:04d}"


def refund_transaction(
    conn: sqlite3.Connection, stall: sqlite3.Row, transaction_no: str, body, idempotency_key: str | None
) -> dict:
    """退货冲正（契约 §3.11）→ `{refund_no, transaction_status, amount_cents, replayed}`。"""
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")

    txn = find_transaction(conn, transaction_no)
    require_stall_scope(txn, stall["stall_id"])
    stall_id = stall["stall_id"]

    # 契约 §3.11：幂等键不传时取请求体字段（与 §3.6 的 `client_idempotency_key` 同款约定）
    key = idempotency_key or body.get("idempotency_key")
    if not isinstance(key, str) or not key or len(key) > IDEMPOTENCY_KEY_MAX_LEN:
        raise TradeError(
            "MT-1008",
            f"缺少幂等键（请求头 Idempotency-Key，长度 ≤{IDEMPOTENCY_KEY_MAX_LEN}）",
            {"header": "Idempotency-Key"},
        )

    amount = body.get("amount_cents")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise TradeError("MT-1008", "`amount_cents` 必须是 >0 的整数", {"field": "amount_cents"})

    actor = f"stall-{stall['stall_no']}"

    # ---- 幂等命中（**必须先于状态判定**，见模块 docstring 第 2 条）--------------
    existing = conn.execute(
        "SELECT * FROM refund WHERE transaction_id = ? AND idempotency_key = ?", (txn["id"], key)
    ).fetchone()
    if existing is not None:
        if int(existing["amount_cents"]) != amount:
            # 契约 §4 `MT-1012`(a)：幂等键已存在且请求体指纹不同 → 不返回首次结果，明确报冲突
            raise TradeError(
                "MT-1012",
                "幂等键已用于另一笔金额不同的退货",
                {"idempotency_key": key, "refund_no": existing["refund_no"]},
            )
        write_audit(
            conn,
            event_type="refund_duplicate_hit",
            ref_table="refund",
            ref_id=existing["id"],
            payload={
                "refund_no": existing["refund_no"],
                "transaction_no": transaction_no,
                "amount_cents": int(existing["amount_cents"]),
            },
            actor=actor,
            stall_id=stall_id,
        )
        conn.commit()
        # 契约 §1.3：重复请求返回**首次结果**（含首次的 `refund_no`），金额不变
        return {
            "refund_no": existing["refund_no"],
            "transaction_status": "refunded",
            "amount_cents": int(existing["amount_cents"]),
            "replayed": True,
        }

    # ---- 状态闸门（`refunded` 是终态：换新键再退同一笔 → `MT-1001`）----------
    if txn["status"] != "paid":
        raise TradeError(
            "MT-1001", "交易不在可退货状态", {"status": txn["status"], "transaction_no": transaction_no}
        )

    # ---- 金额闸门（`REQ-028` / `AC-017`）------------------------------------
    original = int(txn["total_amount_cents"])
    if amount > original:
        raise TradeError(
            "MT-1003",
            "退货金额超过原单金额",
            {"amount_cents": amount, "max_cents": original},
        )

    refund_no = _next_refund_no(conn, txn["business_date"])
    cursor = conn.execute(
        """
        INSERT INTO refund (refund_no, transaction_id, stall_id, amount_cents, idempotency_key, operator)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (refund_no, txn["id"], stall_id, amount, key, actor[:32]),
    )
    refund_id = int(cursor.lastrowid)
    conn.execute(
        'UPDATE "transaction" SET status = ?, updated_at = ? WHERE id = ?',
        ("refunded", now_iso(), txn["id"]),
    )
    write_audit(
        conn,
        event_type="refund_applied",
        ref_table="refund",
        ref_id=refund_id,
        payload={
            "refund_no": refund_no,
            "transaction_no": transaction_no,
            "amount_cents": amount,
            "transaction_total_cents": original,
        },
        actor=actor,
        stall_id=stall_id,
    )
    conn.commit()
    return {
        "refund_no": refund_no,
        "transaction_status": "refunded",
        "amount_cents": amount,
        "replayed": False,
    }
