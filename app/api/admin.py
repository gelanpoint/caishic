"""运营端端点：契约 §3.20 ~ §3.31（字典/别名、佣金口径、看板、日聚合、结算、对账、指标、留痕）。

对应任务：`tasks.md` `T-013`（运营端）/ `T-020`（佣金与结算）。

**本层只做 HTTP**：校验 → 调领域层 → `jsonify`。所有口径（聚合、佣金、指标、对账）都在
`app/domain/` 里，本文件不写一句聚合 SQL —— 运营端是"给别人看数"的地方，
数字一旦在这里被二次加工（取整、估算、兜底 0），第三方就无法逐一核对（`AC-005`）。

三条与"假数"直接相关的纪律，写在最前面：

1. **指标必须真查分子与分母**（§3.30 / `AC-005`）：分母不写死、不估算；已废弃的第四项指标
   （市场口径「日均智能秤交易占比」，`Q-15`）**连字段都不给** —— 它的分母是系统拿不到的外部基准。
2. **留痕只读**（§3.31 / `NFR-009`）：只注册 `GET`；`audit_log` 的 `UPDATE`/`DELETE` 由数据库触发器
   拒绝，**不绕过触发器**、也不提供任何写入口。
3. **无口径 ≠ 0**：看板（§3.12/§3.25）无生效佣金口径时给 0 且不报错（契约未声明 `MT-1013`），
   而日聚合（§3.26）**必须**报 `MT-1013` —— 判断依据是"有没有生效口径"这个事实，不是结果值。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import current_db
from ..domain.audit import query_audit_logs
from ..domain.catalog import create_alias, create_category, list_aliases, list_categories
from ..domain.commission import create_rule, list_rules
from ..domain.metrics import market_dashboard, usage_metrics
from ..domain.settlement import aggregate_day, create_settlement, list_settlements, reconcile

bp = Blueprint("admin", __name__)


# ---------------------------------------------------------------------------
# §3.20 / §3.21 品类字典、§3.22 别名映射
# ---------------------------------------------------------------------------


@bp.get("/api/admin/categories")
def read_categories():
    """契约 §3.20：品类字典与别名映射查询。"""
    conn = current_db()
    return jsonify({"categories": list_categories(conn), "aliases": list_aliases(conn)}), 200


@bp.post("/api/admin/categories")
def create_admin_category():
    """契约 §3.21：新增/停用标准品类 → 201（重复编码 → 409 `MT-1012`）。"""
    return jsonify(create_category(current_db(), request.get_json(silent=True))), 201


@bp.post("/api/admin/aliases")
def create_admin_alias():
    """契约 §3.22：维护「摊位别名 → 标准品类」→ 201。"""
    return jsonify(create_alias(current_db(), request.get_json(silent=True))), 201


# ---------------------------------------------------------------------------
# §3.23 / §3.24 佣金口径
# ---------------------------------------------------------------------------


@bp.get("/api/admin/commission-rules")
def read_commission_rules():
    """契约 §3.23：查询佣金口径（含历史生效期）。响应是**数组**。"""
    return jsonify(list_rules(current_db())), 200


@bp.put("/api/admin/commission-rules")
def put_commission_rule():
    """契约 §3.24：配置佣金口径 → 200 + `commission_rule_changed` 留痕。

    语义是**新增一条口径**（响应为新建行、§3.23 可查、生效期重叠被 `MT-1012` 拒绝），
    而不是替换既有行 —— 与 §2.14"保留历史生效期"一致；详见 `app/domain/commission.py`。
    """
    return jsonify(create_rule(current_db(), request.get_json(silent=True))), 200


# ---------------------------------------------------------------------------
# §3.25 市场方看板
# ---------------------------------------------------------------------------


@bp.get("/api/admin/dashboard")
def read_admin_dashboard():
    """契约 §3.25：全部摊位汇总 + 排行；`business_date` 不传则当日。"""
    return jsonify(market_dashboard(current_db(), request.args.get("business_date"))), 200


# ---------------------------------------------------------------------------
# §3.26 日终聚合
# ---------------------------------------------------------------------------


@bp.post("/api/admin/daily-aggregate")
def post_daily_aggregate():
    """契约 §3.26：日终聚合 / 重算 → 200（无生效佣金口径 → 409 `MT-1013`）。"""
    return jsonify(aggregate_day(current_db(), request.get_json(silent=True))), 200


# ---------------------------------------------------------------------------
# §3.27 / §3.28 结算单
# ---------------------------------------------------------------------------


@bp.post("/api/admin/settlements")
def post_settlement():
    """契约 §3.27：依据当前有效日聚合生成结算单 → 201（期内缺聚合 → 409 `MT-1010`）。"""
    return jsonify(create_settlement(current_db(), request.get_json(silent=True))), 201


@bp.get("/api/admin/settlements")
def read_settlements():
    """契约 §3.28：查询结算单（含历史版本）。响应是**数组**。"""
    return (
        jsonify(
            list_settlements(
                current_db(),
                stall_no=request.args.get("stall_no"),
                period_start=request.args.get("period_start"),
                period_end=request.args.get("period_end"),
            )
        ),
        200,
    )


# ---------------------------------------------------------------------------
# §3.29 对账等式
# ---------------------------------------------------------------------------


@bp.get("/api/admin/reconciliation")
def read_reconciliation():
    """契约 §3.29：现场计算「订单总额 = 支付流水 = 分账明细」（口径见 `app/domain/settlement.py`）。

    `business_date` **必填**（缺 → 422 `MT-1008`）；`stall_no` 可选。
    """
    return (
        jsonify(
            reconcile(
                current_db(),
                business_date=request.args.get("business_date"),
                stall_no=request.args.get("stall_no"),
            )
        ),
        200,
    )


# ---------------------------------------------------------------------------
# §3.30 三个使用率指标
# ---------------------------------------------------------------------------


@bp.get("/api/admin/metrics/usage")
def read_usage_metrics():
    """契约 §3.30：三个指标的**分子与分母一起返回**（`AC-005`：第三方可逐一核对）。

    `business_date` 必填 —— 单日口径；缺参或格式非法 → 422 `MT-1008`。
    """
    return jsonify(usage_metrics(current_db(), request.args.get("business_date"))), 200


# ---------------------------------------------------------------------------
# §3.31 留痕查询（只读）
# ---------------------------------------------------------------------------


@bp.get("/api/admin/audit-logs")
def read_audit_logs():
    """契约 §3.31：只读查询留痕 → `{"total", "items"}`。

    **只注册 `GET`**：`POST`/`PUT`/`DELETE`/`PATCH` 由框架直接回 405（`NFR-009` 的"不提供任何写接口"）。
    时间参数名用契约的 `from` / `to`（`from` 是 Python 关键字，故映射成 `date_from`）。
    """
    return (
        jsonify(
            query_audit_logs(
                current_db(),
                stall_no=request.args.get("stall_no"),
                event_type=request.args.get("event_type"),
                date_from=request.args.get("from"),
                date_to=request.args.get("to"),
                limit=request.args.get("limit"),
                offset=request.args.get("offset"),
            )
        ),
        200,
    )
