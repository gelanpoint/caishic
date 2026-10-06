/* 主机侧单测：报文窄编解码（`T-SCALE-11`；`REQ-034` / `REQ-036` / `REQ-038` / `AC-027`）。
 *
 * 纪律：**不引入任何测试框架依赖**（`plan.md` §4）——自制断言。
 * 请求侧用**逐字节比对契约报文**而不是"自己解自己的码"：`core/proto.c` 里根本没有请求解码函数
 * （秤端只构造请求、不解析请求，`NFR-015` 复杂度预算），且用自己的解码器验证自己的编码器不算证据。
 * 编码器是确定性的，故直接钉住线上字节 —— 字段名 / 顺序 / 转义 / 分隔符的任何漂移都会立刻变红。
 *
 * 编译：
 *   ~/.local/bin/zig cc -target x86_64-linux-musl -std=c11 -Wall -Wextra -Werror \
 *       -I scale-fw/core -o /tmp/test_proto scale-fw/test/test_proto.c scale-fw/core/proto.c
 * 运行：/tmp/test_proto
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "proto.h"

static int g_pass = 0;
static int g_fail = 0;

#define CHECK(cond, ...)         \
    do {                         \
        if (cond) {              \
            g_pass++;            \
        } else {                 \
            g_fail++;            \
            printf("[FAIL] ");   \
            printf(__VA_ARGS__); \
            printf("\n");        \
        }                        \
    } while (0)

static size_t count_hits(const char *body, const char *needle) {
    size_t n = strlen(needle);
    size_t hits = 0;
    for (const char *p = body; *p != '\0'; p++) {
        if (strncmp(p, needle, n) == 0) hits++;
    }
    return hits;
}

/* 逐字节比对：报文格式即契约，任何漂移都必须变红 */
static void check_wire(const char *what, const char *built, const char *expected, size_t length) {
    CHECK(strcmp(built, expected) == 0, "%s 报文与契约不一致\n  实得：%s\n  期望：%s", what, built,
          expected);
    CHECK(length == strlen(expected), "%s 的 out_len 应为 %d（实得 %d）", what, (int)strlen(expected), (int)length);
}

/* ---------------- 端点表（契约 §2） ---------------- */

static void test_endpoint_table(void) {
    static const char *kApiPrefix = "/api/scale/v1/";
    struct { proto_endpoint_t endpoint; const char *method; bool idempotent; } expected[] = {
        {PROTO_EP_DEVICES_ACTIVATE, "POST", false}, {PROTO_EP_DEVICES_HEARTBEAT, "POST", false},
        {PROTO_EP_CATALOG, "GET", false},           {PROTO_EP_PRICE_LIST, "GET", false},
        {PROTO_EP_TRANSACTIONS_REPORT, "POST", true},
        {PROTO_EP_TRANSACTION_SETTLE, "POST", true}, {PROTO_EP_TRANSACTION_REFUND, "POST", true},
    };
    CHECK(PROTO_ENDPOINT_COUNT == 7, "契约共 7 个端点（实得 %d）", PROTO_ENDPOINT_COUNT);
    for (size_t i = 0; i < sizeof(expected) / sizeof(expected[0]); i++) {
        const char *path = proto_endpoint_path(expected[i].endpoint);
        CHECK(path != NULL && strncmp(path, kApiPrefix, strlen(kApiPrefix)) == 0,
              "端点 %d 路径必须带版本段 %s（实得 %s）", (int)expected[i].endpoint, kApiPrefix,
              path == NULL ? "(null)" : path);
        CHECK(strcmp(proto_endpoint_method(expected[i].endpoint), expected[i].method) == 0,
              "端点 %d 方法应为 %s", (int)expected[i].endpoint, expected[i].method);
        CHECK(proto_endpoint_requires_idempotency_key(expected[i].endpoint) == expected[i].idempotent,
              "端点 %d 的幂等键要求不符（契约 §1.3）", (int)expected[i].endpoint);
    }
    CHECK(proto_endpoint_path((proto_endpoint_t)0) == NULL, "非法端点必须返回 NULL");
    CHECK(proto_endpoint_path((proto_endpoint_t)8) == NULL, "越界端点必须返回 NULL");
}

/* ---------------- 请求报文：逐字节契约 + 转义 + 拒绝路径 ---------------- */

static void test_request_wire_format(void) {
    char body[1024];
    size_t length = 0;
    proto_request_t request;

    memset(&request, 0, sizeof(request));
    strcpy(request.device_id, "SC-000123");
    strcpy(request.firmware_version, "0.1.0");
    strcpy(request.hardware_rev, "esp32s3-n8r8");
    CHECK(proto_build_request(PROTO_EP_DEVICES_ACTIVATE, &request, body, sizeof(body), &length), "激活请求必须构造成功");
    check_wire("激活",
               body,
               "{\"device_id\":\"SC-000123\",\"firmware_version\":\"0.1.0\","
               "\"hardware_rev\":\"esp32s3-n8r8\"}",
               length);

    memset(&request, 0, sizeof(request));
    strcpy(request.device_id, "SC-000123");
    strcpy(request.firmware_version, "0.1.0");
    request.pending_count = 3;
    CHECK(proto_build_request(PROTO_EP_DEVICES_HEARTBEAT, &request, body, sizeof(body), &length), "心跳请求必须构造成功");
    check_wire("心跳", body, "{\"device_id\":\"SC-000123\",\"pending_count\":3,\"firmware_version\":\"0.1.0\"}",
               length);

    memset(&request, 0, sizeof(request));
    strcpy(request.business_date, "2026-10-06");
    strcpy(request.captured_at, "2026-10-06 08:20:11");
    strcpy(request.client_idempotency_key, "SC-000123-20261006-0007");
    request.price_list_version = 11;
    request.amount_cents = 250;
    request.round_off_cents = 0;
    request.item_count = 2;
    request.items[0].product_id = 1234;
    request.items[0].weight_grams = 780;
    request.items[0].unit_price_cents = 320;
    request.items[0].amount_cents = 250;
    request.items[1].product_id = 5678;
    request.items[1].weight_grams = 1000;
    request.items[1].unit_price_cents = 198;
    request.items[1].amount_cents = 198;
    CHECK(proto_build_request(PROTO_EP_TRANSACTIONS_REPORT, &request, body, sizeof(body), &length), "交易上报请求必须构造成功");
    check_wire("交易上报（在线）", body, "{\"business_date\":\"2026-10-06\",\"captured_at\":\"2026-10-06 08:20:11\","
               "\"origin\":\"online\",\"price_list_version\":11,"
               "\"items\":[{\"product_id\":1234,\"weight_grams\":780},"
               "{\"product_id\":5678,\"weight_grams\":1000}],\"amount_cents\":250,"
               "\"local_lines\":[{\"product_id\":1234,\"unit_price_cents\":320,"
               "\"weight_grams\":780,\"amount_cents\":250},{\"product_id\":5678,"
               "\"unit_price_cents\":198,\"weight_grams\":1000,\"amount_cents\":198}],"
               "\"round_off_cents\":0,\"client_idempotency_key\":\"SC-000123-20261006-0007\"}",
               length);
    CHECK(count_hits(body, "stall_id") == 0 && count_hits(body, "market_id") == 0,
          "上报请求**不得**出现 stall_id / market_id（契约 §1.6：授权只来自设备令牌）");

    /* 补传：origin=offline_backfill，business_date 必须是**暂存时**的营业日（契约 §6） */
    request.offline_backfill = true;
    strcpy(request.business_date, "2026-10-05");
    CHECK(proto_build_request(PROTO_EP_TRANSACTIONS_REPORT, &request, body, sizeof(body), &length), "补传请求必须构造成功");
    CHECK(strstr(body, "\"origin\":\"offline_backfill\"") != NULL &&
              strstr(body, "\"business_date\":\"2026-10-05\"") != NULL,
          "补传必须带 offline_backfill 与暂存时营业日（实得 %s）", body);

    memset(&request, 0, sizeof(request));
    request.settle_cash = true;
    strcpy(request.client_idempotency_key, "k-1");
    CHECK(proto_build_request(PROTO_EP_TRANSACTION_SETTLE, &request, body, sizeof(body), &length), "现金收款请求必须构造成功");
    check_wire("现金收款", body, "{\"method\":\"cash\"}", length);
    request.settle_cash = false;
    CHECK(proto_build_request(PROTO_EP_TRANSACTION_SETTLE, &request, body, sizeof(body), &length), "收款码请求必须构造成功");
    check_wire("收款码", body, "{\"method\":\"qr\"}", length);

    /* 转义：中文原样直出；`"` `\` 制表 换行一律转义 */
    memset(&request, 0, sizeof(request));
    request.refund_amount_cents = 250;
    strcpy(request.refund_reason, "退货");
    strcpy(request.client_idempotency_key, "k-2");
    CHECK(proto_build_request(PROTO_EP_TRANSACTION_REFUND, &request, body, sizeof(body), &length), "退货请求必须构造成功");
    check_wire("退货（中文）", body, "{\"amount_cents\":250,\"reason\":\"退货\"}", length);

    strcpy(request.refund_reason, "顾客退货\"半价\"\\A\tB\nC");
    CHECK(proto_build_request(PROTO_EP_TRANSACTION_REFUND, &request, body, sizeof(body), &length), "含特殊字符的退货请求必须构造成功");
    check_wire("退货（特殊字符转义）", body, "{\"amount_cents\":250,\"reason\":\"顾客退货\\\"半价\\\"\\\\A\\tB\\nC\"}", length);
    CHECK(count_hits(body, "stall_id") == 0, "退货请求不得出现 stall_id");

    /* 拒绝路径：不得静默截断、不得把非法载荷发出去 */
    memset(&request, 0, sizeof(request));
    request.item_count = 0;
    strcpy(request.client_idempotency_key, "k");
    CHECK(!proto_build_request(PROTO_EP_TRANSACTIONS_REPORT, &request, body, sizeof(body), &length),
          "items 为空必须被拒绝（主契约 §3.6：1~20）");
    request.item_count = PROTO_MAX_ITEMS + 1;
    CHECK(!proto_build_request(PROTO_EP_TRANSACTIONS_REPORT, &request, body, sizeof(body), &length), "items 超过上限必须被拒绝");
    memset(&request, 0, sizeof(request));
    request.settle_cash = true;
    CHECK(!proto_build_request(PROTO_EP_TRANSACTION_SETTLE, &request, body, sizeof(body), &length), "写操作缺幂等键必须被拒绝（契约 §1.3）");
    request.client_idempotency_key[0] = 'k';
    char tiny[16];
    CHECK(!proto_build_request(PROTO_EP_TRANSACTION_SETTLE, &request, tiny, sizeof(tiny), &length), "缓冲区不足必须返回失败，不得静默截断");
    CHECK(!proto_build_request(PROTO_EP_TRANSACTION_SETTLE, NULL, body, sizeof(body), &length), "空请求必须被拒绝");

    /* GET 端点没有报文体 */
    memset(&request, 0, sizeof(request));
    length = 123;
    CHECK(proto_build_request(PROTO_EP_CATALOG, &request, body, sizeof(body), &length) && length == 0 &&
              body[0] == '\0', "GET /catalog 没有报文体");
    CHECK(proto_build_request(PROTO_EP_PRICE_LIST, &request, body, sizeof(body), &length) && length == 0,
          "GET /price-list 没有报文体");
}

/* ---------------- 响应解析（7 端点） ---------------- */

static void test_response_parsing(void) {
    proto_response_t response;

    const char *activate =
        "{\"market\":{\"market_code\":\"M-0001\",\"name\":\"示例菜市场\"},"
        "\"stall\":{\"stall_no\":\"A-012\"},\"business_date\":\"2026-10-06\","
        "\"server_time\":\"2026-10-06 08:12:33\",\"config\":{\"offline_warn_threshold\":200,"
        "\"customer_base_url\":\"http://192.168.1.10:8000/customer\",\"catalog_version\":7,"
        "\"price_list_business_date\":\"2026-10-06\",\"proto\":1}}";
    CHECK(proto_parse_response(PROTO_EP_DEVICES_ACTIVATE, activate, strlen(activate), &response), "激活响应必须解析成功");
    CHECK(strcmp(response.market_code, "M-0001") == 0 && strcmp(response.stall_no, "A-012") == 0,
          "激活响应必须取到 market_code / stall_no");
    CHECK(response.offline_warn_threshold == 200 && response.catalog_version == 7 && response.proto_version == 1,
          "激活响应必须取到运行配置（阈值 / 字典版本 / 协议版本）");
    CHECK(strcmp(response.customer_base_url, "http://192.168.1.10:8000/customer") == 0 &&
              strcmp(response.price_list_business_date, "2026-10-06") == 0,
          "激活响应必须取到顾客页基址与价目表营业日");

    const char *heartbeat = "{\"business_date\":\"2026-10-06\",\"server_time\":\"2026-10-06 08:13:00\","
                            "\"config_changed\":false,\"catalog_version\":7}";
    CHECK(proto_parse_response(PROTO_EP_DEVICES_HEARTBEAT, heartbeat, strlen(heartbeat), &response) &&
              strcmp(response.business_date, "2026-10-06") == 0 && response.catalog_version == 7 &&
              !response.config_changed,
          "心跳响应字段不符");

    const char *catalog =
        "{\"catalog_version\":8,\"products\":[{\"product_id\":1234,\"name\":\"本地小白菜\","
        "\"category_id\":3,\"status\":\"active\"},{\"product_id\":1235,\"name\":\"A\\u00e9B\","
        "\"category_id\":3,\"status\":\"disabled\"}],\"categories\":[{\"category_id\":3,"
        "\"name\":\"蔬菜\"}]}";
    CHECK(proto_parse_response(PROTO_EP_CATALOG, catalog, strlen(catalog), &response), "字典响应必须解析成功");
    CHECK(response.catalog_version == 8 && response.product_count == 2 && response.category_count == 1,
          "字典响应条数不符（products=%d categories=%d）", (int)response.product_count,
          (int)response.category_count);
    CHECK(strcmp(response.products[0].name, "本地小白菜") == 0 && response.products[0].active &&
              response.products[0].product_id == 1234 && response.products[0].category_id == 3,
          "字典首条商品不符");
    CHECK(strcmp(response.products[1].name, "AéB") == 0 && !response.products[1].active,
          "\\uXXXX 中文名与 status=disabled 必须正确（实得 \"%s\" active=%d）", response.products[1].name,
          (int)response.products[1].active);
    CHECK(strcmp(response.categories[0].name, "蔬菜") == 0, "品类名不符");

    const char *price_list = "{\"business_date\":\"2026-10-06\",\"items\":[{\"product_id\":1234,"
                             "\"unit_price_cents\":320}],\"price_list_version\":11}";
    CHECK(proto_parse_response(PROTO_EP_PRICE_LIST, price_list, strlen(price_list), &response) &&
              response.price_list_version == 11 && response.price_item_count == 1 &&
              response.price_items[0].unit_price_cents == 320,
          "价目表响应字段不符");
    /* 契约 §3.4：价目表为空**不得报错**（秤端应拒绝计价并提示先设价，而不是按 0 元成交） */
    const char *empty = "{\"business_date\":\"2026-10-06\",\"items\":[],\"price_list_version\":12}";
    CHECK(proto_parse_response(PROTO_EP_PRICE_LIST, empty, strlen(empty), &response) &&
              response.price_item_count == 0,
          "空价目表必须解析成功且条数为 0");

    const char *report = "{\"transaction_no\":\"T-20261006-0007\",\"business_date\":\"2026-10-06\","
                         "\"status\":\"priced\",\"authoritative_amount_cents\":250,"
                         "\"reported_amount_cents\":251,\"amount_mismatch\":true,"
                         "\"mismatch_detail\":[{\"product_id\":1234,\"reported_amount_cents\":251,"
                         "\"authoritative_amount_cents\":250}],\"replayed\":false,"
                         "\"receipt_url\":\"http://192.168.1.10:8000/customer/receipts/T-20261006-0007\"}";
    CHECK(proto_parse_response(PROTO_EP_TRANSACTIONS_REPORT, report, strlen(report), &response), "上报响应必须解析成功");
    CHECK(strcmp(response.transaction_no, "T-20261006-0007") == 0 && strcmp(response.status, "priced") == 0,
          "上报响应交易号 / 状态不符");
    CHECK(response.authoritative_amount_cents == 250 && response.reported_amount_cents == 251 &&
              response.amount_mismatch && !response.replayed,
          "上报响应必须同时给出两个金额与不一致标记（入账一律以 authoritative 为准，契约 §3.5）");
    CHECK(strstr(response.receipt_url, "/customer/receipts/") != NULL, "receipt_url 必须指向中台顾客页");

    const char *settle = "{\"transaction_no\":\"T-20261006-0007\",\"status\":\"priced\","
                         "\"qr_payload\":\"http://192.168.1.10:8000/customer/receipts/T-20261006-0007\"}";
    CHECK(proto_parse_response(PROTO_EP_TRANSACTION_SETTLE, settle, strlen(settle), &response) &&
              strcmp(response.qr_payload, "http://192.168.1.10:8000/customer/receipts/T-20261006-0007") == 0,
          "收款码必须来自中台下发的 qr_payload（秤端不承载顾客页，REQ-043）");
    const char *cash = "{\"transaction_no\":\"T-20261006-0007\",\"status\":\"paid\","
                       "\"paid_at\":\"2026-10-06 08:21:00\"}";
    CHECK(proto_parse_response(PROTO_EP_TRANSACTION_SETTLE, cash, strlen(cash), &response) &&
              strcmp(response.status, "paid") == 0 && strcmp(response.paid_at, "2026-10-06 08:21:00") == 0,
          "现金收款响应字段不符");

    const char *refund = "{\"transaction_no\":\"T-20261006-0007\",\"status\":\"refunded\","
                         "\"refund_amount_cents\":250,\"commission_delta_cents\":-5}";
    CHECK(proto_parse_response(PROTO_EP_TRANSACTION_REFUND, refund, strlen(refund), &response) &&
              response.refund_amount_cents == 250 && strcmp(response.status, "refunded") == 0,
          "退货响应字段不符");

    CHECK(!proto_parse_response(PROTO_EP_DEVICES_ACTIVATE, "not json", 8, &response), "非 JSON 响应必须解析失败");
    CHECK(!proto_parse_response(PROTO_EP_TRANSACTIONS_REPORT, "{}", 2, &response), "缺交易号的响应必须解析失败");
    CHECK(!proto_parse_response(PROTO_EP_DEVICES_ACTIVATE, NULL, 0, &response), "空指针必须被拒绝");
}

/* ---------------- 越权字段不得改变授权范围（`AC-027` / `REQ-036` / MT-2002 语义） ---------------- */

static void test_escalation_fields_are_not_trusted(void) {
    const char *activate = "{\"market\":{\"market_code\":\"M-0001\"},\"stall\":{\"stall_no\":\"A-012\"},"
                           "\"business_date\":\"2026-10-06\"}";
    proto_response_t activation;
    CHECK(proto_parse_response(PROTO_EP_DEVICES_ACTIVATE, activate, strlen(activate), &activation), "激活响应必须解析成功");

    proto_scope_t scope;
    proto_scope_init(&scope);
    CHECK(!proto_scope_is_active(&scope), "未激活前授权范围必须为空");
    CHECK(proto_scope_apply_activate(&activation, &scope), "激活响应必须能建立授权范围");
    CHECK(proto_scope_is_active(&scope) && proto_scope_matches_stall(&scope, "A-012"), "授权范围应为 A-012");
    CHECK(!proto_scope_matches_stall(&scope, "B-999"), "非本摊位不得匹配");
    CHECK(!proto_scope_matches_stall(&scope, NULL), "空摊位号不得匹配");

    /* 同一份交易响应，带与不带越权字段，解析结果必须**逐字节相同** */
    const char *plain = "{\"transaction_no\":\"T-20261006-0007\",\"status\":\"priced\","
                        "\"authoritative_amount_cents\":250,\"reported_amount_cents\":250,"
                        "\"amount_mismatch\":false,\"replayed\":false}";
    const char *escalated = "{\"transaction_no\":\"T-20261006-0007\",\"status\":\"priced\","
                            "\"stall_id\":999,\"market_id\":888,\"stall_no\":\"B-999\","
                            "\"market_code\":\"M-9999\",\"authoritative_amount_cents\":250,"
                            "\"reported_amount_cents\":250,\"amount_mismatch\":false,\"replayed\":false}";
    proto_response_t without_fields;
    proto_response_t with_fields;
    CHECK(proto_parse_response(PROTO_EP_TRANSACTIONS_REPORT, plain, strlen(plain), &without_fields) &&
              proto_parse_response(PROTO_EP_TRANSACTIONS_REPORT, escalated, strlen(escalated), &with_fields),
          "带 / 不带越权字段的响应都必须解析成功（不报错，但不采信）");
    CHECK(memcmp(&without_fields, &with_fields, sizeof(without_fields)) == 0,
          "响应里出现 stall_id / market_id / stall_no / market_code **不得**改变解析结果（逐字节比对）");
    CHECK(with_fields.market_code[0] == '\0' && with_fields.stall_no[0] == '\0', "交易响应不得填充授权字段 —— 授权字段只由激活响应填");

    proto_scope_t before = scope;
    CHECK(!proto_scope_apply_activate(&with_fields, &scope), "非激活响应**不得**被用来建立授权范围");
    CHECK(memcmp(&before, &scope, sizeof(scope)) == 0, "被拒绝后授权范围必须逐字节不变");
    CHECK(!proto_scope_matches_stall(&scope, "B-999"), "越权响应不得把授权范围扩大到 B-999");
    CHECK(proto_scope_matches_stall(&scope, "A-012"), "原授权范围必须保持 A-012");

    proto_scope_t fresh;
    proto_scope_init(&fresh);
    proto_response_t empty;
    memset(&empty, 0, sizeof(empty));
    CHECK(!proto_scope_apply_activate(&empty, &fresh) && !proto_scope_is_active(&fresh), "空响应不得激活授权范围");
    CHECK(!proto_scope_apply_activate(NULL, &fresh), "空指针必须被拒绝");
    CHECK(!proto_scope_apply_activate(&activation, NULL), "空指针必须被拒绝");
}

/* ---------------- 错误码分类：未知码绝不当成功（契约 §7） ---------------- */

static void test_error_classification(void) {
    proto_error_t error;
    const char *unauthorized = "{\"error\":{\"code\":\"MT-2001\",\"message\":\"设备未激活或令牌无效\","
                               "\"detail\":{}}}";
    CHECK(proto_classify(unauthorized, strlen(unauthorized), 401, &error) == PROTO_RESULT_ERROR_KNOWN,
          "已知错误码必须归类为 ERROR_KNOWN");
    CHECK(strcmp(error.code, "MT-2001") == 0 && strcmp(error.message, "设备未激活或令牌无效") == 0 && error.http_status == 401,
          "错误码 / 消息 / 状态必须解析出来");

    const char *forbidden = "{\"error\":{\"code\":\"MT-2002\",\"message\":\"设备与目标摊位不匹配\"}}";
    CHECK(proto_classify(forbidden, strlen(forbidden), 403, &error) == PROTO_RESULT_ERROR_KNOWN &&
              strcmp(error.code, "MT-2002") == 0,
          "MT-2002 必须可识别（调用方据此清掉越权字段、不重试）");

    /* 未知错误码：即使 HTTP 200 也**绝不当成功** */
    const char *unknown = "{\"error\":{\"code\":\"MT-2999\",\"message\":\"未来版本的新错误\"}}";
    CHECK(proto_classify(unknown, strlen(unknown), 200, &error) == PROTO_RESULT_ERROR_UNKNOWN, "未知错误码即使伴随 2xx 也不得当成功");
    CHECK(strcmp(error.code, "MT-2999") == 0, "未知错误码也必须如实带出");
    CHECK(proto_classify(unknown, strlen(unknown), 409, &error) != PROTO_RESULT_OK, "未知错误码不得被判为 OK");

    const char *no_code = "{\"error\":{\"message\":\"没有错误码\"}}";
    CHECK(proto_classify(no_code, strlen(no_code), 500, &error) == PROTO_RESULT_MALFORMED, "有 error 对象但无错误码必须判为 MALFORMED");

    const char *success = "{\"transaction_no\":\"T-1\",\"status\":\"priced\"}";
    CHECK(proto_classify(success, strlen(success), 200, &error) == PROTO_RESULT_OK, "2xx 正常响应应为 OK");
    CHECK(proto_classify(success, strlen(success), 201, &error) == PROTO_RESULT_OK, "201 也应为 OK");
    CHECK(proto_classify(success, strlen(success), 500, &error) == PROTO_RESULT_MALFORMED, "非 2xx 且无错误体不得当成功");
    CHECK(proto_classify("", 0, 200, &error) == PROTO_RESULT_MALFORMED, "空响应不得当成功");
    CHECK(proto_classify(NULL, 0, 200, &error) == PROTO_RESULT_MALFORMED, "空指针不得当成功");
    const char *lookalike = "{\"status\":\"priced\",\"error_rate\":0}";
    CHECK(proto_classify(lookalike, strlen(lookalike), 200, &error) == PROTO_RESULT_OK, "形近键 error_rate 不得被当成 error 对象");

    CHECK(proto_error_is_known("MT-1001") && proto_error_is_known("MT-1008") &&
              proto_error_is_known("MT-1012") && proto_error_is_known("MT-2005"),
          "主契约与秤端段的已知错误码必须被登记");
    CHECK(!proto_error_is_known("MT-2999") && !proto_error_is_known("MT-0000") &&
              !proto_error_is_known("") && !proto_error_is_known(NULL),
          "未登记 / 空 / NULL 错误码一律不得判为已知");
}

int main(void) {
    test_endpoint_table();
    test_request_wire_format();
    test_response_parsing();
    test_escalation_fields_are_not_trusted();
    test_error_classification();
    printf("[%s] test_proto: 通过 %d，失败 %d\n", g_fail == 0 ? "PASS" : "FAIL", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
