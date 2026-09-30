"""SQLite 连接与迁移执行（`data-model.md` §7）。

约定（`specs/market-trade-flow/plan.md` §4）：
- 经 **Python 标准库 `sqlite3`** 访问，不引入第三方驱动（`ADR-0003`、宪法 §1）；
- 每个连接都设 `PRAGMA journal_mode = WAL`（并发读不阻塞写）、`foreign_keys = ON`（外键真的生效）、
  `busy_timeout`（写锁竞争时短暂等待，而不是立刻抛 `database is locked`）；
- **短事务**：写操作尽量在一个事务内完成（`ADR-0001` §3 的采纳结论）。

迁移策略（`data-model.md` §7 第 1 条）：启动时按**文件名顺序**执行 `app/migrations/*.sql`，
执行记录写入 `schema_migration`，**每次启动自动补齐、不重复执行**（`AC-013` 一条启动命令）。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

# 迁移脚本命名：`NNNN_描述.sql`（按文件名升序执行）
_MIGRATION_NAME_RE = re.compile(r"^\d{4}_.+\.sql$")


def now_iso() -> str:
    """本机本地时间的 ISO-8601 文本（`YYYY-MM-DD HH:MM:SS`，`data-model.md` §0 时间字段约定）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开数据库连接并设置本项目约定的 PRAGMA。

    父目录不存在时自动创建（首次启动、`data/` 尚未建立的情况）。
    """
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL 是**持久**设置（写进库文件），只读连接也会受益；foreign_keys / busy_timeout 是**每连接**设置，
    # 因此必须在每个连接上重新执行 —— 漏掉 foreign_keys 会让外键静默失效（最危险的默认值）。
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def discover_migrations(migrations_dir: Path | None = None) -> list[Path]:
    """按文件名升序返回全部迁移脚本。"""
    directory = Path(migrations_dir) if migrations_dir is not None else config.MIGRATIONS_DIR
    if not directory.is_dir():
        return []
    return sorted(
        (p for p in directory.iterdir() if p.is_file() and _MIGRATION_NAME_RE.match(p.name)),
        key=lambda p: p.name,
    )


def applied_migrations(conn: sqlite3.Connection) -> set[str]:
    """返回已执行过的迁移文件名集合。

    `schema_migration` 表尚不存在（全新库）时返回空集 —— 这样**不需要在别处重复定义该表**：
    它的 DDL 只写在 `0001_init.sql` 里，执行完该脚本后本函数即可正常读取。
    """
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migration'"
    ).fetchone()
    if exists is None:
        return set()
    return {row["filename"] for row in conn.execute("SELECT filename FROM schema_migration")}


def init_database(db_path: Path | str | None = None) -> list[str]:
    """按序补齐全部迁移；返回**本次实际执行**的脚本文件名列表（已执行过的跳过）。

    幂等性说明：迁移脚本内一律 `IF NOT EXISTS` / `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER`，
    因此**即使某个脚本执行到一半失败**（例如进程被杀），下次启动重跑也是安全的 ——
    不会重复建表、不会重复建索引、不会留下半个触发器。
    """
    newly_applied: list[str] = []
    conn = connect(db_path)
    try:
        done = applied_migrations(conn)
        for script in discover_migrations():
            if script.name in done:
                continue
            conn.executescript(script.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migration (filename, applied_at) VALUES (?, ?)",
                (script.name, now_iso()),
            )
            conn.commit()
            newly_applied.append(script.name)
    finally:
        conn.close()
    return newly_applied
