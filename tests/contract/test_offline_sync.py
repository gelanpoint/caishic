"""`T-012` 契约测试（二）：离线**补传**（§3.15 触发补传）。

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

> **本文件由 `Q-16` 的按语义拆分从原 `test_offline.py` 拆出**：只含 **§3.15 触发补传**
> （补传落库 + `NFR-013` 清除本地副本 + `REQ-015` 重复键丢弃且不阻断）。
> §3.13/§3.14（暂存状态、暂存一笔、阈值告警、写入失败注入、参数校验）在 `test_offline_stage.py`；
> **三个端点的「无会话 → 401 `MT-1005`」由 `test_offline_stage.py::test_offline_endpoints_require_session_mt_1005`
> 一并覆盖**（该用例会同时请求 sync 端点），故本文件不再重复一条同类断言。
"""

from __future__ import annotations

import pytest

from contract_support import (
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
# §3.15 补传 + `NFR-013` 清除本地副本（`AC-003`）
# ---------------------------------------------------------------------------


def test_sync_backfills_purges_local_copy_and_does_not_double_orders(client, db_conn):
    """`AC-003`/`NFR-013`/`RL-8`：补传成功 → 订单不翻倍，且**本地副本必须为空**（`payload_json` 空 + `purged_at` 非空）。"""
    token = bind_stall_session(client, STALL_SYNC)
    product_id = _products(client, token)[0]["id"]
    _set_price(client, token, product_id, 400, f"{FUTURE_KEY_PREFIX}-sync-price-1")
    stall_id = _stall_id(db_conn, STALL_SYNC)
    keys = [f"{FUTURE_KEY_PREFIX}-sync-key-1", f"{FUTURE_KEY_PREFIX}-sync-key-2"]

    for key in keys:
        staged = _stage(client, token, key, product_id)
        assert staged.status_code == 201, f"前置：契约 §3.14 期望 201，实际 {staged.status_code}"

    response = _sync(client, token)
    assert response.status_code == 200, f"契约 §3.15 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert set(payload) >= SYNC_FIELDS, f"契约 §3.15 响应缺字段：{sorted(SYNC_FIELDS - set(payload))}"
    assert payload["backfilled"] == 2, f"两笔暂存都应补传成功，实际 backfilled={payload['backfilled']!r}"
    assert payload["failed"] == 0, f"不应有失败条目，实际 failed={payload['failed']!r}"
    assert payload["duplicate_discarded"] == 0, f"本用例无重复键，实际 {payload['duplicate_discarded']!r}"
    assert payload["purged"] == 2, f"契约 §3.15：purged 应等于清除副本的条数，实际 {payload['purged']!r}"
    assert payload["pending_count"] == 0, f"补传后待补传应为 0，实际 {payload['pending_count']!r}"

    rows = db_conn.execute(
        "SELECT client_idempotency_key, status, payload_json, synced_at, purged_at FROM offline_queue "
        "WHERE stall_id = ? AND client_idempotency_key IN (?, ?)",
        (stall_id, *keys),
    ).fetchall()
    assert len(rows) == 2, f"暂存条目不应被物理删除（可审计），实际 {len(rows)} 条"
    for row in rows:
        assert row["status"] == "backfilled", f"{row['client_idempotency_key']} 状态应为 backfilled，实际 {row['status']!r}"
        assert row["payload_json"] is None, (
            f"`NFR-013`：补传成功后**本地副本必须清除**（payload_json 置空），"
            f"{row['client_idempotency_key']} 仍为 {row['payload_json']!r}"
        )
        assert row["purged_at"], f"`NFR-013`：清除动作必须有可核对证据 purged_at，{row['client_idempotency_key']} 为 NULL"
        assert row["synced_at"], "补传成功应写 synced_at"

    txn_count = _count(db_conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?', (stall_id,))
    distinct_no = _count(
        db_conn, 'SELECT COUNT(DISTINCT transaction_no) AS n FROM "transaction" WHERE stall_id = ?', (stall_id,)
    )
    assert txn_count == 2, f"`AC-003`：补传后订单总数不得翻倍，实际 {txn_count} 笔"
    assert distinct_no == txn_count, f"交易号必须唯一（REQ-031）：{distinct_no} vs {txn_count}"
    origins = {
        row["origin"]
        for row in db_conn.execute('SELECT origin FROM "transaction" WHERE stall_id = ?', (stall_id,)).fetchall()
    }
    assert origins == {"backfilled"}, f"补传生成的交易 origin 应为 backfilled（data-model.md §4.3），实际 {origins}"

    audit = _count(
        db_conn,
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'offline_backfilled' AND stall_id = ?",
        (stall_id,),
    )
    assert audit >= 2, f"补传必须写审计留痕（NFR-009 / data-model.md §4.3），实际 {audit} 条"

    reconciliation = client.get(
        "/api/admin/reconciliation",
        query_string={"business_date": today_iso(), "stall_no": STALL_SYNC},
    )
    assert reconciliation.status_code == 200, f"契约 §3.29 期望 200，实际 {reconciliation.status_code}"
    recon = json_of(reconciliation)
    assert recon["balanced"] is True and recon["diff_cents"] == 0, (
        f"`AC-003`：补传后对账等式「订单总额 = 支付流水 = 分账明细」必须成立，实际 {recon!r}"
    )

    again = _sync(client, token)
    assert again.status_code == 200, f"重复补传不应报错，实际 {again.status_code}"
    again_payload = json_of(again)
    assert again_payload["backfilled"] == 0 and again_payload["pending_count"] == 0, (
        f"队列已空时再次补传应为空转，实际 {again_payload!r}"
    )
    assert _count(db_conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?', (stall_id,)) == 2, (
        "重复补传不得再生成交易（订单总数不翻倍）"
    )


def test_sync_discards_duplicate_idempotency_key_and_does_not_block(client, db_conn):
    """`REQ-015`/`spec.md` §5：幂等键重复 → **丢弃该条、写审计日志、不报错、不阻断后续补传**。"""
    token = bind_stall_session(client, STALL_DUP)
    product_id = _products(client, token)[0]["id"]
    _set_price(client, token, product_id, 400, f"{FUTURE_KEY_PREFIX}-dup-price-1")
    stall_id = _stall_id(db_conn, STALL_DUP)
    assert_endpoint_implemented(client.application, "POST", "/api/merchant/offline/sync")

    # 前置：先在线创建一笔交易，占用幂等键 K（此后补传同键条目即构成"重复"）
    duplicated_key = f"{FUTURE_KEY_PREFIX}-dup-key-K"
    online = client.post(
        "/api/merchant/transactions",
        json={"items": [{"product_id": product_id, "weight_grams": 1000}]},
        headers=session_headers(token, duplicated_key),
    )
    assert online.status_code == 201, f"前置：契约 §3.6 期望 201，实际 {online.status_code}"

    fresh_key = f"{FUTURE_KEY_PREFIX}-dup-key-FRESH"
    assert _stage(client, token, duplicated_key, product_id).status_code == 201
    assert _stage(client, token, fresh_key, product_id).status_code == 201

    response = _sync(client, token)
    assert response.status_code == 200, (
        f"幂等键重复**不是错误**（REQ-015 / spec.md §5）：必须返回 200，实际 {response.status_code}"
    )
    payload = json_of(response)
    assert payload["duplicate_discarded"] == 1, f"应丢弃 1 条重复条目，实际 {payload['duplicate_discarded']!r}"
    assert payload["backfilled"] == 1, f"**重复条目不阻断后续补传**：另一条应补传成功，实际 {payload['backfilled']!r}"
    assert payload["failed"] == 0, f"丢弃不是失败，实际 failed={payload['failed']!r}"
    assert payload["pending_count"] == 0, f"两条都应离开 staged 状态，实际 {payload['pending_count']!r}"
    assert "error" not in payload, "丢弃重复条目不得走统一错误响应（spec.md §5：不报错）"

    discarded = db_conn.execute(
        "SELECT status, discard_reason FROM offline_queue WHERE client_idempotency_key = ?", (duplicated_key,)
    ).fetchone()
    assert discarded["status"] == "duplicate_discarded", f"重复条目状态不符：{discarded['status']!r}"
    assert discarded["discard_reason"] == "duplicate_idempotency_key", (
        f"data-model.md §2.13 要求写 discard_reason=duplicate_idempotency_key，实际 {discarded['discard_reason']!r}"
    )
    fresh = db_conn.execute(
        "SELECT status, payload_json FROM offline_queue WHERE client_idempotency_key = ?", (fresh_key,)
    ).fetchone()
    assert fresh["status"] == "backfilled" and fresh["payload_json"] is None, (
        f"被丢弃条目之后的补传必须照常完成（REQ-015）：{dict(fresh)!r}"
    )

    audit = _count(
        db_conn,
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'offline_duplicate_discarded' AND stall_id = ?",
        (stall_id,),
    )
    assert audit >= 1, "丢弃重复补传必须写审计日志（REQ-015 / NFR-009）"

    txn_keys = {
        row["client_idempotency_key"]
        for row in db_conn.execute(
            'SELECT client_idempotency_key FROM "transaction" WHERE stall_id = ?', (stall_id,)
        ).fetchall()
    }
    assert duplicated_key in txn_keys and fresh_key in txn_keys, f"两条交易都应在库中（原单 + 补传），实际 {txn_keys}"
    assert _count(db_conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?', (stall_id,)) == 2, (
        "重复键被丢弃后不得新增第二条同键交易"
    )
