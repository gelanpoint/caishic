# market-trade-flow REST API 契约

- 特性目录: specs/market-trade-flow/
- 数据模型: [../data-model.md](../data-model.md)（**字段语义与校验规则的唯一事实来源**，本文件只写传输层约束，不重复定义字段含义）
- 技术方案: [../plan.md](../plan.md)
- Base URL: `/api` ／ 认证方式: **秤端会话 Token**（请求头 `X-Stall-Session`）；**顾客扫码页与运营端本期不做认证**（`NFR-007` 放宽项，批准记录见 `../spec.md` §4.1，对应 Out-of-Scope 第 5 条）
- 日期: 2026-09-30

## 1. 通用约定

1. 请求与响应均为 JSON（UTF-8）；本特性**无文件上传**（不打印、不上传，`spec.md` §6 第 7 条）。
2. 统一错误响应格式：
   `{"error": {"code": "MT-1002", "message": "重量超出允许范围", "detail": {"weight_grams": 52000, "max_grams": 50000}}}`
3. 写操作端点要求请求头 `Idempotency-Key`（长度 ≤64）；服务端据此去重，重复请求返回**首次结果**而非报错（`REQ-015`、`REQ-026`、`REQ-029`）。
4. 时间字段一律 ISO-8601 `YYYY-MM-DD HH:MM:SS`；**金额一律为整数「分」、重量一律为整数「克」**，单位与取整口径见 [../data-model.md](../data-model.md) §0（本文件不复述换算规则）。
5. **响应体中一律不得出现身份证号或银行卡号字段**；收款标识只以脱敏值出现（`REQ-024`、`NFR-012`）。契约测试须对此做扫描断言（`AC-012`）。
6. 秤端端点一律要求 `X-Stall-Session`，且**只允许访问该会话绑定摊位的数据**（`REQ-032`、`AC-021`）。
7. **幂等语义总表**（各端点的具体幂等键）：

| 场景 | 幂等键 | 重复提交的行为 | 来源 |
| --- | --- | --- | --- |
| 创建交易 / 离线暂存 | `Idempotency-Key`（即 `client_idempotency_key`） | 返回首次结果，不新建交易、不重复计佣 | `REQ-015` |
| 退货冲正 | `Idempotency-Key` | 金额与佣金**不再变化**，返回首次结果 | `REQ-013`、`AC-002` |
| 支付回调 | `callback_no`（请求体字段） | 记录 `is_duplicate = true`，**不重复记账** | `REQ-029`、`AC-010` |
| 补传 | `Idempotency-Key` | 幂等键已存在则丢弃该条、写审计日志，**不报错、不阻断后续补传** | `REQ-015` |

## 2. 端点总表

| # | 端点 | 方法 | 说明 | 关联需求 |
| --- | --- | --- | --- | --- |
| 1 | `/healthz` | GET | 存活与就绪检查（本地） | `NFR-010` |
| 2 | `/api/merchant/session` | POST | 秤端绑定摊位并下发会话 Token | `REQ-032`；`NFR-007` |
| 3 | `/api/merchant/price-list` | GET | 查询某营业日的价目表 | `REQ-003` |
| 4 | `/api/merchant/price-list` | POST | 设置/调整价目表（含「复制上一营业日价格」） | `REQ-003`、`AC-015` |
| 5 | `/api/merchant/products` | GET | 秤端商品列表（图标/快捷键，无文本输入） | `REQ-004`、`AC-006` |
| 6 | `/api/merchant/transactions` | POST | 创建交易并计价（选品 → 重量 → 金额） | `REQ-005`、`REQ-006`、`REQ-027`、`AC-001`/`AC-016`/`AC-017` |
| 7 | `/api/merchant/transactions` | GET | 本摊位交易列表 | `REQ-021` |
| 8 | `/api/merchant/transactions/{transaction_no}` | GET | 交易详情与凭证数据（不打印） | `REQ-012`、`AC-001` |
| 9 | `/api/merchant/transactions/{transaction_no}/price-change` | POST | 改价／抹零（留痕） | `REQ-007`、`REQ-008`、`AC-007`/`AC-008` |
| 10 | `/api/merchant/transactions/{transaction_no}/payment` | POST | 确认收款（现金）或生成收款码 | `REQ-009`、`REQ-010`、`REQ-026`、`AC-004`/`AC-009` |
| 11 | `/api/merchant/transactions/{transaction_no}/refund` | POST | 退货冲正（只冲减一次） | `REQ-013`、`REQ-028`、`AC-002` |
| 12 | `/api/merchant/dashboard` | GET | 商户端看板 | `REQ-021` |
| 13 | `/api/merchant/offline/queue` | GET | 离线暂存状态（可视标识 + 阈值告警） | `REQ-014`、`REQ-016` |
| 14 | `/api/merchant/offline/queue` | POST | 离线暂存一笔交易（断网期间） | `REQ-014`、`REQ-030`、`AC-018`/`AC-019` |
| 15 | `/api/merchant/offline/sync` | POST | 触发补传（幂等去重 + 清除本地副本） | `REQ-015`、`NFR-013`、`AC-003` |
| 16 | `/api/mock/scale/reading` | POST | 模拟电子秤读数（可注入故障；演示用） | `REQ-005`、`REQ-027` |
| 17 | `/api/mock/payment/callback` | POST | 模拟支付回调（成功/失败/超时） | `REQ-011`、`REQ-029`、`AC-009`/`AC-010` |
| 18 | `/api/customer/stalls/{stall_no}/profile` | GET | 顾客可见的摊位信息与信用指标 | `REQ-008`、`REQ-023`、`AC-011` |
| 19 | `/api/customer/receipts/{transaction_no}` | GET | 顾客扫码页数据（只含有采集来源的字段） | `REQ-023`、`AC-011` |
| 20 | `/api/admin/categories` | GET | 品类字典与别名映射查询 | `REQ-002` |
| 21 | `/api/admin/categories` | POST | 维护品类字典（新增/停用标准品类） | `REQ-002` |
| 22 | `/api/admin/aliases` | POST | 维护「摊位别名 → 标准品类」映射 | `REQ-002`、`AC-014` |
| 23 | `/api/admin/commission-rules` | GET | 查询佣金口径 | `REQ-017` |
| 24 | `/api/admin/commission-rules` | PUT | 配置佣金口径（收费对象/费率/品种档位） | `REQ-017`、`AC-022` |
| 25 | `/api/admin/dashboard` | GET | 市场方看板 | `REQ-021` |
| 26 | `/api/admin/daily-aggregate` | POST | 日终聚合与按需重算 | `REQ-018` |
| 27 | `/api/admin/settlements` | POST | 生成结算单 | `REQ-019`、`AC-023` |
| 28 | `/api/admin/settlements` | GET | 查询结算单 | `REQ-019`、`AC-023` |
| 29 | `/api/admin/reconciliation` | GET | 对账：核验「订单总额 = 支付流水 = 分账明细」 | `REQ-020`、`AC-003` |
| 30 | `/api/admin/metrics/usage` | GET | 导出**三个**使用率指标（摊位使用率 / 现金交易占比 / 价目表维护率） | `REQ-022`、`AC-005` |
| 31 | `/api/admin/audit-logs` | GET | 查询资金链路留痕（只读，只增不改） | `NFR-009` |

## 3. 端点详情

> 响应字段一律对应 [../data-model.md](../data-model.md) 的对应小节，**语义与校验不在此重复**；本文件只写传输层约束（类型、必填、长度、枚举）。

### 3.1 GET `/healthz`

- 说明: 存活与就绪检查；无需认证。关联: `NFR-010`。
- 请求体: 无。
- 响应(200): `{"status": "ok", "db": "ok", "offline_queue_pending": 0}`。
- 可能错误: 无（不可用时直接返回 503，无错误码）。

### 3.2 POST `/api/merchant/session`

- 说明: 秤端绑定摊位并下发会话 Token。关联: `REQ-032`、`NFR-007`。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验（引用 ../data-model.md） |
| --- | --- | --- | --- | --- |
| `stall_no` | string | 是 | 长度 1~16 | §2.2 `stall.stall_no`；摊位须为 `active` |

- 响应(201): `session_token`（string，16~64）、`stall_no`、`stall_name` —— 对应 §2.3。
- 可能错误: `MT-1009`(404 摊位不存在)、`MT-1008`(422 参数校验失败)。

### 3.3 GET `/api/merchant/price-list`

- 说明: 查询指定营业日的价目表。关联: `REQ-003`。请求头: `X-Stall-Session`（必填）。
- 查询参数: `business_date`（string，必填，`YYYY-MM-DD`）。
- 响应(200): 数组，元素对应 §2.7；无记录时返回**空数组 + `price_list_missing: true`**（供秤端提示先设价）。
- 可能错误: `MT-1005`(401 会话无效)。

### 3.4 POST `/api/merchant/price-list`

- 说明: 设置或逐条调整价目表；带 `copy_from_previous_day: true` 时复制上一营业日价格。关联: `REQ-003`、`AC-015`。请求头: `X-Stall-Session`（必填）。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验 |
| --- | --- | --- | --- | --- |
| `business_date` | string | 是 | `YYYY-MM-DD` | §2.7 |
| `copy_from_previous_day` | boolean | 否 | 默认 `false` | 复制上一营业日价格（`REQ-003`） |
| `items` | array | 条件必填 | `copy_from_previous_day = true` 时可为空 | 元素：`product_id`(integer, 必填)、`unit_price_cents`(integer, ≥1) |

- 响应(200): `{"business_date": "...", "source": "manual|copied_previous_day", "items": [...]}`，字段对应 §2.7。
- 可能错误: `MT-1005`(401)、`MT-1008`(422)、`MT-1009`(404 上一营业日无可复制数据)。

### 3.5 GET `/api/merchant/products`

- 说明: 秤端可售商品列表（图标/快捷键），供「无文本输入」的选品界面使用。关联: `REQ-004`、`AC-006`。请求头: `X-Stall-Session`（必填）。
- 响应(200): 数组，元素对应 §2.6，且**仅返回 `status = active`**；含 `icon_key` / `hotkey`。
- 可能错误: `MT-1005`(401)。

### 3.6 POST `/api/merchant/transactions`

- 说明: 创建一笔交易并完成计价（选品 + 重量 → 金额）。关联: `REQ-005`、`REQ-006`、`REQ-027`、`AC-001`/`AC-016`/`AC-017`。
- 请求头: `X-Stall-Session`（必填）、`Idempotency-Key`（必填）。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验 |
| --- | --- | --- | --- | --- |
| `items` | array | 是 | 长度 1~20 | 元素：`product_id`(integer, 必填)、`weight_grams`(integer, 必填) |
| `client_idempotency_key` | string | 否 | 长度 ≤64 | 不传时取请求头 `Idempotency-Key`（§2.8） |

- 响应(201): `transaction_no`(string)、`status`(固定 `priced`)、`total_amount_cents`(integer)、`items` 数组（每行含 `amount_cents`、`original_unit_price_cents`、`final_unit_price_cents`、`category_id`）—— 对应 §2.8 / §2.9。
- 可能错误: `MT-1002`(422 重量越界)、`MT-1005`(401)、`MT-1006`(409 价目表缺失)、`MT-1012`(409 幂等键复用于不同请求体)。

### 3.7 GET `/api/merchant/transactions`

- 说明: 本摊位交易列表（看板与对账明细用）。关联: `REQ-021`。请求头: `X-Stall-Session`（必填）。
- 查询参数: `business_date`（可选）、`limit`（可选，默认 50，上限 200）。
- 响应(200): 分页对象 `{"total": n, "items": [...]}`，元素对应 §2.8（**只含本摊位数据**，`REQ-032`）。
- 可能错误: `MT-1005`(401)、`MT-1004`(403 越权访问非本摊位数据)。

### 3.8 GET `/api/merchant/transactions/{transaction_no}`

- 说明: 交易详情与凭证数据（屏幕展示，**不提供打印**）。关联: `REQ-012`、`AC-001`。请求头: `X-Stall-Session`（必填）。
- 响应(200): 顶层对应 §2.8；`items` 对应 §2.9；`payments` 对应 §2.10（含 `method`、`status`、`confirmed_at`）；`printable: false` 固定字段，明示不打印。
- 可能错误: `MT-1005`(401)、`MT-1009`(404)。

### 3.9 POST `/api/merchant/transactions/{transaction_no}/price-change`

- 说明: 改价或抹零，并写入只增不改的留痕。关联: `REQ-007`、`REQ-008`、`AC-007`/`AC-008`。请求头: `X-Stall-Session`（必填）。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验 |
| --- | --- | --- | --- | --- |
| `item_id` | integer | 是 | 属于该交易 | §2.9 |
| `final_unit_price_cents` | integer | 否 | ≥0 | 改后单价；与 `round_off_cents` 二选一 |
| `round_off_cents` | integer | 否 | ≥0 | 抹零金额（`REQ-008` 不计入标价一致率） |
| `confirm_over_threshold` | boolean | 否 | 默认 `false` | 单笔改价幅度 >50% 时须回传 `true`，否则返回 `MT-1011` 提示确认（**不阻止**，`REQ-007`） |

- 响应(200): `{"transaction_no": "...", "total_amount_cents": 0, "audit_event": "price_change"}`；留痕字段对应 §2.18。
- 可能错误: `MT-1001`(409 交易不在可改价状态)、`MT-1005`(401)、`MT-1011`(409 需确认改价幅度)。

### 3.10 POST `/api/merchant/transactions/{transaction_no}/payment`

- 说明: 确认收款；`method = cash` 立即完成并落支付流水，`method = qr` 生成收款码并置待回调。关联: `REQ-009`、`REQ-010`、`REQ-026`、`AC-004`/`AC-009`。
- 请求头: `X-Stall-Session`（必填）、`Idempotency-Key`（必填）。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验 |
| --- | --- | --- | --- | --- |
| `method` | string | 是 | 枚举 `cash` / `qr` | §2.10 `payment.method` |
| `operator` | string | 条件必填 | `method = cash` 时必填，长度 ≤32 | §2.10 `payment.operator`（`REQ-010` 操作人） |

- 响应(200)：`method = cash` → `{"payment_no": "...", "method": "cash", "status": "success", "confirmed_at": "...", "transaction_status": "paid"}`。
- 响应(202)：`method = qr` → `{"payment_no": "...", "method": "qr", "status": "pending", "qr_payload": "...", "receiver_token_masked": "****"}`（收款标识只回脱敏值，`REQ-024`）。
- 可能错误: `MT-1001`(409 状态不允许收款)、`MT-1005`(401)、`MT-1012`(409 幂等键复用于不同请求体)。

### 3.11 POST `/api/merchant/transactions/{transaction_no}/refund`

- 说明: 退货冲正；同日聚合金额与佣金同步减少，且**同一笔只冲减一次**。关联: `REQ-013`、`REQ-028`、`AC-002`。请求头: `X-Stall-Session`（必填）、`Idempotency-Key`（必填）。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验 |
| --- | --- | --- | --- | --- |
| `amount_cents` | integer | 是 | >0 | 不得超过原单金额（`REQ-028`，见 `MT-1003`） |
| `idempotency_key` | string | 否 | 长度 ≤64 | 不传时取请求头 `Idempotency-Key`（§2.12） |

- 响应(200): `{"refund_no": "...", "transaction_status": "refunded", "amount_cents": 0, "replayed": false}`；重复提交时 `replayed: true` 且金额不变。
- 可能错误: `MT-1001`(409 交易非 `paid`)、`MT-1003`(422 退货金额超过原单)、`MT-1005`(401)。

### 3.12 GET `/api/merchant/dashboard`

- 说明: 商户端看板（交易、佣金、经营数据）。关联: `REQ-021`。请求头: `X-Stall-Session`（必填）。
- 查询参数: `business_date`（可选，默认当日）。
- 响应(200): `{"business_date": "...", "txn_count": 0, "gross_amount_cents": 0, "refund_amount_cents": 0, "commission_amount_cents": 0, "price_consistency_bp": 10000}` —— 聚合口径对应 §2.15 / §2.17。
- 可能错误: `MT-1005`(401)。

### 3.13 GET `/api/merchant/offline/queue`

- 说明: 离线/在线状态可视标识与暂存队列状态；**达告警阈值只提示、不阻断**。关联: `REQ-014`、`REQ-016`。请求头: `X-Stall-Session`（必填）。
- 响应(200): `{"pending_count": 0, "threshold": 200, "threshold_warned": false, "oldest_staged_at": null, "online": false}`。
  > `threshold` 的**数值来源**为 `spec.md`（`REQ-016` 的告警阈值），本契约只定义字段，不复述业务取值。
- 可能错误: `MT-1005`(401)。

### 3.14 POST `/api/merchant/offline/queue`

- 说明: 断网期间暂存一笔交易（**本地持久化，绝不静默丢弃**）。关联: `REQ-014`、`REQ-030`、`AC-018`/`AC-019`。请求头: `X-Stall-Session`（必填）、`Idempotency-Key`（必填）。
- 请求体: 与 3.6 相同结构（`items` + `client_idempotency_key`），落库为 §2.13 `payload_json`。
- 响应(201): `{"queue_id": 1, "status": "staged", "pending_count": 1, "threshold_warned": false}`。
- 可能错误: `MT-1007`(500 **暂存写入失败**：磁盘不足等；调用方须向摊主明确报错且**不得标记为已成功记账**)、`MT-1005`(401)。

### 3.15 POST `/api/merchant/offline/sync`

- 说明: 触发补传：按幂等键去重，成功后**清除本地副本**。关联: `REQ-015`、`NFR-013`、`AC-003`。请求头: `X-Stall-Session`（必填）。
- 请求体: 无（服务端扫描 `staged` 队列）。
- 响应(200): `{"backfilled": 2, "duplicate_discarded": 0, "failed": 0, "pending_count": 0, "purged": 2}`；
  `duplicate_discarded > 0` 表示存在幂等键重复的条目——**已丢弃并写审计日志，不影响后续补传**（`REQ-015`）。
- 可能错误: `MT-1005`(401)、`MT-1007`(500 补传中暂存文件不可写)。

### 3.16 POST `/api/mock/scale/reading`

- 说明: **演示用**模拟电子秤读数注入，避免依赖真实硬件。关联: `REQ-005`、`REQ-027`。无需认证（仅本机演示）。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验 |
| --- | --- | --- | --- | --- |
| `weight_grams` | integer | 是 | 0 ~ 200000 | 越界值用于演示 `MT-1002`（`REQ-027`） |

- 响应(200): `{"current_weight_grams": 0, "injected_at": "..."}`。
- 可能错误: `MT-1008`(422 参数校验失败)。

### 3.17 POST `/api/mock/payment/callback`

- 说明: **演示用**支付回调（成功/失败/超时）；重复送达按支付单号幂等。关联: `REQ-011`、`REQ-029`、`AC-009`/`AC-010`。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验 |
| --- | --- | --- | --- | --- |
| `callback_no` | string | 是 | 长度 ≤32 | §2.11；重复即幂等命中 |
| `payment_no` | string | 是 | 长度 ≤32 | §2.10 |
| `result` | string | 是 | 枚举 `success` / `failed` / `timeout` | §2.11 |

- 响应(200): `{"payment_no": "...", "result": "success", "is_duplicate": false, "transaction_status": "paid"}`。
- 重复送达：仍返回 `200`，`is_duplicate: true`，**交易与佣金不重复记账**（`AC-010`）。
- 可能错误: `MT-1009`(404 支付单号不存在)、`MT-1008`(422)。

### 3.18 GET `/api/customer/stalls/{stall_no}/profile`

- 说明: 顾客可见的摊位信息与信用指标（含标价一致率）。关联: `REQ-008`、`REQ-023`、`AC-011`。
- 响应(200): `{"stall_no": "...", "stall_name": "...", "in_business": true, "price_consistency_bp": 10000, "computed_at": "..."}`。
- **字段白名单纪律**：只允许返回上列字段；每个字段都必须能指出采集来源（`REQ-023`、`AC-011`），**不得新增无来源字段**。
- 可能错误: `MT-1009`(404)。

### 3.19 GET `/api/customer/receipts/{transaction_no}`

- 说明: 顾客扫码页数据（支付结果与明细）。关联: `REQ-023`、`AC-011`。
- 响应(200): `{"transaction_no": "...", "status": "paid", "total_amount_cents": 0, "items": [{"name": "...", "weight_grams": 0, "amount_cents": 0}], "paid_at": "..."}`。
- **不得返回**：顾客身份信息、身份证号、银行卡号、完整收款账号（`REQ-024`、`NFR-012`）。
- 可能错误: `MT-1009`(404)。

### 3.20 GET `/api/admin/categories`

- 说明: 品类字典与别名映射查询。关联: `REQ-002`。
- 响应(200): `{"categories": [...], "aliases": [...]}`，分别对应 §2.4 / §2.5。
- 可能错误: `MT-1008`(422 参数校验失败)。

### 3.21 POST `/api/admin/categories`

- 说明: 维护标准品类（新增或停用）。关联: `REQ-002`。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验 |
| --- | --- | --- | --- | --- |
| `code` | string | 是 | 长度 1~16 | §2.4，全表唯一 |
| `name` | string | 是 | 长度 1~32 | §2.4 |
| `status` | string | 否 | 枚举 `active` / `inactive`，默认 `active` | §2.4 |

- 响应(201): `{"id": 1, "code": "...", "name": "...", "status": "active"}`。
- 可能错误: `MT-1012`(409 品类编码已存在)、`MT-1008`(422)。

### 3.22 POST `/api/admin/aliases`

- 说明: 维护「摊位别名 → 标准品类」映射；此后按**标准品类**记账。关联: `REQ-002`、`AC-014`。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验 |
| --- | --- | --- | --- | --- |
| `stall_no` | string | 是 | 长度 1~16 | §2.2 |
| `alias_name` | string | 是 | 长度 1~32 | §2.5，与摊位联合唯一 |
| `category_id` | integer | 是 | 须存在且 `active` | §2.5 |

- 响应(201): `{"id": 1, "stall_no": "...", "alias_name": "...", "category_id": 1}`。
- 可能错误: `MT-1012`(409 该摊位该别名已存在)、`MT-1008`(422)、`MT-1009`(404 品类或摊位不存在)。

### 3.23 GET `/api/admin/commission-rules`

- 说明: 查询佣金口径（含历史生效期）。关联: `REQ-017`。
- 响应(200): 数组，元素对应 §2.14。
- 可能错误: `MT-1008`(422)。

### 3.24 PUT `/api/admin/commission-rules`

- 说明: 配置佣金口径（收费对象、费率、品种档位、生效期）；佣金按**实收金额**计算。关联: `REQ-017`、`AC-022`。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验 |
| --- | --- | --- | --- | --- |
| `pay_object` | string | 是 | 枚举 `merchant` / `customer` / `market` | §2.14 |
| `rate_bp` | integer | 是 | 1~10000（万分比） | §2.14；**不用小数，避免浮点** |
| `category_tier` | string | 否 | 长度 ≤32 | §2.14 |
| `effective_from` | string | 是 | `YYYY-MM-DD` | §2.14 |
| `effective_to` | string | 否 | `YYYY-MM-DD`；≥`effective_from` | §2.14 |

- 响应(200): 新建的规则行，对应 §2.14；同时写 `audit_log`（`event_type = commission_rule_changed`，`NFR-009`）。
- 可能错误: `MT-1008`(422)、`MT-1012`(409 生效期与既有规则重叠且档位相同)。

### 3.25 GET `/api/admin/dashboard`

- 说明: 市场方看板（全部摊位汇总 + 排行）。关联: `REQ-021`。
- 查询参数: `business_date`（可选，默认当日）。
- 响应(200): `{"business_date": "...", "market": {"txn_count": 0, "gross_amount_cents": 0, "commission_amount_cents": 0}, "stalls": [...]}` —— 汇总口径对应 §2.15。
- 可能错误: `MT-1008`(422)。

### 3.26 POST `/api/admin/daily-aggregate`

- 说明: 日终聚合；退货冲正后可按需重算（生成新 `revision`，旧版本保留）。关联: `REQ-018`。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验 |
| --- | --- | --- | --- | --- |
| `business_date` | string | 是 | `YYYY-MM-DD` | §2.15 |
| `stall_no` | string | 否 | 长度 1~16；不传表示全部摊位 | §2.15 |

- 响应(200): `{"business_date": "...", "stalls_aggregated": 300, "revision": 2}`。
- 可能错误: `MT-1008`(422)、`MT-1013`(409 该营业日无生效佣金口径)。

### 3.27 POST `/api/admin/settlements`

- 说明: 依据当日有效的日聚合生成结算单。关联: `REQ-019`、`AC-023`。
- 请求体:

| 字段 | 类型 | 必填 | 传输层约束 | 语义与校验 |
| --- | --- | --- | --- | --- |
| `stall_no` | string | 是 | 长度 1~16 | §2.16 |
| `period_start` | string | 是 | `YYYY-MM-DD` | §2.16 |
| `period_end` | string | 是 | `YYYY-MM-DD`；≥`period_start` | §2.16 |

- 响应(201): `{"settlement_no": "...", "version": 1, "gross_amount_cents": 0, "commission_amount_cents": 0}`。
- 可能错误: `MT-1010`(409 期内日聚合缺失，需先执行日终聚合)、`MT-1008`(422)。

### 3.28 GET `/api/admin/settlements`

- 说明: 查询结算单（含历史版本）。关联: `REQ-019`、`AC-023`。
- 查询参数: `stall_no`（可选）、`period_start`/`period_end`（可选）。
- 响应(200): 数组，元素对应 §2.16（**含全部 `version`，旧版本不删**）。
- 可能错误: `MT-1008`(422)。

### 3.29 GET `/api/admin/reconciliation`

- 说明: 对账核验等式「订单总额 = 支付流水 = 分账明细」。关联: `REQ-020`、`AC-003`。
- 查询参数: `business_date`（必填）、`stall_no`（可选）。
- 响应(200): `{"business_date": "...", "order_total_cents": 0, "payment_total_cents": 0, "split_total_cents": 0, "balanced": true, "diff_cents": 0}`。
  > 等式由 `transaction` / `payment` / `transaction_item` **现场计算，不落表**（见 [../data-model.md](../data-model.md) §1 末注）。
- 可能错误: `MT-1008`(422)。

### 3.30 GET `/api/admin/metrics/usage`

- 说明: 导出**三个**使用率指标。关联: `REQ-022`（**2026-09-30 修订版**）、`AC-005`。
- 查询参数: `business_date`（必填，`YYYY-MM-DD`；**单日口径**，逐项指标的分子分母都在同一营业日内取数）。
- 响应(200):

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `business_date` | string | 统计营业日 |
| `stall_usage_bp` | integer | **摊位使用率**（0–10000）：当日有走秤交易的摊位数 ÷ 在营摊位数 |
| `stall_usage_numerator` | integer | 分子值（当日有走秤交易的摊位数）—— 供第三方逐一核对 |
| `stall_usage_denominator` | integer | 分母值（在营摊位数） |
| `cash_txn_share_bp` | integer | **现金交易占比**（0–10000）：现金笔数 ÷ 当日全部走秤笔数 |
| `cash_txn_numerator` | integer | 分子值（`payment.method = cash` 且成功的笔数） |
| `cash_txn_denominator` | integer | 分母值（当日全部走秤笔数） |
| `price_list_maintenance_bp` | integer | **价目表维护率**（0–10000）：当日价目表完整且已更新的摊位数 ÷ 在营摊位数 |
| `price_list_numerator` | integer | 分子值 |
| `price_list_denominator` | integer | 分母值 |

> **分子/分母必须随指标一起返回** —— 这是 `AC-005`「第三方可逐一核对」的落地形式。口径与表来源见 [../data-model.md](../data-model.md) §5.1。
> **本期不提供市场口径的「日均智能秤交易占比」**：其分母（市场总交易笔数）是外部基准，系统无法获得（未决项 `Q-15`）；**禁止用估算值伪造该分母**。
- 可能错误: `MT-1008`(422 缺少 `business_date` 或日期格式非法)。

### 3.31 GET `/api/admin/audit-logs`

- 说明: 查询资金链路留痕（**只读**；`audit_log` 只增不改）。关联: `NFR-009`。
- 查询参数: `stall_no`（可选）、`event_type`（可选）、`from`/`to`（可选）。
- 响应(200): 分页对象，元素对应 §2.18。**不提供任何写接口**。
- 可能错误: `MT-1008`(422)。

## 4. 统一错误码表

前缀 `MT-`（market-trade），全项目连续编号。

| 错误码 | HTTP 状态 | 含义 | 触发条件 | 调用方处理建议 | 关联 |
| --- | --- | --- | --- | --- | --- |
| `MT-1001` | 409 | 状态不允许当前流转 | 当前状态不在 [../data-model.md](../data-model.md) §4 流转表的前置集合内 | 重新拉取详情，按最新状态渲染可用操作 | `REQ-012`/`REQ-013`/`REQ-026` |
| `MT-1002` | 422 | 重量超出允许范围 | `weight_grams ≤ 0` 或 `> 50000`（即 >50 公斤） | 清除读数并提示重新称重，**不创建任何交易** | `REQ-027`、`AC-017` |
| `MT-1003` | 422 | 退货金额超过原单金额 | `amount_cents >` 原单 `total_amount_cents` | 提示可退上限，不创建 `refund` | `REQ-028`、`AC-017` |
| `MT-1004` | 403 | 越权访问非本摊位数据 | 请求的 `stall_id` 与会话绑定摊位不一致 | 不重试；前端切回本摊位页面 | `REQ-032`、`AC-021` |
| `MT-1005` | 401 | 会话无效或已失效 | `X-Stall-Session` 缺失、过期或 `is_active = 0` | 重新执行秤端绑定（3.2） | `REQ-032`、`NFR-007` |
| `MT-1006` | 409 | 价目表缺失 | 计价时该摊位该商品当日无 `price_item` | 引导摊主先设价（3.4）；**不得以 0 元成交** | `REQ-003`、`REQ-005` |
| `MT-1007` | 500 | 本地暂存写入失败 | 磁盘空间不足或文件不可写 | 向摊主明确报错；**不得标记为已成功记账**；提示联系运维 | `REQ-030`、`AC-019` |
| `MT-1008` | 422 | 参数校验失败 | 字段缺失、类型不符、超长或枚举非法 | 按 `detail` 逐项提示 | 通用 |
| `MT-1009` | 404 | 资源不存在 | `stall_no` / `transaction_no` / `payment_no` 等查无记录 | 提示并返回上一级列表 | 通用 |
| `MT-1010` | 409 | 期内日聚合缺失 | 生成结算单时该区间存在未聚合的营业日 | 先调用 3.26 日终聚合，再重试 | `REQ-018`、`REQ-019` |
| `MT-1011` | 409 | 需确认改价幅度 | 单笔改价幅度 >50% 且未回传 `confirm_over_threshold = true` | 前端弹确认框；用户确认后带 `true` 重发（**系统不阻止该操作**） | `REQ-007`、`AC-007` |
| `MT-1012` | 409 | 幂等键或被唯一约束的标识已被占用 | (a) `Idempotency-Key` 已存在且**请求体指纹不同**；(b) 唯一约束冲突（品类编码、别名、生效期重叠等） | (a) 返回首次结果的提示或提示换键；(b) 提示已存在，改为编辑既有记录 | `REQ-015`、`REQ-026`、`REQ-029` |
| `MT-1013` | 409 | 无生效的佣金口径 | 聚合/结算时该营业日没有任何匹配 `effective_from`~`effective_to` 的规则 | 引导运营端先配置佣金口径（3.24） | `REQ-017`、`AC-022` |

> **幂等命中不是错误**：补传的幂等键重复（`REQ-015`）与支付回调重复（`REQ-029`）**返回成功语义**（200 + `is_duplicate`/`replayed` 标记），
> **不得报错、不得阻断后续处理** —— 这是 `spec.md` §5 边界明确要求的行为。

## 5. 版本策略

- **当前版本**：`v1`，路径**不带版本段**（`/api/...`）。
- **兼容性变更**（直接发布）：新增可选字段、新增端点、放松校验。
- **破坏性变更**（改名、删字段、改语义、收紧校验）：须同步更新本文件、`../data-model.md`、契约测试与 `tasks.md`，**在同一次提交内完成**。
- **兼容期承诺**：本期为**单机演示交付物，无外部消费方**（`spec.md` §1.2 本期实现范围），故**不做多版本并存**；
  承诺形式为「**同提交内全量同步**」：任何契约变更必须在同一个提交里改完本文件、数据模型、契约测试与任务清单，
  并在 `data-model.md` §7 的回滚方案下验证（演示环境可经 `scripts/reset_demo.py` 重建）。
  > 若本项目转为真实生产系统（`ADR-0002` §5 触发条件之一），本承诺升级为"旧版本至少保留一个发布周期"。

## 6. OpenAPI 骨架

```yaml
openapi: 3.0.3
info:
  title: 菜市场数字化交易与佣金系统 API
  version: 1.0.0
  description: 单机演示环境；金额单位为「分」，重量单位为「克」（见 ../data-model.md §0）
paths:
  /healthz:
    get: { summary: 健康检查, responses: { "200": { description: ok }, "503": { description: db 不可用 } } }
  /api/merchant/session:
    post:
      summary: 秤端绑定摊位
      requestBody: { required: true, content: { application/json: { schema: { type: object, required: [stall_no], properties: { stall_no: { type: string, maxLength: 16 } } } } } }
      responses: { "201": { description: 下发 session_token }, "404": { description: MT-1009 }, "422": { description: MT-1008 } }
  /api/merchant/price-list:
    get: { summary: 查询价目表, parameters: [ { name: business_date, in: query, required: true, schema: { type: string } } ], responses: { "200": { description: 价目表数组 }, "401": { description: MT-1005 } } }
    post: { summary: 设置/调整价目表, responses: { "200": { description: 已保存 }, "422": { description: MT-1008 }, "404": { description: MT-1009 } } }
  /api/merchant/products:
    get: { summary: 秤端商品列表, responses: { "200": { description: 仅 active 商品 }, "401": { description: MT-1005 } } }
  /api/merchant/transactions:
    post:
      summary: 创建交易并计价
      parameters: [ { name: Idempotency-Key, in: header, required: true, schema: { type: string, maxLength: 64 } }, { name: X-Stall-Session, in: header, required: true, schema: { type: string } } ]
      requestBody: { required: true, content: { application/json: { schema: { type: object, required: [items], properties: { items: { type: array, minItems: 1, maxItems: 20, items: { type: object, required: [product_id, weight_grams], properties: { product_id: { type: integer }, weight_grams: { type: integer, minimum: 1, maximum: 50000 } } } } } } } } }
      responses: { "201": { description: status=priced }, "401": { description: MT-1005 }, "409": { description: MT-1006 / MT-1012 }, "422": { description: MT-1002 } }
    get: { summary: 本摊位交易列表, responses: { "200": { description: 分页结果 }, "401": { description: MT-1005 }, "403": { description: MT-1004 } } }
  /api/merchant/transactions/{transaction_no}:
    get: { summary: 交易详情与凭证（不打印）, parameters: [ { name: transaction_no, in: path, required: true, schema: { type: string } } ], responses: { "200": { description: 详情 + printable=false }, "401": { description: MT-1005 }, "404": { description: MT-1009 } } }
  /api/merchant/transactions/{transaction_no}/price-change:
    post: { summary: 改价/抹零（留痕）, responses: { "200": { description: 已留痕 }, "409": { description: MT-1001 / MT-1011 }, "401": { description: MT-1005 } } }
  /api/merchant/transactions/{transaction_no}/payment:
    post: { summary: 确认收款（现金/收款码）, responses: { "200": { description: 现金成功 }, "202": { description: 收款码待回调 }, "409": { description: MT-1001 / MT-1012 } } }
  /api/merchant/transactions/{transaction_no}/refund:
    post: { summary: 退货冲正（只冲减一次）, responses: { "200": { description: 已冲正或 replayed }, "409": { description: MT-1001 }, "422": { description: MT-1003 } } }
  /api/merchant/dashboard:
    get: { summary: 商户端看板, responses: { "200": { description: 日聚合视图 }, "401": { description: MT-1005 } } }
  /api/merchant/offline/queue:
    get: { summary: 离线状态与队列, responses: { "200": { description: pending_count / threshold_warned }, "401": { description: MT-1005 } } }
    post: { summary: 断网暂存一笔交易, responses: { "201": { description: status=staged }, "401": { description: MT-1005 }, "500": { description: MT-1007 } } }
  /api/merchant/offline/sync:
    post: { summary: 触发补传并清除本地副本, responses: { "200": { description: backfilled / purged }, "401": { description: MT-1005 } } }
  /api/mock/scale/reading:
    post: { summary: 注入模拟秤读数（演示用）, responses: { "200": { description: 已注入 }, "422": { description: MT-1008 } } }
  /api/mock/payment/callback:
    post: { summary: 模拟支付回调（可重复送达）, responses: { "200": { description: result + is_duplicate }, "404": { description: MT-1009 } } }
  /api/customer/stalls/{stall_no}/profile:
    get: { summary: 顾客可见摊位信息与信用指标, responses: { "200": { description: 字段白名单见契约 §3.18 }, "404": { description: MT-1009 } } }
  /api/customer/receipts/{transaction_no}:
    get: { summary: 顾客扫码页数据, responses: { "200": { description: 无顾客身份信息 }, "404": { description: MT-1009 } } }
  /api/admin/categories:
    get: { summary: 品类字典与别名, responses: { "200": { description: categories + aliases } } }
    post: { summary: 维护标准品类, responses: { "201": { description: 已创建 }, "409": { description: MT-1012 } } }
  /api/admin/aliases:
    post: { summary: 维护别名映射, responses: { "201": { description: 已创建 }, "409": { description: MT-1012 } } }
  /api/admin/commission-rules:
    get: { summary: 查询佣金口径, responses: { "200": { description: 规则数组 } } }
    put: { summary: 配置佣金口径, responses: { "200": { description: 已生效并留痕 }, "409": { description: MT-1012 } } }
  /api/admin/dashboard:
    get: { summary: 市场方看板, responses: { "200": { description: 市场汇总 + 摊位排行 } } }
  /api/admin/daily-aggregate:
    post: { summary: 日终聚合/重算, responses: { "200": { description: revision 递增 }, "409": { description: MT-1013 } } }
  /api/admin/settlements:
    post: { summary: 生成结算单, responses: { "201": { description: version=1 }, "409": { description: MT-1010 } } }
    get: { summary: 查询结算单（含历史版本）, responses: { "200": { description: 结算单数组 } } }
  /api/admin/reconciliation:
    get: { summary: 对账等式核验, responses: { "200": { description: balanced / diff_cents } } }
  /api/admin/metrics/usage:
    get: { summary: 三个使用率指标导出（含各指标分子与分母）, parameters: [ { name: business_date, in: query, required: true, schema: { type: string } } ], responses: { "200": { description: stall_usage_bp / cash_txn_share_bp / price_list_maintenance_bp + 各自分子分母 }, "422": { description: MT-1008 } } }
  /api/admin/audit-logs:
    get: { summary: 查询留痕（只读）, responses: { "200": { description: 分页结果 } } }
```

> **本项目的「OpenAPI 纳入 CI 校验」如何落地（对 SOP 模板的一处显式替代，理由见 `ADR-0003`）**：
> `ADR-0003` §4 已诚实记录代价 —— **Flask 没有自动 OpenAPI 生成**，因此**无法**做"实现代码自动生成的 OpenAPI ↔ 本骨架 diff"。
> 替代方案（目的是同一个：**防止契约与实现漂移**，且可机械失败）：
> 1. `tests/contract/` 断言 **Flask 路由表 ↔ 本文件 §2 端点总表双向一致**：实现多一个或少一个端点，测试即失败；
> 2. `tests/contract/` 逐端点断言 **响应关键字段与状态码**，并对全部响应体做「无身份证/银行卡字段」扫描（`AC-012`）；
> 3. 本骨架与 §2 / §3 冲突时，**以 §2/§3 表格为准**并立即修正骨架。
>
> 该替代已作为待办写入 `tasks.md`；若负责人要求严格按 SOP 原样执行，则需要引入 OpenAPI 生成依赖 —— 那属于对宪法 §1 依赖白名单的变更，**必须先改 ADR 与宪法**。
