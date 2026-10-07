-- =====================================================================
-- 0004_audit_event_types.sql —— 扩展 `audit_log.event_type` 白名单（T-SCALE-02b）
--
-- 为什么要重建表：`0001_init.sql` 里 `audit_log.event_type` 是 **9 值 CHECK 白名单**，
-- 而契约 `contracts/scale-midplatform.md` §5.4 要求金额不一致时写一条
-- `event_type = scale_amount_mismatch` 的留痕（`AC-030` 的三件事之一）。
-- **SQLite 改不了 CHECK 约束**，只能"建新表 → 搬数据 → 换名"。
--
-- 顺序是硬要求（错一步要么搬不动数据，要么把红线触发器弄丢）：
--   1) 先 DROP 两个触发器 —— 否则 `audit_log_no_update` / `audit_log_no_delete`
--      会挡住搬移与建表收尾（`NFR-009`：资金链路审计只增不改，由触发器强制）；
--   2) 建 `audit_log_new`（除 `event_type` 多一个值外，**其余列定义与约束逐字段照抄 0001**）；
--   3) `INSERT ... SELECT` 逐行搬移（含主键 `id`，一行不少）；
--   4) `DROP TABLE audit_log` → `ALTER TABLE audit_log_new RENAME TO audit_log`；
--   5) **重建两个触发器**（`BEFORE UPDATE` / `BEFORE DELETE`，原样 `RAISE(ABORT, ...)`）。
--
-- 两条容易漏掉、本文件显式补上的点：
--   * **两条索引必须重建**：`DROP TABLE audit_log` 会连带删掉 `ix_audit_ref` /
--     `ix_audit_stall_time`（`data-model.md` §6 登记的留痕回查索引）。它们只能在换名**之后**
--     建 —— 换名前建会同名撞车（索引名全库唯一），换名后不建就是静默丢索引。
--   * **不得放宽任何既有约束**：`actor` 的 `length(actor) <= 32`、`ref_id NOT NULL`、
--     `payload_json NOT NULL`、`occurred_at` 的默认值全部原样保留；`ref_table` 白名单不动
--     （`transaction` 已在其中）。
--
-- 原子性：`app/db.py::apply_migration` 用**显式** `BEGIN IMMEDIATE` 包住整个脚本，并在事务外
-- 切换 `PRAGMA foreign_keys`；失败即 `ROLLBACK` —— 故半途失败**整体回滚**，不会留下"旧表已删、
-- 新表未换名"或"红线触发器被摘掉"的中间态（`task-11` 修复；修复前 DDL 各自立即提交，
-- 中途失败确实会永久摘掉那两个触发器）。第 1b 步的 `DROP TABLE IF EXISTS audit_log_new`
-- 是给**旧代码留下的半迁移库**自愈用的，不是用来吞掉资金链路数据的幂等兜底。
-- =====================================================================

-- 1) 先摘掉红线触发器（搬完数据后原样重建）。
--    ⚠️ 这三条 DDL 之所以安全，是因为 `app/db.py::apply_migration` 用**显式**
--    `BEGIN IMMEDIATE` 包住了整个脚本（`task-11` 修复）。修复前它们会各自立即提交，
--    脚本后半段一失败就**永久摘掉**这两个"只增不改"触发器（`NFR-009` / 宪法 §4）。
DROP TRIGGER IF EXISTS audit_log_no_update;
DROP TRIGGER IF EXISTS audit_log_no_delete;

-- 1b) 自愈：若上一次执行在旧代码下失败，可能残留空的 `audit_log_new`。
--     先清掉它，让**已经被弄坏的半迁移库**能重新走完（否则下面的建表会撞"表已存在"）。
DROP TABLE IF EXISTS audit_log_new;

-- 2) 新表：`event_type` = 原 9 值 + `scale_amount_mismatch`，其余逐字段照抄 0001
CREATE TABLE IF NOT EXISTS audit_log_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type    TEXT NOT NULL CHECK (event_type IN (
                      'price_change', 'refund_applied', 'refund_duplicate_hit',
                      'offline_backfilled', 'offline_duplicate_discarded',
                      'payment_callback_duplicate_hit', 'staging_write_failed',
                      'offline_threshold_warned', 'commission_rule_changed',
                      'scale_amount_mismatch')),
    stall_id      INTEGER REFERENCES stall(id),
    ref_table     TEXT NOT NULL CHECK (ref_table IN (
                      'transaction', 'transaction_item', 'payment', 'refund',
                      'offline_queue', 'commission_rule')),
    ref_id        INTEGER NOT NULL,
    payload_json  TEXT NOT NULL,
    actor         TEXT NOT NULL CHECK (length(actor) <= 32),
    occurred_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 3) 逐行搬移（显式列名 + 显式主键，保证"一行不少、内容不改写"）
INSERT INTO audit_log_new (id, event_type, stall_id, ref_table, ref_id, payload_json, actor, occurred_at)
SELECT id, event_type, stall_id, ref_table, ref_id, payload_json, actor, occurred_at
FROM audit_log;

-- 4) 换名
DROP TABLE audit_log;
ALTER TABLE audit_log_new RENAME TO audit_log;

-- 5) 原样重建两个触发器（`NFR-009` / 宪法 §4：只增不改）
CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log 只增不改：禁止 UPDATE（NFR-009 / 宪法 §4）');
END;

CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log 只增不改：禁止 DELETE（NFR-009 / 宪法 §4）');
END;

-- 6) 重建留痕回查索引（随旧表一起被 DROP 掉，见文件头说明）
CREATE INDEX IF NOT EXISTS ix_audit_ref        ON audit_log (ref_table, ref_id);
CREATE INDEX IF NOT EXISTS ix_audit_stall_time ON audit_log (stall_id, occurred_at);
