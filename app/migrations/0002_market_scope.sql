-- =====================================================================
-- 0002_market_scope.sql —— 引入市场（租户）维度（T-SCALE-01）
--
-- 唯一权威：
--   * specs/market-trade-flow/spec.md `REQ-035`（商户 / 摊位 / 商品 / 价目表 / 佣金口径 /
--     日聚合 / 结算单均归属某一市场；**新增第二个市场不得改动表结构**）
--   * specs/market-trade-flow/spec.md `AC-026`（两市场数据互不可见、互不串账）
--   * specs/market-trade-flow/data-model.md §7 第 4 条「多市场多租户」的预留演进路径
--     （原文："新增 `market_id` 列并纳入各唯一约束"）
--
-- **绝不改写 `0001_init.sql`**：既有库不会拾取新 DDL（`Q-17` 同类风险），
-- 故市场维度只以**新迁移**表达；本文件与 `0003_device.sql` 由 `app/db.py` 按文件名升序补齐。
--
-- 三条刻意的实现决定（改动前先读，每条都对应一个已实测的约束）：
--   1) `market_id` 一律 `INTEGER NOT NULL DEFAULT 1`、**不带 `REFERENCES`**：
--      SQLite 明确禁止 `ALTER TABLE ... ADD COLUMN` 添加"带非 NULL 默认值的外键列"
--      （实测报错 `Cannot add a REFERENCES column with non-NULL default value`，`PRAGMA foreign_keys=ON`）。
--      故 `market_id` 的完整性由「默认市场行必然存在」+ 应用层归属校验保证，而不是外键。
--      `NOT NULL DEFAULT 1` 同时完成**回填**：SQLite 给既有行填默认值，既有单市场数据全部落到市场 1。
--   2) `market` 表**不含任何 `*_at` 列**：默认市场行由本迁移写入，而迁移取不到注入时钟
--      （`REQ-033` 的时钟接缝只在应用层）。若加一个由 `datetime('now','localtime')` 填充的
--      `created_at`，就会**新增一处 `T-SIM-00` 已钉死的时钟缺口**（`tests/unit/test_clock_guard.py`
--      的枚举集合会变红）。本表没有任何 `REQ` / `AC` / 契约端点需要创建时间，故不引入该列。
--   3) `ux_stall_no` / `ux_category_code` 由"全库唯一"改为"**市场内唯一**"：
--      `data-model.md` §7 第 4 条要求把 `market_id` 纳入各唯一约束 —— 否则第二个市场
--      不能再有 `A-01` 摊位或 `VEG` 品类编码，多市场从数据层就不成立。
--
-- 幂等：`CREATE ... IF NOT EXISTS` + `DROP INDEX IF EXISTS` + `INSERT ... WHERE NOT EXISTS`；
-- `ALTER TABLE ADD COLUMN` 无 `IF NOT EXISTS`，由 `app/db.py` 的迁移执行器**按列存在性跳过**
-- （否则脚本执行到一半失败后重跑会报 `duplicate column name`，把"重跑安全"这条承诺弄丢）。
-- =====================================================================

-- ---------------------------------------------------------------------
-- 市场（租户）档案
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    market_code  TEXT NOT NULL CHECK (length(market_code) BETWEEN 1 AND 16),
    name         TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 50),
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_market_code ON market (market_code);

-- 默认市场：显式 `id = 1`，与下面 `market_id` 的 `DEFAULT 1` 对齐。
-- 本期只装载一个市场（`REQ-035`）；第二个市场是**纯 DML**（INSERT 一行 + 带 market_id 插业务行），
-- 不需要任何 DDL —— 这正是 `AC-026` 要证的那条。
INSERT INTO market (id, market_code, name, status)
SELECT 1, 'M-0001', '示例菜市场', 'active'
WHERE NOT EXISTS (SELECT 1 FROM market WHERE id = 1);

-- ---------------------------------------------------------------------
-- 市场归属：增 market_id（`NOT NULL DEFAULT 1` 即回填默认市场）
-- ---------------------------------------------------------------------
ALTER TABLE merchant        ADD COLUMN market_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE stall           ADD COLUMN market_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE category        ADD COLUMN market_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE product         ADD COLUMN market_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE price_item      ADD COLUMN market_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE commission_rule ADD COLUMN market_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE daily_aggregate ADD COLUMN market_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE settlement      ADD COLUMN market_id INTEGER NOT NULL DEFAULT 1;

-- ---------------------------------------------------------------------
-- 唯一约束纳入 market_id（决定 3）：市场内唯一，而不是全库唯一
-- ---------------------------------------------------------------------
DROP INDEX IF EXISTS ux_stall_no;
CREATE UNIQUE INDEX IF NOT EXISTS ux_stall_no ON stall (market_id, stall_no);

DROP INDEX IF EXISTS ux_category_code;
CREATE UNIQUE INDEX IF NOT EXISTS ux_category_code ON category (market_id, code);

-- ---------------------------------------------------------------------
-- 市场维度的查询索引：`AC-026` 的"两市场互不可见"靠每条读路径都按 market_id 过滤，
-- 故每个市场归属表都要有以 market_id 打头的索引（不是"为建而建"）。
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_merchant_market        ON merchant (market_id);
CREATE INDEX IF NOT EXISTS ix_stall_market           ON stall (market_id);
CREATE INDEX IF NOT EXISTS ix_category_market        ON category (market_id);
CREATE INDEX IF NOT EXISTS ix_product_market         ON product (market_id, status);
CREATE INDEX IF NOT EXISTS ix_price_item_market      ON price_item (market_id, business_date);
CREATE INDEX IF NOT EXISTS ix_commission_market      ON commission_rule (market_id, effective_from);
CREATE INDEX IF NOT EXISTS ix_daily_aggregate_market ON daily_aggregate (market_id, business_date);
CREATE INDEX IF NOT EXISTS ix_settlement_market      ON settlement (market_id, period_end);
