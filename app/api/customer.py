"""顾客扫码页端点：契约 §3.18 摊位信息与信用指标、§3.19 扫码页数据。

关联：`REQ-008`、`REQ-023`；`AC-011`（并按 `REQ-024` / `NFR-012` / `AC-012` 保证不返回
顾客身份与支付账号）。对应任务：`tasks.md` `T-023`。

**字段白名单是本模块的第一约束**（不是"顺手少给两个字段"）：
《报告》结论 16/17 记录了绵阳等地"扫码看信息"页面的实况 —— 页面上的「抽检记录 / 溯源信息」等字段
**根本没有采集流程，是空壳**，群众原话是"扫过好几次空码、无效信息之后，再也不信这个标签了"。
即**一个空壳字段会连带毁掉整页真实字段的可信度**（`discovery.md` D-08）。所以：
**没有采集来源的字段宁可不做**，投影函数只输出契约 §3.18/§3.19 列出的字段，一个不多
（`T-014` 按"逐一相等 + 每个字段必须有真值"两条机械判据钉住）。

**本端点的两件事都在领域层**：`app/domain/metrics.py` 的 `customer_stall_profile()` 与
`customer_receipt()`（那里同时写着 `price_consistency_bp` 的唯一口径）。
本层只做路由与 `jsonify`。

**无需认证**：契约头部与 §3.18/§3.19 均未要求 `X-Stall-Session`（顾客扫码页本期不做认证，
`NFR-007` 放宽项，批准记录见 `spec.md` §4.1）—— 在此加会话校验就是契约之外的新要求（`RL-1`）。
"""

from __future__ import annotations

from flask import Blueprint, jsonify

from .. import current_db
from ..domain.metrics import customer_receipt, customer_stall_profile

bp = Blueprint("customer", __name__)


@bp.get("/api/customer/stalls/<stall_no>/profile")
def read_stall_profile(stall_no):
    """契约 §3.18：顾客可见的摊位信息与信用指标。

    `stall_no` 用**字符串**形态接收（不加 `<int:...>` 转换器）：契约里它是`stall.stall_no` 文本，
    未知值应由领域层给出契约 §1.2 的统一错误信封（`MT-1009`），而不是被路由层判成裸 404。
    """
    return jsonify(customer_stall_profile(current_db(), stall_no)), 200


@bp.get("/api/customer/receipts/<transaction_no>")
def read_receipt(transaction_no):
    """契约 §3.19：扫码页数据（只含有采集来源的字段）。"""
    return jsonify(customer_receipt(current_db(), transaction_no)), 200
