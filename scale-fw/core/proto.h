/* 秤端 ↔ 中台报文窄编解码（`plan.md` §4 `scale-fw/core/proto.h`；`REQ-034` / `REQ-036` / `REQ-038`）。
 *
 * 契约唯一事实来源：`specs/market-trade-flow/contracts/scale-midplatform.md`（7 个端点）。
 * 「窄」是硬要求，体现在三处：
 *   1. 请求体只包含契约允许的字段；**结构体里根本没有** `stall_id` / `market_id`，
 *      从类型上就不可能把它们发出去（契约 §1.6：授权只来自设备令牌）；
 *   2. 响应解码只填本文件声明的白名单字段 —— 响应里出现 `stall_id` / `market_id`
 *      **一律不采信**，且不改变任何授权范围（`proto_scope_*` 只由激活响应建立）；
 *   3. 未知错误码**不得当成功**（契约 §7：不得按最新版猜着解析）。
 *
 * 本文件不依赖 ESP-IDF、不引入第三方库，可在主机侧编译并单测。
 * 标准头说明：`<string.h>` 仅为取得 `size_t`（ISO C 标准头，非 ESP-IDF）。
 */
#ifndef CAISHIC_SCALE_PROTO_H
#define CAISHIC_SCALE_PROTO_H

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

/* 协议版本（请求头 `X-Scale-Proto`，契约 §1.9；中台发现不兼容按 MT-2003 拒绝） */
#define PROTO_VERSION 1

#define PROTO_ENDPOINT_COUNT 7
#define PROTO_DEVICE_ID_MAX 24
#define PROTO_FIRMWARE_VERSION_MAX 16
#define PROTO_HARDWARE_REV_MAX 24
#define PROTO_DATE_MAX 12        /* YYYY-MM-DD */
#define PROTO_TIME_MAX 20        /* YYYY-MM-DD HH:MM:SS */
#define PROTO_IDEM_KEY_MAX 64
#define PROTO_TX_NO_MAX 24
#define PROTO_STATUS_MAX 16
#define PROTO_NAME_MAX 32
#define PROTO_URL_MAX 128
#define PROTO_REASON_MAX 64
#define PROTO_MARKET_CODE_MAX 16
#define PROTO_STALL_NO_MAX 16
#define PROTO_MAX_ITEMS 20       /* 主契约 §3.6：`items` 长度 1~20 */
#define PROTO_MAX_PRODUCTS 16
#define PROTO_MAX_CATEGORIES 8
#define PROTO_MAX_PRICE_ITEMS 32

typedef enum {
    PROTO_EP_DEVICES_ACTIVATE = 1,
    PROTO_EP_DEVICES_HEARTBEAT = 2,
    PROTO_EP_CATALOG = 3,
    PROTO_EP_PRICE_LIST = 4,
    PROTO_EP_TRANSACTIONS_REPORT = 5,
    PROTO_EP_TRANSACTION_SETTLE = 6,
    PROTO_EP_TRANSACTION_REFUND = 7
} proto_endpoint_t;

/* ---------------- 请求 ---------------- */

typedef struct {
    int64_t product_id;
    int64_t weight_grams;
    int64_t unit_price_cents;
    int64_t amount_cents;
} proto_line_t;

/* 请求体字段的**白名单**。注意这里没有 `stall_id` / `market_id` —— 见文件头第 1 条。 */
typedef struct {
    char device_id[PROTO_DEVICE_ID_MAX];
    char firmware_version[PROTO_FIRMWARE_VERSION_MAX];
    char hardware_rev[PROTO_HARDWARE_REV_MAX];
    int64_t pending_count;

    char business_date[PROTO_DATE_MAX];
    char captured_at[PROTO_TIME_MAX];
    int64_t price_list_version;
    bool offline_backfill;                 /* origin ∈ online / offline_backfill */
    proto_line_t items[PROTO_MAX_ITEMS];
    size_t item_count;
    int64_t amount_cents;                  /* 秤端本地计价合计：**比对输入**，不是账口（REQ-038） */
    int64_t round_off_cents;
    char client_idempotency_key[PROTO_IDEM_KEY_MAX];

    char transaction_no[PROTO_TX_NO_MAX];
    bool settle_cash;                      /* true = cash / false = qr */
    int64_t refund_amount_cents;
    char refund_reason[PROTO_REASON_MAX];

    int64_t since_version;                 /* GET /catalog */
    char query_business_date[PROTO_DATE_MAX]; /* GET /price-list */
} proto_request_t;

/* ---------------- 响应（白名单字段） ---------------- */

typedef struct {
    int64_t product_id;
    int64_t category_id;
    bool active;
    char name[PROTO_NAME_MAX];
} proto_product_t;

typedef struct {
    int64_t category_id;
    char name[PROTO_NAME_MAX];
} proto_category_t;

typedef struct {
    int64_t product_id;
    int64_t unit_price_cents;
} proto_price_item_t;

typedef struct {
    /* 激活 / 心跳 */
    char market_code[PROTO_MARKET_CODE_MAX];
    char stall_no[PROTO_STALL_NO_MAX];
    char business_date[PROTO_DATE_MAX];
    char server_time[PROTO_TIME_MAX];
    int64_t offline_warn_threshold;
    int64_t proto_version;
    char customer_base_url[PROTO_URL_MAX];
    bool config_changed;
    int64_t catalog_version;
    char price_list_business_date[PROTO_DATE_MAX];

    /* 字典 / 价目表 */
    proto_product_t products[PROTO_MAX_PRODUCTS];
    size_t product_count;
    proto_category_t categories[PROTO_MAX_CATEGORIES];
    size_t category_count;
    int64_t price_list_version;
    proto_price_item_t price_items[PROTO_MAX_PRICE_ITEMS];
    size_t price_item_count;

    /* 交易上报 / 收款 / 退货 */
    char transaction_no[PROTO_TX_NO_MAX];
    char status[PROTO_STATUS_MAX];
    int64_t authoritative_amount_cents;    /* 中台重算值：**入账一律用它**（契约 §3.5） */
    int64_t reported_amount_cents;
    bool amount_mismatch;
    bool replayed;
    char mismatch_detail[PROTO_URL_MAX];
    char receipt_url[PROTO_URL_MAX];
    char qr_payload[PROTO_URL_MAX];
    char paid_at[PROTO_TIME_MAX];
    int64_t refund_amount_cents;
} proto_response_t;

/* 授权范围（市场 + 摊位）：**只由设备激活建立**，响应里的 `stall_id` / `market_id` 不采信。 */
typedef struct {
    char market_code[PROTO_MARKET_CODE_MAX];
    char stall_no[PROTO_STALL_NO_MAX];
} proto_scope_t;

/* ---------------- 错误 ---------------- */

typedef struct {
    int64_t http_status;
    char code[16];
    char message[PROTO_NAME_MAX * 2];
} proto_error_t;

typedef enum {
    PROTO_RESULT_OK = 0,
    PROTO_RESULT_ERROR_KNOWN = 1,
    PROTO_RESULT_ERROR_UNKNOWN = 2, /* 未知错误码：**绝不当成功** */
    PROTO_RESULT_MALFORMED = 3      /* 无法解析：同样不得当成功 */
} proto_result_t;

/* ---------------- 端点元数据 ---------------- */

const char *proto_endpoint_path(proto_endpoint_t endpoint);
const char *proto_endpoint_method(proto_endpoint_t endpoint);
bool proto_endpoint_requires_idempotency_key(proto_endpoint_t endpoint);

/* ---------------- 编解码 ---------------- */

/* 构造请求体。GET 端点没有请求体（返回空串，长度 0）。 */
bool proto_build_request(proto_endpoint_t endpoint, const proto_request_t *request, char *out,
                         size_t cap, size_t *out_len);

/* 解析响应体：只填白名单字段；出现 `stall_id` / `market_id` 也不采信。 */
bool proto_parse_response(proto_endpoint_t endpoint, const char *body, size_t length,
                          proto_response_t *out);

/* 请求体**没有**对应的解码函数：秤端只构造请求、不解析请求（NFR-015 的复杂度预算）。
 * 「编解码往返一致」由主机侧 `test_proto.c` 用**独立实现的 JSON 读取器**核对
 * —— 用自己的解码器验证自己的编码器不算证据。 */

/* ---------------- 授权范围 ---------------- */

void proto_scope_init(proto_scope_t *scope);
/* 只允许由**激活响应**建立授权范围；其它响应一律返回 false 且不改动 scope。 */
bool proto_scope_apply_activate(const proto_response_t *response, proto_scope_t *scope);
bool proto_scope_is_active(const proto_scope_t *scope);
bool proto_scope_matches_stall(const proto_scope_t *scope, const char *stall_no);

/* ---------------- 错误码 ---------------- */

bool proto_error_is_known(const char *code);
/* 分类响应：2xx 且无 error 对象 → OK；有 error 对象 → 已知/未知错误码；无法解析 → MALFORMED。
 * **任何非 OK 结果都不得被调用方当成成功**。 */
proto_result_t proto_classify(const char *body, size_t length, int64_t http_status,
                              proto_error_t *out_error);

#endif /* CAISHIC_SCALE_PROTO_H */
