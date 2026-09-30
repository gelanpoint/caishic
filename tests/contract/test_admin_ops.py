"""`T-013` 契约测试（二）：运营端**看板与结算链路**（§3.25 看板、§3.26 日聚合与重算、§3.27/§3.28 结算单、§3.29 对账、§3.30 指标、§3.31 留痕）。

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

> **本文件由 `Q-16` 的按语义拆分从原 `test_admin.py` 拆出**；**配置类**端点（§3.20 字典、§3.21 品类维护、
> §3.22 别名、§3.23/§3.24 佣金口径）在 `test_admin.py`。**佣金口径是本文件的隐含前置**：
> 无生效口径时 §3.26 日聚合直接 `MT-1013`，而口径由 `test_admin.py` 的 §3.24 用例写入
> ⇒ **整份契约测试必须一起跑**（`python -m pytest tests/contract`），**不要只跑本文件**。
> **`Q-19`：三项指标的**真查**由一条灵敏度负例守住** —— `test_usage_metrics_numerator_is_actually_queried`
> 先制造**分子≠分母**的状态（某摊位当日缺一个在售商品的价格；当日存在一笔非现金交易）再断言，
> 因此**把分子或分母写死必然失败**（实测对照见 `docs/PROJECT-STATE.md` `Q-19`）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from conftest import (
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
# §3.26 日终聚合与重算（`REQ-018`）
# ---------------------------------------------------------------------------


def test_daily_aggregate_then_recompute_increases_revision(client, db_conn):
    """`REQ-018`：日终聚合产出 `revision`；同一天重算生成**新 revision**（旧版本保留）。"""
    created = _paid_transaction(client, "agg")
    today = today_iso()

    first = client.post("/api/admin/daily-aggregate", json={"business_date": today})
    assert first.status_code == 200, f"契约 §3.26 期望 200，实际 {first.status_code}"
    first_payload = json_of(first)
    assert {"business_date", "stalls_aggregated", "revision"} <= set(first_payload), (
        f"契约 §3.26 响应缺字段：{sorted(first_payload)}"
    )
    assert first_payload["business_date"] == today
    assert isinstance(first_payload["stalls_aggregated"], int) and first_payload["stalls_aggregated"] >= 1

    stall_id = _stall_id(db_conn)
    aggregate = db_conn.execute(
        "SELECT revision, txn_count, gross_amount_cents, is_current FROM daily_aggregate "
        "WHERE stall_id = ? AND business_date = ? AND is_current = 1",
        (stall_id, today),
    ).fetchone()
    assert aggregate is not None, "日终聚合必须落库（data-model.md §2.15：同一摊位同一营业日仅一条 is_current=1）"
    assert aggregate["txn_count"] >= 1 and aggregate["gross_amount_cents"] > 0
    assert aggregate["revision"] == first_payload["revision"], (
        f"响应 revision 应与库中当前版本一致：{first_payload['revision']} vs {aggregate['revision']}"
    )
    assert aggregate["gross_amount_cents"] >= created["total_amount_cents"], "聚合总额应包含本组新造的那笔交易"

    second = client.post("/api/admin/daily-aggregate", json={"business_date": today})
    assert second.status_code == 200, f"重算期望 200，实际 {second.status_code}"
    assert json_of(second)["revision"] > first_payload["revision"], (
        f"重算必须生成新 revision（REQ-018）：{first_payload['revision']} → {json_of(second)['revision']}"
    )
    versions = _count(
        db_conn,
        "SELECT COUNT(*) AS n FROM daily_aggregate WHERE stall_id = ? AND business_date = ?",
        (stall_id, today),
    )
    assert versions >= 2, "旧 revision 必须保留（可追溯、不可物理删除）"
    current = _count(
        db_conn,
        "SELECT COUNT(*) AS n FROM daily_aggregate WHERE stall_id = ? AND business_date = ? AND is_current = 1",
        (stall_id, today),
    )
    assert current == 1, f"同一摊位同一营业日有且仅有一条 is_current=1，实际 {current}"


def test_daily_aggregate_without_effective_rule_mt_1013_and_bad_body(client):
    """契约 §3.26/§4 `MT-1013`：该营业日无生效佣金口径 → 409（引导先配 §3.24）。"""
    uncovered = (date.today() - timedelta(days=365)).isoformat()
    response = client.post("/api/admin/daily-aggregate", json={"business_date": uncovered})
    assert response.status_code == 409, f"契约 §4 期望 409(MT-1013)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1013")

    bad = client.post("/api/admin/daily-aggregate", json={"business_date": "20260930"})
    assert bad.status_code == 422, f"契约 §3.26 要求 YYYY-MM-DD，实际 {bad.status_code}"
    assert_error_response(bad, expected_code="MT-1008")


# ---------------------------------------------------------------------------
# §3.27 / §3.28 结算单（`REQ-019`、`AC-023`）
# ---------------------------------------------------------------------------


def test_settlement_matches_daily_aggregate_and_lists_versions(client, db_conn):
    """`AC-023`：结算单金额必须与**日聚合结果一致**；查询可见（含 `version`）。"""
    today = today_iso()
    aggregate = db_conn.execute(
        "SELECT gross_amount_cents, commission_amount_cents FROM daily_aggregate "
        "WHERE stall_id = ? AND business_date = ? AND is_current = 1",
        (_stall_id(db_conn), today),
    ).fetchone()
    assert aggregate is not None, "前置：本文件的前一个用例应先完成日终聚合（顺序依赖，见模块 docstring）"

    response = client.post(
        "/api/admin/settlements", json={"stall_no": STALL_ADMIN, "period_start": today, "period_end": today}
    )
    assert response.status_code == 201, f"契约 §3.27 期望 201，实际 {response.status_code}"
    payload = json_of(response)
    assert set(payload) == {"settlement_no", "version", "gross_amount_cents", "commission_amount_cents"}, (
        f"契约 §3.27 响应键不符：{sorted(payload)}"
    )
    assert payload["version"] >= 1
    assert payload["gross_amount_cents"] == aggregate["gross_amount_cents"], (
        f"`AC-023`：结算总额必须等于日聚合总额，{payload['gross_amount_cents']} vs {aggregate['gross_amount_cents']}"
    )
    assert payload["commission_amount_cents"] == aggregate["commission_amount_cents"], (
        f"`AC-023`：结算佣金必须等于日聚合佣金，"
        f"{payload['commission_amount_cents']} vs {aggregate['commission_amount_cents']}"
    )

    listed = client.get("/api/admin/settlements", query_string={"stall_no": STALL_ADMIN})
    assert listed.status_code == 200, f"契约 §3.28 期望 200，实际 {listed.status_code}"
    rows = json_of(listed)
    assert isinstance(rows, list) and rows, "契约 §3.28 应返回结算单数组"
    matched = [row for row in rows if row.get("settlement_no") == payload["settlement_no"]]
    assert matched, "刚生成的结算单必须能在 §3.28 查到"
    assert {"period_start", "period_end", "gross_amount_cents", "commission_amount_cents", "version"} <= set(matched[0]), (
        f"§2.16 结算单元素缺字段：{sorted(matched[0])}"
    )
    assert len(rows) >= 1 and all(row.get("version", 0) >= 1 for row in rows), "结算单版本必须 ≥1（旧版本不删）"


def test_settlement_without_aggregate_mt_1010_and_bad_body(client):
    """契约 §3.27/§4 `MT-1010`：期内日聚合缺失 → 409 `MT-1010`（引导先做 §3.26）。"""
    uncovered = (date.today() - timedelta(days=365)).isoformat()
    response = client.post(
        "/api/admin/settlements", json={"stall_no": STALL_ADMIN, "period_start": uncovered, "period_end": uncovered}
    )
    assert response.status_code == 409, f"契约 §4 期望 409(MT-1010)，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1010")

    bad = client.post("/api/admin/settlements", json={"stall_no": STALL_ADMIN, "period_start": today_iso()})
    assert bad.status_code == 422, f"契约 §3.27 要求 period_end 必填，实际 {bad.status_code}"
    assert_error_response(bad, expected_code="MT-1008")

    reversed_period = client.post(
        "/api/admin/settlements",
        json={"stall_no": STALL_ADMIN, "period_start": today_iso(), "period_end": (date.today() - timedelta(days=1)).isoformat()},
    )
    assert reversed_period.status_code == 422, f"契约 §3.27 要求 period_end ≥ period_start，实际 {reversed_period.status_code}"
    assert_error_response(reversed_period, expected_code="MT-1008")


# ---------------------------------------------------------------------------
# §3.29 对账等式（`REQ-020`、`AC-003` 的运营端形态）
# ---------------------------------------------------------------------------


def test_reconciliation_equation_balanced(client):
    """`REQ-020`：对账返回「订单总额 = 支付流水 = 分账明细」三处数值与 `balanced` 判定。"""
    created = _paid_transaction(client, "recon")
    response = client.get(
        "/api/admin/reconciliation", query_string={"business_date": today_iso(), "stall_no": STALL_ADMIN}
    )
    assert response.status_code == 200, f"契约 §3.29 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert set(payload) == RECONCILIATION_FIELDS, f"契约 §3.29 响应键不符：{sorted(payload)}"
    assert payload["order_total_cents"] == payload["payment_total_cents"] == payload["split_total_cents"], (
        f"`REQ-020`：三处数值必须相等，实际 {payload!r}"
    )
    assert payload["balanced"] is True and payload["diff_cents"] == 0, f"对账必须平衡，实际 {payload!r}"
    assert payload["order_total_cents"] >= created["total_amount_cents"], (
        f"对账总额应包含刚成交的这笔（{created['total_amount_cents']} 分），实际 {payload['order_total_cents']}"
    )

    missing_date = client.get("/api/admin/reconciliation")
    assert missing_date.status_code == 422, f"契约 §3.29 要求 business_date 必填，实际 {missing_date.status_code}"
    assert_error_response(missing_date, expected_code="MT-1008")


# ---------------------------------------------------------------------------
# §3.30 三个使用率指标（`REQ-022` 修订版、`AC-005`）
# ---------------------------------------------------------------------------


def _expected_metrics(db_conn, business_date: str) -> dict[str, int]:
    """按 `data-model.md` §5.1 的口径**从库里直接算**出六个分子/分母（供交叉核对）。"""
    active_stalls = _count(db_conn, "SELECT COUNT(*) AS n FROM stall WHERE status = 'active'")
    transaction_stalls = _count(
        db_conn, 'SELECT COUNT(DISTINCT stall_id) AS n FROM "transaction" WHERE business_date = ?', (business_date,)
    )
    cash_txns = _count(
        db_conn,
        'SELECT COUNT(*) AS n FROM payment p JOIN "transaction" t ON t.id = p.transaction_id '
        "WHERE t.business_date = ? AND p.method = 'cash' AND p.status = 'success'",
        (business_date,),
    )
    all_txns = _count(db_conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE business_date = ?', (business_date,))
    maintained = _count(
        db_conn,
        "SELECT COUNT(*) AS n FROM stall s WHERE s.status = 'active' AND NOT EXISTS ("
        "  SELECT 1 FROM product p WHERE p.stall_id = s.id AND p.status = 'active' AND NOT EXISTS ("
        "    SELECT 1 FROM price_item pi WHERE pi.stall_id = s.id AND pi.product_id = p.id AND pi.business_date = ?))",
        (business_date,),
    )
    return {
        "stall_usage_numerator": transaction_stalls,
        "stall_usage_denominator": active_stalls,
        "cash_txn_numerator": cash_txns,
        "cash_txn_denominator": all_txns,
        "price_list_numerator": maintained,
        "price_list_denominator": active_stalls,
    }


def test_usage_metrics_three_indicators_with_numerator_and_denominator(client, db_conn):
    """`AC-005`：三项指标各自**随分子与分母一起返回**，且逐项回库核对（第三方可逐一核对）。"""
    _paid_transaction(client, "metrics")
    today = today_iso()
    response = client.get("/api/admin/metrics/usage", query_string={"business_date": today})
    assert response.status_code == 200, f"契约 §3.30 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert set(payload) >= METRIC_FIELDS, f"契约 §3.30 响应缺字段：{sorted(METRIC_FIELDS - set(payload))}"
    # 契约 §3.30 末注：本期**不提供**市场口径的「日均智能秤交易占比」（Q-15），禁止用估算值伪造分母
    extra_bp = {key for key in payload if key.endswith("_bp")} - {
        "stall_usage_bp",
        "cash_txn_share_bp",
        "price_list_maintenance_bp",
    }
    assert not extra_bp, f"`AC-005` 只允许三项指标，出现契约外的指标：{sorted(extra_bp)}"

    expected = _expected_metrics(db_conn, today)
    for key, value in expected.items():
        assert payload[key] == value, f"`AC-005` 指标 {key} 与库中直接统计不一致：{payload[key]} vs {value}"
    assert expected["stall_usage_denominator"] == 10, f"种子应有 10 个在营摊位，实际 {expected['stall_usage_denominator']}"

    pairs = {
        "stall_usage_bp": ("stall_usage_numerator", "stall_usage_denominator"),
        "cash_txn_share_bp": ("cash_txn_numerator", "cash_txn_denominator"),
        "price_list_maintenance_bp": ("price_list_numerator", "price_list_denominator"),
    }
    for bp_key, (num_key, den_key) in pairs.items():
        bp = payload[bp_key]
        assert isinstance(bp, int) and 0 <= bp <= 10000, f"{bp_key} 必须是 0–10000 的万分比整数，实际 {bp!r}"
        numerator, denominator = payload[num_key], payload[den_key]
        assert isinstance(numerator, int) and isinstance(denominator, int)
        assert denominator > 0, f"{bp_key} 的分母必须来自系统自有数据且不为 0，实际 {denominator}"
        assert numerator <= denominator, f"{num_key} 不得大于分母：{numerator} > {denominator}"
        expected_bp = round(numerator / denominator * 10000)
        assert abs(bp - expected_bp) <= 1, f"{bp_key} 与分子/分母不一致：{bp} vs {expected_bp}"

    missing = client.get("/api/admin/metrics/usage")
    assert missing.status_code == 422, f"契约 §3.30 要求 business_date 必填，实际 {missing.status_code}"
    assert_error_response(missing, expected_code="MT-1008")


# ---------------------------------------------------------------------------
# §3.25 市场方看板 + §3.31 留痕查询（只读）
# ---------------------------------------------------------------------------


def test_admin_dashboard_market_summary_shape(client):
    response = client.get("/api/admin/dashboard", query_string={"business_date": today_iso()})
    assert response.status_code == 200, f"契约 §3.25 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert {"business_date", "market", "stalls"} <= set(payload), f"契约 §3.25 响应缺字段：{sorted(payload)}"
    market = payload["market"]
    assert {"txn_count", "gross_amount_cents", "commission_amount_cents"} <= set(market), (
        f"契约 §3.25 market 缺字段：{sorted(market)}"
    )
    assert isinstance(market["txn_count"], int) and market["txn_count"] >= 1
    assert isinstance(payload["stalls"], list) and payload["stalls"], "契约 §3.25 应返回摊位汇总数组"


def test_audit_logs_are_read_only_and_cover_funding_events(client):
    """`NFR-009`：留痕**只读**（不提供任何写接口），且四类资金链路事件可查。"""
    listed = client.get("/api/admin/audit-logs", query_string={"event_type": "commission_rule_changed"})
    assert listed.status_code == 200, f"契约 §3.31 期望 200，实际 {listed.status_code}"
    payload = json_of(listed)
    assert {"total", "items"} <= set(payload), f"契约 §3.31 要求分页对象，实际 {sorted(payload)}"
    assert payload["total"] >= 1, "契约 §3.24 的配置变更必须能在留痕里查到"
    for item in payload["items"][:5]:
        assert {"id", "event_type", "ref_table", "ref_id", "payload_json", "actor", "occurred_at"} <= set(item), (
            f"§2.18 留痕元素缺字段：{sorted(item)}"
        )
        assert item["event_type"] == "commission_rule_changed"

    for method in ("post", "put", "delete", "patch"):
        response = getattr(client, method)("/api/admin/audit-logs", json={})
        assert response.status_code == 405, (
            f"契约 §3.31/`NFR-009`：留痕**不提供任何写接口**，{method.upper()} 应为 405，实际 {response.status_code}"
        )
