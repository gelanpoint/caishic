-- =====================================================================
-- 0001_init.sql —— 初始建库脚本（19 张表 / 25 条索引 / audit_log 只增不改触发器）
--
-- 唯一权威：specs/market-trade-flow/data-model.md
--   §1 实体清单（19 个实体，含基础设施表 schema_migration）
--   §2 实体字段表（字段名 / 类型 / 必填 / 校验规则 / 默认值）
--   §6 索引建议（**恰好 25 条**，本文件逐条落实，索引名与 §6 同名）
--
-- 约定（data-model.md §0）：
--   * 金额一律整数「分」（*_cents）、重量一律整数「克」（*_grams）；
--   * 时间 TEXT，ISO-8601 `YYYY-MM-DD HH:MM:SS`（本机本地时间）；营业日 TEXT `YYYY-MM-DD`；
--   * 主键一律 `INTEGER PRIMARY KEY AUTOINCREMENT`；
--   * **全库不得出现身份证号 / 银行卡号字段**（REQ-024 / NFR-012 / 宪法 §2）。
--
-- ⚠️ 两条刻意的实现决定（都为可机械核验服务，变更前先读这里）：
--   1) **唯一性一律用 §6 的具名 `CREATE UNIQUE INDEX` 表达，不在 CREATE TABLE 里写 UNIQUE**。
--      SQLite 会为表内 UNIQUE 自动生成 `sqlite_autoindex_*`，那会让"索引条数"无法与 §6 的 25 条对齐。
--      具名索引同时让唯一约束**有名可查**（便于核对与排错）。
--   2) `status` 等**状态机字段不加 CHECK** —— 状态取值与非法流转由 data-model.md §4 的状态机 +
--      应用层校验（错误码 `MT-1001`）负责。这里只对 §2 字段表**内联给出枚举**的字段加 CHECK。
--
-- 幂等：全部 `IF NOT EXISTS`，启动时按文件名顺序执行，执行记录写入 `schema_migration`（data-model.md §7）。
-- =====================================================================

-- ---------------------------------------------------------------------
-- 2.19 迁移执行记录（先建：db.py 需要它记录"哪些脚本已执行"）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migration (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL CHECK (length(filename) >= 1),
    applied_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.1 商户 merchant
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merchant (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 50),
    -- 校验"仅数字与 -"：不存在"非数字且非横线"的字符
    phone       TEXT NOT NULL CHECK (length(phone) BETWEEN 1 AND 20 AND phone NOT GLOB '*[^0-9-]*'),
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.2 摊位 stall
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stall (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    stall_no               TEXT NOT NULL CHECK (length(stall_no) BETWEEN 1 AND 16),
    merchant_id            INTEGER NOT NULL REFERENCES merchant(id),
    name                   TEXT CHECK (name IS NULL OR length(name) <= 50),
    -- 收款标识（脱敏）：REQ-024 / NFR-012 —— 只允许掩码形式，**不得存完整收款账号**。
    -- 强制"必须含掩码段"是这条红线在数据库层的最后一道闸门。
    payment_receiver_token TEXT NOT NULL
                           CHECK (length(payment_receiver_token) <= 32
                                  AND payment_receiver_token LIKE '%****%'),
    status                 TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at             TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.3 秤端摊位会话 stall_session
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stall_session (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    stall_id       INTEGER NOT NULL REFERENCES stall(id),
    session_token  TEXT NOT NULL CHECK (length(session_token) BETWEEN 16 AND 64),
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_seen_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

-- ---------------------------------------------------------------------
-- 2.4 标准品类 category
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS category (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL CHECK (length(code) BETWEEN 1 AND 16),
    name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 32),
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.5 摊位品类别名 stall_category_alias
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stall_category_alias (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stall_id     INTEGER NOT NULL REFERENCES stall(id),
    alias_name   TEXT NOT NULL CHECK (length(alias_name) BETWEEN 1 AND 32),
    category_id  INTEGER NOT NULL REFERENCES category(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.6 商品 product
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stall_id     INTEGER NOT NULL REFERENCES stall(id),
    name         TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 50),
    category_id  INTEGER NOT NULL REFERENCES category(id),
    unit         TEXT NOT NULL DEFAULT 'kg' CHECK (unit = 'kg'),
    icon_key     TEXT CHECK (icon_key IS NULL OR length(icon_key) <= 32),
    hotkey       TEXT CHECK (hotkey IS NULL OR length(hotkey) BETWEEN 1 AND 4),
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.7 价目表项 price_item
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_item (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    stall_id          INTEGER NOT NULL REFERENCES stall(id),
    product_id        INTEGER NOT NULL REFERENCES product(id),
    business_date     TEXT NOT NULL
                      CHECK (business_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    unit_price_cents  INTEGER NOT NULL CHECK (unit_price_cents >= 1),
    source            TEXT NOT NULL DEFAULT 'manual'
                      CHECK (source IN ('manual', 'copied_previous_day')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    -- 价目表**不属**资金留痕表，允许更新（逐条调整价格），见 data-model.md §2.7
    updated_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.8 交易 transaction
-- ---------------------------------------------------------------------
-- ⚠️ `transaction` 是 SQL 保留字，**必须加双引号**，否则建表直接语法错误（已实测）
CREATE TABLE IF NOT EXISTS "transaction" (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_no          TEXT NOT NULL CHECK (length(transaction_no) <= 32),
    stall_id                INTEGER NOT NULL REFERENCES stall(id),
    business_date           TEXT NOT NULL
                            CHECK (business_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    -- 状态机见 data-model.md §4.1（默认 priced）；非法流转由应用层按 MT-1001 拒绝
    status                  TEXT NOT NULL DEFAULT 'priced',
    total_amount_cents      INTEGER NOT NULL CHECK (total_amount_cents >= 0),
    received_amount_cents   INTEGER CHECK (received_amount_cents IS NULL OR received_amount_cents >= 0),
    round_off_cents         INTEGER NOT NULL DEFAULT 0 CHECK (round_off_cents >= 0),
    origin                  TEXT NOT NULL DEFAULT 'online'
                            CHECK (origin IN ('online', 'offline_staged', 'backfilled')),
    client_idempotency_key  TEXT NOT NULL CHECK (length(client_idempotency_key) <= 64),
    created_at              TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.9 交易明细 transaction_item
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transaction_item (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id            INTEGER NOT NULL REFERENCES "transaction"(id),
    product_id                INTEGER NOT NULL REFERENCES product(id),
    category_id               INTEGER NOT NULL REFERENCES category(id),
    -- REQ-027：重量 >0 且 ≤50 公斤，越界拒绝计价（MT-1002）
    weight_grams              INTEGER NOT NULL CHECK (weight_grams > 0 AND weight_grams <= 50000),
    original_unit_price_cents INTEGER NOT NULL CHECK (original_unit_price_cents >= 0),
    final_unit_price_cents    INTEGER NOT NULL CHECK (final_unit_price_cents >= 0),
    amount_cents              INTEGER NOT NULL CHECK (amount_cents >= 0),
    price_changed             INTEGER NOT NULL DEFAULT 0 CHECK (price_changed IN (0, 1)),
    is_round_off              INTEGER NOT NULL DEFAULT 0 CHECK (is_round_off IN (0, 1)),
    created_at                TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.10 支付流水 payment（现金与收款码**共用本表**，REQ-010）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_no      TEXT NOT NULL CHECK (length(payment_no) <= 32),
    transaction_id  INTEGER NOT NULL REFERENCES "transaction"(id),
    method          TEXT NOT NULL CHECK (method IN ('qr', 'cash')),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents >= 0),
    -- 状态机见 data-model.md §4.2（pending / success / failed / timeout）
    status          TEXT NOT NULL DEFAULT 'pending',
    callback_no     TEXT CHECK (callback_no IS NULL OR length(callback_no) <= 32),
    confirmed_at    TEXT,
    operator        TEXT CHECK (operator IS NULL OR length(operator) <= 32),
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.11 支付回调记录 payment_callback_log
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_callback_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 允许重复：重复到达即"幂等命中"，不重复记账（REQ-029 / AC-010）
    callback_no   TEXT NOT NULL CHECK (length(callback_no) <= 32),
    payment_id    INTEGER REFERENCES payment(id),
    result        TEXT NOT NULL CHECK (result IN ('success', 'failed', 'timeout')),
    is_duplicate  INTEGER NOT NULL DEFAULT 0 CHECK (is_duplicate IN (0, 1)),
    received_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.12 退货冲正 refund
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS refund (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    refund_no        TEXT NOT NULL CHECK (length(refund_no) <= 32),
    transaction_id   INTEGER NOT NULL REFERENCES "transaction"(id),
    stall_id         INTEGER NOT NULL REFERENCES stall(id),
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    idempotency_key  TEXT NOT NULL CHECK (length(idempotency_key) <= 64),
    refunded_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    operator         TEXT NOT NULL CHECK (length(operator) <= 32)
);

-- ---------------------------------------------------------------------
-- 2.13 离线暂存队列 offline_queue
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS offline_queue (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_idempotency_key    TEXT NOT NULL CHECK (length(client_idempotency_key) <= 64),
    stall_id                  INTEGER NOT NULL REFERENCES stall(id),
    business_date             TEXT NOT NULL
                              CHECK (business_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    -- 本地副本本体；补传成功后**置为 NULL**（NFR-013）。payload_json 为空 + purged_at 非空 = 副本已清除的可核对证据。
    payload_json              TEXT,
    -- 状态机见 data-model.md §4.3（staged / backfilled / duplicate_discarded）—— 取值以该节为唯一权威。
    -- 本列**不加 CHECK**：§2.13 的 status 行写的是"状态机枚举，见 §4.3"（而非内联给出取值），
    -- 按本文件约定 2 处理（状态机字段的取值与非法流转由 §4 状态机 + 应用层负责）。
    status                    TEXT NOT NULL DEFAULT 'staged',
    staged_at                 TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    synced_at                 TEXT,
    purged_at                 TEXT,
    over_threshold_notified   INTEGER NOT NULL DEFAULT 0 CHECK (over_threshold_notified IN (0, 1)),
    discard_reason            TEXT CHECK (discard_reason IS NULL
                                          OR discard_reason IN ('duplicate_idempotency_key'))
);

-- ---------------------------------------------------------------------
-- 2.14 佣金口径 commission_rule
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commission_rule (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pay_object      TEXT NOT NULL CHECK (pay_object IN ('merchant', 'customer', 'market')),
    -- 万分比整数，避免浮点（1 bp = 万分之一）
    rate_bp         INTEGER NOT NULL CHECK (rate_bp BETWEEN 1 AND 10000),
    category_tier   TEXT CHECK (category_tier IS NULL OR length(category_tier) <= 32),
    effective_from  TEXT NOT NULL
                    CHECK (effective_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    effective_to    TEXT CHECK (effective_to IS NULL
                                OR effective_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.15 日聚合 daily_aggregate
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_aggregate (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    stall_id                 INTEGER NOT NULL REFERENCES stall(id),
    business_date            TEXT NOT NULL
                             CHECK (business_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    revision                 INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    -- 同一摊位同一营业日"有且仅有一条为 1"：由应用层在重算时保证（重算须把旧行置 0）
    is_current               INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    txn_count                INTEGER NOT NULL DEFAULT 0 CHECK (txn_count >= 0),
    gross_amount_cents       INTEGER NOT NULL DEFAULT 0 CHECK (gross_amount_cents >= 0),
    refund_amount_cents      INTEGER NOT NULL DEFAULT 0 CHECK (refund_amount_cents >= 0),
    commission_amount_cents  INTEGER NOT NULL DEFAULT 0 CHECK (commission_amount_cents >= 0),
    recomputed_at            TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.16 结算单 settlement
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settlement (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_no            TEXT NOT NULL CHECK (length(settlement_no) <= 32),
    stall_id                 INTEGER NOT NULL REFERENCES stall(id),
    period_start             TEXT NOT NULL
                             CHECK (period_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    period_end               TEXT NOT NULL
                             CHECK (period_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    gross_amount_cents       INTEGER NOT NULL CHECK (gross_amount_cents >= 0),
    commission_amount_cents  INTEGER NOT NULL CHECK (commission_amount_cents >= 0),
    -- 重算生成新行、旧行保留（Q-10 可追溯、不可物理删除）
    version                  INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    generated_at             TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.17 摊位信用档案 stall_credit
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stall_credit (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    stall_id              INTEGER NOT NULL REFERENCES stall(id),
    period_start          TEXT NOT NULL
                          CHECK (period_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    period_end            TEXT NOT NULL
                          CHECK (period_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    item_count            INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
    price_changed_count   INTEGER NOT NULL DEFAULT 0 CHECK (price_changed_count >= 0),
    -- 标价一致率（万分比 0~10000）：round((item_count − price_changed_count) ÷ item_count × 10000)；
    -- item_count = 0 时取 10000（口径见 data-model.md §2.17 与 REQ-008：抹零不计入）
    price_consistency_bp  INTEGER NOT NULL DEFAULT 10000
                          CHECK (price_consistency_bp BETWEEN 0 AND 10000),
    computed_at           TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------
-- 2.18 审计日志 audit_log —— 只增不改（资金链路证据链，NFR-009 / 宪法 §4）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type    TEXT NOT NULL CHECK (event_type IN (
                      'price_change', 'refund_applied', 'refund_duplicate_hit',
                      'offline_backfilled', 'offline_duplicate_discarded',
                      'payment_callback_duplicate_hit', 'staging_write_failed',
                      'offline_threshold_warned', 'commission_rule_changed')),
    stall_id      INTEGER REFERENCES stall(id),
    ref_table     TEXT NOT NULL CHECK (ref_table IN (
                      'transaction', 'transaction_item', 'payment', 'refund',
                      'offline_queue', 'commission_rule')),
    ref_id        INTEGER NOT NULL,
    payload_json  TEXT NOT NULL,
    actor         TEXT NOT NULL CHECK (length(actor) <= 32),
    occurred_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- audit_log 只增不改：由数据库触发器强制（data-model.md §2.18、NFR-009）
-- 应用层不提供任何修改接口；此处是最后一道闸门 —— 即使有人直接用 sqlite3 也改不动。
DROP TRIGGER IF EXISTS audit_log_no_update;
CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log 只增不改：禁止 UPDATE（NFR-009 / 宪法 §4）');
END;

DROP TRIGGER IF EXISTS audit_log_no_delete;
CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log 只增不改：禁止 DELETE（NFR-009 / 宪法 §4）');
END;

-- ---------------------------------------------------------------------
-- §6 索引建议 —— 恰好 25 条，索引名与 §6 逐条同名
-- ---------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS ux_stall_no               ON stall (stall_no);
CREATE INDEX        IF NOT EXISTS ix_stall_merchant         ON stall (merchant_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_session_token          ON stall_session (session_token);
CREATE UNIQUE INDEX IF NOT EXISTS ux_category_code          ON category (code);
CREATE UNIQUE INDEX IF NOT EXISTS ux_alias_stall_name       ON stall_category_alias (stall_id, alias_name);
CREATE INDEX        IF NOT EXISTS ix_product_stall_status   ON product (stall_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_price_stall_date_product
                                                            ON price_item (stall_id, business_date, product_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_transaction_no         ON "transaction" (transaction_no);
CREATE UNIQUE INDEX IF NOT EXISTS ux_transaction_idem       ON "transaction" (stall_id, client_idempotency_key);
CREATE INDEX        IF NOT EXISTS ix_transaction_stall_date ON "transaction" (stall_id, business_date);
CREATE INDEX        IF NOT EXISTS ix_transaction_item_txn   ON transaction_item (transaction_id);
CREATE INDEX        IF NOT EXISTS ix_transaction_item_date_flag
                                                            ON transaction_item (created_at, price_changed);
CREATE UNIQUE INDEX IF NOT EXISTS ux_payment_no             ON payment (payment_no);
-- SQLite 中 NULL 不参与唯一性比较 → 该唯一索引天然允许"多行 callback_no 为 NULL"（data-model.md §2.10）
CREATE UNIQUE INDEX IF NOT EXISTS ux_payment_callback_no    ON payment (callback_no);
CREATE INDEX        IF NOT EXISTS ix_payment_txn            ON payment (transaction_id);
CREATE INDEX        IF NOT EXISTS ix_callback_no            ON payment_callback_log (callback_no);
CREATE UNIQUE INDEX IF NOT EXISTS ux_refund_idem            ON refund (transaction_id, idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS ux_offline_idem           ON offline_queue (client_idempotency_key);
CREATE INDEX        IF NOT EXISTS ix_offline_status_staged  ON offline_queue (status, staged_at);
CREATE INDEX        IF NOT EXISTS ix_commission_effective   ON commission_rule (effective_from, effective_to);
CREATE UNIQUE INDEX IF NOT EXISTS ux_daily_current          ON daily_aggregate (stall_id, business_date, revision);
CREATE UNIQUE INDEX IF NOT EXISTS ux_settlement_no          ON settlement (settlement_no);
CREATE UNIQUE INDEX IF NOT EXISTS ux_stall_credit           ON stall_credit (stall_id);
CREATE INDEX        IF NOT EXISTS ix_audit_ref              ON audit_log (ref_table, ref_id);
CREATE INDEX        IF NOT EXISTS ix_audit_stall_time       ON audit_log (stall_id, occurred_at);
