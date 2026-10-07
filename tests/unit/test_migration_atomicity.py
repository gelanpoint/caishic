"""`task-12` **独立回归测试**：迁移原子性 + 外键悬挂 + 跳过逻辑盲区。

## 为什么单开一个文件（而不是并进实现者的提交）

`RL-4`「作者不得自审」+ `AGENTS.md`「验证要能失败」：**修缺陷的人不该同时写"证明缺陷已修好"的那条测试**，
否则"这条测试能失败"就没人独立保证了。实现由 Lead 在 `task-11` 完成，本文件由 verify 写。

⚠️ **独立性声明（如实）**：本测试独立于**实现者**，但本文件作者此前**参与发现了** F1（迁移非原子 ⇒
中途失败摘掉红线触发器）与 F2（跳过逻辑盲区），故对"**缺陷曾经存在**"这一点**不构成全新眼睛**的复核。

## 家族（照 `tests/contract/test_check_sensitivity.py` 的组织方式，**每组自带灵敏度负例**）

| 家族 | 断言什么 |
| --- | --- |
| 1 / 1b | 半途失败 ⇒ 库**原样**（触发器在 / `UPDATE` 被拒 / 无 `audit_log_new` / 行数不变 / 无记账行）；**记账失败 ⇒ 脚本效果同样整体回滚** |
| 2 | 悬挂 `stall_id` ⇒ 迁移**成功**且留痕**一行不丢**（语义见下） |
| 3 / 3b | 跳过逻辑：省略 `COLUMN`、块注释前缀、参数化/多词类型**不得误报**、真差异必须报错；+ 正对照（真新列要加得上） |
| 4 | **灵敏度负例**：去掉事务包裹 ⇒ 家族 1 的五条断言**必红**；还原复绿（两次输出都打印） |
| 5 | 旧代码留下的**半迁移库自愈** + 全新库跑完 4 个迁移且重跑为 `[]` |

**家族 2 的语义（父代理裁定，别写反）**：悬挂引用（外键关闭时写入的留痕）是**合法的历史数据**，
迁移期间关闭外键校验后必然能通过；故正确语义是"**成功且一行不丢**"，**不是**"应当报错中止"
（后者只会无谓地让应用起不来；本文件作者最初的建议已被否决，此处**不得**按旧建议断言）。
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # 与 `tests/unit/clock_support.py` 同一垫片，便于从任意目录跑
    sys.path.insert(0, str(REPO_ROOT))

from app import db  # noqa: E402  (上面的 sys.path 垫片必须先行)

MIGRATIONS_DIR = REPO_ROOT / "app" / "migrations"
#: 真实的 `0004` 原文 —— **不改 `app/**` 里的脚本本体**，半途失败靠"文本尾部追加非法 SQL"注入
SCRIPT_0004 = (MIGRATIONS_DIR / "0004_audit_event_types.sql").read_text(encoding="utf-8")

#: 迁移集**从目录推导**，不写死 —— 写死的话，每加一个迁移（如 `0005_demo_console.sql`）
#: 都会让"原子性/自愈"这些判据假红，而它们其实与迁移条数无关。判据是"目录里的都按序跑完"。
LEGACY_NAMES = ["0001_init.sql", "0002_market_scope.sql", "0003_device.sql"]


def _migration_names() -> list[str]:
    return sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))


def _pending_after_legacy() -> list[str]:
    """旧库（建到 `0003`）还欠的那些迁移，按序。"""
    return [name for name in _migration_names() if name not in LEGACY_NAMES]
#: 注入的非法语句（真 SQL 解析错误，不是 Mock 出来的异常）
BROKEN_TAIL = "\nTHIS IS NOT VALID SQL;\n"
BROKEN_SCRIPT_NAME = "9999_broken.sql"

RED_LINE_TRIGGERS = frozenset({"audit_log_no_update", "audit_log_no_delete"})


# ---------------------------------------------------------------------------
# 库状态读取助手（断言尽量落在**库里的状态**上，而不是函数返回值）
# ---------------------------------------------------------------------------


def _record(conn: sqlite3.Connection, name: str) -> None:
    """`schema_migration` 记账行（与 `init_database` 的 `on_before_commit` 同形）。"""
    conn.execute(
        "INSERT INTO schema_migration (filename, applied_at) VALUES (?, datetime('now'))", (name,)
    )


def _build_legacy_db(path: Path) -> sqlite3.Connection:
    """把库建到 `0003` 并写入两条留痕 —— 模拟"待升级的既有库"。"""
    conn = db.connect(path)
    for name in LEGACY_NAMES:
        text = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        db.apply_migration(conn, text, on_before_commit=lambda n=name: _record(conn, n))
    for event, table in (("price_change", "transaction"), ("refund_applied", "refund")):
        conn.execute(
            "INSERT INTO audit_log (event_type, ref_table, ref_id, payload_json, actor)"
            " VALUES (?, ?, 1, '{\"k\": \"v\"}', 't')",
            (event, table),
        )
    conn.commit()
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _triggers(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}


def _audit_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"])


def _recorded(conn: sqlite3.Connection) -> set[str]:
    return {r["filename"] for r in conn.execute("SELECT filename FROM schema_migration")}


def _update_is_rejected(conn: sqlite3.Connection) -> tuple[bool, str]:
    """试着 UPDATE 一条留痕：被红线触发器拒 = `(True, 错误文本)`；成功 = `(False, ...)`（并回滚）。"""
    try:
        conn.execute("UPDATE audit_log SET actor = 'x' WHERE id = (SELECT MIN(id) FROM audit_log)")
        conn.rollback()
        return False, "UPDATE 竟然成功了（未被触发器拒绝）"
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        return True, str(exc)


# ---------------------------------------------------------------------------
# 家族 1：半途失败 ⇒ 库原样（判据抽成函数，家族 4 的灵敏度负例要对它断言"必红"）
# ---------------------------------------------------------------------------


def _assert_db_untouched_after_failure(
    conn: sqlite3.Connection, *, rows_before: int, script_name: str
) -> None:
    """家族 1 的五条判据。**任何一条不成立都抛 `AssertionError`**（家族 4 依赖这一点）。"""
    missing = RED_LINE_TRIGGERS - _triggers(conn)
    assert not missing, f"① 红线触发器被摘掉了：{sorted(missing)}（NFR-009 / 宪法 §4）"

    rejected, message = _update_is_rejected(conn)
    assert rejected, f"② `UPDATE audit_log` 未被拒绝 —— 只增不改闸门失效：{message}"
    assert "只增不改" in message, f"② 拒绝原因不是红线触发器，而是：{message!r}"

    assert "audit_log_new" not in _tables(conn), "③ 半迁移残留 `audit_log_new` 还在"
    assert _audit_count(conn) == rows_before, (
        f"④ 留痕行数变了：失败前 {rows_before}，现在 {_audit_count(conn)}"
    )
    assert script_name not in _recorded(conn), f"⑤ 失败脚本不得留下记账行：{script_name}"


def test_mid_script_failure_leaves_the_database_untouched(tmp_path):
    """家族 1：脚本尾部注入**真实** SQL 语法错 ⇒ 库回到脚本开始前的完全一致状态。"""
    conn = _build_legacy_db(tmp_path / "midfail.sqlite3")
    rows_before = _audit_count(conn)

    with pytest.raises(sqlite3.OperationalError):
        db.apply_migration(
            conn, SCRIPT_0004 + BROKEN_TAIL,
            on_before_commit=lambda: _record(conn, BROKEN_SCRIPT_NAME),
        )

    _assert_db_untouched_after_failure(conn, rows_before=rows_before, script_name=BROKEN_SCRIPT_NAME)
    conn.close()


# ---------------------------------------------------------------------------
# 家族 2：悬挂 `stall_id` ⇒ 迁移成功且一行不丢（走**真实** `init_database` 路径）
# ---------------------------------------------------------------------------


def test_dangling_stall_id_migration_succeeds_and_keeps_every_row(tmp_path):
    """家族 2：旧库含悬挂 `stall_id` 的留痕 ⇒ `0004` **成功**、留痕逐字段原样、触发器在、新值可写。"""
    path = tmp_path / "dangling.sqlite3"
    conn = _build_legacy_db(path)
    # 关外键写一条指向不存在摊位的留痕 —— 这正是"直连 sqlite3 改库"留下的**合法历史数据**
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO audit_log (event_type, stall_id, ref_table, ref_id, payload_json, actor, occurred_at)"
        " VALUES ('price_change', 999, 'transaction', 7, '{\"note\": \"悬挂\"}', 'legacy', '2026-01-02 03:04:05')"
    )
    conn.commit()
    columns = "id, event_type, stall_id, ref_table, ref_id, payload_json, actor, occurred_at"
    before = [tuple(r) for r in conn.execute(f"SELECT {columns} FROM audit_log ORDER BY id")]
    conn.close()

    applied = db.init_database(path)  # ← 真实启动路径
    assert applied == _pending_after_legacy(), f"应只补跑旧库欠的那些迁移，实际 {applied}"

    conn = db.connect(path)
    after = [tuple(r) for r in conn.execute(f"SELECT {columns} FROM audit_log ORDER BY id")]
    assert after == before, f"留痕必须一行不丢、逐字段原样：\n before={before}\n after ={after}"
    assert RED_LINE_TRIGGERS <= _triggers(conn), "重建后红线触发器必须在"
    # 约束不得放宽：`REFERENCES stall(id)` 必须仍在表定义里（迁移期间只是**不校验**，不是删约束）
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'audit_log'").fetchone()["sql"]
    assert "REFERENCES stall(id)" in ddl, f"重建后丢了外键声明（要求 G）：{ddl}"
    conn.execute(
        "INSERT INTO audit_log (event_type, ref_table, ref_id, payload_json, actor)"
        " VALUES ('scale_amount_mismatch', 'transaction', 1, '{}', 't')"
    )
    conn.commit()
    assert _audit_count(conn) == len(before) + 1, "迁移后新值 `scale_amount_mismatch` 必须可写"
    conn.close()


# ---------------------------------------------------------------------------
# 家族 3：跳过逻辑（`task-11` 要求 H）—— 三个盲区 + 正对照
# ---------------------------------------------------------------------------


def _fresh_t(tmp_path, name: str) -> sqlite3.Connection:
    conn = db.connect(tmp_path / name)
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.commit()
    return conn


def test_add_column_without_column_keyword_is_skipped_not_duplicated(tmp_path):
    """盲区一：`ALTER TABLE t ADD a TEXT`（**省略 `COLUMN`**，SQLite 合法）⇒ 正常跳过，不得撞重名。"""
    conn = _fresh_t(tmp_path, "skip_no_column.sqlite3")
    assert db.apply_migration(conn, "ALTER TABLE t ADD a TEXT;") == ["t.a"]
    conn.close()


def test_add_column_with_block_comment_prefix_is_skipped(tmp_path):
    """盲区二：`/* c */ ALTER TABLE …`（**块注释前缀**）⇒ 同样正常跳过。"""
    conn = _fresh_t(tmp_path, "skip_block_comment.sqlite3")
    assert db.apply_migration(conn, "/* c */ ALTER TABLE t ADD COLUMN a TEXT;") == ["t.a"]
    conn.close()


def test_add_column_type_mismatch_is_refused_not_silently_skipped(tmp_path):
    """盲区三：同列名但**类型不同** ⇒ 必须**报错**（`MigrationError`），不得静默跳过。"""
    conn = _fresh_t(tmp_path, "skip_type_mismatch.sqlite3")
    with pytest.raises(db.MigrationError):
        db.apply_migration(conn, "ALTER TABLE t ADD COLUMN a INTEGER;")
    types = {r[1]: r[2] for r in conn.execute('PRAGMA table_info("t")')}
    assert types == {"a": "TEXT"}, f"被拒后库不得被改动，实际 {types}"
    conn.close()


def test_add_column_without_declared_type_is_skipped(tmp_path):
    """未声明类型（`ADD COLUMN a NOT NULL`）⇒ **不做类型比对**、正常跳过（避免误报）。"""
    conn = _fresh_t(tmp_path, "skip_no_type.sqlite3")
    assert db.apply_migration(conn, "ALTER TABLE t ADD COLUMN a NOT NULL;") == ["t.a"]
    conn.close()


def _slug(text: str) -> str:
    return re.sub(r"\W+", "_", text)


@pytest.mark.parametrize(
    "declared_type", ["VARCHAR(20)", "DECIMAL(10,2)", "DOUBLE PRECISION", "UNSIGNED BIG INT"]
)
def test_parameterized_and_multiword_types_are_not_false_positives(tmp_path, declared_type):
    """家族 3b（F3 回归）：类型**未变** ⇒ 正常跳过。首版只取首词、且 `rstrip(";,()")` 吃掉右括号
    （`VARCHAR(20)`→`VARCHAR(20`）⇒ 合法类型被误报"类型不同"（后果同"重跑撞重名"：应用起不来）。"""
    conn = db.connect(tmp_path / f"same_{_slug(declared_type)}.sqlite3")
    conn.execute(f"CREATE TABLE t (a {declared_type})")
    conn.commit()
    assert db.apply_migration(conn, f"ALTER TABLE t ADD COLUMN a {declared_type};") == ["t.a"]
    conn.close()


@pytest.mark.parametrize(
    ("existing", "declared"),
    [("VARCHAR(20)", "VARCHAR(30)"), ("DOUBLE PRECISION", "TEXT"), ("UNSIGNED BIG INT", "INTEGER")],
)
def test_real_type_changes_are_still_refused_after_normalization(tmp_path, existing, declared):
    """家族 3b 的**对照臂**：归一化不得把类型检查变成"永远跳过" —— 真差异仍须报错且库不改动。"""
    conn = db.connect(tmp_path / f"diff_{_slug(existing)}.sqlite3")
    conn.execute(f"CREATE TABLE t (a {existing})")
    conn.commit()
    with pytest.raises(db.MigrationError):
        db.apply_migration(conn, f"ALTER TABLE t ADD COLUMN a {declared};")
    assert {r[1]: r[2] for r in conn.execute('PRAGMA table_info("t")')} == {"a": existing}
    conn.close()


def test_genuinely_new_column_is_added(tmp_path):
    """**正对照**：跳过逻辑不是"永远跳过"—— 真新列必须真的加上去。"""
    conn = _fresh_t(tmp_path, "new_column.sqlite3")
    assert db.apply_migration(conn, "ALTER TABLE t ADD COLUMN b INTEGER;") == []
    assert {r[1]: r[2] for r in conn.execute('PRAGMA table_info("t")')} == {"a": "TEXT", "b": "INTEGER"}
    conn.close()


# ---------------------------------------------------------------------------
# 家族 4：灵敏度负例 —— 去掉事务包裹 ⇒ 家族 1 的断言必红
# ---------------------------------------------------------------------------


def _legacy_apply_migration(conn: sqlite3.Connection, script: str, *, on_before_commit=None) -> None:
    """**修复前**的执行器（逐条 `execute`、无显式事务、不切外键）—— 只用于灵敏度负例。

    刻意照抄旧行为：这样负例证明的是"**正是那层事务包裹**在起作用"。
    """
    for statement in db.split_statements(script):
        conn.execute(statement)
    if on_before_commit is not None:
        on_before_commit()


def test_sensitivity_without_the_transaction_the_same_assertions_go_red(tmp_path):
    """灵敏度：**同一组断言**在"有事务"时全绿、在"去掉事务包裹"时必红；再还原复绿。"""
    broken = SCRIPT_0004 + BROKEN_TAIL

    # (a) 绿：修复后的执行器（有显式事务）
    conn = _build_legacy_db(tmp_path / "sens_green.sqlite3")
    rows_before = _audit_count(conn)
    with pytest.raises(sqlite3.OperationalError):
        db.apply_migration(
            conn, broken, on_before_commit=lambda: _record(conn, BROKEN_SCRIPT_NAME)
        )
    _assert_db_untouched_after_failure(conn, rows_before=rows_before, script_name=BROKEN_SCRIPT_NAME)
    conn.close()
    print("[家族4] 有事务包裹：家族 1 的五条断言全部通过 ✓（库原样）")

    # (b) 红：去掉事务包裹（旧执行器）
    conn = _build_legacy_db(tmp_path / "sens_red.sqlite3")
    rows_before = _audit_count(conn)
    with pytest.raises(sqlite3.OperationalError):
        _legacy_apply_migration(
            conn, broken, on_before_commit=lambda: _record(conn, BROKEN_SCRIPT_NAME)
        )
    # 旧 `init_database` 在 `finally` 里直接 close（未提交即回滚）—— 这里显式回滚以还原那一刻的状态
    conn.rollback()
    with pytest.raises(AssertionError) as excinfo:
        _assert_db_untouched_after_failure(conn, rows_before=rows_before, script_name=BROKEN_SCRIPT_NAME)
    assert "触发器" in str(excinfo.value), f"红的应当是红线触发器那条，实际：{excinfo.value}"
    print(f"[家族4] 去掉事务包裹：家族 1 的断言变红 ✓ → {str(excinfo.value).splitlines()[0]}")
    conn.close()

    # (c) 还原复绿：换回修复后的执行器，同一组断言再次通过
    conn = _build_legacy_db(tmp_path / "sens_green_again.sqlite3")
    rows_before = _audit_count(conn)
    with pytest.raises(sqlite3.OperationalError):
        db.apply_migration(
            conn, broken, on_before_commit=lambda: _record(conn, BROKEN_SCRIPT_NAME)
        )
    _assert_db_untouched_after_failure(conn, rows_before=rows_before, script_name=BROKEN_SCRIPT_NAME)
    conn.close()
    print("[家族4] 还原（有事务包裹）：同一组断言复绿 ✓")

# ---------------------------------------------------------------------------
# 家族 5：`0004` 的自愈与全新库路径（独立核对 `task-11` 的另两条声明）
# ---------------------------------------------------------------------------


def test_half_migrated_database_left_by_the_old_code_self_heals(tmp_path):
    """家族 5：**旧代码留下的半迁移库**（触发器已摘、残留空 `audit_log_new`）能被 `0004` 修回来。

    真造那个状态（与 `task-9` 评审里实测到的一模一样）：手工执行 `0004` 到"建新表"那条为止后直接
    close（旧代码下这些 DDL 各自立即提交），再用**真实** `init_database()` 升级。
    """
    path = tmp_path / "half_migrated.sqlite3"
    conn = _build_legacy_db(path)
    # **按内容定位**切点，不按下标硬编码：`0004` 的语句顺序已被改过一次（中间插入了
    # `DROP TABLE IF EXISTS audit_log_new`），硬编码 `[:3]` 会切在错误位置 —— 本用例的前置断言抓到了这点。
    statements = db.split_statements(SCRIPT_0004)
    cut = 1 + max(
        index for index, text in enumerate(statements)
        if "CREATE TABLE IF NOT EXISTS audit_log_new" in text
    )
    for statement in statements[:cut]:
        conn.execute(statement)
    conn.close()  # 未提交即回滚（模拟进程被杀）
    conn = db.connect(path)
    assert RED_LINE_TRIGGERS - _triggers(conn) == RED_LINE_TRIGGERS, "前置失败：触发器应已被摘掉"
    assert "audit_log_new" in _tables(conn), "前置失败：应残留 audit_log_new"
    rows_before = _audit_count(conn)
    conn.close()

    assert db.init_database(path) == _pending_after_legacy()
    conn = db.connect(path)
    assert RED_LINE_TRIGGERS <= _triggers(conn), "自愈后红线触发器必须回来"
    assert "audit_log_new" not in _tables(conn), "自愈后不得残留 audit_log_new"
    assert _audit_count(conn) == rows_before, "自愈不得丢留痕"
    conn.close()


def test_fresh_database_applies_all_migrations_and_rerun_is_a_noop(tmp_path):
    """家族 5：全新库一次跑完**目录里的全部**迁移；再跑一次返回 `[]`（幂等）。"""
    path = tmp_path / "fresh.sqlite3"
    applied = db.init_database(path)
    assert applied == _migration_names(), applied
    assert db.init_database(path) == [], "重跑必须什么都不做"
    conn = db.connect(path)
    assert RED_LINE_TRIGGERS <= _triggers(conn)
    conn.close()

def test_recording_failure_rolls_back_the_script_too(tmp_path):
    """家族 1 的**另一半**（要求 B）：记账行插入失败 ⇒ 脚本效果也必须整体回滚。

    为什么必须有这一条：若"脚本已提交但记账没提交"，下次启动会**重跑**该脚本，而重建表型脚本
    并不天然幂等 —— 那正是 `task-11` 要根除的失败模式。判据落在**库的状态**上：回滚后
    `audit_log` 必须还是**旧表**（9 值白名单 ⇒ 新值 `scale_amount_mismatch` **写不进去**）。
    """
    conn = _build_legacy_db(tmp_path / "record_fail.sqlite3")
    rows_before = _audit_count(conn)

    def _boom() -> None:
        raise sqlite3.IntegrityError("记账失败（模拟 `schema_migration` 写入异常）")

    with pytest.raises(sqlite3.IntegrityError):
        db.apply_migration(conn, SCRIPT_0004, on_before_commit=_boom)

    assert RED_LINE_TRIGGERS <= _triggers(conn), "记账失败时红线触发器必须还在"
    assert "audit_log_new" not in _tables(conn), "记账失败时不得残留 audit_log_new"
    assert _audit_count(conn) == rows_before, "记账失败时留痕行数不得变"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO audit_log (event_type, ref_table, ref_id, payload_json, actor)"
            " VALUES ('scale_amount_mismatch', 'transaction', 1, '{}', 't')"
        )
    conn.rollback()
    print("[家族1b] 记账失败 ⇒ 脚本效果整体回滚（新值仍写不进去）✓")
    conn.close()
