"""演示用 Mock 端点：契约 §3.16 模拟电子秤读数、§3.17 模拟支付回调（可注入成功 / 失败 / 超时）。

关联：`REQ-005`、`REQ-011`、`REQ-027`、`REQ-029`；`AC-009`、`AC-010`。对应任务：`tasks.md` `T-017`。

**为什么要有这两个端点**：收款码（§3.10 `method = qr`）只把支付流水置为 `pending`，
真正让它转成 `success` / 失败 / 超时的，是**支付平台回调**；而计价链条的入口是**电子秤读数**。
本期不接真实硬件与真实支付平台（`spec.md` §5 边界），故用进程内 Mock 注入 ——
否则 `qr` 路径永远停在 `pending`（交易到不了 `paid`，后面的退货、日聚合、对账、顾客扫码页全都无路可走），
秤端也只能靠前端自己编一个重量。

三条纪律：

1. **无需认证**（契约 §3.16 对 Mock 端点明文"仅本机演示"；§3.17 同属 Mock 组）——
   它们是演示动线的注入口，不是业务端点，**不得**在这里引入会话校验（那是契约之外的新要求）。
2. **幂等命中不是错误**：重复送达返回 `200 + is_duplicate: true`，**不重复记账、不阻断后续处理**
   （ `spec.md` §5 / 契约 §4 末注）。写成 4xx 就与需求矛盾 —— 这条判断在领域层
   （`apply_payment_callback`），本层只负责把结果原样转成 HTTP。
3. **模拟秤读数是"注入"，不是"测量"**：它只回显本次注入的读数与时刻（契约 §3.16 的
   `current_weight_grams` / `injected_at`），**不落表** —— `data-model.md` 里没有"秤读数"实体，
   为演示端点新开一张表就是给系统加一份没人维护的事实来源。数值的**业务校验**（>0 且 ≤50kg → `MT-1002`）
   发生在计价（§3.6），故本端点**故意接受越界值**（0~200000），供演示 `MT-1002` 用（契约 §3.16 原文）。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import TradeError, current_db
from ..db import now_iso
from ..domain.payment import apply_payment_callback

bp = Blueprint("mock", __name__)

#: 契约 §3.16 的传输层约束（`weight_grams` 0 ~ 200000）
SCALE_READING_MIN, SCALE_READING_MAX = 0, 200_000


@bp.post("/api/mock/scale/reading")
def mock_scale_reading():
    """契约 §3.16：注入一次模拟电子秤读数 → 200 `{current_weight_grams, injected_at}`。

    `weight_grams` 只校验**传输层约束**（整数、0~200000）→ 越界抛 `MT-1008`（契约 §3.16 声明的唯一错误码）。
    注意 `0` 与 `>50000` 是**合法注入**：它们用来演示下游计价返回 `MT-1002`（`REQ-027` / `AC-017`），
    在这里就拦掉，演示动线就断了。
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")
    weight_grams = body.get("weight_grams")
    if isinstance(weight_grams, bool) or not isinstance(weight_grams, int):
        raise TradeError("MT-1008", "`weight_grams` 必须是整数", {"field": "weight_grams"})
    if not SCALE_READING_MIN <= weight_grams <= SCALE_READING_MAX:
        raise TradeError(
            "MT-1008",
            f"`weight_grams` 必须在 {SCALE_READING_MIN}~{SCALE_READING_MAX} 之间",
            {"field": "weight_grams", "min": SCALE_READING_MIN, "max": SCALE_READING_MAX},
        )
    return jsonify({"current_weight_grams": weight_grams, "injected_at": now_iso()}), 200


@bp.post("/api/mock/payment/callback")
def mock_payment_callback():
    """契约 §3.17：注入一次支付回调（`success` / `failed` / `timeout`）；重复送达幂等。

    请求体校验、幂等判定与记账全部在 `app/domain/payment.apply_payment_callback()`；
    `MT-1009`（支付单号不存在）与 `MT-1008`（参数校验失败）也由它抛出，经统一错误处理器转成
    契约 §1.2 响应体。
    """
    payload, status = apply_payment_callback(current_db(), request.get_json(silent=True))
    return jsonify(payload), status
