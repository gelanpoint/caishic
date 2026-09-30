"""健康检查端点：契约 §3.1 `GET /healthz`（`NFR-010`）。

职责只有一件：**回答"这个实例现在能不能干活"**（存活 + 就绪）。
本模块**不承载业务逻辑**，也不做鉴权（契约 §3.1 原文："无需认证"）。

两条容易写歪的地方，先写在前面：

1. **不可用时不返回错误码**：契约 §3.1 原文是"可能错误: 无（不可用时直接返回 503，无错误码）"。
   故这里**不构造 `{"error": {...}}` 信封** —— 统一错误信封的每个码都必须出自契约 §4，
   而 §4 里没有"健康检查失败"这个码；硬塞一个进来就是在契约之外发明错误码（违反 `RL-1`）。
2. **就绪 = 库真的能查**：`db` 字段不是"文件存在"这种表面判断，而是**实际执行一次查询**。
   迁移没跑（表不存在）同样算未就绪 —— 那正是"起不来"的最常见形态，若不在此暴露，
   调用方会拿到 200 之后在第一个业务请求上撞 500。

> `offline_queue_pending` 的口径与契约 §3.13 的 `pending_count` **同源**（都数 `status = 'staged'` 的行）：
> 同一个数在两个端点上给出不同值，秤端与运维端就会看到两套"待补传"事实。
"""

from __future__ import annotations

import sqlite3

from flask import Blueprint, jsonify

from .. import current_db

bp = Blueprint("health", __name__)


def _staged_count(conn: sqlite3.Connection) -> int:
    """待补传条数（`offline_queue.status = 'staged'`，口径同契约 §3.13）。"""
    row = conn.execute("SELECT COUNT(*) AS n FROM offline_queue WHERE status = 'staged'").fetchone()
    return int(row["n"])


@bp.get("/healthz")
def healthz():
    """存活与就绪检查（契约 §3.1）：正常 200，库不可用 503（**无错误码**）。"""
    try:
        pending = _staged_count(current_db())
    except (sqlite3.Error, OSError):
        # 库打不开 / 迁移未跑（表不存在）等一律按"未就绪"处理。
        # 保持与 200 相同的键集合（值退化为 null），让调用方不必为两条分支写两套解析；
        # 且**不返回 error 信封**（契约 §3.1 明确"无错误码"）。
        return (
            jsonify({"status": "unavailable", "db": "error", "offline_queue_pending": None}),
            503,
        )
    return jsonify({"status": "ok", "db": "ok", "offline_queue_pending": pending}), 200
