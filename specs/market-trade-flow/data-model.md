# market-trade-flow 数据模型

- 特性目录: specs/market-trade-flow/
- 需求规格: [spec.md](./spec.md) / 技术方案: [plan.md](./plan.md)
- 存储: SQLite（WAL 模式），经 Python 标准库 `sqlite3` 访问（来源: [plan.md](./plan.md) §1、ADR-0003）
- 日期: 2026-09-30（**形态 2 同步: 2026-10-06**；**演示控制台同步: 2026-10-08**，迁移 `0002_market_scope.sql` / `0003_device.sql` / `0004_audit_event_types.sql` / `0005_demo_console.sql`）
- 接口契约: [contracts/](./contracts/)（端点、错误码定义与请求/响应字段一律以该目录为准，本文件只定义**持久化字段**）

## 0. 全局约定（全表适用，先读）

| 约定 | 取值 | 来源 |
| --- | --- | --- |
| 金额单位 | **整数「分」**（`INTEGER`）；展示层再格式化为两位小数的元 | `REQ-005`（金额保留两位小数）+ 对账等式必须精确成立（`AC-001`/`AC-003`）：用浮点会造成对账漂移 |
| 重量单位 | **整数「克」**（`INTEGER`） | `REQ-005`（以公斤为单位、金额两位小数）；整数克消除浮点误差 |
| 计价公式 | `金额(分) = round(单价(分/公斤) × 重量(克) ÷ 1000)`，四舍五入到分 | `REQ-005`；取整规则为**实现层口径**，spec 未规定取整方式（见 §4.3 注） |
| 时间字段 | `TEXT`，ISO-8601 `YYYY-MM-DD HH:MM:SS`（本机本地时间） | 实现便利（单机演示环境） |
| 营业日字段 | `TEXT`，`YYYY-MM-DD` | `REQ-018`（按日聚合）、`REQ-003`（复制上一营业日价格） |
| 主键 | `INTEGER PRIMARY KEY AUTOINCREMENT` | 实现便利 |
| 命名 | 表名与字段名一律 `snake_case`；金额字段以 `_cents` 结尾、重量字段以 `_grams` 结尾 | 实现便利（可机械校验单位） |
| **敏感字段禁令** | **全库不得出现身份证号与银行卡号字段**：字段名禁止匹配 `id_card*` / `id_no*` / `bank_card*` / `card_no*` / `bank_account*`；已有字段中凡疑似者一律不建 | `REQ-024`、`NFR-012`、宪法 §2 第 3 条（`AC-012` 以此做扫描） |
| 收款标识 | 只存**脱敏值**（掩码形式），不存完整收款账号 | `REQ-024`、`NFR-012` |
| 只增不改表 | `audit_log` 与全部资金链路留痕表：只允许 `INSERT`，由数据库触发器拒绝 `UPDATE` / `DELETE` | `NFR-009`、宪法 §4「证据链不可削弱」 |
| 顾客数据 | **不建任何顾客实体、不存可识别顾客身份的信息** | `spec.md` §5 边界「顾客身份」（有意设计，非遗漏） |

## 1. 实体清单

| 实体 | 表名 | 职责（一句话） | 主要服务的 REQ |
| --- | --- | --- | --- |
| 市场 | `market` | 市场（租户）档案：市场维度的落点，八张业务表的归属对象 | `REQ-035` |
| 商户 | `merchant` | 摊主档案：姓名、联系方式、在营状态 | `REQ-001` |
| 摊位 | `stall` | 摊位档案与收款标识（脱敏）；秤端绑定的主体 | `REQ-001`、`REQ-024` |
| 秤端摊位会话 | `stall_session` | 秤端「已绑定摊位」的会话，落实摊主只能访问本摊位数据 | `REQ-032`、`NFR-007` |
| 设备 | `device` | 秤端设备注册与「市场 + 摊位」绑定、设备令牌摘要（形态 2） | `REQ-036` |
| 标准品类 | `category` | 品类字典（记账口径的标准分类） | `REQ-002`、`REQ-014` |
| 摊位品类别名 | `stall_category_alias` | 「摊位别名 → 标准品类」映射，按标准品类记账 | `REQ-002` |
| 商品 | `product` | 摊位商品档案（可标价对象） | `REQ-003`、`REQ-004` |
| 价目表项 | `price_item` | 按营业日的摊位价目表；支持复制上一营业日 | `REQ-003` |
| 交易 | `transaction` | 一笔交易的头部：交易号、金额、状态、幂等键、来源（在线/离线补传） | `REQ-005`、`REQ-012`、`REQ-014`、`REQ-015`、`REQ-031` |
| 交易明细 | `transaction_item` | 一笔交易内的商品行：重量、原价、改后价、金额、改价与抹零标记 | `REQ-005`~`REQ-008` |
| 支付流水 | `payment` | **唯一的支付流水表**：收款码与**现金**都写这里，以 `method` 区分 | `REQ-009`~`REQ-011`、`REQ-029` |
| 支付回调记录 | `payment_callback_log` | 每次支付回调的到达记录与幂等命中标记 | `REQ-011`、`REQ-029` |
| 退货冲正 | `refund` | 退货冲正记录：金额、关联原单号、操作摊位、幂等键 | `REQ-013`、`REQ-028` |
| 离线暂存队列 | `offline_queue` | 断网期间的本地持久暂存与补传状态；补传成功后载荷置空 | `REQ-014`~`REQ-016`、`REQ-030`、`NFR-013`、`NFR-014` |
| 佣金口径 | `commission_rule` | 收费对象、费率、品种档位与生效期 | `REQ-017` |
| 日聚合 | `daily_aggregate` | 按摊位 × 营业日的交易与佣金聚合快照（可重算） | `REQ-018` |
| 结算单 | `settlement` | 依据日聚合生成的结算单，可追溯多版本 | `REQ-019` |
| 摊位信用档案 | `stall_credit` | 「标价一致率」等信用指标的档案（对顾客可见） | `REQ-008` |
| 审计日志 | `audit_log` | **只增不改**的资金链路留痕：改价、退货冲正、离线补传、幂等命中、确认收款、取消交易、催缴发送 | `NFR-009`、`REQ-007`、`REQ-013`、`REQ-015`、`REQ-054`、`REQ-055`、`REQ-056` |
| 催缴短信记录 | `payment_reminder` | 每次催缴短信的发送留痕：脱敏手机号 + 当次应缴金额 + 营业日（演示口径，不接真实网关） | `REQ-056`、`AC-046` |
| 迁移执行记录 | `schema_migration` | 基础设施表：记录已执行的迁移脚本，保证启动时按序补齐、不重复执行 | 实现便利（`AC-013` 一条启动命令） |

> **实体共 22 个**（`0001_init.sql` 的 19 个 + `0002`~`0005` 新增 3 个）：`market` 由 `0002_market_scope.sql` 引入、
> `device` 由 `0003_device.sql` 引入、`payment_reminder` 由 `0005_demo_console.sql` 引入；
> 字段表见 §2.20 / §2.21 / §2.22，索引见 §6。本清单与 `sqlite_master` 的表清单逐条同名。

> **改价留痕落在哪里**：`REQ-007` 要求记录「原价、改后价、改价时间、操作摊位」。本模型**不另建** `price_change_log` 表，
> 而是把该事件作为 `audit_log` 中 `event_type = price_change` 的一条记录（载荷含上述四项 + 明细行引用），
> 同时在 `transaction_item` 上保留终态价格字段。**同一事实不建两张表**（避免两处各存一份而漂移）。
>
> **对账等式不落表**：「订单总额 = 支付流水 = 分账明细」由 `transaction` / `payment` / `transaction_item` 现场计算，
> **不建对账结果表** —— 落表即产生第二份事实来源（`REQ-020`、`AC-003`）。

## 2. 实体字段表

### 2.1 商户（`merchant`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `name` | TEXT | 是 | 长度 1~50（`REQ-001` 摊主姓名） | 无 | 摊主姓名；**属个人信息，展示与日志按基线脱敏，禁止写入日志**（`NFR-012`） |
| `phone` | TEXT | 是 | 长度 1~20，仅数字与 `-`（`REQ-001` 联系方式） | 无 | 复称投诉要能找到人；**禁止写入日志**（`NFR-012`） |
| `status` | TEXT | 是 | 枚举：`active`（在营）/ `inactive`（停用）（`REQ-001`） | `active` | 在营状态 |
| `created_at` | TEXT | 是 | ISO-8601 | 当前时间 | |
| `updated_at` | TEXT | 是 | ISO-8601 | 当前时间 | |
| `market_id` | INTEGER | 是 | 市场归属；`NOT NULL DEFAULT 1`（既有行由 SQLite 就地回填默认市场）；**无 `REFERENCES`**，原因见 §2.20 注 | 1 | 所属市场（`REQ-035`） |

### 2.2 摊位（`stall`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `stall_no` | TEXT | 是 | **市场内唯一**：`ux_stall_no` = (`market_id`, `stall_no`)；长度 1~16（`REQ-001` 按摊位查询、`REQ-035` 市场维度） | 无 | 摊位号 |
| `merchant_id` | INTEGER | 是 | 外键 → `merchant.id`（`REQ-001`） | 无 | 所属商户 |
| `name` | TEXT | 否 | 长度 ≤50（实现便利） | NULL | 摊位展示名 |
| `payment_receiver_token` | TEXT | 是 | **脱敏值**：掩码形式 = **前 2 位 + `****` + 后 4 位**（契约 `rest-api.md` §3.34 的注册口径），长度 ≤32，且**必须含 `****`**（`0001_init.sql` 用 `LIKE '%****%'` 在库层强制）；**不得存完整收款账号**（`REQ-024`/`NFR-012`） | 无 | 收款标识（脱敏） |
| `status` | TEXT | 是 | 枚举：`active` / `inactive`（`REQ-001`） | `active` | 在营状态 |
| `created_at` | TEXT | 是 | ISO-8601 | 当前时间 | |
| `market_id` | INTEGER | 是 | 市场归属；`NOT NULL DEFAULT 1`（既有行由 SQLite 就地回填默认市场）；**无 `REFERENCES`**，原因见 §2.20 注 | 1 | 所属市场（`REQ-035`） |

> 本表**不冗余**「当期价目表是否存在」这类派生标记（`REQ-022` 的价目表维护率由 `price_item` 计算），避免第二份事实来源。

### 2.3 秤端摊位会话（`stall_session`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `stall_id` | INTEGER | 是 | 外键 → `stall.id`（`REQ-032`） | 无 | 本会话绑定的摊位 |
| `session_token` | TEXT | 是 | 全表唯一；长度 16~64（`REQ-032`/`NFR-007`） | 无 | 秤端会话凭据；只经环境/请求头传递，**不写入日志全量**（`NFR-012`） |
| `created_at` | TEXT | 是 | ISO-8601 | 当前时间 | |
| `last_seen_at` | TEXT | 是 | ISO-8601 | 当前时间 | |
| `is_active` | INTEGER | 是 | 0/1（实现便利） | 1 | 失效后该会话一律拒绝（`REQ-032`） |

### 2.4 标准品类（`category`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `code` | TEXT | 是 | **市场内唯一**：`ux_category_code` = (`market_id`, `code`)；长度 1~16 | 无 | 品类编码（记账键；`REQ-035` 市场维度） |
| `name` | TEXT | 是 | 长度 1~32（`REQ-002`） | 无 | 标准品类名 |
| `status` | TEXT | 是 | 枚举：`active` / `inactive` | `active` | |
| `created_at` | TEXT | 是 | ISO-8601 | 当前时间 | |
| `market_id` | INTEGER | 是 | 市场归属；`NOT NULL DEFAULT 1`（既有行由 SQLite 就地回填默认市场）；**无 `REFERENCES`**，原因见 §2.20 注 | 1 | 所属市场（`REQ-035`） |

### 2.5 摊位品类别名（`stall_category_alias`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `stall_id` | INTEGER | 是 | 外键 → `stall.id` | 无 | |
| `alias_name` | TEXT | 是 | 长度 1~32；与 `stall_id` 联合唯一（`REQ-002`） | 无 | 摊位自己的叫法 |
| `category_id` | INTEGER | 是 | 外键 → `category.id`（`REQ-002` 按标准品类记账） | 无 | 映射到的标准品类 |
| `created_at` | TEXT | 是 | ISO-8601 | 当前时间 | |

> **`alias_name` 承担两类语义（父代理 `2026-09-30` 裁定，必须明示，不得当成纯粹的同物异名表）**：
> ① **同物异名**（`上海青` / `小油菜` / `青菜`）—— 真正的别名，用于把摊位叫法**归一**到标准品类；
> ② **品种 / 细分名**（`红富士苹果` / `黄元帅苹果`）—— 作为**摊位级选品入口**，让摊主无论怎么叫都能查到标准品类。
> **代价（有意接受的粒度取舍）**：`REQ-002` 规定「按**标准品类**记账」，因此**品种粒度在统计中不保留**
> —— 红富士与黄元帅会合并计入「苹果」。这不是 bug，是本期在「每摊位 20～30 个 SKU」与
> 「每个标准品类 1～3 个别名」两个约束下**唯一可行**的取舍（改动它会越出 `T-005` 的种子规模裁定：
> 要么把标准品类加到 100+，要么把每摊位 SKU 降到 10 余个）。
> **商品身份在 `product` 表，不在本表** —— 本表只负责「别名 → 标准品类」的可查性，不承担商品档案职责。
> 生成规则与自检见 `scripts/gen_seed.py`；同一说明见 `app/seed.py` 模块 docstring。

### 2.6 商品（`product`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `stall_id` | INTEGER | 是 | 外键 → `stall.id` | 无 | |
| `name` | TEXT | 是 | 长度 1~50（`REQ-003`、`REQ-004`） | 无 | 商品名（秤端以图标/快捷键选择） |
| `category_id` | INTEGER | 是 | 外键 → `category.id`（`REQ-002`） | 无 | 记账用标准品类 |
| `unit` | TEXT | 是 | 固定 `kg`（`REQ-005` 以公斤计价） | `kg` | 计量单位 |
| `icon_key` | TEXT | 否 | 长度 ≤32（`REQ-004` 图标/快捷键） | NULL | 秤端图标键 |
| `hotkey` | TEXT | 否 | 长度 1~4（`REQ-004`） | NULL | 秤端快捷键 |
| `status` | TEXT | 是 | 枚举：`active` / `inactive`（`REQ-004` 摊位停用商品不出现在秤端） | `active` | |
| `created_at` | TEXT | 是 | ISO-8601 | 当前时间 | |
| `market_id` | INTEGER | 是 | 市场归属；`NOT NULL DEFAULT 1`（既有行由 SQLite 就地回填默认市场）；**无 `REFERENCES`**，原因见 §2.20 注 | 1 | 所属市场（`REQ-035`） |

> **`product` 没有 `(stall_id, name)` 唯一索引（有意为之，本轮不加）**：`REQ-058`/`AC-048` 的「同摊位同名 ⇒ 更新而非新增」
> **由应用层保证**（`POST /api/merchant/products` 先查后写）；**库里挡不住并发双写** —— 两条并发请求可能各插一行同名商品，
> 之后按"同名"查询会命中多行。实测全库索引中**无** `(stall_id, name)` 唯一索引（§6）。
> 若将来要 DB 级强制：须**新增迁移 + §6 登记**，且迁移前先确认历史数据无重名（否则唯一索引建不上）。

### 2.7 价目表项（`price_item`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `stall_id` | INTEGER | 是 | 外键 → `stall.id` | 无 | |
| `product_id` | INTEGER | 是 | 外键 → `product.id`；与 `stall_id`、`business_date` 联合唯一（`REQ-003`） | 无 | |
| `business_date` | TEXT | 是 | `YYYY-MM-DD`（`REQ-003`、`REQ-018`） | 无 | 生效营业日 |
| `unit_price_cents` | INTEGER | 是 | ≥1（分/公斤）（`REQ-005`） | 无 | 标准单价 |
| `source` | TEXT | 是 | 枚举：`manual`（逐条调整）/ `copied_previous_day`（复制上一营业日）（`REQ-003`） | `manual` | 来源可追溯 |
| `created_at` | TEXT | 是 | ISO-8601 | 当前时间 | |
| `updated_at` | TEXT | 是 | ISO-8601 | 当前时间 | 逐条调整会更新本行（价目表不属资金留痕表，允许更新） |
| `market_id` | INTEGER | 是 | 市场归属；`NOT NULL DEFAULT 1`（既有行由 SQLite 就地回填默认市场）；**无 `REFERENCES`**，原因见 §2.20 注 | 1 | 所属市场（`REQ-035`） |

### 2.8 交易（`transaction`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `transaction_no` | TEXT | 是 | 全表唯一；长度 ≤32（`REQ-031` 交易号唯一） | 系统生成 | 交易号（凭证展示用，`REQ-012`） |
| `stall_id` | INTEGER | 是 | 外键 → `stall.id`（`REQ-032` 数据边界） | 无 | |
| `business_date` | TEXT | 是 | `YYYY-MM-DD`（`REQ-018` 日聚合键） | 由创建时间推导 | |
| `status` | TEXT | 是 | 状态机枚举，见 §4.1（`REQ-012`、`REQ-013`、`REQ-055` 取消终态） | `priced` | |
| `total_amount_cents` | INTEGER | 是 | ≥0；= Σ `transaction_item.amount_cents`（`REQ-006`） | 无 | 应收金额 |
| `received_amount_cents` | INTEGER | 否 | ≥0；支付成功后写入（`REQ-017` 按实收金额计佣） | NULL | 实收金额 |
| `round_off_cents` | INTEGER | 是 | ≥0（`REQ-007`/`REQ-008`） | 0 | 抹零金额（与改价分开统计） |
| `origin` | TEXT | 是 | 枚举：`online` / `offline_staged` / `backfilled`（`REQ-014`/`REQ-015`） | `online` | 交易来源，供离线补传追溯 |
| `client_idempotency_key` | TEXT | 是 | 长度 ≤64；与 `stall_id` 联合唯一（`REQ-015` 补传去重） | 无 | 客户端幂等键 |
| `created_at` | TEXT | 是 | ISO-8601 | 当前时间 | |
| `updated_at` | TEXT | 是 | ISO-8601 | 当前时间 | 状态流转会更新本表状态字段（资金留痕由 `audit_log` 承担） |

### 2.9 交易明细（`transaction_item`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `transaction_id` | INTEGER | 是 | 外键 → `transaction.id`（`REQ-006` 同一笔内累加） | 无 | |
| `product_id` | INTEGER | 是 | 外键 → `product.id` | 无 | |
| `category_id` | INTEGER | 是 | 外键 → `category.id`；取**标准品类**快照（`REQ-002`） | 无 | 记账口径 |
| `weight_grams` | INTEGER | 是 | **> 0 且 ≤ 50000**（即 >0 且 ≤50 公斤）；越界拒绝计价（`REQ-027`） | 无 | 整数克 |
| `original_unit_price_cents` | INTEGER | 是 | ≥0（分/公斤）（`REQ-007` 原价） | 无 | 改价留痕的「原价」 |
| `final_unit_price_cents` | INTEGER | 是 | ≥0；默认等于原价（`REQ-007` 改后价） | 无 | 实际计价单价 |
| `amount_cents` | INTEGER | 是 | ≥0；= `round(final_unit_price_cents × weight_grams ÷ 1000)`（`REQ-005`） | 无 | 本行金额 |
| `price_changed` | INTEGER | 是 | 0/1；为 1 当且仅当 `final ≠ original`（`REQ-008` 改价**计入**标价一致率） | 0 | 指标计算用标记 |
| `is_round_off` | INTEGER | 是 | 0/1（`REQ-008` 抹零**不计入**标价一致率） | 0 | 指标计算用标记 |
| `created_at` | TEXT | 是 | ISO-8601 | 当前时间 | |

### 2.10 支付流水（`payment`）—— 现金与收款码共用本表

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `payment_no` | TEXT | 是 | 全表唯一；长度 ≤32 | 系统生成 | 支付流水号 |
| `transaction_id` | INTEGER | 是 | 外键 → `transaction.id`；**一笔成功交易只允许一条成功流水**（`REQ-026` 不得两笔记录/两次佣金） | 无 | |
| `method` | TEXT | 是 | 枚举：`qr`（收款码）/ `cash`（现金）（`REQ-009`、`REQ-010`） | 无 | 支付方式 |
| `amount_cents` | INTEGER | 是 | ≥0；`cash` 时必须等于应收金额（`REQ-010`） | 无 | |
| `status` | TEXT | 是 | 状态机枚举，见 §4.2（`REQ-011` 成功/失败/超时三选一） | `pending` | |
| `callback_no` | TEXT | 否 | 全表唯一（NULL 不参与唯一性，SQLite 语义）；`method = qr` 成功后必填（`REQ-029` 支付单号幂等） | NULL | 支付平台单号 |
| `confirmed_at` | TEXT | 否 | `method = cash` 时为**收款确认时间**，必填（`REQ-010`） | NULL | |
| `operator` | TEXT | 否 | `method = cash` 时必填，长度 ≤32（`REQ-010` 操作人） | NULL | 收款操作人 |
| `created_at` | TEXT | 是 | ISO-8601 | 当前时间 | |

### 2.11 支付回调记录（`payment_callback_log`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `callback_no` | TEXT | 是 | 长度 ≤32；允许重复（重复即幂等命中）（`REQ-029`） | 无 | 支付平台单号 |
| `payment_id` | INTEGER | 否 | 外键 → `payment.id`；幂等命中时指向已存在的流水 | NULL | |
| `result` | TEXT | 是 | 枚举：`success` / `failed` / `timeout`（`REQ-011`） | 无 | |
| `is_duplicate` | INTEGER | 是 | 0/1；为 1 表示本次为幂等命中、**不重复记账**（`REQ-029`） | 0 | |
| `received_at` | TEXT | 是 | ISO-8601 | 当前时间 | |

### 2.12 退货冲正（`refund`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `refund_no` | TEXT | 是 | 全表唯一；长度 ≤32 | 系统生成 | 退货单号 |
| `transaction_id` | INTEGER | 是 | 外键 → `transaction.id`（关联原单号，`REQ-013`） | 无 | |
| `stall_id` | INTEGER | 是 | 外键 → `stall.id`（`REQ-013` 操作摊位） | 无 | |
| `amount_cents` | INTEGER | 是 | **> 0 且 ≤ 原单金额**；大于原单金额则拒绝（`REQ-028`） | 无 | |
| `idempotency_key` | TEXT | 是 | 长度 ≤64；与 `transaction_id` 联合唯一（`REQ-013` **只冲减一次**） | 无 | 重复提交被唯一约束拦下 |
| `refunded_at` | TEXT | 是 | ISO-8601（`REQ-013`） | 当前时间 | |
| `operator` | TEXT | 是 | 长度 ≤32（`REQ-013` 操作摊位/操作人） | 无 | |

> `refund` **不存**顾客身份信息（`spec.md` §5 边界：退货记录只含时间、金额、关联原单号、操作摊位）。

### 2.13 离线暂存队列（`offline_queue`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `client_idempotency_key` | TEXT | 是 | 全表唯一；长度 ≤64（`REQ-015` 补传幂等键） | 无 | |
| `stall_id` | INTEGER | 是 | 外键 → `stall.id`（`REQ-032`） | 无 | |
| `business_date` | TEXT | 是 | `YYYY-MM-DD` | 无 | 离线期间所属营业日 |
| `payload_json` | TEXT | 否 | 交易载荷快照（商品行、重量、单价、金额、收款方式）；**不得含身份证号/银行卡号**（`REQ-024`）；补传成功后**置为 NULL**（`NFR-013`） | 无 | 本地副本本体 |
| `status` | TEXT | 是 | 状态机枚举，见 §4.3（`REQ-015`） | `staged` | |
| `staged_at` | TEXT | 是 | ISO-8601（`REQ-014`、`REQ-016` 阈值判断依据） | 当前时间 | |
| `synced_at` | TEXT | 否 | ISO-8601；补传成功时写入（`REQ-015`） | NULL | |
| `purged_at` | TEXT | 否 | ISO-8601；**补传成功且本地副本清除完成时写入**（`NFR-013`） | NULL | 清除动作的可核对证据 |
| `over_threshold_notified` | INTEGER | 是 | 0/1；达告警阈值时置 1，**不阻断新交易**（`REQ-016`/`NFR-014`） | 0 | |
| `discard_reason` | TEXT | 否 | 枚举：`duplicate_idempotency_key`（`spec.md` §5：补传键重复时丢弃该条并写审计日志，不报错阻断后续补传） | NULL | |

> **暂存写入失败（磁盘不足等）不落任何行**：按 `REQ-030` 直接向摊主报错，且不得把该笔标记为已成功记账；
> 同时写一条 `audit_log`（`event_type = staging_write_failed`）。

### 2.14 佣金口径（`commission_rule`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `pay_object` | TEXT | 是 | 枚举：`merchant`（向商户收）/ `customer`（向顾客收）/ `market`（市场自留）（`REQ-017` 收费对象） | 无 | |
| `rate_bp` | INTEGER | 是 | 1~10000（**万分比**，避免浮点）（`REQ-017` 费率） | 无 | 费率 |
| `category_tier` | TEXT | 否 | 长度 ≤32；为空表示对全部品类生效（`REQ-017` 品种档位） | NULL | 档位 |
| `effective_from` | TEXT | 是 | `YYYY-MM-DD`（`REQ-017`、`REQ-018` 按日重算） | 无 | |
| `effective_to` | TEXT | 否 | `YYYY-MM-DD`；为空表示长期有效 | NULL | |
| `created_at` | TEXT | 是 | ISO-8601 | 当前时间 | 佣金按**实收金额**计算（`REQ-017`），不按标价 |
| `market_id` | INTEGER | 是 | 市场归属；`NOT NULL DEFAULT 1`（既有行由 SQLite 就地回填默认市场）；**无 `REFERENCES`**，原因见 §2.20 注 | 1 | 所属市场（`REQ-035`） |

### 2.15 日聚合（`daily_aggregate`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `stall_id` | INTEGER | 是 | 外键 → `stall.id`；与 `business_date`、`revision` 联合唯一（`REQ-018`） | 无 | |
| `business_date` | TEXT | 是 | `YYYY-MM-DD`（`REQ-018`） | 无 | |
| `revision` | INTEGER | 是 | ≥1，逐次重算 +1（`REQ-018` 退货冲正后按需重算） | 1 | 旧版本保留，可追溯 |
| `is_current` | INTEGER | 是 | 0/1；同一摊位同一营业日**有且仅有一条为 1**（实现便利） | 1 | 供看板与结算取当前口径 |
| `txn_count` | INTEGER | 是 | ≥0（`REQ-021`） | 0 | |
| `gross_amount_cents` | INTEGER | 是 | ≥0（`REQ-018`） | 0 | 交易总额 |
| `refund_amount_cents` | INTEGER | 是 | ≥0（`REQ-013` 退货后同步减少） | 0 | 退货冲正额 |
| `commission_amount_cents` | INTEGER | 是 | ≥0（`REQ-017`、`REQ-018`） | 0 | 佣金 |
| `recomputed_at` | TEXT | 是 | ISO-8601 | 当前时间 | |
| `market_id` | INTEGER | 是 | 市场归属；`NOT NULL DEFAULT 1`（既有行由 SQLite 就地回填默认市场）；**无 `REFERENCES`**，原因见 §2.20 注 | 1 | 所属市场（`REQ-035`） |

### 2.16 结算单（`settlement`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `settlement_no` | TEXT | 是 | 全表唯一；长度 ≤32 | 系统生成 | |
| `stall_id` | INTEGER | 是 | 外键 → `stall.id`（`REQ-019`） | 无 | |
| `period_start` | TEXT | 是 | `YYYY-MM-DD` ≤ `period_end`（`REQ-019`） | 无 | |
| `period_end` | TEXT | 是 | `YYYY-MM-DD`（`REQ-019`） | 无 | |
| `gross_amount_cents` | INTEGER | 是 | ≥0；等于期内各 `daily_aggregate` 之和（`REQ-019`、`AC-023`） | 无 | |
| `commission_amount_cents` | INTEGER | 是 | ≥0；同上（`AC-023`） | 无 | |
| `version` | INTEGER | 是 | ≥1；重算生成新行、旧行保留（`Q-10` 可追溯、不可物理删除） | 1 | |
| `generated_at` | TEXT | 是 | ISO-8601 | 当前时间 | |
| `market_id` | INTEGER | 是 | 市场归属；`NOT NULL DEFAULT 1`（既有行由 SQLite 就地回填默认市场）；**无 `REFERENCES`**，原因见 §2.20 注 | 1 | 所属市场（`REQ-035`） |

### 2.17 摊位信用档案（`stall_credit`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `stall_id` | INTEGER | 是 | 外键 → `stall.id`；全表唯一（每摊位一份档案）（`REQ-008`） | 无 | |
| `period_start` | TEXT | 是 | `YYYY-MM-DD`（`REQ-008`） | 无 | 统计期起 |
| `period_end` | TEXT | 是 | `YYYY-MM-DD`（`REQ-008`） | 无 | 统计期止 |
| `item_count` | INTEGER | 是 | ≥0（`REQ-008` 分母） | 0 | 期内交易明细总数 |
| `price_changed_count` | INTEGER | 是 | ≥0；`price_changed = 1` 的明细数（`REQ-008` 改价**计入**） | 0 | |
| `price_consistency_bp` | INTEGER | 是 | 0~10000（万分比）：`round((item_count − price_changed_count) ÷ item_count × 10000)`；`item_count = 0` 时取 10000（`REQ-008`；**抹零不计入**，见 §4.3 注） | 10000 | 标价一致率 |
| `computed_at` | TEXT | 是 | ISO-8601；随日聚合一并重算（`REQ-018`） | 当前时间 | |

> 「标价一致率」的**计算口径是实现层定义**（依据 `REQ-008`「改价计入、抹零不计入」）。
> 若业务方另有加权口径，**必须先在 `spec.md` 立项再改本表**（禁止在实现里私改口径）。

### 2.18 审计日志（`audit_log`）—— 只增不改

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `event_type` | TEXT | 是 | 枚举：`price_change` / `refund_applied` / `refund_duplicate_hit` / `offline_backfilled` / `offline_duplicate_discarded` / `payment_callback_duplicate_hit` / `staging_write_failed` / `offline_threshold_warned` / `commission_rule_changed` / `scale_amount_mismatch` / `transaction_confirmed` / `transaction_cancelled`（`NFR-009`、`REQ-007`、`REQ-013`、`REQ-015`、`REQ-016`、`REQ-029`、`REQ-030`、`REQ-038`/`AC-030`、`REQ-054`/`AC-044`、`REQ-055`/`AC-045`） | 无 | 事件类型；`scale_amount_mismatch` 由 `0004_audit_event_types.sql` 扩入白名单，载荷含设备号、幂等键、秤端上报金额、中台重算金额与逐行差异（契约 `scale-midplatform.md` §5 第 4 条）；**末两个值**（`transaction_confirmed` / `transaction_cancelled`）由 `0005_demo_console.sql` 扩入白名单，分别对应「确认即支付」与「取消即撤销」的留痕（契约 `rest-api.md` §3.35、§3.36） |
| `stall_id` | INTEGER | 否 | 外键 → `stall.id`；操作摊位（`REQ-007`） | NULL | |
| `ref_table` | TEXT | 是 | 枚举：`transaction` / `transaction_item` / `payment` / `refund` / `offline_queue` / `commission_rule`（`NFR-009` 可追溯） | 无 | |
| `ref_id` | INTEGER | 是 | 被引用行的主键 | 无 | |
| `payload_json` | TEXT | 是 | 事件载荷：改价留痕含**原价、改后价、改价时间、操作摊位**（`REQ-007`）；**不得含身份证号/银行卡号**（`REQ-024`） | 无 | |
| `actor` | TEXT | 是 | 长度 ≤32（操作人/摊位标识）（`REQ-007`、`REQ-010`） | 无 | |
| `occurred_at` | TEXT | 是 | ISO-8601（`REQ-007` 改价时间） | 当前时间 | |

> **只增不改由数据库强制**：迁移脚本中建立触发器 `audit_log_no_update` / `audit_log_no_delete`，
> 对 `audit_log` 的 `UPDATE` / `DELETE` 直接 `RAISE(ABORT, ...)`；应用层不提供任何修改接口（`NFR-009`、宪法 §4）。
>
> **扩展 `event_type` 白名单须重建本表**（`0004_audit_event_types.sql` 与 `0005_demo_console.sql` 各一次，`AC-030`/`AC-044`）：SQLite 改不了 CHECK 约束，
> 只能「建新表 → 逐行搬移（含主键）→ `DROP` 旧表 → 换名 → 原样重建两个触发器与 §6 的 `ix_audit_ref` / `ix_audit_stall_time`」。
> **顺序是硬要求**：`DROP TABLE` 会连带删掉两条索引，换名前建会同名撞车、换名后不建即静默丢索引；
> 且**不得放宽任何既有约束**（`actor` 长度、`ref_id` / `payload_json` 的 `NOT NULL`、`ref_table` 白名单逐字段照抄）。
>
> **为什么"催缴已发送"不在这里（`2026-10-08` 裁定）**：`REQ-056` 的催缴短信**已有自己的留痕表** `payment_reminder`（§2.22）——
> 记录脱敏手机号、应缴金额、渠道与 `sent_at`，契约 `rest-api.md` §3.37 的"发送动作落表留痕"指的就是它。
> 一度设计过 `event_type = 'payment_reminder_sent'`，**已撤销**，两条理由：
> ① 写它需要把 `ref_table` 白名单加上 `payment_reminder`，而那属于**放宽**本表约束 —— 正是上一段明文禁止的动作
>（`phone_masked` 的 `LIKE '%****%'` 是**收紧**，不受此限）；
> ② `audit_log` 是**资金链路**只增不改留痕（宪法 §4），催缴不是资金动作，把它塞进来会让"资金链路审计"这个定位变模糊。
> **故本表的 `ref_table` 白名单逐字段照抄、一个取值都不增**；将来若确要让某类动作进本表，须先改本段规则并说明该取值指向的真实表。

### 2.19 迁移执行记录（`schema_migration`，基础设施表）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `filename` | TEXT | 是 | 全表唯一；`app/migrations/` 下的文件名（实现便利） | 无 | 已执行的迁移脚本 |
| `applied_at` | TEXT | 是 | ISO-8601 | 当前时间 | |

> 本表无业务语义，只保证启动时**按序补齐且不重复执行**（`AC-013` 一条启动命令）。

### 2.20 市场（`market`）—— 形态 2 新增（`REQ-035`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | 市场（租户）主键；默认市场**显式为 `1`**，与各表 `market_id` 的 `DEFAULT 1` 对齐 |
| `market_code` | TEXT | 是 | 全表唯一：`ux_market_code` = (`market_code`)；长度 1~16（`REQ-035`） | 无 | 市场编码；契约 `scale-midplatform.md` §3.1 响应体的 `market.market_code` 即本列 |
| `name` | TEXT | 是 | 长度 1~50（`REQ-035`） | 无 | 市场名（契约 §3.1 的 `market.name`） |
| `status` | TEXT | 是 | 枚举：`active` / `inactive`（`0002_market_scope.sql` 的 CHECK） | `active` | 市场停用后其设备激活被拒（`app/domain/device.py`） |

> **市场维度是本期的已落地维度，不是预留**（`REQ-035`）。**带 `market_id` 的是这 8 张表**：
> `merchant` / `stall` / `category` / `product` / `price_item` / `commission_rule` / `daily_aggregate` / `settlement`
> （前 7 张即 `REQ-035` 点名的归属对象；`category` 是第 8 张 —— `0002_market_scope.sql` 把 `ux_category_code` 改为
> **市场内唯一**，品类编码只在市场内唯一，故必须记录归属）。其余表**不带 `market_id`**，其市场归属经既有外键链传递
> （业务表经 `stall`，`payment_reminder` 经 `merchant`；`REQ-035` 未点名它们，故不为此扩列）。
>
> **`market_id` 一律不带 `REFERENCES`（有意为之，不得擅自补外键）**：SQLite 明确禁止
> `ALTER TABLE ... ADD COLUMN` 添加「带非 NULL 默认值的外键列」（实测报错
> `Cannot add a REFERENCES column with non-NULL default value`，`PRAGMA foreign_keys = ON`）。
> 故这 8 列的完整性由「**默认市场行必然存在**」（本表由 `0002` 写入 `id = 1`）+ **应用层归属校验**保证，
> 而不是外键；`NOT NULL DEFAULT 1` 同时完成**回填** —— SQLite 给既有行填默认值，既有单市场数据全部落到市场 1。
>
> **本期只装载一个市场**（`REQ-035`）：默认市场为 `id = 1` / `market_code = 'M-0001'` / `name = '示例菜市场'` / `status = 'active'`。
> 装载第二个市场是**纯 DML**（`INSERT` 一行市场 + 带 `market_id` 插业务行），**不需要任何 DDL** —— `AC-026` 以此取证。
>
> **本表不含任何 `*_at` 列（有意为之，不得擅自补）**：默认市场行由迁移写入，而迁移取不到 `REQ-033` 的注入时钟；
> 若加一个由 `datetime('now','localtime')` 填充的 `created_at`，就会新增一处 `T-SIM-00` 已钉死的时钟缺口
> （`tests/unit/test_clock_guard.py` 的枚举集合会变红）。本表没有任何 `REQ` / `AC` / 契约端点需要创建时间。

### 2.21 设备（`device`）—— 形态 2 新增（`REQ-036`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `device_id` | TEXT | 是 | 全表唯一：`ux_device_id` = (`device_id`)；长度 1~32（`REQ-036`） | 无 | 设备标识（出厂烧录；契约 §3.1 请求体的 `device_id`） |
| `token_digest` | TEXT | 是 | **长度必须 = 64**（sha256 十六进制）；**明文令牌绝不入库**（`REQ-036`、`NFR-012`、`RL-5` 同口径） | 无 | 设备令牌摘要；`length = 64` 的 CHECK 是数据库层最后一道闸门 |
| `market_id` | INTEGER | 是 | ≥1；**无 `REFERENCES`**，原因见 §2.20 注 | 无 | 授权范围的市场侧（`REQ-036`） |
| `stall_id` | INTEGER | 是 | ≥1；**无 `REFERENCES`**，原因见 §2.20 注 | 无 | 授权范围的摊位侧（`REQ-036`） |
| `firmware_version` | TEXT | 否 | 长度 ≤32（契约 §3.1 请求体） | NULL | 固件版本 |
| `hardware_rev` | TEXT | 否 | 长度 ≤32（契约 §3.1 请求体） | NULL | 硬件版本 |
| `status` | TEXT | 是 | 枚举：`registered`（预注册待激活）/ `active`（已激活）/ `disabled`（运维解绑，`MT-2005` 的处置路径） | `registered` | 取值由 `0003_device.sql` 的 CHECK 约束 |
| `last_heartbeat_at` | TEXT | 否 | ISO-8601；NULL = 尚未心跳（与「心跳过」可区分） | NULL | 心跳时间（契约 §3.2） |
| `created_at` | TEXT | 是 | ISO-8601；**不设 SQL 默认值**，由应用层经 `app/db.py::now_iso()`（`REQ-033` 时钟接缝）写入 | 无 | 见下方注 |
| `activated_at` | TEXT | 否 | ISO-8601；首次激活时写入；**重复激活不改绑定、也不刷新本列**（契约 §3.1） | NULL | 首次激活时间 |

> **`*_at` 列不设 SQL 默认值（有意为之）**：设备行由应用层写入（`app/domain/device.py`），
> `created_at` 若交给 `datetime('now','localtime')` 就又多一处注入时钟管不到的缺口
> （`T-SIM-00` 已把这类缺口枚举钉死在 `tests/unit/test_clock_guard.py`）。
>
> **(`market_id`, `stall_id`) 不设唯一约束（有意为之）**：绑定唯一性由 `ux_device_id` 保证
> （一台设备只有一个绑定），但一个摊位**允许有替换机 / 备用机** —— 把摊位也做成唯一会在「换秤」时
> 把新设备直接挡在库外（`IntegrityError` → 500），而 `REQ-036` 与契约都没有要求「一个摊位只能有一台设备」。
>
> **授权只来自本表**：`market_id` / `stall_id` 是设备注册时的绑定，即该设备的授权范围；
> 请求体或路径里的 `stall_id` / `market_id` **一律不采信**（契约 §1 第 6 条、`REQ-032`）。

### 2.22 催缴短信记录（`payment_reminder`）—— 演示控制台新增（`REQ-056`）

| 字段 | 类型 | 必填 | 校验规则（来源） | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | INTEGER | 是 | 主键自增 | 无 | |
| `merchant_id` | INTEGER | 是 | 外键 → `merchant.id`（`REQ-056` 向商家催缴） | 无 | 收到催缴的商家 |
| `business_date` | TEXT | 是 | `YYYY-MM-DD`（`REQ-056`、`AC-046` 按营业日算应缴） | 无 | 本次催缴对应的营业日 |
| `payable_cents` | INTEGER | 是 | ≥0；= 该商家当日**应缴金额**：按 `REQ-017` 佣金口径对其**实收金额**逐笔计算之和，**已取消交易不计入**（`REQ-056`、`AC-046`） | 无 | 当次提醒的应缴金额 |
| `phone_masked` | TEXT | 是 | 长度 ≤32，且**必须含 `****`**（`0005_demo_console.sql` 用 `LIKE '%****%'` 在库层强制 —— 与 `stall.payment_receiver_token` 同一道闸门）；**只存脱敏值**，**不得存完整手机号**（`REQ-024`、`NFR-012`） | 无 | 发送目标的脱敏手机号 |
| `channel` | TEXT | 是 | 枚举：`sms`（本期只做短信，`CHECK (channel IN ('sms'))`）（`REQ-056`） | `sms` | 发送渠道 |
| `sent_at` | TEXT | 是 | ISO-8601；**每隔一天**的间隔判据取自本列（`REQ-056`、契约 `rest-api.md` §3.37 的 `interval_days = 2`） | SQLite 求值 `datetime('now', 'localtime')` | 发送时间；见下方注 |

> **本表是「发送动作的留痕」，不是短信网关**：`spec.md` §5 边界明确本期**不接真实短信网关**，发送动作**落表**、页面据此展示"已发送"（契约 §3.37）。
> 契约响应里的 `skipped`（`nothing_payable` / `interval_not_elapsed`）是**本次调用的结果说明**，本文件不为它登记持久列。
>
> **「每隔一天」的判据 = 本表按 (`merchant_id`, `sent_at`) 取该商家最近一次发送**（`ix_reminder_merchant_time` 正是为此建的索引，§6）。
>
> **手机号只存脱敏值**：`merchant.phone` 存原文（`REQ-056` 发送用），本表**一律掩码**（`REQ-024`、`NFR-012`）—— 与 `stall.payment_receiver_token` 同一条纪律。
>
> **`sent_at` 带 SQL 默认值**（`DEFAULT (datetime('now', 'localtime'))`）：该默认值只是**兜底** —— 应用层（`app/domain/notify.py`）**一律显式写入** `app/db.py::now_iso()`（`REQ-033` 时钟接缝），
> 故本列**不进** `T-SIM-00` 的墙钟缺口枚举（`tests/unit/test_clock_guard.py` 钉住的是"应用层未显式赋值"的那些 SQL 默认列）；**不得**改为依赖该 DEFAULT 取时间。
>
> **本表无 `market_id`**：市场归属经 `merchant.market_id` 传递（口径同 §2.20 注）。

## 3. 实体关系

- `merchant` 1 — N `stall`：`stall.merchant_id` → `merchant.id`。
- `stall` 1 — N `stall_session`：`stall_session.stall_id` → `stall.id`（秤端会话绑定摊位）。
- `stall` 1 — N `stall_category_alias`：`stall_category_alias.stall_id` → `stall.id`。
- `category` 1 — N `stall_category_alias`：`stall_category_alias.category_id` → `category.id`（别名 → 标准品类）。
- `category` 1 — N `product`、`category` 1 — N `transaction_item`：记账口径统一落在**标准品类**上（`REQ-002`）。
- `stall` 1 — N `product`：`product.stall_id` → `stall.id`。
- `product` 1 — N `price_item`：`price_item.product_id` → `product.id`（按营业日的价目表）。
- `stall` 1 — N `transaction`：`transaction.stall_id` → `stall.id`。
- `transaction` 1 — N `transaction_item`：`transaction_item.transaction_id` → `transaction.id`。
- `transaction` 1 — N `payment`：`payment.transaction_id` → `transaction.id`；**成功流水最多一条**（`REQ-026`）。
- `payment` 1 — N `payment_callback_log`：`payment_callback_log.payment_id` → `payment.id`（幂等命中时同一次回调指向已存在流水）。
- `transaction` 1 — N `refund`：`refund.transaction_id` → `transaction.id`；同一交易同一幂等键最多一条（`REQ-013`）。
- `stall` 1 — N `offline_queue`：`offline_queue.stall_id` → `stall.id`。
- `commission_rule` 与 `daily_aggregate`：按 `business_date` 落在 `effective_from`~`effective_to` 区间匹配（无外键，口径可随时间变化）。
- `stall` 1 — N `daily_aggregate`：`daily_aggregate.stall_id` → `stall.id`（按营业日多版本）。
- `stall` 1 — N `settlement`：`settlement.stall_id` → `stall.id`；结算单 = 期内 `is_current = 1` 的日聚合之和。
- `stall` 1 — 1 `stall_credit`：信用档案为摊位维度单份。
- `audit_log` 通过 `ref_table` + `ref_id` **弱引用**上述表（不建外键：留痕不得因业务行删除而受限，`NFR-009`）。
- `market` 1 — N `merchant` / `stall` / `category` / `product` / `price_item` / `commission_rule` / `daily_aggregate` / `settlement`：各表 `market_id` **弱引用** `market.id`（**不建外键**，原因见 §2.20 注）；`AC-026` 的「两市场互不可见、互不串账」要求每条读路径都按 `market_id` 过滤。
- `market` 1 — N `device`、`stall` 1 — N `device`：`device` 的 (`market_id`, `stall_id`) 是注册时的绑定、即该设备的授权范围（`REQ-036`）；**不设唯一约束**（允许替换机 / 备用机，见 §2.21 注），绑定唯一性由 `ux_device_id` 保证。
- `merchant` 1 — N `payment_reminder`：`payment_reminder.merchant_id` → `merchant.id`（催缴短信按**商家**发送与去重，不是按摊位）。
- **无顾客实体**：系统不采集可识别顾客身份的信息（`spec.md` §5 边界，`REQ-023`）。

## 4. 状态机

> **不在流转表内的流转一律非法**，由契约层返回 `MT-1001`（非法状态流转），错误码定义见 [contracts/rest-api.md](./contracts/rest-api.md)。

### 4.1 实体：`transaction`

**状态定义**

| 状态枚举值 | 中文名 | 含义 | 是否终态 |
| --- | --- | --- | --- |
| `priced` | 已计价待收款 | 称重计价完成，等待摊主确认收款方式 | 否 |
| `payment_failed` | 支付失败或超时 | 收款码回调为失败/超时，可重试或改记现金 | 否 |
| `paid` | 已收款 | 已落库并展示凭证（`REQ-012`） | 否（可被退货冲正） |
| `refunded` | 已冲正 | 已退货冲正（`REQ-013`） | 是 |
| `cancelled` | 已取消 | 计价后**未确认收款**即被撤销；**账上等同于未发生**（`REQ-055`） | 是 |

**流转表**

| 当前状态 | 事件（触发操作/端点） | 目标状态 | 前置条件 | 副作用 |
| --- | --- | --- | --- | --- |
| （无） | 计价完成（秤端计价端点） | `priced` | 重量 >0 且 ≤50 公斤（`REQ-027`）；价目表项存在 | 生成 `transaction_no`；写 `transaction_item`；写 `audit_log`（若发生改价则 `event_type = price_change`） |
| `priced` | 现金收款确认（收款端点，`method = cash`） | `paid` | 同摊位会话有效（`REQ-032`） | 写 `payment`（`method = cash`、含确认时间与操作人）；写 `received_amount_cents` |
| `priced` | 收款码回调 `success`（Mock 回调端点） | `paid` | 回调单号未被使用（`REQ-029`） | 写 `payment`（`method = qr`、`status = success`）；写回调记录；写 `received_amount_cents`；**先落库再展示凭证**（`REQ-012`） |
| `priced` | 收款码回调 `failed` / `timeout` | `payment_failed` | 同上 | 写 `payment`（`status = failed` / `timeout`）；写回调记录；摊主可重试或改记现金 |
| `payment_failed` | 改记现金（收款端点，`method = cash`） | `paid` | 该交易尚**无成功流水**（`REQ-026`：只允许一条交易记录与一次佣金） | 写新的 `payment` 行（`cash`）；失败的 `payment` 行保留作留痕 |
| `payment_failed` | 重试收款码成功（回调端点 `success`） | `paid` | 同上 | 同上；`callback_no` 不得与既有成功流水重复（`REQ-029`） |
| `paid` | 退货冲正（退货端点） | `refunded` | 退货金额 >0 且 ≤ 原单金额（`REQ-028`）；幂等键未用过（`REQ-013`） | 写 `refund`；触发相关营业日 `daily_aggregate` 重算（`REQ-018`）；写 `audit_log`（`refund_applied`） |
| `refunded` | 再次提交同一次退货 | `refunded`（不变） | 幂等键已存在 | **只冲减一次**：不改金额，写 `audit_log`（`refund_duplicate_hit`），返回成功语义（`REQ-013`） |
| `priced` | 取消（演示控制台端点 `POST /api/demo/transactions/{transaction_no}/cancel`） | `cancelled` | **尚无成功支付流水**（已 `paid` 的交易**不得**取消 ⇒ `MT-1001`）；幂等键未用过 | 交易行**保留**、`status` 置 `cancelled`（**不是 `DELETE`**）；写 `audit_log`（`transaction_cancelled`）留痕；该笔**从一切聚合与应缴中排除**（`REQ-055`、`AC-045`） |
| `cancelled` | 再次提交同一次取消 | `cancelled`（不变） | 幂等键已存在 | **只生效一次**：不改结果、**不重复留痕**，返回成功语义（`REQ-055`、`AC-045`） |

> **取消不是 `DELETE`，也不是退货**（两者不得混用）：
> ① **行保留** —— "账上等同于未发生"**不等于**证据消失：只改 `status`，且**动作本身必须留痕**（`NFR-009` 审计只增不改、`RL-9` 不得静默丢弃）；
> ② **退货是另一条路** —— 它作用于**已收款**的交易，写 `refund` 表并触发相关营业日 `daily_aggregate` 重算（见上表 `paid → refunded` 行，`REQ-013`/`REQ-028`）。取消与退货**不共用状态、不共用表、不共用端点**。
>
> **`paid` 是取消的硬边界**：已确认收款的交易不得取消 ⇒ `MT-1001`(409)（契约 `rest-api.md` §3.35）。
>
> **排除口径**：`status = 'cancelled'` 的交易**不计入**应缴（`REQ-056`）、**不计入** §5.1 的三项派生指标、也**不进** §2.15 日聚合的 `txn_count` / `gross_amount_cents` ——
> 即取消后"金额与笔数回到取消前"（`REQ-055`、`AC-045`）。

### 4.2 实体：`payment`

**状态定义**

| 状态枚举值 | 中文名 | 含义 | 是否终态 |
| --- | --- | --- | --- |
| `pending` | 待结果 | 收款码已生成、等待回调 | 否 |
| `success` | 成功 | 到账确认（现金由 `confirmed_at` 表达） | 是 |
| `failed` | 失败 | 回调返回失败（`REQ-011`） | 是 |
| `timeout` | 超时 | 回调超时（`REQ-011`） | 是 |

**流转表**

| 当前状态 | 事件（触发操作/端点） | 目标状态 | 前置条件 | 副作用 |
| --- | --- | --- | --- | --- |
| （无） | 摊主选择现金（收款端点） | `success` | 同摊位会话有效 | 写 `confirmed_at`、`operator`（`REQ-010`） |
| （无） | 摊主选择收款码（收款端点） | `pending` | 交易为 `priced` 或 `payment_failed` | 生成收款码（基于 `stall.payment_receiver_token` 脱敏标识） |
| `pending` | 回调 `success` | `success` | `callback_no` 未被使用 | 写 `callback_no`；驱动交易转 `paid`（`REQ-012`） |
| `pending` | 回调 `failed` | `failed` | 回调单号未被使用 | 驱动交易转 `payment_failed` |
| `pending` | 回调 `timeout` | `timeout` | 回调单号未被使用 | 驱动交易转 `payment_failed` |
| `success` | 同一次回调重复送达 | `success`（不变） | `callback_no` 已存在 | 写 `payment_callback_log`（`is_duplicate = 1`）+ `audit_log`（`payment_callback_duplicate_hit`）；**不重复记账**（`REQ-029`、`AC-010`） |
| `failed` / `timeout` | 摊主改记现金 | `success`（写入**新的** `payment` 行） | 交易尚无成功流水（`REQ-026`） | 见 §4.1 `payment_failed` → `paid` 行 |

### 4.3 实体：`offline_queue`

**状态定义**

| 状态枚举值 | 中文名 | 含义 | 是否终态 |
| --- | --- | --- | --- |
| `staged` | 已暂存待补传 | 断网期间交易已本地持久化（`REQ-014`） | 否 |
| `backfilled` | 已补传 | 补传成功，`payload_json` 置空且写入 `purged_at`（`REQ-015`、`NFR-013`） | 是 |
| `duplicate_discarded` | 幂等键重复已丢弃 | 补传键与既有交易重复，丢弃该条并写审计日志，**不报错阻断后续补传**（`spec.md` §5） | 是 |

**流转表**

| 当前状态 | 事件（触发操作/端点） | 目标状态 | 前置条件 | 副作用 |
| --- | --- | --- | --- | --- |
| （无） | 断网期间完成一笔交易 | `staged` | 暂存写入成功 | 写 `payload_json`、`staged_at`；若条数达告警阈值则置 `over_threshold_notified = 1` 并写 `audit_log`（`offline_threshold_warned`），**仍继续接受新交易**（`REQ-016`/`NFR-014`） |
| （无） | 暂存写入失败（磁盘不足等） | （不落行） | — | 向摊主明确报错，**不得标记为已成功记账**；写 `audit_log`（`staging_write_failed`）（`REQ-030`） |
| `staged` | 网络恢复自动补传（补传端点） | `backfilled` | 幂等键在 `transaction` 中不存在 | 创建 `transaction`（`origin = backfilled`）与明细、`payment`；置 `payload_json = NULL`、写 `synced_at` 与 `purged_at`（`NFR-013`）；写 `audit_log`（`offline_backfilled`） |
| `staged` | 网络恢复自动补传（补传端点） | `duplicate_discarded` | 幂等键已存在 | 丢弃该条、写 `discard_reason`、写 `audit_log`（`offline_duplicate_discarded`）；**不报错、不阻断后续补传**（`REQ-015`） |

> **取整与口径的两条实现层定义**（spec 未规定，此处明确以免实现发散；如需变更须先在 `spec.md` 立项）：
> ① 计价取整：`round(单价(分/公斤) × 重量(克) ÷ 1000)`，四舍五入到分（`REQ-005` 只规定"保留两位小数"）；
> ② 「标价一致率」公式见 §2.17，依据 `REQ-008` 的"改价计入、抹零不计入"。
>
> **离线状态的界面可见性与显式切换**（`REQ-014`）由秤端前端状态与后端 `offline_queue` 计数共同驱动，**不落表**。

### 4.4 实体：`settlement`

**状态定义**

| 状态枚举值 | 中文名 | 含义 | 是否终态 |
| --- | --- | --- | --- |
| `generated` | 已生成 | 依据当前有效的日聚合生成 | 是（重算产生新 `version` 行，旧行保留） |

**流转表**

| 当前状态 | 事件（触发操作/端点） | 目标状态 | 前置条件 | 副作用 |
| --- | --- | --- | --- | --- |
| （无） | 生成结算单（运营端结算端点） | `generated` | 期内 `daily_aggregate.is_current = 1` 的行全部存在（`REQ-019`） | 写 `settlement`（`version = 1`） |
| `generated` | 退货冲正后重算 | 新行 `generated`（`version + 1`） | 期内日聚合已重算（`REQ-018`） | 旧行**保留不改**（可追溯、不可物理删除，`Q-10`） |

### 4.5 无状态实体（不设状态机）

| 实体 | 说明 |
| --- | --- |
| `merchant` / `stall` / `stall_session` / `category` / `stall_category_alias` / `product` / `price_item` | 用 `status` 或 `is_active` 字段表达启停，无流转约束 |
| `market` / `device` | 用 `status` 表达启停；`device.status` 的取值（`registered` / `active` / `disabled`）由 `0003_device.sql` 的 CHECK 约束，**本期不设 §4 流转表**（激活 / 解绑的处置见契约 `scale-midplatform.md` §3.1 与 §4 的 `MT-2005`） |
| `transaction_item` / `payment_callback_log` / `refund` / `audit_log` / `payment_reminder` | 写入即终态的事实行（`audit_log` 由触发器禁止修改；`payment_reminder` 是"发送动作"的留痕，无流转） |
| `daily_aggregate` | 可重算快照：用 `revision` + `is_current` 表达版本，无状态流转 |
| `commission_rule` | 用 `effective_from` / `effective_to` 表达有效期，无状态流转 |
| `stall_credit` | 派生档案：随日聚合重算覆盖更新 |
| `schema_migration` | 基础设施表：写入即终态 |

## 5. 数据量级与增长预估

| 项 | 估算值 | 估算依据 |
| --- | --- | --- |
| 本期用户数 | 总用户 ≤10（摊主 1 / 运营方 1–2 / 顾客 1） | `spec.md` §1.2、`discovery.md` R3 Q2.2 |
| **本期种子数据（演示规模）** | 1 个市场 × **10 摊位** × 每摊位 **20～30 个商品**（本实现取 25）≈ **250 条商品记录**；标准品类 **58 个**（覆盖蔬菜 / 水果 / 肉类 / 水产四类）；摊位别名 **1～3 个/标准品类**；价目表 = 摊位 × 商品 × **2 个营业日** = **500 行**（由 `app/seed.py` 从仓库内本地文件 `app/seed_data/seed.json` 导入，**不依赖外网**，`REQ-025`） | `REQ-025`；父代理 `2026-09-30` 在 `T-005` 实现期的**演示规模裁定**（理由与修正注记见下） |
| 本期增量 | 演示期 20–30 笔/日 → **<1000 笔/月**；每笔 1–3 条明细 | `discovery.md` R5 Q3.2 |
| 论证范围的种子规模（**不进本期数据**） | 300 摊位 × 约 50 个商品/摊位 ≈ **1.5 万条商品记录 + 300 条摊位档案** | `discovery.md` `Q2.2`/`Q3.2`（**市场实际规模**）；`spec.md` §1.2「方案论证范围」——**仅文字论证，不进容量目标** |
| 论证范围增量（**推算，非实测**） | 2.4 万–4.5 万笔/日 → 约 73 万–137 万笔/月 → 一年 **880 万–1640 万笔**；明细约 2–3 倍 | `discovery.md` R5 Q3.2 推算链：`Q-9` 待确认假设（单摊位 80–150 笔/日，行业常识未验证）× 300 摊 |
| 文件年增量 | **0**：不打印、不上传，无图片与附件；离线暂存载荷在补传成功后清除（`NFR-013`） | `spec.md` §6 第 7 条（不做非秤类硬件）、`NFR-013` |
| 容量规划结论 | **本期 SQLite 单文件足够**：<1000 笔/月、无文件存储。按论证范围推算的一年 880 万–1640 万笔在行数维度仍可承载，但**并发写入是真正的瓶颈**（嵌入式库单写者锁）——触发重评的阈值与判据见 `docs/adr/0001-单体与嵌入式数据库.md` §5，本文件不复述 | `ADR-0001` §5；`discovery.md` R5 Q3.2 |

> **修正注记（`2026-09-30`，`T-005` 实现期）**：本表原把「1 个市场 × 300 摊位 × 约 50 个商品/摊位 ≈ 1.5 万条商品记录」
> 列为**本期种子数据**，属**范围归属错误** —— 300 摊位是**市场实际规模**（**方案论证范围**，见 `spec.md` §1.2 与
> `discovery.md` `Q2.2` 第 300 行、`Q3.2` 第 461 行「300 摊口径**仅作文档论证**」），**不是本期演示需要的规模**。
> 本期规模由父代理在 `T-005` 实现期裁定为**演示规模**：① 本期是**单机演示**，1.5 万条会让启动与导入变慢、
> 现场找商品困难，**可演示性与现场稳定性优先**；② 300 摊位不进种子数据；③ 但**四大品类 + 别名归一必须有真实样本**
> （否则演示不出本系统「数据不脏」的核心设计），故品类字典给到 58 个并覆盖四类。
> **原记载见 `discovery.md` `Q3.2`（该文件为访谈留痕，原文保留不改，另追加「修正 4」）。**

### 5.1 派生指标口径（**不落表**，随查询实时计算）

`REQ-022`（**2026-09-30 修订版**）的三个使用率指标由 `transaction` / `payment` / `stall` / `product` / `price_item` 现场计算，
**不建指标表**（避免第二份事实来源）。**三项的分子与分母全部来自系统自有数据**，逐项落到具体表与字段：

| 指标（产出为万分比整数 0–10000） | 分子 | 分母 | 来源 |
| --- | --- | --- | --- |
| **摊位使用率** | 当日 `transaction` 中 **distinct `stall_id`** 的数量 | `stall.status = 'active'` 的摊位数 | `REQ-022` / `AC-005`；表见 §2.8 / §2.2 |
| **现金交易占比** | 当日 `payment.method = 'cash'` 且 `status = 'success'` 的笔数 | 当日**全部走秤笔数** = `transaction` 中该 `business_date` 的交易数 | `REQ-022` / `AC-005`；表见 §2.10 / §2.8 |
| **价目表维护率** | 「当日价目表完整且已更新」的摊位数：该摊位**全部 `product.status = 'active'` 的商品**在当日都有 `price_item` 行（`source` 为 `manual` 或 `copied_previous_day` 均算已维护） | `stall.status = 'active'` 的摊位数 | `REQ-022` / `AC-005`；表见 §2.7 / §2.6 / §2.2 |

> **`cancelled` 交易一律排除**（`REQ-055`/`AC-045`：「不计入应缴、不计入看板」）：本表三项指标的**分子与分母**、§2.15 日聚合与 §2.22 的应缴，
> 均只统计**非 `cancelled`** 的交易；取消后金额与笔数须**回到取消前**。取消的状态语义与 `paid` 边界见 §4.1。

> **已废弃指标（本期不做）**：市场口径的「**日均智能秤交易占比**」（走秤笔数 ÷ **市场总交易笔数**）——
> 其分母需要一个**外部基准**（人工盘点 / 抽样统计 / 外部系统），**系统自有数据算不出来**，
> 已被 `2026-09-30` 的 `REQ-022` 修订废弃并转入未决项 `Q-15`。参见 `discovery.md` 「修正 3」与 D-14 修订记录。
> **禁止**在实现里"估算"该分母（如用走秤笔数 ÷ 单摊位日均假设值），那是把推断当事实。
>
> **变更纪律**：这三个口径的分子分母来自 `REQ-022` 修订版，**已是需求**（不再是"实现层定义"）；
> 若业务方要改口径，**必须先改 `spec.md` 再改本表与实现**（否则 `AC-005` 的"第三方可逐一核对"就落空）。

## 6. 索引建议

| 索引名 | 表 | 字段 | 类型 | 服务的查询 / 约束 |
| --- | --- | --- | --- | --- |
| `ux_stall_no` | `stall` | (`market_id`, `stall_no`) | 唯一（**市场内唯一**） | 按摊位查询档案（`REQ-001`、`AC-013` 种子导入幂等）；`REQ-035` 要求市场维度纳入唯一约束 —— 否则第二个市场不能再有 `A-01` 摊位 |
| `ix_stall_merchant` | `stall` | (`merchant_id`) | 普通 | 商户 → 摊位归集 |
| `ux_session_token` | `stall_session` | (`session_token`) | 唯一 | 秤端会话鉴权与摊位绑定（`REQ-032`、`AC-021`） |
| `ux_category_code` | `category` | (`market_id`, `code`) | 唯一（**市场内唯一**） | 品类字典按编码取标准品类（`REQ-002`）；同上 —— 否则第二个市场不能再有 `VEG` 品类编码 |
| `ux_alias_stall_name` | `stall_category_alias` | (`stall_id`, `alias_name`) | 唯一 | 别名 → 标准品类的查表（`REQ-002`、`AC-014`） |
| `ix_product_stall_status` | `product` | (`stall_id`, `status`) | 普通 | 秤端商品图标/快捷键列表（`REQ-004`） |
| `ux_price_stall_date_product` | `price_item` | (`stall_id`, `business_date`, `product_id`) | 唯一 | 计价取价、复制上一营业日价目表（`REQ-003`、`REQ-005`、`AC-015`） |
| `ux_transaction_no` | `transaction` | (`transaction_no`) | 唯一 | 交易号唯一与凭证查询（`REQ-031`、`REQ-012`） |
| `ux_transaction_idem` | `transaction` | (`stall_id`, `client_idempotency_key`) | 唯一 | 离线补传去重兜底（`REQ-015`、`AC-003`） |
| `ix_transaction_stall_date` | `transaction` | (`stall_id`, `business_date`) | 普通 | 日聚合、看板与结算（`REQ-018`、`REQ-021`、`AC-023`） |
| `ix_transaction_item_txn` | `transaction_item` | (`transaction_id`) | 普通 | 交易明细与对账（`REQ-020`） |
| `ix_transaction_item_date_flag` | `transaction_item` | (`created_at`, `price_changed`) | 普通 | 「标价一致率」统计（`REQ-008`、`AC-008`） |
| `ux_payment_no` | `payment` | (`payment_no`) | 唯一 | 支付流水号唯一 |
| `ux_payment_callback_no` | `payment` | (`callback_no`) | 唯一（NULL 可重复） | 支付单号幂等（`REQ-029`、`AC-010`） |
| `ix_payment_txn` | `payment` | (`transaction_id`) | 普通 | 对账等式「订单总额 = 支付流水」（`REQ-020`、`AC-003`） |
| `ix_callback_no` | `payment_callback_log` | (`callback_no`) | 普通 | 幂等命中排查与留痕（`REQ-029`） |
| `ux_refund_idem` | `refund` | (`transaction_id`, `idempotency_key`) | 唯一 | **同一退货只冲减一次**（`REQ-013`、`AC-002`） |
| `ux_offline_idem` | `offline_queue` | (`client_idempotency_key`) | 唯一 | 补传幂等（`REQ-015`） |
| `ix_offline_status_staged` | `offline_queue` | (`status`, `staged_at`) | 普通 | 补传扫描与暂存条数阈值判断（`REQ-014`~`REQ-016`） |
| `ix_commission_effective` | `commission_rule` | (`effective_from`, `effective_to`) | 普通 | 按营业日匹配佣金口径（`REQ-017`） |
| `ux_daily_current` | `daily_aggregate` | (`stall_id`, `business_date`, `revision`) | 唯一 | 日聚合版本唯一；`is_current = 1` 供看板/结算取当前口径（`REQ-018`、`REQ-019`） |
| `ux_settlement_no` | `settlement` | (`settlement_no`) | 唯一 | 结算单查询（`REQ-019`） |
| `ux_stall_credit` | `stall_credit` | (`stall_id`) | 唯一 | 摊位信用档案（对顾客可见）（`REQ-008`） |
| `ix_audit_ref` | `audit_log` | (`ref_table`, `ref_id`) | 普通 | 按业务行回查留痕（`NFR-009`） |
| `ix_audit_stall_time` | `audit_log` | (`stall_id`, `occurred_at`) | 普通 | 摊位维度的留痕与信用展示（`REQ-007`、`REQ-008`） |
| `ux_market_code` | `market` | (`market_code`) | 唯一 | 按市场编码取市场（`REQ-035`；契约 §3.1 的 `market_code` 查表） |
| `ix_merchant_market` | `merchant` | (`market_id`) | 普通 | 市场 → 商户归集（`REQ-035`、`AC-026`） |
| `ix_stall_market` | `stall` | (`market_id`) | 普通 | 市场 → 摊位归集（`REQ-035`、`AC-026`） |
| `ix_category_market` | `category` | (`market_id`) | 普通 | 市场 → 品类字典（`REQ-035`、`AC-026`） |
| `ix_product_market` | `product` | (`market_id`, `status`) | 普通 | 市场内取在营商品（`REQ-004`、`REQ-035`） |
| `ix_price_item_market` | `price_item` | (`market_id`, `business_date`) | 普通 | 市场 × 营业日的价目表（`REQ-003`、`REQ-035`） |
| `ix_commission_market` | `commission_rule` | (`market_id`, `effective_from`) | 普通 | 市场内按生效期匹配佣金口径（`REQ-017`、`REQ-035`） |
| `ix_daily_aggregate_market` | `daily_aggregate` | (`market_id`, `business_date`) | 普通 | 市场 × 营业日的日聚合（`REQ-018`、`REQ-035`） |
| `ix_settlement_market` | `settlement` | (`market_id`, `period_end`) | 普通 | 市场维度的结算单查询（`REQ-019`、`REQ-035`） |
| `ux_device_id` | `device` | (`device_id`) | 唯一 | 设备身份唯一：一台设备只有一个绑定（`REQ-036`、`AC-027`） |
| `ix_device_binding` | `device` | (`market_id`, `stall_id`) | 普通 | 按摊位 / 市场取设备（心跳观测、运营端解绑、`AC-027` 的绑定核对） |
| `ix_reminder_merchant_time` | `payment_reminder` | (`merchant_id`, `sent_at`) | 普通 | 「每隔一天」的间隔判据：取该商家**最近一次**催缴发送时间（`REQ-056`、`AC-046`；契约 §3.37 的 `interval_days = 2`） |

> **形态 2 新增索引 11 条**：市场 1 条（`ux_market_code`）+ 市场维度查询 8 条（`ix_*_market`）+ 设备 2 条
> （`ux_device_id` / `ix_device_binding`）；另 `ux_stall_no` / `ux_category_code` 两条**就地改为市场内唯一**
> （索引名不变、列构成加 `market_id`）。**演示控制台新增 1 条**（`ix_reminder_merchant_time`）。
> 全库实际索引 **37 条**（`0001_init.sql` 的 25 条 + 形态 2 的 11 条 + 演示控制台的 1 条），与本节逐条同名，
> 且 `sqlite_master` 中**无 `sqlite_autoindex_*`**（唯一性一律用具名索引表达，便于机械核对）。
>
> **`ix_audit_ref` / `ix_audit_stall_time` 在重建 `audit_log` 后依然存在**：`0004_audit_event_types.sql` 与
> `0005_demo_console.sql` **两次都**先 `DROP TABLE audit_log` 再换名重建，而 `DROP TABLE` 会连带删掉这两条索引
> —— 故**每个**重建脚本都**必须**在换名之后重新创建它们（不重建即静默丢索引）。
>
> 索引只在上表定义；新增索引须说明它服务哪个查询，禁止「为建而建」。

## 7. 迁移与演进策略

1. **建表方式**：启动时按文件名顺序执行 `app/migrations/*.sql`（`0001_init.sql` 起，当前为 `0001`~`0005`），执行记录写入 `schema_migration` 表；应用每次启动自动补齐，**不需要人工介入**（`AC-013` 一条启动命令）。脚本一律可重跑：`CREATE TABLE / INDEX IF NOT EXISTS`、`DROP ... IF EXISTS`，而 `ALTER TABLE ... ADD COLUMN`（SQLite 无 `IF NOT EXISTS`）由 `app/db.py::apply_migration` **按列存在性跳过** —— 否则脚本执行到一半失败后重跑会撞 `duplicate column name`，应用再也起不来。
2. **变更规则（只增不改）**：新增列必须带默认值或允许 NULL；**不删列**；不修改既有列的类型；重命名走「加新列 → 双写 → 观察一个迭代 → 清理旧列」。`audit_log` 与全部资金留痕表的行**禁止 UPDATE / DELETE**（触发器强制，`NFR-009`）。**唯一的 DDL 例外是改 CHECK 白名单**（`0004` / `0005` 各一次）：`0004_audit_event_types.sql` 与 `0005_demo_console.sql` 为扩展 `audit_log.event_type` 走了「建新表 → 逐行搬移（含主键）→ `DROP` 旧表 → 换名 → 原样重建触发器与 §6 的两条索引」，且**不得放宽任何既有约束**（`actor` 长度、`ref_id` / `payload_json` 的 `NOT NULL`、`ref_table` 白名单逐字段照抄）。
3. **回滚方案**：因只增不改，**回滚 = 回退代码到上一版 + 保留当前数据文件**（旧代码忽略新增列即可继续运行）；演示环境另有更强手段：`scripts/reset_demo.py` 删除数据文件并由种子重建（`NFR-003`/`NFR-004` 的恢复路径）。数据文件按 `data/` 目录存放，按日期复制即可备份（手工，本期不做自动备份，`NFR-004`）。
4. **演进路径**（第 1 项**已落地**，其余为已知项、本期不做，届时按此路径改）：
   - **多市场多租户 —— 已落地为维度**（`REQ-035`，迁移 `0002_market_scope.sql`）：八张业务表带 `market_id`（归属与「无 `REFERENCES`」的原因见 §2.20 注），`stall` / `category` 的唯一约束已纳入 `market_id`（§6）；**本期只装载一个市场**（默认市场 `id = 1`），装载第二个市场是**纯 DML**（`INSERT` 一行市场 + 带 `market_id` 插业务行），**不改表结构** —— `AC-026` 以此取证。**多市场的运营流程**（跨市场权限、跨市场结算与报表）仍在 Out-of-Scope 第 4 条，属对架构叶子的重评，须先改 `docs/adr/`。
   - **离线暂存加密存储**（`Q-7`）：在 `offline_queue.payload_json` 上加加密层，或改为独立加密文件 + 元数据表；**不改列语义**。
   - **迁移到客户端-服务器数据库**：本模型字段类型（`INTEGER`/`TEXT`）可直接映射到 PostgreSQL/MySQL；触发条件见 `docs/adr/0001-单体与嵌入式数据库.md` §5。
   - **财务证据链保留期**（`Q-10`）：年限由甲方财务确认；本模型已按「可追溯、不可物理删除、支持归档」设计（版本行保留 + 禁止物理删除），**年限确认后只需增加归档作业，不改表结构**。
   - **批发 / 团购纸质凭证**（`Q-8`）：若后续开放打印，新增 `print_log` 表，不改 `transaction`。
