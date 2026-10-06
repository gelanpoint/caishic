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
from collections.abc import Callable
from pathlib import Path

from . import clock, config

# 迁移脚本命名：`NNNN_描述.sql`（按文件名升序执行）
_MIGRATION_NAME_RE = re.compile(r"^\d{4}_.+\.sql$")

# `ALTER TABLE ... ADD COLUMN`：SQLite **没有** `ADD COLUMN IF NOT EXISTS`（见 `apply_migration`）。
# `COLUMN` 关键字**可省略**（SQLite 允许 `ALTER TABLE t ADD a INTEGER`），故写成可选；
# 末尾捕获"类型 + 约束"的余下部分，用于**跳过时比对列类型**（避免"以为改了其实没改"）。
_ALTER_ADD_COLUMN_RE = re.compile(
    r'^\s*ALTER\s+TABLE\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\s+'
    r'ADD\s+(?:COLUMN\s+)?(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\s*(.*)$',
    re.IGNORECASE | re.DOTALL,
)

#: 类型位之后若紧跟这些词，说明**没有声明类型**（是约束子句）——`ADD COLUMN a NOT NULL` 等
_NOT_A_TYPE = frozenset(
    {
        "PRIMARY", "NOT", "NULL", "UNIQUE", "CHECK", "DEFAULT",
        "REFERENCES", "COLLATE", "GENERATED", "AS", "CONSTRAINT",
    }
)


class MigrationError(RuntimeError):
    """迁移脚本无法安全执行（与"SQL 语法错"区分：这是**本执行器**主动拒绝）。"""


def now_iso() -> str:
    """本机本地时间（或注入时钟）的 ISO-8601 文本（`data-model.md` §0 时间字段约定）。

    `T-SIM-00`（`REQ-033` / `AC-024`）之后本函数只是 `app/clock.py` 的**转发**：
    **时间戳的来源只有一个落点**（否则"现在是几点"会有 N 个出处）。全项目的时间戳都经这里取，
    因此注入时钟能一次覆盖全部时间戳——包括审计留痕：**注入模式下审计时间戳 = 仿真时钟，不是墙钟**
    （语义声明见 `spec.md` §5「边界与例外」）。
    """
    return clock.now_iso()


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


def split_statements(script: str) -> list[str]:
    """把迁移脚本切成**单条** SQL 语句。

    为什么不用 `script.split(";")`：`0001_init.sql` 的两个触发器体内含分号
    （`BEGIN ... RAISE(ABORT, ...); END;`），裸切会把触发器切成半截。
    `sqlite3.complete_statement` 是 SQLite 自己的完整性判据（认识触发器与注释），故用它。
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    # 收尾：文件末尾允许只有空行与 `--` 注释（它们不构成语句）
    if any(text.strip() and not text.strip().startswith("--") for text in buffer.splitlines()):
        statements.append(buffer.strip())
    return statements


def _code_of(statement: str) -> str:
    """去掉注释后的语句正文（用于识别 `ALTER TABLE`，注释不影响执行）。

    **两种注释都要剥**：只剥整行 `--` 会让 `/* c */ ALTER TABLE …` 失配 ⇒ 列已存在时不再跳过
    ⇒ 重跑撞 `duplicate column name`（`task-11` 要求 H）。
    """
    text = re.sub(r"/\*.*?\*/", " ", statement, flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))


def _normalize_type(text: str) -> str:
    """声明类型的**对称**归一化：去空白 + 大写。

    两侧必须用**同一个**函数归一化，否则就是"拿苹果比橘子"：`PRAGMA table_info` 回的是
    完整声明（`VARCHAR(20)` / `DOUBLE PRECISION`），而只取首词会把它们读成 `VARCHAR(20`
    / `DOUBLE` —— 于是**合法类型反而误报"类型不同"**，把"重跑撞 duplicate column name"
    换成"重跑撞 MigrationError"，可用性后果一样（`task-12` 的 verify 实测出这一点）。
    """
    return re.sub(r"\s+", "", text).upper()


def _declared_type(remainder: str) -> str | None:
    """从 `ADD [COLUMN] <列> <余下>` 的余下部分取**完整声明类型**；未声明类型时返回 `None`。

    类型**可能是多个词**（`DOUBLE PRECISION`、`UNSIGNED BIG INT`），故不能只取首词：
    从前往后取到**第一个约束关键字**为止。若第一个词就是约束关键字（如 `ADD COLUMN a NOT NULL`），
    说明作者没写类型 —— 返回 `None`，**不做类型比对**（避免误报）。

    ⚠️ 语句结尾的分号必须先剥：`split_statements` 保留原文，`ADD COLUMN a TEXT;` 不剥会带上 `;`。
    """
    tokens = remainder.strip().rstrip(";").split()
    kept: list[str] = []
    for token in tokens:
        if token.upper().strip("(,") in _NOT_A_TYPE:
            break
        kept.append(token)
    if not kept:
        return None
    return _normalize_type(" ".join(kept))


def _column_type(conn: sqlite3.Connection, table: str, column: str) -> str | None:
    """既有列的声明类型（大写）；列不存在时返回 `None`。

    "列是否存在"与"列的声明类型"是同一个查询的两个用途，故合并为一个函数 ——
    分开写两份 `PRAGMA table_info` 就是下次漂移的种子。
    """
    for row in conn.execute(f'PRAGMA table_info("{table}")'):
        if row[1] == column:
            return (row[2] or "").upper()
    return None


def apply_migration(
    conn: sqlite3.Connection,
    script: str,
    *,
    on_before_commit: Callable[[], None] | None = None,
) -> list[str]:
    """**原子地**执行一个迁移脚本；返回**被跳过**的 `ALTER TABLE ADD COLUMN` 清单（`表.列`）。

    ## 为什么必须显式开事务（`task-11` 高危缺陷的修复）

    `connect()` 用的是 Python `sqlite3` 的 legacy 事务模式（`isolation_level=""`），该模式
    **只对 DML 自动开启事务，DDL 不会**。而迁移脚本普遍以 DDL 开头（`DROP TRIGGER` /
    `CREATE TABLE`），于是那些语句**各自立即提交** —— 一旦脚本后半段失败，回滚只能回到第一条
    DML，**前面已提交的 DDL 回不来**。

    实测后果（`0004_audit_event_types.sql` 重建 `audit_log`）：中途失败会**永久摘掉
    `audit_log_no_update` / `audit_log_no_delete` 两个触发器**，此后任何直连 sqlite3 都能
    UPDATE / DELETE 资金链路留痕 —— **`NFR-009` / 宪法 §4 的"只增不改"闸门失效，且再没有
    任何一次启动会恢复它**。故这里用**显式** `BEGIN IMMEDIATE` 包住整个脚本，失败即 `ROLLBACK`，
    把库还原到脚本开始前的完全一致状态。

    ## 为什么要在事务外切换 `PRAGMA foreign_keys`

    SQLite 明文规定：**`PRAGMA foreign_keys` 在事务内是 no-op**（只能在无 `BEGIN`/`SAVEPOINT`
    时切换）。而"重建表"型迁移（`DROP TABLE` + `CREATE TABLE` + `INSERT … SELECT` + `RENAME`）
    在搬移阶段会**重新校验源行的外键**，历史库里合法的悬挂引用（外键关闭时写入的留痕）
    会撞 `FOREIGN KEY constraint failed`。故顺序固定为：

        PRAGMA foreign_keys=OFF  →  BEGIN IMMEDIATE  →  执行脚本  →  COMMIT  →  PRAGMA foreign_keys=ON

    ⚠️ **注意这是"迁移期间不校验"，不是"放宽约束"**：表定义里的 `REFERENCES` 一律原样保留；
    代价是**迁移脚本作者对搬移数据的引用完整性负责**（`0004` 搬移的是既有留痕，必须一行不丢）。

    ## 参数

    `on_before_commit`：在 `COMMIT` **之前**、同一事务内调用的回调 —— `init_database` 用它把
    `schema_migration` 记账行与脚本效果**一起提交**（否则"脚本成功但记账失败"会导致重跑，
    而重建表型脚本并不天然幂等）。

    ## 为什么跳过"列已存在"的 `ADD COLUMN`

    `ALTER TABLE ... ADD COLUMN` 没有 `IF NOT EXISTS`；不跳过的话，脚本半途失败后重跑会撞
    `duplicate column name`，应用再也起不来。跳过时**比对既有列的类型**：不一致就报
    `MigrationError`，**绝不静默跳过** —— 否则迁移作者会以为改了列、其实没改。
    """
    skipped: list[str] = []
    fk_was_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if fk_was_on:
        conn.execute("PRAGMA foreign_keys = OFF")  # 必须在 BEGIN 之前
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in split_statements(script):
                match = _ALTER_ADD_COLUMN_RE.match(_code_of(statement))
                if match is not None:
                    table = match.group(1) or match.group(2)
                    column = match.group(3) or match.group(4)
                    existing = _column_type(conn, table, column)
                    if existing is not None:
                        declared = _declared_type(match.group(5) or "")
                        if declared is not None and declared != _normalize_type(existing):
                            raise MigrationError(
                                f"{table}.{column} 已存在且类型不同（既有 {existing}，脚本声明 "
                                f"{declared}）—— SQLite 不支持改列类型，请另写重建表型迁移；"
                                f"本执行器拒绝静默跳过"
                            )
                        skipped.append(f"{table}.{column}")
                        continue
                conn.execute(statement)
            if on_before_commit is not None:
                on_before_commit()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        if fk_was_on and not conn.in_transaction:
            conn.execute("PRAGMA foreign_keys = ON")
    return skipped


def init_database(db_path: Path | str | None = None) -> list[str]:
    """按序补齐全部迁移；返回**本次实际执行**的脚本文件名列表（已执行过的跳过）。

    幂等性说明（`2026-10-06` 修正）：**每个脚本在自己的显式事务内执行**
    （`apply_migration` 的 `BEGIN IMMEDIATE`），`schema_migration` 的记账行与该脚本效果
    **同事务提交**。因此：

    - 某个脚本执行到一半失败（含进程被杀）⇒ 该脚本的改动**整体回滚**，库回到脚本开始前的
      状态，**红线触发器不会被摘掉**，下次启动重跑安全；
    - 脚本内一律 `IF NOT EXISTS` / `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER`，
      `ALTER TABLE ADD COLUMN` 由 `apply_migration` 按列存在性（并比对类型）跳过。

    ⚠️ **本节此前写着"不会留下半个触发器"——那是一句与实测不符的承诺**：修复前 `apply_migration`
    没有显式事务，脚本开头的 DDL 各自提交，中途失败确实会永久摘掉 `audit_log` 的两个
    "只增不改"触发器（`task-11` 已复现并修复）。现在那句话由显式事务兜住。
    """
    newly_applied: list[str] = []
    conn = connect(db_path)
    try:
        done = applied_migrations(conn)
        for script in discover_migrations():
            if script.name in done:
                continue

            def _record(name: str = script.name) -> None:
                conn.execute(
                    "INSERT INTO schema_migration (filename, applied_at) VALUES (?, ?)",
                    (name, now_iso()),
                )

            apply_migration(conn, script.read_text(encoding="utf-8"), on_before_commit=_record)
            newly_applied.append(script.name)
    finally:
        conn.close()
    return newly_applied
