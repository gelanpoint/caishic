"""`T-032` 端到端验证（三之二）：**其余 `AC` 的运营端覆盖**（真实服务 + 真库）。

| AC | 本文件验证的行为 |
| --- | --- |
| `AC-022` | 佣金按**实收金额**与**所配口径（`rate_bp`）**计算 —— 用 `改价` 让实收 ≠ 标价，两条算式的差必须能被这条检查区分出来 |
| `AC-023` | 结算单可查看、金额与**日聚合结果一致**，且**旧版本不删** |
| `AC-005` | 三个使用率指标：**九个数字全部按 `data-model.md` §5.1 的口径独立回库复算**再比对 |

为什么这三条放这里：它们是**运营端读模型**（佣金/结算/指标），与交易写路径分属不同端点语义
（与 `test_coverage_rest.py` 的分工一致，单文件也都 ≤400 行）。

**指标不是"返回了 6 个数"就算过关**（`Q-19` 的教训：分子恰好等于分母时，写死与真查输出相同）：
本文件用**规格口径**（不是抄实现）重新算一遍分子分母与万分比，再与接口逐一比对；
并且断言响应里**只有**契约 §3.30 表里的 10 个键 —— 多出第四项指标（市场口径「日均智能秤交易占比」）
就是编造了拿不到的分母（`Q-15` / 契约 §3.30 末注）。
"""

from __future__ import annotations

import time

import pytest

from conftest import (
    active_products,
    bind_stall,
    count,
    create_priced,
    ensure_commission_rule,
    evidence_key,
    half_up_div,
    item_amount,
    pay_transaction,
    ratio_bp,
    session_headers,
    stall_id,
    today_iso,
)


def _dashboard(server, token: str) -> dict:
    status, payload = server.api(
        "GET", f"/api/merchant/dashboard?business_date={today_iso()}", headers=session_headers(token)
    )
    assert status == 200, f"契约 §3.12 期望 200，实际 {status}：{payload}"
    return payload


# ---------------------------------------------------------------------------
# AC-022：佣金按实收金额与所配口径计算（REQ-017）
# ---------------------------------------------------------------------------


def test_commission_follows_configured_rate_on_received_amount_ac_022(live_server):
    """`AC-022`：配置费率后，佣金 = `round(实收分 × rate_bp ÷ 10000)`，且**基数是实收**。

    怎么证明"基数是实收"：本系统里实收 = 收款那一刻的订单总额，而它同时**不等于**另外两个数 ——
    ① 改价前的**标价**（原单价 × 重量）；② 抹零前的**明细之和**。
    先用改价与抹零把三者拉开，再逐一算：只有"实收"这条算得出看板给的那个佣金。
    **若三者恰好算到同一个数，本用例会自己喊出来**（那说明这条检查是盲的，不是通过）。
    """
    stall = "A-07"
    token = bind_stall(live_server, stall)
    rule = ensure_commission_rule(live_server, rate_bp=250)
    rate = int(rule["rate_bp"])
    assert rule["pay_object"] in {"merchant", "customer", "market"}, f"收费对象枚举不符：{rule}"

    product = active_products(live_server, token)[0]
    txn = create_priced(live_server, token, product["id"], evidence_key("ac022"), weight_grams=2000)
    item = txn["items"][0]
    listed = item_amount(item["original_unit_price_cents"], item["weight_grams"])  # 改价前的标价金额

    # ① 改价 -40%（不超 50%，无需确认）→ 订单总额低于标价
    new_price = max(1, int(item["final_unit_price_cents"] * 6 // 10))
    status, changed = live_server.api(
        "POST", f"/api/merchant/transactions/{txn['transaction_no']}/price-change",
        {"item_id": item["id"], "final_unit_price_cents": new_price},
        session_headers(token, evidence_key("ac022-price")),
    )
    assert status == 200, f"改价期望 200，实际 {status}：{changed}"
    items_sum = int(changed["total_amount_cents"])

    # ② 抹零 50 分 → 实收再低一档（抹零不属于可计佣金额）
    status, rounded = live_server.api(
        "POST", f"/api/merchant/transactions/{txn['transaction_no']}/price-change",
        {"item_id": item["id"], "round_off_cents": 50},
        session_headers(token, evidence_key("ac022-roundoff")),
    )
    assert status == 200, f"抹零期望 200，实际 {status}：{rounded}"
    received = int(rounded["total_amount_cents"])
    assert received == items_sum - 50, f"抹零应从总额里减掉：{items_sum} → {received}"

    status, paid = pay_transaction(
        live_server, token, txn["transaction_no"], {"method": "cash", "operator": "e2e32"},
        evidence_key("ac022-pay"),
    )
    assert status == 200, f"现金收款期望 200，实际 {status}：{paid}"

    on_received = half_up_div(received * rate, 10_000)
    on_items_sum = half_up_div(items_sum * rate, 10_000)
    on_listed = half_up_div(listed * rate, 10_000)
    assert len({on_received, on_items_sum, on_listed}) == 3, (
        f"三个基数算出的佣金没能拉开（实收 {on_received} / 明细和 {on_items_sum} / 标价 {on_listed}）："
        "这条检查对「按实收」是盲的，请调整重量或改价幅度"
    )

    dash = _dashboard(live_server, token)
    assert dash["gross_amount_cents"] == received, (
        f"看板订单总额口径应为改价与抹零之后的成交额：{dash} vs 实收 {received}"
    )
    assert dash["commission_amount_cents"] == on_received, (
        f"合同口径：佣金 = round(实收 {received} × {rate} ÷ 10000) = {on_received}，"
        f"看板给出 {dash['commission_amount_cents']}"
        f"（若为 {on_items_sum} 则错在没扣抹零，若为 {on_listed} 则错在用了改价前的标价）"
    )
    print(f"[AC-022] 费率 {rate} bp；标价 {listed} → 明细和 {items_sum} → 实收 {received}；"
          f"佣金 {dash['commission_amount_cents']} = round({received}×{rate}/10000)"
          f"（用标价会得 {on_listed}、用抹零前明细和会得 {on_items_sum}，三者可区分）")


# ---------------------------------------------------------------------------
# AC-023：结算单与日聚合一致、旧版本不删（REQ-019）
# ---------------------------------------------------------------------------


def test_settlement_matches_daily_aggregate_and_keeps_versions_ac_023(live_server):
    """`AC-023`：日聚合后生成结算单 → 可查看，且金额与**日聚合快照**逐字段一致；再生成 → `version` 递增且旧版保留。"""
    stall = "A-07"
    stall_db_id = stall_id(live_server, stall)
    token = bind_stall(live_server, stall)
    ensure_commission_rule(live_server, rate_bp=250)
    day = today_iso()

    status, agg = live_server.api(
        "POST", "/api/admin/daily-aggregate", {"business_date": day, "stall_no": stall}
    )
    assert status == 200, f"契约 §3.26 期望 200，实际 {status}：{agg}"
    assert int(agg["stalls_aggregated"]) >= 1, f"应至少聚合本摊位：{agg}"
    revision = int(agg["revision"])
    assert revision >= 1, f"revision 应从 1 起：{agg}"

    status, created = live_server.api(
        "POST", "/api/admin/settlements",
        {"stall_no": stall, "period_start": day, "period_end": day},
    )
    assert status == 201, f"契约 §3.27 期望 201，实际 {status}：{created}"
    assert int(created["version"]) == 1, f"首张结算单应为 version 1：{created}"

    conn = live_server.connect_db()
    try:
        snapshot = conn.execute(
            "SELECT gross_amount_cents, commission_amount_cents, revision FROM daily_aggregate "
            "WHERE stall_id = ? AND business_date = ? ORDER BY revision DESC LIMIT 1",
            (stall_db_id, day),
        ).fetchone()
    finally:
        conn.close()
    assert snapshot is not None, "日聚合必须真的落快照（`data-model.md` §2.15）"
    assert created["gross_amount_cents"] == snapshot["gross_amount_cents"], (
        f"结算单金额与日聚合快照不一致：{created} vs {dict(snapshot)}"
    )
    assert created["commission_amount_cents"] == snapshot["commission_amount_cents"], (
        f"结算单佣金与日聚合快照不一致：{created} vs {dict(snapshot)}"
    )

    dash = _dashboard(live_server, token)
    assert created["gross_amount_cents"] == dash["gross_amount_cents"], (
        f"结算单金额应等于同日看板口径（同一条 `stall_day_facts`）：{created} vs {dash}"
    )
    assert created["commission_amount_cents"] == dash["commission_amount_cents"], (
        f"结算单佣金应等于同日看板佣金：{created} vs {dash}"
    )

    # 再生成一次 → 新版本，且旧版本不删（契约 §3.28「含全部 version，旧版本不删」）
    status, again = live_server.api(
        "POST", "/api/admin/settlements",
        {"stall_no": stall, "period_start": day, "period_end": day},
    )
    assert status == 201, f"再次生成结算单期望 201，实际 {status}：{again}"
    assert int(again["version"]) == 2, f"第二次生成的版本号应递增：{again}"

    status, listed = live_server.api(
        "GET", f"/api/admin/settlements?stall_no={stall}&period_start={day}&period_end={day}"
    )
    assert status == 200, f"契约 §3.28 期望 200，实际 {status}：{listed}"
    versions = sorted(int(row["version"]) for row in listed)
    assert versions == [1, 2], f"查询应返回全部历史版本：{versions}"
    amounts = {(int(row["version"]), row["gross_amount_cents"], row["commission_amount_cents"]) for row in listed}
    assert len({(g, c) for _, g, c in amounts}) == 1, f"同一期数据不应因版本不同而变：{amounts}"
    print(f"[AC-023] 日聚合 revision {revision} → 结算单 v1 金额 {created['gross_amount_cents']}/"
          f"{created['commission_amount_cents']}（与快照、看板三方一致）；再生成 v2，历史版本保留 {versions}")


# ---------------------------------------------------------------------------
# AC-005：三个使用率指标 —— 九个数字独立回库复算（REQ-022）
# ---------------------------------------------------------------------------


def test_usage_metrics_match_independent_recount_ac_005(live_server):
    """`AC-005`：三项指标的**每个分子与分母都能被第三方逐一核对**。

    复算口径**照 `data-model.md` §5.1 的三行原文写**（不抄实现）：
    ① 摊位使用率 = 当日 `transaction` 中 distinct `stall_id` 数 ÷ `stall.status='active'` 摊位数；
    ② 现金交易占比 = 当日 `payment.method='cash'` 且 `status='success'` 的笔数 ÷ 当日交易数；
    ③ 价目表维护率 = 「当日全部 active 商品都有 `price_item`」的摊位数 ÷ 在营摊位数。

    同时断言响应**恰好**是契约 §3.30 表里的 10 个键：多出第四项（市场口径指标）即为
    **编造拿不到的分母**（`Q-15`；契约 §3.30 末注明确禁止）。

    **先造"非退化"状态再断言**（`Q-19` 的教训）：分子恰好等于分母时，"真查"与"写死"输出**同一个数**，
    那时的检查是**盲的**。故本用例先把"现金分子 < 分母"（加一笔收款码成功交易）与
    "维护率分子 = 分母 − 1"（删掉某在营摊位当日一行在售商品的价格）造出来，再核对九个数字 ——
    退化状态下本用例会直接喊出来（断言非退化），不会假装通过。
    """
    day = today_iso()

    # ① 非退化（现金）：一笔**收款码 + 成功回调**的交易 → 现金笔数严格小于当日走秤笔数
    qr_token = bind_stall(live_server, "A-08")
    qr_product = active_products(live_server, qr_token)[0]
    qr_txn = create_priced(live_server, qr_token, qr_product["id"], evidence_key("ac005-qr"))
    status, qr = pay_transaction(
        live_server, qr_token, qr_txn["transaction_no"], {"method": "qr", "operator": "e2e32"},
        evidence_key("ac005-qr-pay"),
    )
    assert status == 202, f"收款码期望 202，实际 {status}：{qr}"
    status, cb = live_server.api(
        "POST", "/api/mock/payment/callback",
        {"callback_no": f"CB-AC005-{time.time_ns()}", "payment_no": qr["payment_no"], "result": "success"},
    )
    assert status == 200 and cb["transaction_status"] == "paid", f"回调应让交易转为 paid：{cb}"

    # ② 非退化（价目表维护率）：删掉 A-09 当日**一行**在售商品的价格 → 该摊位不再"已维护"
    conn = live_server.connect_db()
    try:
        removed = conn.execute(
            """
            DELETE FROM price_item WHERE id = (
                SELECT pi.id FROM price_item pi
                JOIN product p ON p.id = pi.product_id
                JOIN stall s ON s.id = p.stall_id
                WHERE s.stall_no = 'A-09' AND p.status = 'active' AND pi.business_date = ?
                LIMIT 1)
            """,
            (day,),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    assert removed == 1, f"应删掉恰好一行价格来制造非退化状态，实际 {removed}"

    status, payload = live_server.api("GET", f"/api/admin/metrics/usage?business_date={day}")
    assert status == 200, f"契约 §3.30 期望 200，实际 {status}：{payload}"
    assert set(payload) == {
        "business_date",
        "stall_usage_bp", "stall_usage_numerator", "stall_usage_denominator",
        "cash_txn_share_bp", "cash_txn_numerator", "cash_txn_denominator",
        "price_list_maintenance_bp", "price_list_numerator", "price_list_denominator",
    }, f"§3.30 字段集合不符（多一个就可能是在编造指标）：{sorted(payload)}"
    assert payload["business_date"] == day

    conn = live_server.connect_db()
    try:
        active_stalls = count(conn, "SELECT COUNT(*) AS n FROM stall WHERE status = 'active'")
        txn_stalls = count(
            conn, 'SELECT COUNT(DISTINCT stall_id) AS n FROM "transaction" WHERE business_date = ?', (day,)
        )
        all_txns = count(conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE business_date = ?', (day,))
        cash_txns = count(
            conn,
            'SELECT COUNT(*) AS n FROM payment p JOIN "transaction" t ON t.id = p.transaction_id '
            "WHERE p.method = 'cash' AND p.status = 'success' AND t.business_date = ?",
            (day,),
        )
        maintained = count(
            conn,
            """
            SELECT COUNT(*) AS n FROM stall s
            WHERE s.status = 'active'
              AND NOT EXISTS (
                    SELECT 1 FROM product p
                    WHERE p.stall_id = s.id AND p.status = 'active'
                      AND NOT EXISTS (
                            SELECT 1 FROM price_item pi
                            WHERE pi.stall_id = s.id AND pi.product_id = p.id AND pi.business_date = ?
                      )
              )
            """,
            (day,),
        )
    finally:
        conn.close()

    expected = {
        "stall_usage_numerator": txn_stalls,
        "stall_usage_denominator": active_stalls,
        "cash_txn_numerator": cash_txns,
        "cash_txn_denominator": all_txns,
        "price_list_numerator": maintained,
        "price_list_denominator": active_stalls,
    }
    for field, value in expected.items():
        assert payload[field] == value, (
            f"§5.1 口径复算不符：{field} 接口给 {payload[field]}，独立回库算得 {value}"
        )
    # **非退化前提**（`Q-19`）：分子 ≠ 分母，写死与真查才会给出不同的数
    assert expected["cash_txn_numerator"] != expected["cash_txn_denominator"], (
        f"现金占比处于退化状态（{expected['cash_txn_numerator']}/{expected['cash_txn_denominator']}）："
        "此时写死与真查输出相同，本用例对它是盲的"
    )
    assert expected["price_list_numerator"] != expected["price_list_denominator"], (
        f"价目表维护率处于退化状态（{expected['price_list_numerator']}/{expected['price_list_denominator']}）："
        "此时写死与真查输出相同，本用例对它是盲的"
    )
    for bp_field, num_field, den_field in (
        ("stall_usage_bp", "stall_usage_numerator", "stall_usage_denominator"),
        ("cash_txn_share_bp", "cash_txn_numerator", "cash_txn_denominator"),
        ("price_list_maintenance_bp", "price_list_numerator", "price_list_denominator"),
    ):
        assert 0 <= payload[bp_field] <= 10000, f"{bp_field} 必须是 0~10000 的万分比整数：{payload[bp_field]}"
        assert payload[bp_field] == ratio_bp(payload[num_field], payload[den_field]), (
            f"{bp_field} 与其分子/分母自相矛盾：{payload[bp_field]} vs "
            f"{ratio_bp(payload[num_field], payload[den_field])}（{payload[num_field]}/{payload[den_field]}）"
        )
        assert payload[bp_field] == ratio_bp(expected[num_field], expected[den_field]), (
            f"{bp_field} 与独立复算不符：{payload[bp_field]} vs {ratio_bp(expected[num_field], expected[den_field])}"
        )
    print(f"[AC-005] 九个数字逐一独立回库复算一致：摊位 {expected['stall_usage_numerator']}/"
          f"{expected['stall_usage_denominator']} → {payload['stall_usage_bp']}；现金 "
          f"{expected['cash_txn_numerator']}/{expected['cash_txn_denominator']} → {payload['cash_txn_share_bp']}；"
          f"价目表 {expected['price_list_numerator']}/{expected['price_list_denominator']} → "
          f"{payload['price_list_maintenance_bp']}")
