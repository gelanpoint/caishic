"""秤端接入 · §3.5 交易上报（在线与补传**共用同一端点**）（`T-SCALE-06`）。

唯一权威：`specs/market-trade-flow/contracts/scale-midplatform.md` §1.1（幂等总表）、§3.5、§5、§6。
领域逻辑（载荷校验、重算比对、留痕）在 `app/domain/scale_ingest.py`；本文件只是 HTTP 皮肤。

三条口径：

1. **在线与补传不另设端点**（契约 §1.1 末注）：差别只在**时间** —— 补传的 `business_date` 取
   秤端**暂存时**记下的营业日，中台按该值入账，而不是按到达日的墙钟。多一个端点就多一处口径分叉。
2. **响应 201（新建）/ 200（幂等命中）**：幂等命中返回**首次结果**（含首次的比对结论），
   本层不重新比对、不重新记账。
3. **令牌明文不落库、不进日志**：本层只把请求头原样交给领域层做**摘要比对**，不打印、不存储。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import current_db
from ..domain import device as device_domain
from ..domain import scale_ingest

bp = Blueprint("scale_ingest", __name__)

#: 设备令牌请求头（契约 §1 认证方式）
TOKEN_HEADER = "X-Device-Token"
#: 协议版本请求头（契约 §1 第 9 条 / §7）
PROTO_HEADER = "X-Scale-Proto"
#: 写操作幂等键请求头（契约 §1.3；本端点必填）
IDEMPOTENCY_HEADER = "Idempotency-Key"


@bp.post("/api/scale/v1/transactions")
def report_scale_transaction():
    """契约 §3.5：上报一笔秤端交易 → 201 新建 / 200 幂等命中。

    - 入账金额**一律**取中台重算值（`authoritative_amount_cents`）；
    - 金额不一致**不是错误响应**：仍 2xx，由 `amount_mismatch` + `mismatch_detail` 如实暴露，
      并已写 `audit_log(scale_amount_mismatch)` 留痕（`AC-030`）；
    - 授权范围只来自设备令牌，请求体里的 `stall_id` / `market_id` 不采信（`MT-2002`）。
    """
    device_domain.require_supported_proto(request.headers.get(PROTO_HEADER))
    conn = current_db()
    device = device_domain.authenticate_device(conn, request.headers.get(TOKEN_HEADER))
    payload, created = scale_ingest.ingest_transaction(
        conn,
        device,
        request.get_json(silent=True),
        request.headers.get(IDEMPOTENCY_HEADER),
        # `receipt_url` 必须指向**中台**顾客页（REQ-043）；领域层据此拼上交易号
        receipt_url_base=f"{request.host_url.rstrip('/')}/customer/receipts",
    )
    return jsonify(payload), (201 if created else 200)
