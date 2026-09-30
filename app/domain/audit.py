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

from .. import TradeError
from ..db import now_iso
from .catalog import parse_business_date


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


#: `audit_log` 的对外字段（`data-model.md` §2.18）
AUDIT_FIELDS = "l.id, l.event_type, l.stall_id, l.ref_table, l.ref_id, l.payload_json, l.actor, l.occurred_at"
#: §3.7 同款分页默认；契约 §3.31 只要求"分页对象"，上限取与列表端点一致的 200，避免一次拖全表
DEFAULT_LIMIT, MAX_LIMIT = 50, 200


def query_audit_logs(
    conn: sqlite3.Connection,
    *,
    stall_no: str | None = None,
    event_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> dict:
    """契约 §3.31：**只读**查询资金链路留痕 → `{"total": n, "items": [...]}`。

    - 查询端**只能读**：`UPDATE` / `DELETE` 已被数据库触发器拒绝（`audit_log_no_update` /
      `audit_log_no_delete`），本函数也不提供任何写入；**不绕过触发器**（`NFR-009`）。
      路由只注册 `GET`，其余方法由框架回 405（`T-013` 的只读用例即断言这一点）。
    - `payload_json` **原样返回**（`§2.18` 里它是 TEXT）。**不在查询端解析成对象**：
      解析会把"留存原文"变成"二次加工过的视图"，一旦解析规则变了，历史留痕的含义就被改写。
    - 时间过滤参数名取自契约的 `from` / `to`，按**日期**比较（`occurred_at` 是
      `YYYY-MM-DD HH:MM:SS`，取前 10 位比较，`to` 含当天）。
    """
    if date_from is not None:
        date_from = parse_business_date(date_from, field="from")
    if date_to is not None:
        date_to = parse_business_date(date_to, field="to")
    if limit is None:
        limit = DEFAULT_LIMIT
    elif isinstance(limit, str):
        # 查询串里的 `limit=abc` 若被静默当成默认值，就是一次"安静的谎报"（调用方以为分页生效了）
        try:
            limit = int(limit)
        except ValueError as exc:
            raise TradeError("MT-1008", "`limit` 必须是整数", {"field": "limit"}) from exc
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise TradeError("MT-1008", f"`limit` 必须是 1~{MAX_LIMIT} 的整数", {"field": "limit"})
    if offset is None:
        offset = 0
    elif isinstance(offset, str):
        try:
            offset = int(offset)
        except ValueError as exc:
            raise TradeError("MT-1008", "`offset` 必须是整数", {"field": "offset"}) from exc
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise TradeError("MT-1008", "`offset` 必须是 ≥0 的整数", {"field": "offset"})

    where: list[str] = []
    params: list = []
    if stall_no is not None:
        where.append("EXISTS (SELECT 1 FROM stall s WHERE s.id = l.stall_id AND s.stall_no = ?)")
        params.append(stall_no)
    if event_type is not None:
        where.append("l.event_type = ?")
        params.append(event_type)
    if date_from is not None:
        where.append("substr(l.occurred_at, 1, 10) >= ?")
        params.append(date_from)
    if date_to is not None:
        where.append("substr(l.occurred_at, 1, 10) <= ?")
        params.append(date_to)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    total = int(conn.execute(f"SELECT COUNT(*) AS n FROM audit_log l {clause}", tuple(params)).fetchone()["n"])
    rows = conn.execute(
        f"""
        SELECT {AUDIT_FIELDS} FROM audit_log l
        {clause}
        ORDER BY l.occurred_at DESC, l.id DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    return {"total": total, "items": [dict(row) for row in rows]}
