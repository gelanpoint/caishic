"""`T-012` 契约测试（一）：离线**暂存**（§3.13 暂存状态 / §3.14 暂存一笔）。

关联：`REQ-014`、`REQ-015`、`REQ-016`、`REQ-030`；`NFR-013`、`NFR-014`；`AC-003`、`AC-018`、`AC-019`。
覆盖契约：§3.13 暂存状态、§3.14 暂存一笔交易、§3.15 触发补传。
错误码用例：`MT-1005`（会话无效）、`MT-1007`（**暂存写入失败**）、`MT-1008`（参数校验失败）。

> **本文件三条硬要求（`spec.md` §5 边界表逐条对应）**：
> ① `NFR-013`/`RL-8`：补传成功后**必须清除本地副本** —— 断言 `payload_json IS NULL` 且 `purged_at` 非空；
> ② `REQ-015`：补传幂等键重复 → **丢弃该条、写审计日志、不报错、不阻断后续补传**（返回 200 且 `duplicate_discarded` 计数）；
> ③ `NFR-014`/`RL-9`：达告警阈值**只告警、仍继续接受**，且**绝不静默丢弃**（逐条计数核对）。
>
> **`AC-019`（`MT-1007`）的故障注入方式（如实说明，不是"猜实现"）**：契约把 `MT-1007` 定义为
> 「暂存写入失败（磁盘空间不足 / 文件不可写）」。本文件在 **`offline_queue` 表上临时加一个
> `BEFORE INSERT` 触发器 `RAISE(ABORT)`** 来注入"存储拒绝接受这条暂存记录"这一故障：
> 它**只影响暂存表的写入**（鉴权与其它读路径完全不受影响）、**不依赖任何实现内部函数名**，
> 因而不会随 `T-019` 的实现风格漂移；测试结束即 `DROP TRIGGER` 复原。
> 若 `T-019` 把暂存写路径的 `sqlite3.Error` 映射成了别的错误码，本用例会变红并给出映射提示 ——
> 那是契约要求的错误码没被实现，不是测试写错。

> **本文件由 `Q-16` 的按语义拆分从原 `test_offline.py` 拆出**（原文件 425 行超 `quality-gates` §1.2 的
> 400 行阈值）：**§3.13 暂存状态 / §3.14 暂存一笔**（含达阈值仍接受、暂存写入失败注入、参数与会话校验）
> 留在这里；**§3.15 补传**（补传落库、清除本地副本、重复键丢弃）在 `test_offline_sync.py`。
> 两半各自独立自足（各自用专属摊位、各自造数），**没有跨文件顺序依赖**。
> 摊位数与专用摊位说明见下方常量区（两半共用同一张摊位分配表）。
"""

from __future__ import annotations

import pytest

from conftest import (
    assert_endpoint_implemented,
    assert_error_response,
    assert_exact_keys,
    bind_stall_session,
    json_of,
    session_headers,
    today_iso,
)

STALL_SYNC = "A-06"  # 补传 / 清除副本主用例
STALL_THRESHOLD = "A-07"  # 告警阈值用例（会暂存大量条目，单独占一个摊位）
STALL_FAULT = "A-08"  # 暂存写入失败注入
STALL_DUP = "A-09"  # 幂等键重复丢弃
STALL_MISC = "A-10"  # 状态查询与参数校验

QUEUE_STATUS_FIELDS = {"pending_count", "threshold", "threshold_warned", "oldest_staged_at", "online"}
STAGE_FIELDS = {"queue_id", "status", "pending_count", "threshold_warned"}
SYNC_FIELDS = {"backfilled", "duplicate_discarded", "failed", "pending_count", "purged"}
FUTURE_KEY_PREFIX = "t012"

def _stall_id(db_conn, stall_no: str) -> int:
    row = db_conn.execute("SELECT id FROM stall WHERE stall_no = ?", (stall_no,)).fetchone()
    assert row is not None, f"种子数据应有摊位 {stall_no}"
    return row["id"]


def _count(db_conn, sql: str, params: tuple = ()) -> int:
    return db_conn.execute(sql, params).fetchone()["n"]


def _products(client, token: str) -> list:
    response = client.get("/api/merchant/products", headers=session_headers(token))
    assert response.status_code == 200, f"前置：契约 §3.5 期望 200，实际 {response.status_code}"
    products = json_of(response)
    assert products, "前置：种子数据应有可售商品"
    return products


def _set_price(client, token: str, product_id: int, price_cents: int, key: str) -> None:
    """前置：把当日单价设成能整除的值（消除取整歧义，同 `T-009` 的做法）。"""
    response = client.post(
        "/api/merchant/price-list",
        json={"business_date": today_iso(), "items": [{"product_id": product_id, "unit_price_cents": price_cents}]},
        headers=session_headers(token, key),
    )
    assert response.status_code == 200, f"前置：契约 §3.4 设价失败 {response.status_code}"


def _queue_status(client, token: str):
    return client.get("/api/merchant/offline/queue", headers=session_headers(token))


def _stage(client, token: str, key: str, product_id: int, weight_grams: int = 1000):
    """按契约 §3.14 暂存一笔交易（载荷结构与 §3.6 相同）。"""
    return client.post(
        "/api/merchant/offline/queue",
        json={
            "items": [{"product_id": product_id, "weight_grams": weight_grams}],
            "client_idempotency_key": key,
        },
        headers=session_headers(token, key),
    )


def _sync(client, token: str):
    return client.post("/api/merchant/offline/sync", headers=session_headers(token))



# ---------------------------------------------------------------------------
# §3.13 暂存状态（`REQ-014` 可视标识 + `REQ-016` 阈值）
# ---------------------------------------------------------------------------


def test_offline_queue_status_shape(client):
    """契约 §3.13：状态字段与阈值由接口给出（阈值数值来源是 `spec.md` `REQ-016`，此处不硬编码）。"""
    token = bind_stall_session(client, STALL_MISC)
    response = _queue_status(client, token)
    assert response.status_code == 200, f"契约 §3.13 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert set(payload) >= QUEUE_STATUS_FIELDS, f"契约 §3.13 响应缺字段：{sorted(QUEUE_STATUS_FIELDS - set(payload))}"
    assert isinstance(payload["pending_count"], int) and payload["pending_count"] >= 0
    assert isinstance(payload["threshold"], int) and payload["threshold"] > 0, (
        f"契约 §3.13 的 threshold 应为正整数（来源 spec.md REQ-016），实际 {payload['threshold']!r}"
    )
    assert isinstance(payload["threshold_warned"], bool), "threshold_warned 应为布尔值"
    assert isinstance(payload["online"], bool), "online 应为布尔值（REQ-014 离线/在线可视标识）"


def test_stage_persists_payload_and_reports_pending(client, db_conn):
    """契约 §3.14：暂存成功 → 201 + `staged`，载荷**本地持久化**（本地副本本体）。"""
    token = bind_stall_session(client, STALL_MISC)
    product_id = _products(client, token)[0]["id"]
    _set_price(client, token, product_id, 400, f"{FUTURE_KEY_PREFIX}-misc-price-1")

    response = _stage(client, token, f"{FUTURE_KEY_PREFIX}-misc-stage-1", product_id)
    assert response.status_code == 201, f"契约 §3.14 期望 201，实际 {response.status_code}"
    payload = json_of(response)
    assert set(payload) >= STAGE_FIELDS, f"契约 §3.14 响应缺字段：{sorted(STAGE_FIELDS - set(payload))}"
    assert payload["status"] == "staged"
    assert isinstance(payload["queue_id"], int)
    assert payload["pending_count"] >= 1, f"暂存后 pending_count 应 ≥1，实际 {payload['pending_count']!r}"

    stall_id = _stall_id(db_conn, STALL_MISC)
    row = db_conn.execute(
        "SELECT status, payload_json, business_date, stall_id FROM offline_queue WHERE client_idempotency_key = ?",
        (f"{FUTURE_KEY_PREFIX}-misc-stage-1",),
    ).fetchone()
    assert row is not None, "暂存未落库（REQ-014：本地持久暂存）"
    assert row["status"] == "staged"
    assert row["stall_id"] == stall_id and row["business_date"] == today_iso()
    assert row["payload_json"], "本地副本本体 payload_json 不得为空（NFR-013 的前提）"

    status = json_of(_queue_status(client, token))
    assert status["pending_count"] == payload["pending_count"], "契约 §3.13 与 §3.14 的 pending_count 应一致"
    assert status["oldest_staged_at"], "有暂存条目时 oldest_staged_at 不得为 null"


# ---------------------------------------------------------------------------
# `AC-018` 阈值只告警、仍继续接受（`REQ-016` / `NFR-014` / `RL-9`）
# ---------------------------------------------------------------------------


def test_threshold_warns_but_keeps_accepting_without_dropping(client, db_conn):
    """`AC-018`/`NFR-014`：达告警阈值只提示，**继续接受新交易且绝不静默丢弃**（逐条计数核对）。"""
    token = bind_stall_session(client, STALL_THRESHOLD)
    product_id = _products(client, token)[0]["id"]
    _set_price(client, token, product_id, 400, f"{FUTURE_KEY_PREFIX}-thresh-price-1")

    status = json_of(_queue_status(client, token))
    threshold = status["threshold"]
    assert isinstance(threshold, int) and threshold > 0
    assert status["pending_count"] == 0, f"前置：该摊位应是空队列，实际 {status['pending_count']}"

    total = threshold + 1
    for index in range(total):
        staged = _stage(client, token, f"{FUTURE_KEY_PREFIX}-thresh-{index:04d}", product_id)
        assert staged.status_code == 201, (
            f"`NFR-014`：达阈值后**仍须继续接受**新交易 —— 第 {index + 1} 笔返回 {staged.status_code}"
        )
        body = json_of(staged)
        assert body["pending_count"] == index + 1, f"pending_count 应逐笔 +1，实际 {body['pending_count']!r}"

    stall_id = _stall_id(db_conn, STALL_THRESHOLD)
    staged_rows = _count(
        db_conn, "SELECT COUNT(*) AS n FROM offline_queue WHERE stall_id = ? AND status = 'staged'", (stall_id,)
    )
    assert staged_rows == total, f"`RL-9` 绝不静默丢弃：应为 {total} 条 staged，实际 {staged_rows} 条"

    final = json_of(_queue_status(client, token))
    assert final["pending_count"] == total, f"状态接口应反映全部暂存条目，实际 {final['pending_count']!r}"
    assert final["threshold_warned"] is True, (
        f"`REQ-016`：暂存超过告警阈值 {threshold} 时必须给出告警标识，实际 {final['threshold_warned']!r}"
    )
    assert final["oldest_staged_at"], "达阈值时应能给出最早暂存时间（供摊主判断离线时长）"


# ---------------------------------------------------------------------------
# `AC-019` 暂存写入失败 → `MT-1007` 且不得标记为已成功记账（`REQ-030`）
# ---------------------------------------------------------------------------

FAULT_TRIGGER = "t012_staging_write_fault"


def test_staging_write_failure_returns_mt_1007_and_marks_nothing(client, db_conn):
    """`AC-019`/`REQ-030`：暂存写入失败 → 500 `MT-1007`；**不得标记为已成功记账**，且写审计留痕。"""
    token = bind_stall_session(client, STALL_FAULT)
    product_id = _products(client, token)[0]["id"]
    _set_price(client, token, product_id, 400, f"{FUTURE_KEY_PREFIX}-fault-price-1")
    stall_id = _stall_id(db_conn, STALL_FAULT)
    key = f"{FUTURE_KEY_PREFIX}-fault-key-1"
    assert_endpoint_implemented(client.application, "POST", "/api/merchant/offline/queue")

    queue_before = _count(db_conn, "SELECT COUNT(*) AS n FROM offline_queue WHERE stall_id = ?", (stall_id,))
    txn_before = _count(db_conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?', (stall_id,))

    db_conn.execute(
        f"CREATE TRIGGER {FAULT_TRIGGER} BEFORE INSERT ON offline_queue "
        "BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END"
    )
    db_conn.commit()
    try:
        response = _stage(client, token, key, product_id)
    finally:
        db_conn.execute(f"DROP TRIGGER IF EXISTS {FAULT_TRIGGER}")
        db_conn.commit()

    assert response.status_code != 201, (
        f"`REQ-030`：暂存写入失败**不得标记为已成功记账**，实际返回 {response.status_code}"
    )
    assert response.status_code == 500, f"契约 §3.14/§4 期望 500，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1007")

    queue_after = _count(db_conn, "SELECT COUNT(*) AS n FROM offline_queue WHERE stall_id = ?", (stall_id,))
    txn_after = _count(db_conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?', (stall_id,))
    assert queue_after == queue_before, f"失败的暂存不得落任何行（data-model.md §2.13 末注）：{queue_before} → {queue_after}"
    assert txn_after == txn_before, f"暂存失败不得生成交易：{txn_before} → {txn_after}"
    assert _count(
        db_conn, "SELECT COUNT(*) AS n FROM offline_queue WHERE client_idempotency_key = ?", (key,)
    ) == 0, "失败的条目不占幂等键（不得留下半条记录）"

    audit = _count(
        db_conn,
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'staging_write_failed' AND stall_id = ?",
        (stall_id,),
    )
    assert audit >= 1, "`REQ-030` 失败处置必须留痕（data-model.md §2.13 末注：event_type = staging_write_failed）"


# ---------------------------------------------------------------------------
# §4 会话与参数校验
# ---------------------------------------------------------------------------


def test_offline_endpoints_require_session_mt_1005(client):
    token = bind_stall_session(client, STALL_MISC)
    product_id = _products(client, token)[0]["id"]

    no_status = client.get("/api/merchant/offline/queue")
    assert no_status.status_code == 401, f"契约 §1.6 期望 401，实际 {no_status.status_code}"
    assert_error_response(no_status, expected_code="MT-1005")

    no_stage = client.post(
        "/api/merchant/offline/queue",
        json={"items": [{"product_id": product_id, "weight_grams": 1000}]},
        headers={"Idempotency-Key": f"{FUTURE_KEY_PREFIX}-misc-nosess"},
    )
    assert no_stage.status_code == 401, f"契约 §1.6 期望 401，实际 {no_stage.status_code}"
    assert_error_response(no_stage, expected_code="MT-1005")

    no_sync = client.post("/api/merchant/offline/sync")
    assert no_sync.status_code == 401, f"契约 §1.6 期望 401，实际 {no_sync.status_code}"
    assert_error_response(no_sync, expected_code="MT-1005")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"items": []},
        {"items": [{"product_id": 1}]},
        {"items": [{"product_id": 1, "weight_grams": 0}]},
        {"items": [{"product_id": 1, "weight_grams": 1000}], "client_idempotency_key": "x" * 65},
    ],
)
def test_stage_invalid_body_mt_1008(client, body):
    """契约 §3.14：载荷与 §3.6 同构 → 缺 `items` / 空数组 / 重量越界 / 幂等键超长都属参数校验失败。"""
    token = bind_stall_session(client, STALL_MISC)
    response = client.post(
        "/api/merchant/offline/queue", json=body, headers=session_headers(token, f"{FUTURE_KEY_PREFIX}-misc-bad")
    )
    assert response.status_code == 422, f"契约 §3.14/§4 期望 422，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1008")
