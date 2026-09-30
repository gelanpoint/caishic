"""演示用 Mock 端点：契约 §3.17 模拟支付回调（可注入成功 / 失败 / 超时）。

关联：`REQ-011`、`REQ-029`；`AC-009`、`AC-010`。对应任务：`tasks.md` `T-017`。

**为什么要有这个端点**：收款码（§3.10 `method = qr`）只把支付流水置为 `pending`，
真正让它转成 `success` / 失败 / 超时的，是**支付平台回调**。本期不接真实支付平台（`spec.md` §5 边界），
故用进程内 Mock 注入 —— 否则 `qr` 路径永远停在 `pending`，交易永远到不了 `paid`，
后面的退货、日聚合、对账、顾客扫码页全都无路可走。

两条纪律：

1. **无需认证**（契约 §3.16 对 Mock 端点明文"仅本机演示"；§3.17 同属 Mock 组）——
   它是演示动线的注入口，不是业务端点，**不得**在这里引入会话校验（那是契约之外的新要求）。
2. **幂等命中不是错误**：重复送达返回 `200 + is_duplicate: true`，**不重复记账、不阻断后续处理**
   （ `spec.md` §5 / 契约 §4 末注）。写成 4xx 就与需求矛盾 —— 这条判断在领域层
   （`apply_payment_callback`），本层只负责把结果原样转成 HTTP。

**本模块当前只有 §3.17**：同属 Mock 组的 §3.16 模拟电子秤读数（`/api/mock/scale/reading`）
尚未实现，属 `T-017` 余下部分 —— **不预写半成品**。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import current_db
from ..domain.payment import apply_payment_callback

bp = Blueprint("mock", __name__)


@bp.post("/api/mock/payment/callback")
def mock_payment_callback():
    """契约 §3.17：注入一次支付回调（`success` / `failed` / `timeout`）；重复送达幂等。

    请求体校验、幂等判定与记账全部在 `app/domain/payment.apply_payment_callback()`；
    `MT-1009`（支付单号不存在）与 `MT-1008`（参数校验失败）也由它抛出，经统一错误处理器转成
    契约 §1.2 响应体。
    """
    payload, status = apply_payment_callback(current_db(), request.get_json(silent=True))
    return jsonify(payload), status
