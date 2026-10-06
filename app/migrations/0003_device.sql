-- =====================================================================
-- 0003_device.sql —— 中台设备模型（T-SCALE-02）
--
-- 唯一权威：
--   * specs/market-trade-flow/spec.md `REQ-036`（秤端经**设备注册**绑定唯一的「市场 + 摊位」，
--     以**设备令牌**标识身份；中台拒绝未注册设备与令牌不符的请求）
--   * specs/market-trade-flow/contracts/scale-midplatform.md §1 第 6 条（**授权只来自设备令牌，
--     不来自请求参数**）、§3.1（激活）、§3.2（心跳）、§4（`MT-2001`/`MT-2005`）
--   * specs/market-trade-flow/spec.md `AC-027`（未注册设备上报被拒且不产生交易行；
--     已注册设备的上报绑定到它注册的摊位）
--
-- 三条刻意的实现决定：
--   1) **只存令牌摘要**（`token_digest` = sha256 十六进制，64 字符）：明文令牌绝不入库
--      （`RL-5` 同口径；`spec.md` `NFR-012` 的"从入口杜绝"）。摘要列加 `length = 64` 的 CHECK，
--      使"有人往库里塞明文令牌"在**数据库层**就被拒绝，而不是靠应用层自觉。
--   2) **`*_at` 列不设 SQL 默认值**：设备行由应用层写入（`app/domain/device.py` 经
--      `app/db.py::now_iso()`，即 `REQ-033` 的时钟接缝）。若给 `created_at` 一个
--      `datetime('now','localtime')` 默认值，就又多一处"SQLite 求值、注入时钟管不到"的缺口
--      （`T-SIM-00` 已把这类缺口枚举钉死在 `tests/unit/test_clock_guard.py`）。
--   3) `(market_id, stall_id)` **不加唯一约束**：绑定唯一性由 `ux_device_id` 保证
--      （一台设备只有一个绑定），但一个摊位允许有替换机/备用机 —— 把摊位也做成唯一
--      会在"换秤"时把新设备直接挡在库外（`IntegrityError` → 500），而契约与 `REQ-036`
--      都没有要求"一个摊位只能有一台设备"。
-- =====================================================================

CREATE TABLE IF NOT EXISTS device (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id          TEXT NOT NULL CHECK (length(device_id) BETWEEN 1 AND 32),
    -- 令牌摘要（sha256 hex）。**明文绝不入库**：CHECK 长度 64 是数据库层的最后一道闸门。
    token_digest       TEXT NOT NULL CHECK (length(token_digest) = 64),
    -- 绑定的「市场 + 摊位」= 该设备的**授权范围**（契约 §1 第 6 条）。
    market_id          INTEGER NOT NULL CHECK (market_id >= 1),
    stall_id           INTEGER NOT NULL CHECK (stall_id >= 1),
    firmware_version   TEXT CHECK (firmware_version IS NULL OR length(firmware_version) <= 32),
    hardware_rev       TEXT CHECK (hardware_rev IS NULL OR length(hardware_rev) <= 32),
    -- `registered`（预注册待激活）/ `active`（已激活）/ `disabled`（运维解绑，MT-2005 的处置路径）
    status             TEXT NOT NULL DEFAULT 'registered'
                       CHECK (status IN ('registered', 'active', 'disabled')),
    -- 心跳时间；NULL = 尚未心跳（与"心跳过"可区分）
    last_heartbeat_at  TEXT,
    -- 应用层显式赋值（见决定 2），故**不设 SQL 默认值**
    created_at         TEXT NOT NULL,
    -- 首次激活时间；重复激活不改绑定，也不刷新本列
    activated_at       TEXT
);

-- 设备身份唯一：一个 `device_id` 只有一个绑定（`REQ-036` / `MT-2005` 的约束兜底）
CREATE UNIQUE INDEX IF NOT EXISTS ux_device_id ON device (device_id);
-- 按摊位/市场取设备（心跳观测、运营端解绑、`AC-027` 的绑定核对）
CREATE INDEX IF NOT EXISTS ix_device_binding ON device (market_id, stall_id);
