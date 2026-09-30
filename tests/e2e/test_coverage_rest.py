"""`T-032` 端到端验证（三之一）：**交易与商品侧的行为覆盖**（真实服务 + 真库 + 真浏览器）。

`T-030` 覆盖了三条主线的"能跑通"，`T-031` 覆盖了异常路径；本文件补齐**规格里逐条写明的行为**：

| AC | 本文件验证的行为 |
| --- | --- |
| `AC-016` | 同一笔里两个商品的金额**累加**在一笔交易内 |
| `AC-006` | 全程**零文本输入**且**交互次数 ≤ 3**（选品 1 → 计价 1 → 收款 1）—— 结构上验证"页面上没有可输入的控件" |
| `AC-007` | 改价幅度 >50%：不带确认 → `MT-1011`（**不阻止**）；带确认 → 成功，且留痕里有原价、改后价、时间、摊位 |
| `AC-008` | 标价一致率：**改价计入、抹零不计入**（逐状态断言万分比 + 信用档案随日聚合同步） |

目录/价目表与顾客页三条（`AC-014`/`AC-015`/`AC-011`）在 `test_coverage_catalog.py`；
运营端的佣金/结算/指标在 `test_coverage_admin.py` —— 按**端点语义**分工，单文件均 ≤400 行
（`quality-gates.md` §1.2；`Q-16` 的拆法先例：按语义拆，不做机械对半切）。

判据来源：`spec.md` §7 的 `AC` 原文 + `data-model.md` §2.17（标价一致率口径）。
**指标与阈值不在本文件复述数值**：公式按 `data-model.md` §0 的取整口径**独立复算**再比对。
"""

from __future__ import annotations

import pytest

from conftest import (
    active_products,
    bind_stall,
    count,
    create_priced,
    create_transaction,
    ensure_commission_rule,
    evidence_key,
    half_up_div,
    http_json,
    item_amount,
    new_page,
    pay_transaction,
    session_headers,
    snap,
    stall_id,
    today_iso,
)



from conftest import (
    active_products,
    bind_stall,
    count,
    create_priced,
    create_transaction,
    ensure_commission_rule,
    evidence_key,
    half_up_div,
    http_json,
    item_amount,
    new_page,
    pay_transaction,
    session_headers,
    snap,
    stall_id,
    today_iso,
)


# ---------------------------------------------------------------------------
# AC-016：多商品累加在同一笔
# ---------------------------------------------------------------------------


def test_multi_item_amounts_accumulate_in_one_transaction_ac_016(live_server):
    """`AC-016`：再加第二个商品 → 两个商品金额**累加在同一笔交易**内（不是两笔）。"""
    stall = "A-01"
    token = bind_stall(live_server, stall)
    products = active_products(live_server, token)[:2]
    assert len(products) >= 2, "种子数据每摊位应有多个商品"

    key = evidence_key("ac016")
    status, txn = create_transaction(
        live_server, token,
        [
            {"product_id": products[0]["id"], "weight_grams": 700},
            {"product_id": products[1]["id"], "weight_grams": 1300},
        ],
        key,
    )
    assert status == 201, f"契约 §3.6 期望 201，实际 {status}：{txn}"

    items = txn["items"]
    assert len(items) == 2, f"同一笔里应有 2 行明细，实际 {len(items)}"
    assert txn["total_amount_cents"] == sum(item["amount_cents"] for item in items), (
        f"`AC-016`：订单总额应等于各行之和：{txn['total_amount_cents']} vs {items}"
    )
    for item in items:  # 每行金额必须能由"单价 × 重量"独立复算（可被第三方核对）
        assert item["amount_cents"] == item_amount(item["final_unit_price_cents"], item["weight_grams"]), (
            f"明细金额与单价×重量不符：{item}"
        )

    conn = live_server.connect_db()
    try:
        rows = count(
            conn,
            'SELECT COUNT(*) AS n FROM "transaction" WHERE client_idempotency_key = ?', (key,)
        )
        lines = count(
            conn,
            "SELECT COUNT(*) AS n FROM transaction_item WHERE transaction_id = "
            '(SELECT id FROM "transaction" WHERE client_idempotency_key = ?)',
            (key,),
        )
    finally:
        conn.close()
    assert rows == 1 and lines == 2, f"两个商品必须落在**同一笔**交易的两行里：交易 {rows} / 明细 {lines}"
    print(f"[AC-016] 一笔含 2 行明细：{txn['total_amount_cents']} 分 = "
          f"{' + '.join(str(item['amount_cents']) for item in items)}")


# ---------------------------------------------------------------------------
# AC-006：零文本输入、交互次数 ≤ 3
# ---------------------------------------------------------------------------


def test_scale_flow_needs_no_text_input_and_at_most_three_clicks_ac_006(live_server, browser, shots):
    """`AC-006`：从选品到确认收款**全程无文本输入**，且**交互次数 ≤ 3**。

    "无文本输入"用**结构判据**验：页面上不存在任何可输入文本的控件
    （`input` 的文本类/`textarea`/`contenteditable`）—— 这样"能不能输入"就不是靠肉眼看的。
    "≤3 次"按规格给出的三段动线计数：选品 1 次 → 计价 1 次 → 确认收款 1 次
    （`#defaultStall` 是**绑定摊位**，属开机前置动作，不计入这笔交易的 3 次）。
    """
    page = new_page(browser)
    page.goto(f"{live_server.base}/scale/")
    page.click("#defaultStall")
    page.wait_for_selector(".tile")
    page.locator("#weights button").first.click()

    inputs = page.evaluate(
        """() => Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'))
                 .filter(el => el.tagName === 'TEXTAREA'
                            || el.isContentEditable
                            || ['text','number','password','email','search','tel','url']
                                 .includes((el.getAttribute('type') || 'text').toLowerCase()))
                 .map(el => el.tagName + '#' + el.id + '[' + (el.getAttribute('type') || '') + ']')"""
    )
    assert not inputs, f"`AC-006`：秤端主界面不得有可输入文本的控件，实际 {inputs}"

    clicks = 0
    page.locator(".tile").first.click(); clicks += 1                      # ① 选品
    page.click("#checkout"); clicks += 1                                  # ② 计价
    page.wait_for_function("document.querySelector('#txnNo').textContent.trim() !== '—'")
    page.click("#payCash"); clicks += 1                                   # ③ 确认收款
    page.wait_for_selector("#afterSale:not(.hide)")
    assert "现金收款成功" in page.inner_text("body"), "三次点击后应完成收款"
    assert clicks <= 3, f"`AC-006`：交互次数必须 ≤3，实际 {clicks}"
    snap(page, shots, "coverage-ac006-three-clicks")
    print(f"[AC-006] 零可输入控件；{clicks} 次点击完成「选品 → 计价 → 现金收款」；交易号 "
          f"{page.inner_text('#txnNo').strip()}")
    page.close()


# ---------------------------------------------------------------------------
# AC-007：改价 >50% 需确认（不阻止）+ 留痕四要素
# ---------------------------------------------------------------------------


def test_price_change_over_threshold_requires_confirmation_ac_007(live_server):
    """`AC-007`：幅度 >50% → 先提示确认（`MT-1011`，**不阻止**）→ 带确认后仍可继续；
    记录里**同时存在**原价、改后价、改价时间、操作摊位。"""
    stall = "A-02"
    stall_db_id = stall_id(live_server, stall)
    token = bind_stall(live_server, stall)
    product = active_products(live_server, token)[0]
    txn = create_priced(live_server, token, product["id"], evidence_key("ac007"), weight_grams=1000)
    item = txn["items"][0]
    original = item["final_unit_price_cents"]
    target = max(1, original // 4)  # 降到 25%：幅度 75% > 50%，必然触发确认

    # ① 不带确认 → 409 MT-1011，且**什么都没改**
    status, payload = live_server.api(
        "POST", f"/api/merchant/transactions/{txn['transaction_no']}/price-change",
        {"item_id": item["id"], "final_unit_price_cents": target},
        session_headers(token, evidence_key("ac007-noconfirm")),
    )
    assert status == 409, f"契约 §3.9/§4 期望 409 MT-1011，实际 {status}：{payload}"
    assert payload["error"]["code"] == "MT-1011", f"实际错误码 {payload['error']['code']}"
    assert payload["error"].get("detail", {}).get("confirm_over_threshold") is True, (
        f"`MT-1011` 的 detail 应告诉调用方怎么继续：{payload}"
    )

    conn = live_server.connect_db()
    try:
        after_refuse = conn.execute(
            "SELECT final_unit_price_cents FROM transaction_item WHERE id = ?", (item["id"],)
        ).fetchone()[0]
        audits_refused = count(
            conn, "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'price_change' AND ref_id = ?", (item["id"],)
        )
    finally:
        conn.close()
    assert after_refuse == original, f"未确认的改价不得生效：{original} → {after_refuse}"
    assert audits_refused == 0, "未确认的改价不得写留痕"

    # ② 带确认重发 → 成功（**不阻止**）
    status, ok = live_server.api(
        "POST", f"/api/merchant/transactions/{txn['transaction_no']}/price-change",
        {"item_id": item["id"], "final_unit_price_cents": target, "confirm_over_threshold": True},
        session_headers(token, evidence_key("ac007-confirm")),
    )
    assert status == 200, f"带确认的改价期望 200（REQ-007：系统不阻止）：{status} {ok}"
    assert ok["audit_event"] == "price_change", f"响应应明示留痕事件：{ok}"
    assert ok["total_amount_cents"] == item_amount(target, item["weight_grams"]), (
        f"改价后总额应按新单价重算：{ok}"
    )

    conn = live_server.connect_db()
    try:
        row = conn.execute(
            "SELECT original_unit_price_cents, final_unit_price_cents, price_changed FROM transaction_item WHERE id = ?",
            (item["id"],),
        ).fetchone()
        audit = conn.execute(
            "SELECT payload_json, occurred_at, stall_id FROM audit_log "
            "WHERE event_type = 'price_change' AND ref_id = ? ORDER BY id DESC LIMIT 1",
            (item["id"],),
        ).fetchone()
    finally:
        conn.close()

    assert row["original_unit_price_cents"] == original, f"记录里必须留下原价：{dict(row)}"
    assert row["final_unit_price_cents"] == target, f"记录里必须留下改后价：{dict(row)}"
    assert row["price_changed"] == 1, "改价必须置 `price_changed = 1`（`REQ-008` 计入标价一致率）"
    assert audit is not None, "改价必须写留痕"
    assert audit["occurred_at"], f"留痕必须带**改价时间**：{dict(audit)}"
    assert int(audit["stall_id"]) == stall_db_id, f"留痕必须带**操作摊位**：{dict(audit)}"
    payload_json = audit["payload_json"]
    assert str(original) in payload_json and str(target) in payload_json, (
        f"留痕里必须同时有原价与改后价：{payload_json}"
    )
    print(f"[AC-007] 无确认 → MT-1011（零改动）；带确认 → 200，单价 {original} → {target}，"
          f"留痕含原价/改后价/时间 {audit['occurred_at']}/摊位 {stall}")


# ---------------------------------------------------------------------------
# AC-008：标价一致率 —— 改价计入、抹零不计入
# ---------------------------------------------------------------------------


def test_price_consistency_counts_price_change_not_round_off_ac_008(live_server):
    """`AC-008`：同一摊位内，**抹零不改变**标价一致率，**改价让它下降**。

    公式取自 `data-model.md` §2.17：`round((明细数 − 改价明细数) ÷ 明细数 × 10000)`；
    本测试**逐步**核对万分比（10000 → 10000 → 5000），而不是只看"它动了"。
    """
    stall = "A-03"
    token = bind_stall(live_server, stall)
    products = active_products(live_server, token)

    def bp() -> int:
        status, payload = live_server.api(
            "GET", f"/api/merchant/dashboard?business_date={today_iso()}", headers=session_headers(token)
        )
        assert status == 200, f"契约 §3.12 期望 200，实际 {status}：{payload}"
        return int(payload["price_consistency_bp"])

    assert bp() == 10000, "前置：该摊位本模块尚无明细（无明细口径取 10000）"

    # ① 一笔 + 抹零：`is_round_off = 1`、`price_changed = 0` → **不计入**，一致率不变
    txn1 = create_priced(live_server, token, products[0]["id"], evidence_key("ac008-round"))
    status, rounded = live_server.api(
        "POST", f"/api/merchant/transactions/{txn1['transaction_no']}/price-change",
        {"item_id": txn1["items"][0]["id"], "round_off_cents": 50},
        session_headers(token, evidence_key("ac008-roundoff")),
    )
    assert status == 200, f"抹零期望 200，实际 {status}：{rounded}"
    assert rounded["total_amount_cents"] == txn1["total_amount_cents"] - 50, (
        f"抹零应从总额里减掉：{txn1['total_amount_cents']} → {rounded['total_amount_cents']}"
    )
    assert bp() == 10000, "`AC-008`：**抹零不计入**标价一致率（必须仍是 10000）"

    # ② 另一笔 + 改价：`price_changed = 1` → **计入**，一致率降到 5000（2 条明细、1 条改价）
    txn2 = create_priced(live_server, token, products[1]["id"], evidence_key("ac008-change"))
    item2 = txn2["items"][0]
    status, changed = live_server.api(
        "POST", f"/api/merchant/transactions/{txn2['transaction_no']}/price-change",
        {"item_id": item2["id"], "final_unit_price_cents": max(1, item2["final_unit_price_cents"] - 10)},
        session_headers(token, evidence_key("ac008-price")),
    )
    assert status == 200, f"改价期望 200，实际 {status}：{changed}"
    observed = bp()
    expected = half_up_div((2 - 1) * 10_000, 2)
    assert observed == expected, f"`AC-008`：2 条明细、1 条改价 → 应为 {expected}，实际 {observed}"

    conn = live_server.connect_db()
    try:
        flags = conn.execute(
            "SELECT is_round_off, price_changed FROM transaction_item WHERE transaction_id IN "
            '(SELECT id FROM "transaction" WHERE transaction_no IN (?, ?)) ORDER BY id',
            (txn1["transaction_no"], txn2["transaction_no"]),
        ).fetchall()
    finally:
        conn.close()
    assert [(row["is_round_off"], row["price_changed"]) for row in flags] == [(1, 0), (0, 1)], (
        f"抹零只置 is_round_off、改价只置 price_changed：{[dict(r) for r in flags]}"
    )

    # ③ 顾客看到的**信用档案**（`REQ-008` 明写"查看该摊位信用档案与标价一致率"）：
    #    信用档案随日聚合重算（`data-model.md` §2.17），重算后必须与现场口径同一个数。
    #    日聚合需要当日有生效佣金口径（否则 `MT-1013`）—— 这不是本测试的考察点，故先备好。
    ensure_commission_rule(live_server)
    status, agg = live_server.api(
        "POST", "/api/admin/daily-aggregate", {"business_date": today_iso(), "stall_no": stall}
    )
    assert status == 200, f"契约 §3.26 期望 200，实际 {status}：{agg}"
    status, profile = http_json(live_server.base, "GET", f"/api/customer/stalls/{stall}/profile")
    assert status == 200, f"契约 §3.18 期望 200，实际 {status}：{profile}"
    assert profile["price_consistency_bp"] == observed, (
        f"信用档案里的标价一致率应与现场口径一致：{profile['price_consistency_bp']} vs {observed}"
    )
    print(f"[AC-008] 抹零后一致率 10000（不计入）→ 改价后 {observed}（计入）；"
          f"明细标记 {[(r['is_round_off'], r['price_changed']) for r in flags]}；"
          f"日聚合后信用档案同为 {profile['price_consistency_bp']}")


