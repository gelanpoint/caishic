"""审计留痕写入（`NFR-009`：只增不改；`data-model.md` §2.18）。

契约 §3.9 改价、§3.11 退货、§3.15 补传等资金链路动作都要在这里留痕。
**只增不改**由数据库触发器强制（`app/migrations/0001_init.sql`），应用层**不提供任何修改入口** ——
本模块只导出"写入"一个动作，没有 update / delete。

关于 `event_type` / `ref_table` 的取值集合：**不在这里再抄一遍**。它们的唯一权威是
`0001_init.sql` 里 `audit_log` 的 `CHECK` 约束（那是最后一道闸门，抄一遍就等于埋一个漂移点）。
"""

from __future__ import annotations

import json
import sqlite3

from ..db import now_iso


def write_audit(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    ref_table: str,
    ref_id: int,
    payload: dict,
    actor: str,
    stall_id: int | None = None,
    occurred_at: str | None = None,
) -> int:
    """写入一条留痕，返回 `audit_log.id`。

    `payload_json` 统一由本函数序列化（保证是合法 JSON，而不是各调用点各写一遍 `json.dumps`）。
    """
    cursor = conn.execute(
        """
        INSERT INTO audit_log (event_type, stall_id, ref_table, ref_id, payload_json, actor, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            stall_id,
            ref_table,
            ref_id,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            actor[:32],
            occurred_at or now_iso(),
        ),
    )
    return int(cursor.lastrowid)
