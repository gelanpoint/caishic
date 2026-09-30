"""`T-009` 契约测试：交易创建与改价/抹零。

关联：`REQ-005`、`REQ-006`、`REQ-007`、`REQ-027`；`AC-001`、`AC-007`、`AC-016`、`AC-017`。
覆盖契约：§3.6 创建交易并计价、§3.9 改价/抹零（含留痕）。
错误码用例：`MT-1002`（重量越界）、`MT-1006`（价目表缺失）、`MT-1011`（需确认改价幅度）、
`MT-1012`（幂等键复用于不同请求体）、`MT-1005`（会话无效）、`MT-1001`（状态不允许改价）、`MT-1008`。

计价口径（`data-model.md` §0 / §7 注①）：`金额(分) = round(单价(分/公斤) × 重量(克) ÷ 1000)`。
本文件一律**先把单价设成能整除的整数**（400 / 500 分）再计价，避免把"四舍五入"的边界
（`round` 的银行家舍入 vs 四舍五入）混进契约测试 —— 边界取整属实现层单测，不属契约面。
"""

from __future__ import annotations

import pytest

from contract_support import (
    assert_error_response,
    bind_stall_session,
    create_priced_transaction,
    json_of,
    session_headers,
    today_iso,
)

STALL = "A-03"  # 本组专用摊位，降低跨组状态干扰
TXN_ITEM_FIELDS = {"amount_cents", "original_unit_price_cents", "final_unit_price_cents", "category_id"}


def _price_map(client, token: str) -> dict[int, int]:
    """取回当日价目表 → `{product_id: unit_price_cents}`。"""
    payload = client.get(
        "/api/merchant/price-list",
        query_string={"business_date": today_iso()},
        headers=session_headers(token),
    ).get_json(silent=True)
    items = payload if isinstance(payload, list) else (payload or {}).get("items", [])
    return {item["product_id"]: item["unit_price_cents"] for item in items}


def _set_prices(client, token: str, prices: dict[int, int], key: str) -> None:
    """按契约 §3.4 把指定商品的当日单价设成**能整除**的值（消除取整歧义）。"""
    response = client.post(
        "/api/merchant/price-list",
        json={
            "business_date": today_iso(),
            "items": [{"product_id": pid, "unit_price_cents": price} for pid, price in prices.items()],
        },
        headers=session_headers(token, key),
    )
    assert response.status_code == 200, f"前置：契约 §3.4 设价失败 {response.status_code}"


def _product_ids(client, token: str, count: int) -> list[int]:
    products = json_of(client.get("/api/merchant/products", headers=session_headers(token)))
    assert len(products) >= count, f"种子商品不足（需要 {count} 个，实际 {len(products)} 个）"
    return [item["id"] for item in products[:count]]


def _item_ids(client, token: str, transaction_no: str) -> list[int]:
    """从契约 §3.8 交易详情取明细行 `id`（§3.9 的 `item_id` 只能从这里来）。"""
    detail = json_of(
        client.get(f"/api/merchant/transactions/{transaction_no}", headers=session_headers(token))
    )
    items = detail.get("items") or []
    assert items, "契约 §3.8 未返回交易明细，无法取得 item_id"
    ids = [item.get("id") for item in items]
    assert all(isinstance(i, int) for i in ids), f"契约 §2.9 明细行应含整数 id，实际 {ids!r}"
    return ids


# ---------------------------------------------------------------------------
# §3.6 创建交易并计价（`AC-001` / `AC-016`）
# ---------------------------------------------------------------------------


def test_create_transaction_prices_and_returns_item_breakdown(client):
    """`AC-001`：选品 + 重量 → 金额；响应含每行的原价/改后价/金额与品类。"""
    token = bind_stall_session(client, STALL)
    product_id = _product_ids(client, token, 1)[0]
    _set_prices(client, token, {product_id: 400}, "t009-price-1")  # 400 分/公斤 × 1250 克 = 500 分

    response = client.post(
        "/api/merchant/transactions",
        json={"items": [{"product_id": product_id, "weight_grams": 1250}]},
        headers=session_headers(token, "t009-txn-1"),
    )
    assert response.status_code == 201, f"契约 §3.6 期望 201，实际 {response.status_code}"
    payload = json_of(response)
    assert payload.get("status") == "priced", f"契约 §3.6 要求 status=priced，实际 {payload.get('status')!r}"
    assert isinstance(payload.get("transaction_no"), str) and payload["transaction_no"]
    items = payload.get("items") or []
    assert len(items) == 1, f"契约 §3.6 期望 1 行明细，实际 {len(items)}"
    assert TXN_ITEM_FIELDS <= set(items[0]), f"契约 §3.6 明细缺字段：{sorted(items[0])}"
    assert items[0]["amount_cents"] == 500, f"计价口径不符：期望 500 分，实际 {items[0]['amount_cents']}"
    assert items[0]["original_unit_price_cents"] == 400
    assert payload["total_amount_cents"] == 500, f"契约 §3.6 总额应为 500，实际 {payload['total_amount_cents']}"


def test_create_transaction_accumulates_multiple_items(client):
    """`AC-016`/`REQ-006`：多商品在同一笔内累加。"""
    token = bind_stall_session(client, STALL)
    first, second = _product_ids(client, token, 2)
    _set_prices(client, token, {first: 400, second: 500}, "t009-price-2")  # 400×1250/1000=500；500×2000/1000=1000

    response = client.post(
        "/api/merchant/transactions",
        json={"items": [{"product_id": first, "weight_grams": 1250}, {"product_id": second, "weight_grams": 2000}]},
        headers=session_headers(token, "t009-txn-2"),
    )
    assert response.status_code == 201, f"契约 §3.6 期望 201，实际 {response.status_code}"
    payload = json_of(response)
    amounts = [item["amount_cents"] for item in payload["items"]]
    assert amounts == [500, 1000], f"逐行金额不符（AC-016）：{amounts}"
    assert payload["total_amount_cents"] == sum(amounts) == 1500, (
        f"契约 §3.6 总额应等于各行之和 1500，实际 {payload['total_amount_cents']}"
    )


@pytest.mark.parametrize("weight_grams", [0, -1, 50001, 200000])
def test_weight_out_of_range_returns_mt_1002_and_creates_nothing(client, db_conn, weight_grams):
    """`AC-017`/`REQ-027`：重量 ≤0 或 >50 公斤 → 422 `MT-1002`，**不创建任何交易**。"""
    token = bind_stall_session(client, STALL)
    product_id = _product_ids(client, token, 1)[0]
    stall_id = db_conn.execute("SELECT id FROM stall WHERE stall_no = ?", (STALL,)).fetchone()["id"]
    before = db_conn.execute(
        "SELECT COUNT(*) AS n FROM \"transaction\" WHERE stall_id = ?", (stall_id,)
    ).fetchone()["n"]

    response = client.post(
        "/api/merchant/transactions",
        json={"items": [{"product_id": product_id, "weight_grams": weight_grams}]},
        headers=session_headers(token, f"t009-weight-{weight_grams}"),
    )
    assert response.status_code == 422, f"契约 §4 期望 422，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1002")

    after = db_conn.execute(
        "SELECT COUNT(*) AS n FROM \"transaction\" WHERE stall_id = ?", (stall_id,)
    ).fetchone()["n"]
    assert after == before, f"重量越界却落了库（REQ-027 要求不创建任何交易）：{before} → {after}"


def test_missing_price_item_returns_mt_1006_without_zero_price_deal(client, db_conn):
    """契约 §4 `MT-1006`：计价时该摊位该商品当日无 `price_item` → 409，**不得以 0 元成交**。"""
    token = bind_stall_session(client, STALL)
    product_id = _product_ids(client, token, 1)[0]
    stall_id = db_conn.execute("SELECT id FROM stall WHERE stall_no = ?", (STALL,)).fetchone()["id"]
    db_conn.execute(
        "DELETE FROM price_item WHERE stall_id = ? AND product_id = ? AND business_date = ?",
        (stall_id, product_id, today_iso()),
    )
    db_conn.commit()

    before = db_conn.execute(
        "SELECT COUNT(*) AS n FROM \"transaction\" WHERE stall_id = ?", (stall_id,)
    ).fetchone()["n"]
    response = client.post(
        "/api/merchant/transactions",
        json={"items": [{"product_id": product_id, "weight_grams": 1000}]},
        headers=session_headers(token, "t009-noprice-1"),
    )
    assert response.status_code == 409, f"契约 §4 期望 409，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1006")
    after = db_conn.execute(
        "SELECT COUNT(*) AS n FROM \"transaction\" WHERE stall_id = ?", (stall_id,)
    ).fetchone()["n"]
    assert after == before, "价目缺失却落了库（契约 §4：不得以 0 元成交）"

    _set_prices(client, token, {product_id: 400}, "t009-restore-price")  # 复原，避免影响后续用例


def test_create_transaction_requires_session_and_idempotency_key(client):
    token = bind_stall_session(client, STALL)
    product_id = _product_ids(client, token, 1)[0]
    body = {"items": [{"product_id": product_id, "weight_grams": 1000}]}

    no_session = client.post("/api/merchant/transactions", json=body, headers={"Idempotency-Key": "k1"})
    assert no_session.status_code == 401, f"契约 §1.6 期望 401，实际 {no_session.status_code}"
    assert_error_response(no_session, expected_code="MT-1005")

    no_key = client.post("/api/merchant/transactions", json=body, headers=session_headers(token))
    assert no_key.status_code == 422, f"契约 §3.6 要求 Idempotency-Key 必填（422 MT-1008），实际 {no_key.status_code}"
    assert_error_response(no_key, expected_code="MT-1008")


# ---------------------------------------------------------------------------
# §1.7 / §3.6 幂等语义
# ---------------------------------------------------------------------------


def test_same_idempotency_key_same_body_returns_first_result(client, db_conn):
    """契约 §1.3/§1.7：重复请求返回**首次结果**，不新建交易、不重复计佣。"""
    token = bind_stall_session(client, STALL)
    product_id = _product_ids(client, token, 1)[0]
    _set_prices(client, token, {product_id: 400}, "t009-price-3")
    body = {"items": [{"product_id": product_id, "weight_grams": 1000}]}
    headers = session_headers(token, "t009-idem-same")

    first = client.post("/api/merchant/transactions", json=body, headers=headers)
    assert first.status_code == 201, f"契约 §3.6 期望 201，实际 {first.status_code}"
    first_no = json_of(first)["transaction_no"]

    second = client.post("/api/merchant/transactions", json=body, headers=headers)
    assert second.status_code in {200, 201}, f"重复请求不应报错（契约 §1.3），实际 {second.status_code}"
    assert json_of(second)["transaction_no"] == first_no, "契约 §1.3：重复请求必须返回**首次结果**"

    stall_id = db_conn.execute("SELECT id FROM stall WHERE stall_no = ?", (STALL,)).fetchone()["id"]
    rows = db_conn.execute(
        "SELECT COUNT(*) AS n FROM \"transaction\" WHERE stall_id = ? AND transaction_no = ?",
        (stall_id, first_no),
    ).fetchone()["n"]
    assert rows == 1, f"重复请求产生了多笔交易（REQ-015）：{rows}"


def test_idempotency_key_reused_with_different_body_returns_mt_1012(client):
    """契约 §4 `MT-1012`(a)：幂等键已存在且请求体指纹不同 → 409。"""
    token = bind_stall_session(client, STALL)
    first, second = _product_ids(client, token, 2)
    _set_prices(client, token, {first: 400, second: 400}, "t009-price-4")
    key = "t009-idem-conflict"

    ok = client.post(
        "/api/merchant/transactions",
        json={"items": [{"product_id": first, "weight_grams": 1000}]},
        headers=session_headers(token, key),
    )
    assert ok.status_code == 201, f"前置失败：{ok.status_code}"

    conflict = client.post(
        "/api/merchant/transactions",
        json={"items": [{"product_id": second, "weight_grams": 1000}]},
        headers=session_headers(token, key),
    )
    assert conflict.status_code == 409, f"契约 §4 期望 409，实际 {conflict.status_code}"
    assert_error_response(conflict, expected_code="MT-1012")


# ---------------------------------------------------------------------------
# §3.9 改价 / 抹零（`AC-007` / `AC-008`）
# ---------------------------------------------------------------------------


def test_price_change_records_audit_and_updates_total(client, db_conn):
    """`AC-007`：改价返回 `audit_event = price_change`，并写入只增不改的留痕。"""
    token = bind_stall_session(client, STALL)
    product_id = _product_ids(client, token, 1)[0]
    _set_prices(client, token, {product_id: 400}, "t009-price-5")
    created = create_priced_transaction(client, token, idempotency_key="t009-txn-pc-1", weights_grams=[1250])
    item_id = _item_ids(client, token, created["transaction_no"])[0]

    response = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/price-change",
        json={"item_id": item_id, "final_unit_price_cents": 420},  # +5%，未超 50%
        headers=session_headers(token, "t009-pc-1"),
    )
    assert response.status_code == 200, f"契约 §3.9 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert payload.get("audit_event") == "price_change", f"契约 §3.9 要求 audit_event=price_change：{payload!r}"
    assert payload.get("transaction_no") == created["transaction_no"]
    # 1250 克 × 420 分/公斤 = 525 分
    assert payload.get("total_amount_cents") == 525, f"改价后总额应重算为 525，实际 {payload.get('total_amount_cents')}"

    rows = db_conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = ?", ("price_change",)
    ).fetchone()["n"]
    assert rows >= 1, "契约 §3.9 要求写入留痕（audit_log.event_type = price_change），实际 0 行"


def test_price_change_over_50_percent_needs_confirmation_mt_1011(client):
    """`AC-007`/`REQ-007`：单笔幅度 >50% 未回传 `confirm_over_threshold` → 409 `MT-1011`；回传后放行。"""
    token = bind_stall_session(client, STALL)
    product_id = _product_ids(client, token, 1)[0]
    _set_prices(client, token, {product_id: 400}, "t009-price-6")
    created = create_priced_transaction(client, token, idempotency_key="t009-txn-pc-2")
    item_id = _item_ids(client, token, created["transaction_no"])[0]
    path = f"/api/merchant/transactions/{created['transaction_no']}/price-change"

    blocked = client.post(
        path, json={"item_id": item_id, "final_unit_price_cents": 800}, headers=session_headers(token, "t009-pc-2")
    )
    assert blocked.status_code == 409, f"契约 §3.9/§4 期望 409，实际 {blocked.status_code}"
    assert_error_response(blocked, expected_code="MT-1011")

    confirmed = client.post(
        path,
        json={"item_id": item_id, "final_unit_price_cents": 800, "confirm_over_threshold": True},
        headers=session_headers(token, "t009-pc-3"),
    )
    assert confirmed.status_code == 200, f"回传确认后必须放行（系统不阻止该操作），实际 {confirmed.status_code}"


def test_round_off_is_not_counted_in_price_consistency(client):
    """`AC-008`：抹零**不计入**标价一致率 —— 抹零后看板的一致率不应因该笔而下降。"""
    token = bind_stall_session(client, STALL)
    product_id = _product_ids(client, token, 1)[0]
    _set_prices(client, token, {product_id: 400}, "t009-price-7")

    def consistency() -> int:
        response = client.get(
            "/api/merchant/dashboard",
            query_string={"business_date": today_iso()},
            headers=session_headers(token),
        )
        assert response.status_code == 200, f"契约 §3.12 期望 200，实际 {response.status_code}"
        return json_of(response)["price_consistency_bp"]

    before = consistency()
    created = create_priced_transaction(client, token, idempotency_key="t009-txn-ro-1")
    item_id = _item_ids(client, token, created["transaction_no"])[0]
    response = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/price-change",
        json={"item_id": item_id, "round_off_cents": 5},
        headers=session_headers(token, "t009-ro-1"),
    )
    assert response.status_code == 200, f"契约 §3.9 期望 200，实际 {response.status_code}"
    assert consistency() >= before, f"抹零被计入了标价一致率（AC-008）：{before} → {consistency()}"


def test_price_change_on_paid_transaction_returns_mt_1001(client):
    """契约 §3.9/§4 `MT-1001`：交易不在可改价状态（此处已收款）→ 409。"""
    token = bind_stall_session(client, STALL)
    product_id = _product_ids(client, token, 1)[0]
    _set_prices(client, token, {product_id: 400}, "t009-price-8")
    created = create_priced_transaction(client, token, idempotency_key="t009-txn-pc-3")
    item_id = _item_ids(client, token, created["transaction_no"])[0]

    paid = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/payment",
        json={"method": "cash", "operator": "probe"},
        headers=session_headers(token, "t009-pay-1"),
    )
    assert paid.status_code == 200, f"前置：契约 §3.10 期望 200，实际 {paid.status_code}"

    response = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/price-change",
        json={"item_id": item_id, "final_unit_price_cents": 410},
        headers=session_headers(token, "t009-pc-4"),
    )
    assert response.status_code == 409, f"契约 §4 期望 409(MT-1001)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1001")


def test_price_change_requires_both_or_either_amount_field_mt_1008(client):
    """契约 §3.9：`final_unit_price_cents` 与 `round_off_cents` 二选一；两者都不给应判参数校验失败。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key="t009-txn-pc-4")
    item_id = _item_ids(client, token, created["transaction_no"])[0]
    response = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/price-change",
        json={"item_id": item_id},
        headers=session_headers(token, "t009-pc-5"),
    )
    assert response.status_code == 422, f"契约 §3.9/§4 期望 422，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1008")
