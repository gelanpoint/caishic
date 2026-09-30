"""`T-008` 契约测试：秤端会话、商品、价目表（+ 秤端读端点 §3.7/§3.8/§3.12）。

关联：`REQ-003`、`REQ-004`、`REQ-008`、`REQ-032`；`AC-006`、`AC-014`、`AC-015`、`AC-021`。
覆盖契约：§3.2 会话、§3.3/§3.4 价目表、§3.5 商品、§3.7 交易列表、§3.8 交易详情、§3.12 商户看板。
错误码用例：`MT-1004`（越权访问非本摊位数据）、`MT-1005`（会话无效）、`MT-1009`（资源不存在）、`MT-1008`（参数校验失败）。

> **范围说明（缺口已于 `2026-09-30` 补进规则，本文件不再"自行承接"）**：`tasks.md` §1.3 的合并规则原先把
> §3.2~3.5 归 T-008、§3.6+§3.9 归 T-009、§3.10+§3.17 归 T-010、§3.11 归 T-011、§3.13~3.15 归 T-012、
> §3.18~3.19 归 T-014、§3.20~3.31 归 T-013 —— **§3.7 / §3.8 / §3.12 三处没有归属**，而 `MT-1004`
> 只在 §3.7 声明。该缺口由本文件执行时发现并上报父代理，**现已补进 `tasks.md` §1.3**
> （连同补录时新发现的同类的 §3.1 一起，四处一并写明 → T-008 / T-007），故本文件承接这三个秤端读端点
> 是**规则内的归属**，不再是例外。

> **契约歧义（如实标注，不自行发明）**：§3.3 写"响应(200): 数组"，同时又写"无记录时返回空数组 +
> `price_list_missing: true`" —— 数组无法携带同级的 `price_list_missing`。本文件对两种形态都接受
> （数组形态，或 `{items: [...], price_list_missing: bool}` 对象形态），仅在请求明确"空"时要求后者的标记。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from contract_support import (
    assert_endpoint_implemented,
    assert_error_response,
    assert_exact_keys,
    bind_stall_session,
    create_priced_transaction,
    json_of,
    session_headers,
    today_iso,
)

SESSION_FIELDS = {"session_token", "stall_no", "stall_name"}
PRODUCT_FIELDS = {"id", "name", "icon_key", "hotkey", "status"}
DASHBOARD_FIELDS = {
    "business_date",
    "txn_count",
    "gross_amount_cents",
    "refund_amount_cents",
    "commission_amount_cents",
    "price_consistency_bp",
}


def _price_items(payload) -> list:
    """容错取价目表条目（见模块 docstring 的契约歧义说明）。"""
    if isinstance(payload, list):
        return payload
    assert isinstance(payload, dict) and isinstance(payload.get("items"), list), (
        f"契约 §3.3 期望数组（或含 items 的对象），实际 {type(payload).__name__}: {payload!r}"
    )
    return payload["items"]


# ---------------------------------------------------------------------------
# §3.2 秤端会话
# ---------------------------------------------------------------------------


def test_session_binds_stall_and_returns_token(client):
    """`AC-021` 前置：绑定摊位后下发会话 Token，字段与契约 §3.2 一致。"""
    response = client.post("/api/merchant/session", json={"stall_no": "A-01"})
    assert response.status_code == 201, f"契约 §3.2 期望 201，实际 {response.status_code}"
    payload = json_of(response)
    assert_exact_keys(payload, SESSION_FIELDS, "§3.2 响应")
    assert payload["stall_no"] == "A-01"
    assert isinstance(payload["stall_name"], str) and payload["stall_name"].strip()


def test_session_rejects_unknown_stall_with_mt_1009(client):
    # 前置防"假绿"：端点不存在时通用 404 处理器同样返回 MT-1009，那种绿不来自实现
    assert_endpoint_implemented(client.application, "POST", "/api/merchant/session")
    response = client.post("/api/merchant/session", json={"stall_no": "Z-99"})
    assert response.status_code == 404, f"契约 §3.2 期望 404，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1009")


@pytest.mark.parametrize("body", [{}, {"stall_no": ""}, {"stall_no": "X" * 17}])
def test_session_rejects_invalid_body_with_mt_1008(client, body):
    response = client.post("/api/merchant/session", json=body)
    assert response.status_code == 422, f"契约 §3.2/§4 期望 422(MT-1008)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1008")


# ---------------------------------------------------------------------------
# §3.5 秤端商品（`AC-006`：图标 / 快捷键，无文本输入）
# ---------------------------------------------------------------------------


def test_products_returns_active_items_with_icon_and_hotkey(client):
    token = bind_stall_session(client)
    response = client.get("/api/merchant/products", headers=session_headers(token))
    assert response.status_code == 200, f"契约 §3.5 期望 200，实际 {response.status_code}"
    products = json_of(response)
    assert isinstance(products, list) and products, "契约 §3.5 应返回非空商品数组"
    for item in products:
        assert_exact_keys(item, PRODUCT_FIELDS | {"category_id", "stall_id"}, "§3.5 商品元素")
        assert {"id", "name", "icon_key", "hotkey"} <= set(item), f"商品元素缺字段：{sorted(item)}"
        assert item["status"] == "active", f"契约 §3.5 要求只返回 status=active，实际 {item['status']!r}"


def test_products_requires_valid_session_with_mt_1005(client):
    missing = client.get("/api/merchant/products")
    assert missing.status_code == 401, f"契约 §1.6/§4 期望 401(MT-1005)，实际 {missing.status_code}"
    assert_error_response(missing, expected_code="MT-1005")

    invalid = client.get("/api/merchant/products", headers={"X-Stall-Session": "not-a-real-token"})
    assert invalid.status_code == 401
    assert_error_response(invalid, expected_code="MT-1005")


# ---------------------------------------------------------------------------
# §3.3 / §3.4 价目表（`AC-014`/`AC-015`）
# ---------------------------------------------------------------------------


def test_price_list_returns_items_for_business_date(client):
    token = bind_stall_session(client)
    response = client.get(
        "/api/merchant/price-list",
        query_string={"business_date": today_iso()},
        headers=session_headers(token),
    )
    assert response.status_code == 200, f"契约 §3.3 期望 200，实际 {response.status_code}"
    items = _price_items(json_of(response))
    assert items, "种子数据当日价目表应为 250 行（T-005 实测），此处不应为空"
    for item in items:
        assert {"product_id", "unit_price_cents"} <= set(item), f"§2.7 价目表元素缺字段：{sorted(item)}"
        assert isinstance(item["unit_price_cents"], int) and item["unit_price_cents"] >= 1, (
            f"契约 §3.4 要求 unit_price_cents ≥ 1，实际 {item['unit_price_cents']!r}"
        )


def test_price_list_empty_day_marks_missing(client):
    """契约 §3.3：无记录时返回空数组 + `price_list_missing: true`（供秤端提示先设价）。"""
    token = bind_stall_session(client)
    future = (date.today() + timedelta(days=30)).isoformat()
    response = client.get(
        "/api/merchant/price-list", query_string={"business_date": future}, headers=session_headers(token)
    )
    assert response.status_code == 200, f"契约 §3.3 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert _price_items(payload) == [], f"未来的营业日不应有价目表，实际 {payload!r}"
    assert payload.get("price_list_missing") is True, (
        f"契约 §3.3 要求空价目表返回 price_list_missing=true，实际 {payload!r}"
    )


def test_price_list_invalid_session_with_mt_1005(client):
    response = client.get(
        "/api/merchant/price-list",
        query_string={"business_date": today_iso()},
        headers={"X-Stall-Session": "bad"},
    )
    assert response.status_code == 401
    assert_error_response(response, expected_code="MT-1005")


def test_price_list_set_manual_items(client):
    """`AC-015`：逐条调整价目表（`source = manual`）。"""
    token = bind_stall_session(client)
    products = json_of(client.get("/api/merchant/products", headers=session_headers(token)))
    product_id = products[0]["id"]
    response = client.post(
        "/api/merchant/price-list",
        json={"business_date": today_iso(), "items": [{"product_id": product_id, "unit_price_cents": 777}]},
        headers=session_headers(token, "t008-price-set-1"),
    )
    assert response.status_code == 200, f"契约 §3.4 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert payload.get("business_date") == today_iso()
    assert payload.get("source") == "manual", f"契约 §3.4 期望 source=manual，实际 {payload.get('source')!r}"

    items = _price_items(payload)
    matched = [item for item in items if item.get("product_id") == product_id]
    assert matched and matched[0]["unit_price_cents"] == 777, f"改后价未生效：{matched!r}"


def test_price_list_copy_from_previous_day(client):
    """`AC-014`/`REQ-003`：复制上一营业日价格（`source = copied_previous_day`）。"""
    token = bind_stall_session(client)
    target_day = (date.today() + timedelta(days=1)).isoformat()
    response = client.post(
        "/api/merchant/price-list",
        json={"business_date": target_day, "copy_from_previous_day": True},
        headers=session_headers(token, "t008-price-copy-1"),
    )
    assert response.status_code == 200, f"契约 §3.4 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert payload.get("source") == "copied_previous_day", (
        f"契约 §3.4 期望 source=copied_previous_day，实际 {payload.get('source')!r}"
    )
    assert _price_items(payload), "复制上一营业日价格后不应为空"


def test_price_list_copy_without_previous_day_data_with_mt_1009(client):
    """契约 §3.4：上一营业日无可复制数据 → 404 `MT-1009`。"""
    token = bind_stall_session(client)
    far_day = (date.today() + timedelta(days=60)).isoformat()
    response = client.post(
        "/api/merchant/price-list",
        json={"business_date": far_day, "copy_from_previous_day": True},
        headers=session_headers(token, "t008-price-copy-2"),
    )
    assert response.status_code == 404, f"契约 §3.4 期望 404，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1009")


def test_price_list_missing_body_with_mt_1008(client):
    token = bind_stall_session(client)
    response = client.post(
        "/api/merchant/price-list", json={}, headers=session_headers(token, "t008-price-bad-1")
    )
    assert response.status_code == 422, f"契约 §3.4 期望 422，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1008")


# ---------------------------------------------------------------------------
# §3.7 / §3.8 / §3.12 秤端读端点 + `MT-1004`（`AC-021` 摊位数据边界）
# ---------------------------------------------------------------------------


def test_transaction_list_is_limited_to_bound_stall(client):
    """`REQ-032`/`AC-021`：秤端列表只含本摊位数据。"""
    token_a = bind_stall_session(client, "A-01")
    created = create_priced_transaction(client, token_a, idempotency_key="t008-scope-txn-1")

    token_b = bind_stall_session(client, "A-02")
    response = client.get(
        "/api/merchant/transactions",
        query_string={"business_date": today_iso(), "limit": 200},
        headers=session_headers(token_b),
    )
    assert response.status_code == 200, f"契约 §3.7 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert {"total", "items"} <= set(payload), f"契约 §3.7 要求分页对象 {{total, items}}，实际 {sorted(payload)}"
    leaked = [
        item for item in payload["items"] if item.get("transaction_no") == created["transaction_no"]
    ]
    assert not leaked, f"A-02 的列表泄露了 A-01 的交易（REQ-032/AC-021）：{leaked!r}"


def test_cross_stall_transaction_detail_with_mt_1004(client):
    """`MT-1004`（403）：请求非本摊位数据被拒（契约 §3.7/§4）。"""
    token_a = bind_stall_session(client, "A-01")
    created = create_priced_transaction(client, token_a, idempotency_key="t008-scope-txn-2")
    token_b = bind_stall_session(client, "A-02")

    response = client.get(
        f"/api/merchant/transactions/{created['transaction_no']}", headers=session_headers(token_b)
    )
    assert response.status_code == 403, f"契约 §4 期望 403(MT-1004)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1004")


def test_transaction_detail_exposes_receipt_data_without_printing(client):
    """`REQ-012`/`AC-001`：详情含 items / payments 与固定 `printable: false`（不打印）。"""
    token = bind_stall_session(client)
    created = create_priced_transaction(client, token, idempotency_key="t008-detail-txn-1")
    response = client.get(
        f"/api/merchant/transactions/{created['transaction_no']}", headers=session_headers(token)
    )
    assert response.status_code == 200, f"契约 §3.8 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert payload.get("transaction_no") == created["transaction_no"]
    assert payload.get("printable") is False, "契约 §3.8 要求 printable 固定为 false"
    assert isinstance(payload.get("items"), list) and payload["items"], "契约 §3.8 要求含 items"
    assert isinstance(payload.get("payments"), list), "契约 §3.8 要求含 payments 数组"


def test_transaction_detail_unknown_with_mt_1009(client):
    token = bind_stall_session(client)
    response = client.get(
        "/api/merchant/transactions/ZZ-NOT-A-REAL-TXN", headers=session_headers(token)
    )
    assert response.status_code == 404
    assert_error_response(response, expected_code="MT-1009")


def test_merchant_dashboard_shape(client):
    """契约 §3.12 商户端看板字段。"""
    token = bind_stall_session(client)
    response = client.get(
        "/api/merchant/dashboard",
        query_string={"business_date": today_iso()},
        headers=session_headers(token),
    )
    assert response.status_code == 200, f"契约 §3.12 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert_exact_keys(payload, DASHBOARD_FIELDS, "§3.12 响应")
    assert isinstance(payload["price_consistency_bp"], int) and 0 <= payload["price_consistency_bp"] <= 10000
