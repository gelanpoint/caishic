"""`T-031` 端到端验证（二之三）：**离线链路异常**（真实服务进程 + 真实库）。

覆盖：

| # | 场景 | 判据 | 关联 |
| --- | --- | --- | --- |
| 1 | 暂存写入失败 | 500 `MT-1007`、**一行不留**、留痕、故障消除后可重试 | `REQ-030`、`AC-019` |
| 2 | 暂存达告警阈值 | 只告警、**继续接受**、一笔不丢、且阈值之上的照样能补传 | `REQ-016`、`AC-018` |

两条硬要求在这里被"反过来"验证（`RL-9` / `NFR-014`「绝不静默丢弃」）：

- 失败那条**必须吵**：报 500 + 契约错误码 + 留痕，**且不得留下半条记录** ——
  最坏的失败不是"报错"，而是"报错了但库里已经有这笔"；
- 达阈值那条**必须继续收**：`threshold` 是**告警**阈值，不是硬上限 ——
  测试从接口读阈值（`§3.13` 的 `threshold`），**不在本文件复述业务数值**。

**故障注入方式（如实说明）**：在真库上加 `BEFORE INSERT ... RAISE(ABORT)` 触发器制造
"存储拒绝接受这条暂存记录"，与 `T-012` 契约测试同一套手法（口径一致），结束时 `DROP TRIGGER`。
它在**独立的服务实例**（`fault_server`）上做 —— 否则"上一次故障注入"会变成"下一次失败"的假凶手。

> 本文件与 `test_edge_cases.py`（输入与权限边界）、`test_edge_payment.py`（支付回调异常）
> 按**语义**拆分（单文件 ≤400 行），三者共用 `tests/conftest.py` 的夹具与助手。
"""

from __future__ import annotations

from conftest import (
    active_products,
    assert_api_error,
    bind_stall,
    count,
    evidence_key,
    session_headers,
    stall_id,
)

FAULT_TRIGGER = "e2e31_staging_write_fault"


# ---------------------------------------------------------------------------
# 场景 1：暂存写入失败 → MT-1007，且**不得标记为已成功记账**（AC-019 / REQ-030）
# ---------------------------------------------------------------------------


def test_staging_write_failure_mt_1007_marks_nothing_ac_019(fault_server):
    """`AC-019`：存储拒绝接受暂存记录 → **明确报错**、**一行不留**、**留痕**、**可重试**。"""
    stall = "A-05"
    stall_db_id = stall_id(fault_server, stall)
    token = bind_stall(fault_server, stall)
    product = active_products(fault_server, token)[0]
    key = evidence_key("stage-fault")
    body = {"items": [{"product_id": product["id"], "weight_grams": 1000}], "client_idempotency_key": key}

    conn = fault_server.connect_db()
    try:
        queue_before = count(conn, "SELECT COUNT(*) AS n FROM offline_queue WHERE stall_id = ?", (stall_db_id,))
        txn_before = count(conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?', (stall_db_id,))
        conn.execute(
            f"CREATE TRIGGER {FAULT_TRIGGER} BEFORE INSERT ON offline_queue "
            "BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END"
        )
        conn.commit()
        try:
            status, payload = fault_server.api(
                "POST", "/api/merchant/offline/queue", body, session_headers(token, key)
            )
        finally:
            conn.execute(f"DROP TRIGGER IF EXISTS {FAULT_TRIGGER}")
            conn.commit()

        error = assert_api_error(status, payload, "MT-1007", 500, "暂存写入失败")
        assert status != 201, "`REQ-030`：写入失败**绝不能**标记为已成功记账"

        queue_after = count(conn, "SELECT COUNT(*) AS n FROM offline_queue WHERE stall_id = ?", (stall_db_id,))
        txn_after = count(conn, 'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?', (stall_db_id,))
        by_key = count(conn, "SELECT COUNT(*) AS n FROM offline_queue WHERE client_idempotency_key = ?", (key,))
        audit = count(
            conn,
            "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'staging_write_failed' AND stall_id = ?",
            (stall_db_id,),
        )
    finally:
        conn.close()

    assert queue_after == queue_before, f"失败条目一行都不许留：{queue_before} → {queue_after}"
    assert txn_after == txn_before, f"暂存失败不得生成交易：{txn_before} → {txn_after}"
    assert by_key == 0, "失败的条目不得占用幂等键"
    assert audit >= 1, "失败处置必须留痕（`staging_write_failed`）"

    # **确定性反馈**的另一半：故障消除后同一笔重试必须成功（摊主有路可走，不是死胡同）
    status, retry = fault_server.api(
        "POST", "/api/merchant/offline/queue", body, session_headers(token, key)
    )
    assert status == 201 and retry["status"] == "staged", (
        f"故障消除后重试应 201 staged，实际 {status}：{retry}"
    )
    print(f"[边界] 暂存写入失败 → {error['code']} 500；零残留（队列 {queue_before} 条）、留痕 {audit} 条；"
          f"故障消除后重试 → 201 staged")


# ---------------------------------------------------------------------------
# 场景 2：暂存达告警阈值仍继续接受（AC-018 / REQ-016 / NFR-014）
# ---------------------------------------------------------------------------


def test_offline_threshold_keeps_accepting_ac_018(live_server):
    """`AC-018`：达告警阈值后**新交易仍被接受**，且**一笔都不丢**、**阈值之上的也能补传**。

    阈值**从接口读**（`§3.13` 的 `threshold`）—— 业务取值只有一个出处，测试只核对它声明的行为。
    """
    stall = "A-04"
    token = bind_stall(live_server, stall)
    product = active_products(live_server, token)[0]

    status, queue = live_server.api("GET", "/api/merchant/offline/queue", headers=session_headers(token))
    assert status == 200, f"契约 §3.13 期望 200，实际 {status}：{queue}"
    threshold = int(queue["threshold"])
    assert threshold > 0, f"告警阈值应来自服务端配置：{queue}"
    assert queue["pending_count"] == 0 and queue["threshold_warned"] is False, (
        f"前置：该摊位初始应无待补传：{queue}"
    )

    total = threshold + 2
    warned_at = None
    for index in range(total):
        status, staged = live_server.api(
            "POST", "/api/merchant/offline/queue",
            {"items": [{"product_id": product["id"], "weight_grams": 1000}]},
            session_headers(token, evidence_key(f"thr-{index}")),
        )
        assert status == 201, f"第 {index + 1} 笔暂存必须被接受（绝不静默丢弃）：HTTP {status} {staged}"
        assert staged["pending_count"] == index + 1, (
            f"第 {index + 1} 笔后 pending 应为 {index + 1}，实际 {staged['pending_count']}（有丢弃？）"
        )
        if staged["threshold_warned"] and warned_at is None:
            warned_at = index + 1

    assert warned_at == threshold, f"应在第 {threshold} 笔开始告警，实际第 {warned_at} 笔"

    status, queue = live_server.api("GET", "/api/merchant/offline/queue", headers=session_headers(token))
    assert status == 200 and queue["threshold_warned"] is True, f"达阈值后状态应告警：{queue}"
    assert queue["oldest_staged_at"], "达阈值时应能给出最早暂存时刻"

    conn = live_server.connect_db()
    try:
        rows = count(
            conn,
            "SELECT COUNT(*) AS n FROM offline_queue WHERE stall_id = "
            "(SELECT id FROM stall WHERE stall_no = ?)",
            (stall,),
        )
        staged_only = count(
            conn,
            "SELECT COUNT(*) AS n FROM offline_queue WHERE stall_id = "
            "(SELECT id FROM stall WHERE stall_no = ?) AND status = 'staged'",
            (stall,),
        )
    finally:
        conn.close()
    assert rows == total and staged_only == total, f"库里应有且仅有 {total} 条待补传：{rows}/{staged_only}"

    # 收口：阈值之上的这些必须能真的补传成功并清除本地副本（"接受了"不等于"能落地"）
    status, synced = live_server.api("POST", "/api/merchant/offline/sync", headers=session_headers(token))
    assert status == 200, f"契约 §3.15 期望 200，实际 {status}：{synced}"
    assert synced["backfilled"] == total and synced["failed"] == 0, f"阈值之上的交易也必须能补传：{synced}"
    assert synced["purged"] == total and synced["pending_count"] == 0, f"补传后应清除全部本地副本：{synced}"
    print(f"[边界] 阈值 {threshold}：第 {warned_at} 笔起告警；再收 2 笔仍 201；"
          f"补传 {synced['backfilled']} 笔、清除副本 {synced['purged']} 笔、零丢弃")
