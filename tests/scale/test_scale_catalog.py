"""§3.3 商品字典 / §3.4 价目表的契约测试（`T-SCALE-03`）。

覆盖：`REQ-002`、`REQ-037`；`AC-028`（离线计价的依据 = 中台价目表）；错误码 `MT-2001`。

两条纪律：
1. **窄响应体按字段白名单逐一相等**（契约 §1.1 / §3.3：不含成本、不含佣金相关字段；`REQ-036`、`NFR-015`）；
2. 白名单断言器与"禁止字段"扫描器**各带灵敏度负例**（`AGENTS.md`：「不验证灵敏度的验证是摆设」）——
   故意注入一个多出来的字段 ⇒ 必红 ⇒ 清掉 ⇒ 复绿。
"""

from __future__ import annotations

import pytest

from scale_provision import EMPTY_PRICE_DATE, active_device_token
from scale_support import (
    CATALOG_CATEGORY_KEYS,
    CATALOG_KEYS,
    CATALOG_PRODUCT_KEYS,
    PRICE_LIST_ITEM_KEYS,
    PRICE_LIST_KEYS,
    SEED_STALL_NO,
    assert_exact_keys,
    assert_narrow_body,
    assert_scale_error_response,
    forbidden_narrow_keys,
    get_catalog,
    get_price_list,
    json_of,
    stall_id_of,
    today_iso,
)


def _token(client, db_conn) -> str:
    """数据面要求设备**已激活**（未激活 → `MT-2001`），故前置走一次 §3.1 激活。"""
    return active_device_token(client, db_conn)


# ---------------------------------------------------------------------------
# 灵敏度负例：两个检查器都必须能失败（纯函数，不依赖服务）
# ---------------------------------------------------------------------------


def test_narrow_body_checker_flags_an_injected_extra_field():
    """灵敏度：白名单断言器必须对**多出来的字段**报错，且干净样例必须通过。"""
    clean = {"catalog_version": 8, "products": [], "categories": []}
    assert_narrow_body(clean, CATALOG_KEYS, "干净样例")  # 干净 ⇒ 复绿
    with pytest.raises(AssertionError):
        assert_narrow_body({**clean, "total_margin_cents": 1}, CATALOG_KEYS, "注入一个字段")  # ⇒ 必红


def test_forbidden_narrow_key_scanner_detects_cost_and_commission_fields():
    """灵敏度：成本 / 佣金类字段必须被扫出来；干净样例必须返回空（不是"永远有命中"）。"""
    assert forbidden_narrow_keys({"catalog_version": 8, "products": [], "categories": []}) == []
    hits = forbidden_narrow_keys(
        {"products": [{"product_id": 1, "cost_cents": 10, "commission_rate_bp": 200}]}
    )
    assert len(hits) == 2, f"成本/佣金字段必须被扫出，实际命中：{hits}"


# ---------------------------------------------------------------------------
# §3.3 字典
# ---------------------------------------------------------------------------


def test_catalog_returns_narrow_body_with_exact_field_whitelist(client, db_conn):
    """§3.3：响应只含秤端选品与计价所需字段 —— 白名单逐一相等 + 禁止字段扫描（`REQ-036`、`NFR-015`）。"""
    response = get_catalog(client, _token(client, db_conn))
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    payload = json_of(response)
    assert_narrow_body(payload, CATALOG_KEYS, "§3.3 字典响应")
    assert payload["products"], "种子数据应下发本摊位商品"
    for product in payload["products"]:
        assert_exact_keys(product, CATALOG_PRODUCT_KEYS, "§3.3 字典商品")
    for category in payload["categories"]:
        assert_exact_keys(category, CATALOG_CATEGORY_KEYS, "§3.3 字典品类")


def test_catalog_only_contains_the_bound_stalls_products(client, db_conn):
    """§3.3 + `REQ-032`：字典只能含**本摊位**商品（授权取自绑定，不取自请求参数）。"""
    response = get_catalog(client, _token(client, db_conn))
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    stall_id = stall_id_of(db_conn, SEED_STALL_NO)
    own = {
        int(row["id"])
        for row in db_conn.execute("SELECT id FROM product WHERE stall_id = ?", (stall_id,)).fetchall()
    }
    returned = {int(item["product_id"]) for item in json_of(response)["products"]}
    assert returned <= own, f"字典下发了非本摊位商品：{sorted(returned - own)}"


def test_catalog_without_token_returns_mt2001(client):
    """§4 `MT-2001`：字典端点同样要求设备令牌。"""
    response = get_catalog(client, None)
    assert response.status_code == 401, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2001")


# ---------------------------------------------------------------------------
# §3.4 价目表
# ---------------------------------------------------------------------------


def test_price_list_returns_items_matching_the_stalls_prices(client, db_conn):
    """§3.4 + `AC-028`：价目表是离线计价的**唯一依据**，逐条单价必须与库里的权威价目表相同。"""
    business_date = today_iso()
    response = get_price_list(client, _token(client, db_conn), business_date=business_date)
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    payload = json_of(response)
    assert_narrow_body(payload, PRICE_LIST_KEYS, "§3.4 价目表响应")
    assert payload["business_date"] == business_date
    assert payload["items"], "种子数据应有当日价目表"
    for item in payload["items"]:
        assert_exact_keys(item, PRICE_LIST_ITEM_KEYS, "§3.4 价目表条目")
    stall_id = stall_id_of(db_conn, SEED_STALL_NO)
    authoritative = {
        int(row["product_id"]): int(row["unit_price_cents"])
        for row in db_conn.execute(
            "SELECT product_id, unit_price_cents FROM price_item WHERE stall_id = ? AND business_date = ?",
            (stall_id, business_date),
        ).fetchall()
    }
    for item in payload["items"]:
        product_id = int(item["product_id"])
        assert product_id in authoritative, f"价目表下发了非本摊位/非当日的商品：{product_id}"
        assert int(item["unit_price_cents"]) == authoritative[product_id], (
            f"商品 {product_id} 下发的单价与库里权威价目表不一致"
        )


def test_price_list_empty_for_a_date_without_prices_returns_empty_array_not_error(client, db_conn):
    """§3.4：**价目表为空时不得报错** —— 返回空 `items` 数组。

    真造"空价目表"态：`EMPTY_PRICE_DATE` 在库里**没有任何价目表行**（不动种子数据）。
    中台若在这里报错，秤端就没有"拒绝计价并提示先设价"的余地 —— 那正是契约要避免的
    "缺价时静默按 0 元成交"（对应主契约 `MT-1006` 的语义）。
    """
    response = get_price_list(client, _token(client, db_conn), business_date=EMPTY_PRICE_DATE)
    assert response.status_code == 200, (
        f"空价目表不得报错（契约 §3.4），实际 {response.status_code}："
        f"{response.get_data(as_text=True)[:300]}"
    )
    payload = json_of(response)
    assert payload["items"] == [], f"空价目表必须返回空数组，实际 {payload['items']!r}"
    assert payload["business_date"] == EMPTY_PRICE_DATE


def test_price_list_without_token_returns_mt2001(client):
    """§4 `MT-2001`：价目表端点同样要求设备令牌。"""
    response = get_price_list(client, None)
    assert response.status_code == 401, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2001")
