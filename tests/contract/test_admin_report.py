"""`T-013` 契约测试（三）：运营端**看板、指标与留痕**（§3.25 市场方看板、§3.30 三项使用率指标、§3.31 留痕查询）。

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

> **本文件由 `Q-16` 的按语义拆分从原 `test_admin.py` 拆出**，并经受一次**再拆分**：
> 从 `test_admin_ops.py` 分出**「给别人看数」的那三块** —— §3.25 市场方看板、§3.30 三项使用率指标
> （含 `Q-19` 灵敏度负例）、§3.31 留痕查询。**钱怎么算**（§3.26 聚合 / §3.27-§3.28 结算 / §3.29 对账）
> 在 `test_admin_ops.py`；**配置类**端点（§3.20~§3.24）在 `test_admin.py`。
> **本文件内部有顺序依赖**：§3.25 看板要求「当日已有交易」，而交易由 §3.30 的用例造出，
> 故 **§3.30 必须排在 §3.25 之前**（文件内已按此排列）；`Q-19` 负例会**改共享数据**（删一行价格、
> 加一笔未收款交易），故它排在**文件最后**（见该用例注释的副作用说明）。
> **跨文件前置**：§3.31 断言 `commission_rule_changed` 留痕存在，由 `test_admin.py` 的 §3.24 用例写入 ⇒
> **不要只跑本文件**，整份契约测试必须一起跑。
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


# ---------------------------------------------------------------------------
# §3.30 的灵敏度负例（`Q-19`）：**制造"分子≠分母"再断言**
# ---------------------------------------------------------------------------
#
# 为什么必须有这一条（`Q-19` 实测，2026-09-30）：
# `test_usage_metrics_three_indicators_with_numerator_and_denominator` 会把六个分子/分母逐项回库核对，
# 判据本身很硬 —— 但它是在**种子 + 本套测试的数据状态**下跑的，而那个状态下
# `cash_txn` 分子 1 / 分母 1、`price_list` 分子 10 / 分母 10（**分子恰好等于分母**）。
# 于是"把分子写死成常量"与"真去查库"**输出完全一样**，该用例**抓不到写死**（实测：把
# `price_list_numerator` 写死成 `active_stalls`，上面的用例仍然通过）。
#
# 本用例的作用就是**把这个盲区堵上**：先把状态造成"分子 ≠ 分母"，再断言。
# 这样任何一处写死（分子固定、分母固定、"分子=分母"）都必然失败。
#
# 副作用与安全边界（有意接受）：本用例会**删掉一个在营摊位当日某个在售商品的价格行**、
# 并**新增一笔未收款的交易**，这两处都改变了会话级共享数据。故：
#   - 它放在本文件**最后**（前面的用例如需完整价目表，早已跑完）；
#   - 删除只针对**一个**摊位的**一行**，且该摊位**不是** `STALL_ADMIN`（避免影响结算/对账用例）；
#   - 全仓库没有任何其它用例断言"价目表维护率 = 10000"（已核：`_expected_metrics` 是**回库现算**的，
#     不写死期望值，故它反而会跟着新状态一起走）；


def test_usage_metrics_numerator_is_actually_queried(client, db_conn):
    """`Q-19` / `AC-005` 灵敏度负例：分子与分母**必须真的来自查询**，写死即失败。"""
    today = today_iso()
    _paid_transaction(client, "q19")

    # ① 先造一笔**未收款**交易：分母（当日全部走秤笔数）会 +1，而现金分子不动 ⇒ 现金分子 < 分母
    token = bind_stall_session(client, STALL_ADMIN)
    create_priced_transaction(client, token, idempotency_key="t013-q19-unpaid")

    # ② 再让某个**非观察摊位**当日缺一个在售商品的价格 ⇒ 价目表维护率分子 = 分母 − 1
    victim = db_conn.execute(
        """
        SELECT s.id, s.stall_no FROM stall s
        WHERE s.status = 'active' AND s.stall_no <> ?
          AND EXISTS (SELECT 1 FROM product p JOIN price_item pi
                        ON pi.product_id = p.id AND pi.stall_id = s.id
                      WHERE p.stall_id = s.id AND p.status = 'active' AND pi.business_date = ?)
        ORDER BY s.stall_no LIMIT 1
        """,
        (STALL_ADMIN, today),
    ).fetchone()
    assert victim is not None, "前置：应存在一个已维护价目表的在营摊位"
    removed = db_conn.execute(
        """
        DELETE FROM price_item WHERE id = (
            SELECT pi.id FROM price_item pi JOIN product p ON p.id = pi.product_id
            WHERE p.stall_id = ? AND p.status = 'active' AND pi.business_date = ?
            ORDER BY pi.id LIMIT 1)
        """,
        (int(victim["id"]), today),
    ).rowcount
    assert removed == 1, f"前置：应删掉 {victim['stall_no']} 当日的一行价格，实际 {removed}"
    db_conn.commit()

    payload = json_of(client.get("/api/admin/metrics/usage", query_string={"business_date": today}))
    expected = _expected_metrics(db_conn, today)

    # 六个数逐一回库核对（与主用例同强度）
    for key, value in expected.items():
        assert payload[key] == value, f"`AC-005` 指标 {key} 与库中直接统计不一致：{payload[key]} vs {value}"

    # 本用例的**关键断言**：状态已造成"分子≠分母"，写死任何一侧都过不了
    assert payload["price_list_numerator"] == payload["price_list_denominator"] - 1, (
        "`Q-19`：已造出『某摊位当日缺一个在售商品价格』的状态，价目表维护率分子应恰为分母 − 1；"
        f"实际 分子={payload['price_list_numerator']} 分母={payload['price_list_denominator']}"
        "（若这里相等，说明分子没有真的查库）"
    )
    assert payload["cash_txn_numerator"] < payload["cash_txn_denominator"], (
        "`Q-19`：已造出一笔未收款交易，现金交易占比的分子应严格小于分母；"
        f"实际 分子={payload['cash_txn_numerator']} 分母={payload['cash_txn_denominator']}"
    )
    assert payload["cash_txn_numerator"] == expected["cash_txn_numerator"], "现金分子必须等于回库统计"

    # 万分比必须由这两个数推出来（不许另算一套），容差 ±1 吸收取整方式差异
    for bp_key, num_key, den_key in (
        ("price_list_maintenance_bp", "price_list_numerator", "price_list_denominator"),
        ("cash_txn_share_bp", "cash_txn_numerator", "cash_txn_denominator"),
    ):
        numerator, denominator = payload[num_key], payload[den_key]
        assert denominator > 0
        expected_bp = round(numerator / denominator * 10000)
        assert abs(payload[bp_key] - expected_bp) <= 1, (
            f"{bp_key} 与分子/分母不一致：{payload[bp_key]} vs {expected_bp}"
        )
