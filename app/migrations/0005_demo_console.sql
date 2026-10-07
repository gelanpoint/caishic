-- =====================================================================
-- 0005_demo_console.sql —— 演示控制台（`REQ-053`~`REQ-057`，`2026-10-08`）
--
-- 两件事：
--   1) 扩展 `audit_log.event_type` 白名单：追加 `transaction_confirmed` / `transaction_cancelled`
--      （`REQ-054` / `REQ-055`）→ 共 12 值。
--      **只有这两个**：催缴（`REQ-056`）的留痕住在新建的 `payment_reminder` 表里，不需要
--      `audit_log` 事件。为什么不顺手加 `payment_reminder_sent`：重建 `audit_log` 时
--      **不得放宽任何既有约束**、`ref_table` 白名单要**逐字段照抄**（`data-model.md`
--      §2.18 / §5.2 —— 唯一获批的 DDL 例外只有 `event_type`），而"催缴已发送"要落
--      `audit_log` 就必须扩 `ref_table` 去指向 `payment_reminder` 的真行。收益不值当，
--      故**不加该取值**，也不留一个没有写手的白名单值（那正是"已建模却未实现"）。
--      另：`audit_log` 是**资金链路**只增不改留痕（宪法 §4），催缴不是资金动作。
--   2) 新建 `payment_reminder` —— 催缴短信的**留痕表**（`REQ-056`：本期不接真实短信网关，
--      发送动作落表，含**脱敏**手机号与应缴金额）。
--
-- 为什么又要重建 `audit_log`：`event_type` 是 CHECK 白名单，**SQLite 改不了 CHECK 约束**
-- （同 `0004_audit_event_types.sql`）。顺序照抄 0004，一步不能少：
--   DROP 两个触发器 → 建新表（其余列逐字段照抄）→ `INSERT…SELECT`（含主键，一行不少）
--   → DROP 旧表 → RENAME → **重建两个触发器** → **重建两条索引**。
--   * 漏掉触发器 = 拆掉 `NFR-009` / 宪法 §4「资金链路审计只增不改」的闸门；
--   * 漏掉索引 = 静默丢掉 `data-model.md` §6 登记的留痕回查索引（`DROP TABLE` 会连带删掉它们，
--     且索引名全库唯一，只能在 RENAME **之后**建）。
--
-- 原子性：`app/db.py::apply_migration` 用**显式** `BEGIN IMMEDIATE` 包住整个脚本
-- （`task-11` 修复），并在事务外切换 `PRAGMA foreign_keys` —— 故半途失败**整体回滚**，
-- 不会留下"旧表已删、新表未换名"或"红线触发器被摘掉"的中间态。
-- =====================================================================

-- 1) 先摘掉红线触发器（搬完数据后原样重建）
DROP TRIGGER IF EXISTS audit_log_no_update;
DROP TRIGGER IF EXISTS audit_log_no_delete;

-- 1b) 自愈：上一次在旧代码下失败可能残留空的 `audit_log_new`
DROP TABLE IF EXISTS audit_log_new;

-- 2) 新表：`event_type` = 0004 的 10 值 + 本次 2 值；`ref_table` = **逐字段照抄** 0004 的 6 值
--    （`data-model.md` §5.2：重建不得放宽任何既有约束）；其余列定义与约束同样照抄
CREATE TABLE IF NOT EXISTS audit_log_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type    TEXT NOT NULL CHECK (event_type IN (
                      'price_change', 'refund_applied', 'refund_duplicate_hit',
                      'offline_backfilled', 'offline_duplicate_discarded',
                      'payment_callback_duplicate_hit', 'staging_write_failed',
                      'offline_threshold_warned', 'commission_rule_changed',
                      'scale_amount_mismatch',
                      'transaction_confirmed', 'transaction_cancelled')),
    stall_id      INTEGER REFERENCES stall(id),
    ref_table     TEXT NOT NULL CHECK (ref_table IN (
                      'transaction', 'transaction_item', 'payment', 'refund',
                      'offline_queue', 'commission_rule')),
    ref_id        INTEGER NOT NULL,
    payload_json  TEXT NOT NULL,
    actor         TEXT NOT NULL CHECK (length(actor) <= 32),
    occurred_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 3) 逐行搬移（显式列名 + 显式主键：一行不少、内容不改写）
INSERT INTO audit_log_new (id, event_type, stall_id, ref_table, ref_id, payload_json, actor, occurred_at)
SELECT id, event_type, stall_id, ref_table, ref_id, payload_json, actor, occurred_at
FROM audit_log;

-- 4) 换名
DROP TABLE audit_log;
ALTER TABLE audit_log_new RENAME TO audit_log;

-- 5) 原样重建两个触发器（`NFR-009` / 宪法 §4）
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

-- 6) 重建留痕回查索引（随旧表一起被 DROP，见文件头）
CREATE INDEX IF NOT EXISTS ix_audit_ref        ON audit_log (ref_table, ref_id);
CREATE INDEX IF NOT EXISTS ix_audit_stall_time ON audit_log (stall_id, occurred_at);

-- 7) 催缴短信留痕（`REQ-056`）。DDL 照 `tasks.md` `task-25` 给定文本，但 `phone_masked` 的
--    CHECK **补上与 `stall.payment_receiver_token` 同款的掩码闸门**（`length <= 32 AND LIKE '%****%'`）：
--    同一条 `NFR-012`「只存脱敏值」，两个字段的库层强制力度必须一致 —— 否则"只存掩码"就只剩应用层自觉。
--    `sent_at` 的 DEFAULT 只是兜底 —— `app/domain/notify.py` **一律显式写** `now_iso()`
--    （`REQ-033` 时钟缝：注入时钟能覆盖全部时间戳），故该列不进墙钟缺口枚举。
CREATE TABLE IF NOT EXISTS payment_reminder (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_id   INTEGER NOT NULL REFERENCES merchant(id),
    business_date TEXT NOT NULL CHECK (business_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    payable_cents INTEGER NOT NULL CHECK (payable_cents >= 0),
    phone_masked  TEXT NOT NULL CHECK (length(phone_masked) <= 32 AND phone_masked LIKE '%****%'),
    channel       TEXT NOT NULL DEFAULT 'sms' CHECK (channel IN ('sms')),
    sent_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS ix_reminder_merchant_time ON payment_reminder (merchant_id, sent_at);
