"""演示控制台端点：契约 §3.33~§3.37（商家注册 / 取消并撤销 / 中台视图 / 催缴短信）。

关联：`REQ-053`~`REQ-057`；`AC-043`~`AC-047`。

**为什么这五个端点无需认证**（与 `app/api/mock.py` 同一纪律）：契约 §3.33 明文"与 §3.16/§3.17 的
Mock 组同一纪律" —— 它们是**仅本机演示**的控制台入口，不是业务端点。在这里加会话校验，
就是在契约之外给调用方新增一个必填项（`RL-1`），演示页也就没法"打开即用"。
**注意**：取消端点虽然无认证，但它改的是**真实交易状态**（不是演示层伪造），
故规则全在领域层、且留痕落 `audit_log`（`REQ-055`）。

三条纪律：

1. **业务规则一条都不在这里重写**：注册与脱敏在 `domain/merchant_reg.py`、取消在
   `domain/transactions.py`、应缴与事件表在 `domain/demo_hub.py`、催缴在 `domain/notify.py`。
2. **明文收款码只经过本层一次**（`request.get_json` → 领域层当场脱敏）：本层**不打印、不记录**它。
3. **幂等命中不是错误**：取消重复提交返回 `200 + is_duplicate: true`（契约 §3.35 / §4 末注），
   写成 4xx 就与需求矛盾。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import clock, current_db
from ..domain.catalog import parse_business_date
from ..domain.demo_hub import hub_events, payables_for_day
from ..domain.merchant_reg import list_merchants, register_merchant
from ..domain.notify import send_sms_reminders
from ..domain.transactions import cancel_transaction

bp = Blueprint("demo", __name__)


@bp.get("/api/demo/merchants")
def demo_list_merchants():
    """契约 §3.33：已注册商家列表（**只回脱敏值**）→ 200（空列表回 200 + `items: []`）。"""
    return jsonify(list_merchants(current_db())), 200


@bp.post("/api/demo/merchants")
def demo_register_merchant():
    """契约 §3.34：商家注册（收款码**脱敏后存储**，明文不落库）→ 201。"""
    return jsonify(register_merchant(current_db(), request.get_json(silent=True))), 201


@bp.post("/api/demo/transactions/<transaction_no>/cancel")
def demo_cancel_transaction(transaction_no):
    """契约 §3.35：取消并撤销（幂等；**账上等同未发生，动作留痕**）→ 200。"""
    return jsonify(cancel_transaction(current_db(), transaction_no, request.get_json(silent=True))), 200


@bp.get("/api/demo/hub")
def demo_hub():
    """契约 §3.36：中台视图（事件表 + 各商家应缴）→ 200；`business_date` 缺省取服务端当日。"""
    conn = current_db()
    raw = request.args.get("business_date")
    day = clock.today_iso() if raw is None else parse_business_date(raw)
    return (
        jsonify(
            {
                "business_date": day,
                "events": hub_events(conn, day),
                "payables": payables_for_day(conn, day),
            }
        ),
        200,
    )


@bp.post("/api/demo/sms-reminders")
def demo_sms_reminders():
    """契约 §3.37：触发催缴短信（**每隔一天**；本期不接真实网关，落表留痕）→ 200。"""
    return jsonify(send_sms_reminders(current_db(), request.get_json(silent=True))), 200
