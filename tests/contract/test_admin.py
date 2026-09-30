"""`T-013` 契约测试（一）：运营端**字典与佣金口径**（§3.20/§3.21 品类字典、§3.22 别名映射、§3.23/§3.24 佣金口径）。

关联：`REQ-002`、`REQ-017`、`REQ-018`、`REQ-019`、`REQ-020`、`REQ-022`；`AC-005`、`AC-022`、`AC-023`。
覆盖契约：§3.20/§3.21 品类字典、§3.22 别名映射、§3.23/§3.24 佣金口径、§3.25 市场方看板、
§3.26 日终聚合与重算、§3.27/§3.28 结算单、§3.29 对账等式、§3.30 三个使用率指标、§3.31 留痕查询。
错误码用例：`MT-1008`（参数校验失败）、`MT-1010`（期内日聚合缺失）、`MT-1012`（唯一约束 / 生效期重叠）、
`MT-1013`（无生效佣金口径）。

> **用例内存在顺序依赖（不是随机失败，已在下方逐条标注）**：契约要求「先配佣金口径 → 再日终聚合 → 再生成结算单」
> （§3.26 前置：`MT-1013` 无生效口径则拒绝；§3.27 前置：`MT-1010` 期内部署日缺聚合）。故本文件按定义顺序执行即可，
> **不要单独按名字乱序运行**；需要独立复现时请整文件运行 `python -m pytest tests/contract/test_admin.py`。

> **`AC-005` 的断言强度**：三个指标的**分子与分母全部回库交叉核对**（口径来源 `data-model.md` §5.1），
> 而不只是"字段存在"。另外显式断言**不存在第四个 `*_bp` 指标** —— 市场口径的「日均智能秤交易占比」
> 已被 `REQ-022` 修订废弃（`Q-15`），本期**禁止**用估算值伪造它（契约 §3.30 末注）。

> **本文件由 `Q-16` 的按语义拆分从原 `test_admin.py` 拆出**（原文件 479 行超 `quality-gates` §1.2 的
> 400 行阈值）：**配置类**端点留在这里，**看板与结算链路**（§3.25 看板、§3.26 日聚合、§3.27/§3.28 结算单、
> §3.29 对账、§3.30 指标、§3.31 留痕）在 `test_admin_ops.py`。**两半之间没有顺序依赖**：
> 本文件的用例不依赖任何交易数据（§3.25 及以后的用例才需要）。
> `Q-19` 的指标灵敏度负例随 §3.30 的用例走，见 `test_admin_ops.py`。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from contract_support import (
    assert_error_response,
    bind_stall_session,
    create_priced_transaction,
    json_of,
    session_headers,
    today_iso,
)

STALL_ADMIN = "A-03"  # 本组交易与结算的观察摊位
METRIC_FIELDS = {
    "business_date",
    "stall_usage_bp",
    "stall_usage_numerator",
    "stall_usage_denominator",
    "cash_txn_share_bp",
    "cash_txn_numerator",
    "cash_txn_denominator",
    "price_list_maintenance_bp",
    "price_list_numerator",
    "price_list_denominator",
}
CASH_FIELDS = {"payment_no", "method", "status", "confirmed_at", "transaction_status"}
RECONCILIATION_FIELDS = {
    "business_date",
    "order_total_cents",
    "payment_total_cents",
    "split_total_cents",
    "balanced",
    "diff_cents",
}

def _count(db_conn, sql: str, params: tuple = ()) -> int:
    return db_conn.execute(sql, params).fetchone()["n"]

def _stall_id(db_conn, stall_no: str = STALL_ADMIN) -> int:
    row = db_conn.execute("SELECT id FROM stall WHERE stall_no = ?", (stall_no,)).fetchone()
    assert row is not None, f"种子数据应有摊位 {stall_no}"
    return row["id"]

def _paid_transaction(client, tag: str) -> dict:
    """前置：用秤端主链路造一笔**已收款**交易（运营端聚合/结算/对账的数据来源）。"""
    token = bind_stall_session(client, STALL_ADMIN)
    product_id = json_of(client.get("/api/merchant/products", headers=session_headers(token)))[0]["id"]
    priced = client.post(
        "/api/merchant/price-list",
        json={"business_date": today_iso(), "items": [{"product_id": product_id, "unit_price_cents": 400}]},
        headers=session_headers(token, f"t013-{tag}-price"),
    )
    assert priced.status_code == 200, f"前置：契约 §3.4 设价失败 {priced.status_code}"
    created = create_priced_transaction(client, token, idempotency_key=f"t013-{tag}-txn", weights_grams=[1000])
    paid = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/payment",
        json={"method": "cash", "operator": "t013-cashier"},
        headers=session_headers(token, f"t013-{tag}-pay"),
    )
    assert paid.status_code == 200, f"前置：契约 §3.10 期望 200，实际 {paid.status_code}"
    body = json_of(paid)
    assert set(body) >= CASH_FIELDS
    return created


# ---------------------------------------------------------------------------
# §3.20 / §3.21 品类字典（`REQ-002`）
# ---------------------------------------------------------------------------


def test_categories_get_returns_dict_and_aliases(client):
    response = client.get("/api/admin/categories")
    assert response.status_code == 200, f"契约 §3.20 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert set(payload) == {"categories", "aliases"}, f"契约 §3.20 响应键应为 categories/aliases，实际 {sorted(payload)}"
    assert isinstance(payload["categories"], list) and payload["categories"], "品类字典不应为空（种子 58 个标准品类）"
    assert isinstance(payload["aliases"], list) and payload["aliases"], "别名映射不应为空（种子 351 条）"
    for item in payload["categories"][:5]:
        assert {"id", "code", "name", "status"} <= set(item), f"§2.4 品类元素缺字段：{sorted(item)}"
        assert item["status"] in {"active", "inactive"}, f"§2.4 status 枚举不符：{item['status']!r}"
    for item in payload["aliases"][:5]:
        assert {"id", "stall_no", "alias_name", "category_id"} <= set(item), f"§2.5 别名元素缺字段：{sorted(item)}"


def test_create_category_then_duplicate_code_mt_1012(client):
    """契约 §3.21：新增标准品类 201；`code` 全表唯一，重复即 409 `MT-1012`。"""
    body = {"code": "T013C1", "name": "契约测试品类", "status": "active"}
    created = client.post("/api/admin/categories", json=body)
    assert created.status_code == 201, f"契约 §3.21 期望 201，实际 {created.status_code}"
    payload = json_of(created)
    assert set(payload) == {"id", "code", "name", "status"}, f"契约 §3.21 响应键不符：{sorted(payload)}"
    assert payload["code"] == "T013C1" and payload["status"] == "active"

    duplicate = client.post("/api/admin/categories", json=body)
    assert duplicate.status_code == 409, f"契约 §4 期望 409(MT-1012)，实际 {duplicate.status_code}"
    assert_error_response(duplicate, expected_code="MT-1012")

    listed = json_of(client.get("/api/admin/categories"))["categories"]
    assert any(item["code"] == "T013C1" for item in listed), "新建品类应能在 §3.20 查到"


def test_create_category_stop_and_invalid_body(client):
    """契约 §3.21：停用即 `status = inactive`；非法枚举 / 缺字段 → 422 `MT-1008`。"""
    created = client.post("/api/admin/categories", json={"code": "T013C2", "name": "待停用品类", "status": "inactive"})
    assert created.status_code == 201, f"契约 §3.21 期望 201，实际 {created.status_code}"
    assert json_of(created)["status"] == "inactive"

    for bad in [{}, {"code": "T013C3"}, {"name": "缺编码"}, {"code": "T013C4", "name": "枚举非法", "status": "disabled"},
                {"code": "X" * 17, "name": "编码超长"}, {"code": "T013C5", "name": "名" * 33}]:
        response = client.post("/api/admin/categories", json=bad)
        assert response.status_code == 422, f"契约 §3.21/§4 期望 422，实际 {response.status_code}（body={bad!r}）"
        assert_error_response(response, expected_code="MT-1008")


def test_create_alias_and_conflicts(client):
    """契约 §3.22：维护「摊位别名 → 标准品类」；联合唯一冲突 409、品类不存在 404。"""
    categories = json_of(client.get("/api/admin/categories"))["categories"]
    category_id = categories[0]["id"]
    body = {"stall_no": STALL_ADMIN, "alias_name": "T013别名", "category_id": category_id}

    created = client.post("/api/admin/aliases", json=body)
    assert created.status_code == 201, f"契约 §3.22 期望 201，实际 {created.status_code}"
    payload = json_of(created)
    assert set(payload) == {"id", "stall_no", "alias_name", "category_id"}, f"契约 §3.22 响应键不符：{sorted(payload)}"
    assert payload["stall_no"] == STALL_ADMIN and payload["category_id"] == category_id

    duplicate = client.post("/api/admin/aliases", json=body)
    assert duplicate.status_code == 409, f"契约 §4 期望 409(MT-1012)，实际 {duplicate.status_code}"
    assert_error_response(duplicate, expected_code="MT-1012")

    missing_category = client.post(
        "/api/admin/aliases", json={"stall_no": STALL_ADMIN, "alias_name": "T013别名2", "category_id": 10**9}
    )
    assert missing_category.status_code == 404, f"契约 §3.22 期望 404(MT-1009)，实际 {missing_category.status_code}"
    assert_error_response(missing_category, expected_code="MT-1009")

    invalid = client.post("/api/admin/aliases", json={"stall_no": STALL_ADMIN, "category_id": category_id})
    assert invalid.status_code == 422, f"契约 §3.22 期望 422(MT-1008)，实际 {invalid.status_code}"
    assert_error_response(invalid, expected_code="MT-1008")


# ---------------------------------------------------------------------------
# §3.23 / §3.24 佣金口径（`REQ-017`、`AC-022`）
# ---------------------------------------------------------------------------


def test_commission_rule_put_get_and_audit_trail(client, db_conn):
    """`AC-022`：配置佣金口径（收费对象 + 费率 + 生效期）→ 立即可查，且写 `commission_rule_changed` 留痕。"""
    today = today_iso()
    rule = {"pay_object": "merchant", "rate_bp": 200, "effective_from": today}
    response = client.put("/api/admin/commission-rules", json=rule)
    assert response.status_code == 200, f"契约 §3.24 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert {"id", "pay_object", "rate_bp", "effective_from"} <= set(payload), f"§2.14 规则行缺字段：{sorted(payload)}"
    assert payload["pay_object"] == "merchant" and payload["rate_bp"] == 200
    assert payload["effective_from"] == today

    listed = json_of(client.get("/api/admin/commission-rules"))
    assert isinstance(listed, list) and listed, "契约 §3.23 应返回规则数组"
    assert any(item.get("id") == payload["id"] for item in listed), "新建规则应能在 §3.23 查到"

    audit = _count(
        db_conn,
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'commission_rule_changed' AND ref_table = 'commission_rule'",
    )
    assert audit >= 1, "契约 §3.24 明确要求配置佣金口径时同时写 audit_log（NFR-009）"

    overlap = client.put("/api/admin/commission-rules", json={**rule, "rate_bp": 300})
    assert overlap.status_code == 409, f"契约 §3.24 期望 409(MT-1012)，实际 {overlap.status_code}"
    assert_error_response(overlap, expected_code="MT-1012")


@pytest.mark.parametrize(
    "rule",
    [
        {},
        {"pay_object": "bank", "rate_bp": 200, "effective_from": today_iso()},
        {"pay_object": "merchant", "rate_bp": 0, "effective_from": today_iso()},
        {"pay_object": "merchant", "rate_bp": 10001, "effective_from": today_iso()},
        {"pay_object": "merchant", "rate_bp": 200, "effective_from": "not-a-date"},
        {"pay_object": "merchant", "rate_bp": 200, "effective_from": "2099-01-02", "effective_to": "2099-01-01"},
    ],
)
def test_commission_rule_invalid_body_mt_1008(client, rule):
    response = client.put("/api/admin/commission-rules", json=rule)
    assert response.status_code == 422, f"契约 §3.24/§4 期望 422(MT-1008)，实际 {response.status_code}（rule={rule!r}）"
    assert_error_response(response, expected_code="MT-1008")
