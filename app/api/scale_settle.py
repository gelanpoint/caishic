"""秤端接入 · §3.6 收款确认（现金 / 取回收款码）/ §3.7 退货申请（`T-SCALE-07`）。

唯一权威：`specs/market-trade-flow/contracts/scale-midplatform.md` §3.6、§3.7。
领域逻辑复用主契约同一套实现：`app/domain/payment.py`（现金与收款码共用 `payment` 表）、
`app/domain/refund.py`（冲正只冲减一次）、`app/domain/commission.py`（佣金只由中台算）。

四条口径：

1. **`qr_payload` 指向中台顾客页**（`REQ-043` / `AC-033`）：秤端据此本地生成二维码，
   **不承载顾客页面**，也不得把地址指回秤端自己。
2. **现金写同一张 `payment` 表**（`REQ-010`）：不另立现金表 —— 另立就等于把"同一件事"记两处。
3. **退货冲正与佣金扣减全部在中台计算**（`REQ-042` / `NFR-015`）：秤端只提交申请；
   `commission_delta_cents` 由中台按该营业日生效口径现算，秤端不参与任何金额计算。
4. **授权只来自设备令牌**：交易号必须属于设备绑定的摊位，否则 `MT-2002`（`REQ-032`）。
"""

from __future__ import annotations

import sqlite3

from flask import Blueprint, jsonify, request

from .. import TradeError, current_db
from ..domain import device as device_domain
from ..domain.commission import commission_of, find_effective_rule
from ..domain.payment import pay_transaction
from ..domain.refund import refund_transaction

bp = Blueprint("scale_settle", __name__)

#: 设备令牌请求头（契约 §1 认证方式）
TOKEN_HEADER = "X-Device-Token"
#: 协议版本请求头（契约 §1 第 9 条 / §7）
PROTO_HEADER = "X-Scale-Proto"
#: 写操作幂等键请求头（契约 §1.3；两个端点都必填）
IDEMPOTENCY_HEADER = "Idempotency-Key"
#: 幂等键长度上限（契约 §1.3）
IDEMPOTENCY_KEY_MAX_LEN = 64


def _stall_of(conn: sqlite3.Connection, device: dict) -> sqlite3.Row:
    """设备绑定的摊位行（列名与 `catalog.require_session` 一致，供主契约的领域函数直接使用）。

    `payment.pay_transaction` 需要 `payment_receiver_token`（脱敏值），`refund.refund_transaction`
    需要 `stall_no`；两者都从**设备绑定**的摊位取，不从请求里取。
    """
    row = conn.execute(
        "SELECT id AS stall_id, stall_no, payment_receiver_token FROM stall WHERE id = ?",
        (int(device["stall_id"]),),
    ).fetchone()
    if row is None:
        raise TradeError("MT-2001", "设备绑定的摊位不存在，请重新预注册", {"device_id": device["device_id"]})
    return row


def _own_transaction(conn: sqlite3.Connection, device: dict, transaction_no: str) -> sqlite3.Row:
    """交易号必须属于设备绑定的摊位，否则 `MT-2002`（`REQ-032` / `AC-021`）。

    不存在与属他人**同码**：契约 §3.6/§3.7 的"可能错误"只登记了 `MT-2002`，
    没有 `MT-1009`；且对调用方而言"这笔不是你的"与"这笔不存在"都不该拿到操作权。
    """
    txn = conn.execute(
        'SELECT * FROM "transaction" WHERE transaction_no = ?', (transaction_no,)
    ).fetchone()
    if txn is None or int(txn["stall_id"]) != int(device["stall_id"]):
        raise TradeError(
            "MT-2002", "交易号不属于本摊位", {"transaction_no": transaction_no}
        )
    return txn


def _idempotency_key(body: dict) -> str:
    """写操作幂等键：请求头优先（契约 §1.3），缺失/超长 → `MT-2004`。"""
    key = request.headers.get(IDEMPOTENCY_HEADER) or body.get("idempotency_key")
    if not isinstance(key, str) or not 1 <= len(key) <= IDEMPOTENCY_KEY_MAX_LEN:
        raise TradeError(
            "MT-2004",
            f"缺少幂等键（请求头 Idempotency-Key，长度 1~{IDEMPOTENCY_KEY_MAX_LEN}）",
            {"header": IDEMPOTENCY_HEADER},
        )
    return key


def _qr_payload(transaction_no: str) -> str:
    """**中台**顾客页地址（`REQ-043`）：秤端拿它本地生成二维码，顾客扫到的是中台。"""
    return f"{request.host_url.rstrip('/')}/customer/receipts/{transaction_no}"


# ---------------------------------------------------------------------------
# §3.6 收款确认
# ---------------------------------------------------------------------------


@bp.post("/api/scale/v1/transactions/<transaction_no>/settle")
def settle_scale_transaction(transaction_no):
    """契约 §3.6：`method = qr` → 200（返回中台顾客页收款码）；`method = cash` → 200（已收款）。

    幂等：同一笔已成功收款的现金重复确认**返回首次结果**（不重复记账，`REQ-026`）；
    `refunded` 是终态 → `MT-1001`（契约 §4）。
    """
    device_domain.require_supported_proto(request.headers.get(PROTO_HEADER))
    conn = current_db()
    device = device_domain.authenticate_device(conn, request.headers.get(TOKEN_HEADER))
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise TradeError("MT-2004", "请求体必须是 JSON 对象")
    method = body.get("method")
    if method not in ("cash", "qr"):
        raise TradeError("MT-2004", "`method` 必须是 cash 或 qr", {"field": "method"})
    key = _idempotency_key(body)
    txn = _own_transaction(conn, device, transaction_no)
    stall = _stall_of(conn, device)

    if txn["status"] == "paid":
        # 已收款：现金重复确认返回**首次结果**；已收款再要收款码属状态不允许
        paid = conn.execute(
            "SELECT * FROM payment WHERE transaction_id = ? AND status = 'success' ORDER BY id LIMIT 1",
            (txn["id"],),
        ).fetchone()
        if method == "cash" and paid is not None:
            return (
                jsonify(
                    {
                        "transaction_no": transaction_no,
                        "status": "paid",
                        "paid_at": paid["confirmed_at"],
                    }
                ),
                200,
            )
        raise TradeError(
            "MT-1001", "交易不在可收款状态", {"status": txn["status"], "transaction_no": transaction_no}
        )
    if txn["status"] not in ("priced", "payment_failed"):
        # `refunded` 是终态（data-model.md §4.1）：不得再收款
        raise TradeError(
            "MT-1001", "交易不在可收款状态", {"status": txn["status"], "transaction_no": transaction_no}
        )

    if method == "qr":
        # 同键/同笔重复取码不新建流水（契约 §1.1：按交易状态机返回，不重复记账）
        existing = conn.execute(
            "SELECT payment_no FROM payment WHERE transaction_id = ? AND method = 'qr' ORDER BY id LIMIT 1",
            (txn["id"],),
        ).fetchone()
        if existing is None:
            pay_transaction(conn, stall, transaction_no, {"method": "qr"}, key)
        return (
            jsonify(
                {
                    "transaction_no": transaction_no,
                    "status": txn["status"],
                    "qr_payload": _qr_payload(transaction_no),
                }
            ),
            200,
        )

    # 现金：操作人取设备身份（契约 §3.6 的请求体只有 `method`，主契约要求的 `operator` 由中台补）
    payload, _ = pay_transaction(
        conn,
        stall,
        transaction_no,
        {"method": "cash", "operator": f"device-{device['device_id']}"[:32]},
        key,
    )
    return (
        jsonify({"transaction_no": transaction_no, "status": "paid", "paid_at": payload["confirmed_at"]}),
        200,
    )


# ---------------------------------------------------------------------------
# §3.7 退货申请（冲正由中台执行）
# ---------------------------------------------------------------------------


@bp.post("/api/scale/v1/transactions/<transaction_no>/refund")
def refund_scale_transaction(transaction_no):
    """契约 §3.7：退货申请 → 201；冲正与佣金扣减**全部由中台计算**，同键重复只冲减一次。"""
    device_domain.require_supported_proto(request.headers.get(PROTO_HEADER))
    conn = current_db()
    device = device_domain.authenticate_device(conn, request.headers.get(TOKEN_HEADER))
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise TradeError("MT-2004", "请求体必须是 JSON 对象")
    amount = body.get("amount_cents")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise TradeError("MT-2004", "`amount_cents` 必须是 >0 的整数", {"field": "amount_cents"})
    key = _idempotency_key(body)
    txn = _own_transaction(conn, device, transaction_no)
    stall = _stall_of(conn, device)

    # 冲正与状态闸门（`MT-1003` / `MT-1001`）在领域层；重复申请返回首次结果且只冲减一次
    result = refund_transaction(conn, stall, transaction_no, body, key)
    rule = find_effective_rule(conn, txn["business_date"])
    return (
        jsonify(
            {
                "transaction_no": transaction_no,
                "status": "refunded",
                "refund_amount_cents": int(result["amount_cents"]),
                # 佣金扣减**由中台按生效口径现算**（REQ-042 / NFR-015）：秤端不参与计算
                "commission_delta_cents": -commission_of(int(result["amount_cents"]), rule),
            }
        ),
        201,
    )
