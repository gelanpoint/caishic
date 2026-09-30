"""`T-032` 端到端验证（三之二）：**目录、价目表与顾客页**（真实服务 + 真库）。

| AC | 本文件验证的行为 |
| --- | --- |
| `AC-014` | 别名归集：按摊位别名选的品**归入标准品类**；新增别名映射可查 |
| `AC-015` | 「复制昨天价」→ 与上一营业日逐条一致；随后**逐条调整**只改指定那条 |
| `AC-011` | 顾客两个端点的响应**键集合恰好等于白名单**（多一个键就是无来源字段） |

与 `test_coverage_rest.py`（交易行为）、`test_coverage_admin.py`（运营端读模型）按**端点语义**分工
（单文件 ≤400 行，`quality-gates.md` §1.2）。`AC-014` 的样本必须"标准品类另有其名"
（如摊位的「小油菜」记成标准品类「上海青」），否则证明不了归一确实发生。
"""

from __future__ import annotations

from conftest import (
    active_products,
    bind_stall,
    create_priced,
    evidence_key,
    http_json,
    pay_transaction,
    session_headers,
    today_iso,
)


# ---------------------------------------------------------------------------
# AC-014：别名归集到标准品类
# ---------------------------------------------------------------------------


def test_alias_maps_to_standard_category_ac_014(live_server):
    """`AC-014`：摊主选的是**摊位别名**，记账与统计必须归入**标准品类**。

    做法：从库里取一条"商品名 == 该摊位别名"的真实样本（种子里有 351 条别名映射），
    按它下一笔交易，核对明细的 `category_id` 是**别名指向的标准品类**，且标准品类的 `code`
    与别名不是同一个东西（能区分才不会假绿）。再走一遍运营端新增别名映射（`REQ-002`）。
    """
    stall = "A-04"
    token = bind_stall(live_server, stall)
    products = active_products(live_server, token)

    conn = live_server.connect_db()
    try:
        # 必须挑一条**真正发生归一**的样本：`alias_name` 与标准品类同名的那一类
        # （`data-model.md` §2.5 注记的语义②）证明不了"归集"这件事 ——
        # 只有"摊位叫小油菜、账上记上海青"这种才说明映射真的生效了。
        sample = conn.execute(
            """
            SELECT p.id AS product_id, p.name AS product_name, a.alias_name, a.category_id,
                   c.code AS category_code, c.name AS category_name
            FROM product p
            JOIN stall s ON s.id = p.stall_id
            JOIN stall_category_alias a ON a.stall_id = s.id AND a.alias_name = p.name
            JOIN category c ON c.id = a.category_id
            WHERE s.stall_no = ? AND p.status = 'active' AND a.alias_name <> c.name
            LIMIT 1
            """,
            (stall,),
        ).fetchone()
    finally:
        conn.close()
    assert sample is not None, (
        f"摊位 {stall} 应有「商品名是别名、且标准品类另有其名」的真实样本（`data-model.md` §2.5："
        "四大品类 + 别名归一必须有真实样本）—— 否则 `AC-014` 无从演示"
    )

    txn = create_priced(live_server, token, sample["product_id"], evidence_key("ac014"))
    conn = live_server.connect_db()
    try:
        recorded = conn.execute(
            "SELECT category_id FROM transaction_item WHERE transaction_id = "
            '(SELECT id FROM "transaction" WHERE transaction_no = ?)',
            (txn["transaction_no"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert int(recorded) == int(sample["category_id"]), (
        f"按别名选品必须归入标准品类 {sample['category_code']}（{sample['category_id']}），实际记成 {recorded}"
    )

    # 运营端新增一条别名映射，必须可查（`REQ-002` 的写路径）
    status, categories = live_server.api("GET", "/api/admin/categories")
    assert status == 200, f"契约 §3.20 期望 200，实际 {status}：{categories}"
    target_category = categories["categories"][0]
    alias_name = f"端到端别名{__import__('time').time_ns() % 100000}"
    status, created = live_server.api(
        "POST", "/api/admin/aliases",
        {"stall_no": stall, "alias_name": alias_name, "category_id": target_category["id"]},
    )
    assert status == 201, f"契约 §3.22 期望 201，实际 {status}：{created}"
    status, categories = live_server.api("GET", "/api/admin/categories")
    found = [a for a in categories["aliases"] if a["alias_name"] == alias_name]
    assert found and int(found[0]["category_id"]) == int(target_category["id"]), (
        f"新增的别名映射必须可查且指向标准品类：{found}"
    )
    print(f"[AC-014] 别名「{sample['alias_name']}」→ 标准品类 {sample['category_code']}"
          f"（{sample['category_name']}）；新增映射「{alias_name}」可查")


# ---------------------------------------------------------------------------
# AC-015：复制昨天价 + 逐条调整
# ---------------------------------------------------------------------------


def test_copy_previous_day_price_then_adjust_one_item_ac_015(live_server):
    """`AC-015`：`copy_from_previous_day` → 与上一营业日**逐条一致**；随后**逐条调整**只改那一条。"""
    from datetime import date, timedelta

    stall = "A-05"
    token = bind_stall(live_server, stall)
    today = today_iso()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    status, prev = live_server.api(
        "GET", f"/api/merchant/price-list?business_date={yesterday}", headers=session_headers(token)
    )
    assert status == 200, f"契约 §3.3 期望 200，实际 {status}：{prev}"
    prev_items = prev["items"] if isinstance(prev, dict) else prev
    assert prev_items, f"种子数据应给上一营业日 {yesterday} 留价目表：{prev}"
    prev_prices = {item["product_id"]: item["unit_price_cents"] for item in prev_items}

    status, copied = live_server.api(
        "POST", "/api/merchant/price-list",
        {"business_date": today, "copy_from_previous_day": True, "items": []},
        session_headers(token),
    )
    assert status == 200, f"契约 §3.4 期望 200，实际 {status}：{copied}"
    assert copied["source"] == "copied_previous_day", f"复制来源应为 copied_previous_day：{copied}"
    copied_prices = {item["product_id"]: item["unit_price_cents"] for item in copied["items"]}
    assert copied_prices == prev_prices, (
        "「复制昨天价」必须与上一营业日**逐条一致**："
        f"差异 {[(k, prev_prices.get(k), copied_prices.get(k)) for k in set(prev_prices) ^ set(copied_prices)]}，"
        f"改价项 {[(k, prev_prices[k], copied_prices[k]) for k in prev_prices if copied_prices.get(k) != prev_prices[k]]}"
    )

    # 逐条调整：只改一条，其余不动
    target = copied["items"][0]
    new_price = int(target["unit_price_cents"]) + 37
    status, adjusted = live_server.api(
        "POST", "/api/merchant/price-list",
        {"business_date": today, "items": [{"product_id": target["product_id"], "unit_price_cents": new_price}]},
        session_headers(token),
    )
    assert status == 200, f"逐条调整期望 200，实际 {status}：{adjusted}"
    assert adjusted["source"] == "manual", f"手工调整的来源应为 manual：{adjusted}"
    adjusted_prices = {item["product_id"]: item["unit_price_cents"] for item in adjusted["items"]}
    assert adjusted_prices[target["product_id"]] == new_price, "被调整的那条必须生效"
    others = {k: v for k, v in adjusted_prices.items() if k != target["product_id"]}
    assert others == {k: v for k, v in copied_prices.items() if k != target["product_id"]}, (
        "逐条调整**不得**动到别的商品"
    )
    print(f"[AC-015] 复制 {yesterday} 的 {len(copied_prices)} 条价格，逐条一致；"
          f"调整 1 条（{target['unit_price_cents']} → {new_price}），其余 {len(others)} 条未变")


# ---------------------------------------------------------------------------
# AC-011：顾客端点的字段白名单（键集合恰好相等）
# ---------------------------------------------------------------------------


def test_customer_endpoints_expose_only_sourced_fields_ac_011(live_server):
    """`AC-011`：顾客两个端点的**键集合恰好等于契约白名单** —— 多一个键就是"无来源字段"。

    `T-030` 验证的是"浏览器里渲染出来的字段"，本测试补的是**接口层**的同一件事
    （两侧都查，才挡得住"接口多给了、页面没显示"这种半泄漏）。
    """
    stall = "A-06"
    token = bind_stall(live_server, stall)
    product = active_products(live_server, token)[0]
    txn = create_priced(live_server, token, product["id"], evidence_key("ac011"))
    status, paid = pay_transaction(
        live_server, token, txn["transaction_no"], {"method": "cash", "operator": "e2e32"},
        evidence_key("ac011-pay"),
    )
    assert status == 200, f"现金收款期望 200，实际 {status}：{paid}"

    status, profile = http_json(live_server.base, "GET", f"/api/customer/stalls/{stall}/profile")
    assert status == 200, f"契约 §3.18 期望 200，实际 {status}：{profile}"
    assert set(profile) == {"stall_no", "stall_name", "in_business", "price_consistency_bp", "computed_at"}, (
        f"§3.18 字段白名单不符：{sorted(profile)}"
    )

    status, receipt = http_json(live_server.base, "GET", f"/api/customer/receipts/{txn['transaction_no']}")
    assert status == 200, f"契约 §3.19 期望 200，实际 {status}：{receipt}"
    assert set(receipt) == {"transaction_no", "status", "total_amount_cents", "items", "paid_at"}, (
        f"§3.19 字段白名单不符：{sorted(receipt)}"
    )
    for item in receipt["items"]:
        assert set(item) == {"name", "weight_grams", "amount_cents"}, (
            f"§3.19 明细字段白名单不符：{sorted(item)}"
        )
    forbidden = ("id_card", "bank_card", "customer_name", "phone", "receiver_account", "id_no", "card_no")
    blob = f"{profile}{receipt}"
    hits = [name for name in forbidden if name in blob]
    assert not hits, f"顾客端点出现禁令字段（`REQ-024`/`NFR-012`）：{hits}"
    print(f"[AC-011] profile 5 键 / receipt 5 键 / 明细 3 键，恰好等于契约白名单；禁令字段零命中")
