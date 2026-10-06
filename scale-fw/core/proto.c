/* 秤端 ↔ 中台**报文**窄编解码的实现（`plan.md` §4 `scale-fw/core/proto.c`）。
 * 只做报文体：端点数与字段白名单以 `contracts/scale-midplatform.md` 为准；路径 / 查询串 / 请求头
 * 属传输层装配，归 `T-SCALE-12` 的 HTTP 客户端。不依赖 ESP-IDF、不引入第三方库。 */
#include "proto.h"

typedef struct { const char *path; const char *method; bool idempotent; } proto_route_t;
static const proto_route_t kRoutes[PROTO_ENDPOINT_COUNT] = {
    {"/api/scale/v1/devices/activate", "POST", false},
    {"/api/scale/v1/devices/heartbeat", "POST", false},
    {"/api/scale/v1/catalog", "GET", false},
    {"/api/scale/v1/price-list", "GET", false},
    {"/api/scale/v1/transactions", "POST", true},
    {"/api/scale/v1/transactions/{transaction_no}/settle", "POST", true},
    {"/api/scale/v1/transactions/{transaction_no}/refund", "POST", true},
};
static bool route_ok(proto_endpoint_t e) { return e >= PROTO_EP_DEVICES_ACTIVATE && e <= PROTO_EP_TRANSACTION_REFUND; }
const char *proto_endpoint_path(proto_endpoint_t e) { return route_ok(e) ? kRoutes[e - 1].path : NULL; }
const char *proto_endpoint_method(proto_endpoint_t e) { return route_ok(e) ? kRoutes[e - 1].method : NULL; }
bool proto_endpoint_requires_idempotency_key(proto_endpoint_t e) { return route_ok(e) && kRoutes[e - 1].idempotent; }

static const char *p_skip(const char *p, const char *e) {
    while (p < e && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) p++;
    return p;
}
static const char *p_find(const char *b, const char *e, const char *key) {
    size_t n = strlen(key);
    for (const char *p = b; p + n + 2 <= e; p++) {
        if (*p != '"' || memcmp(p + 1, key, n) != 0 || p[1 + n] != '"') continue;
        const char *q = p_skip(p + 2 + n, e);
        if (q < e && *q == ':') return q + 1;
    }
    return NULL;
}
static const char *p_int_at(const char *p, const char *e, int64_t *out) {
    p = p_skip(p, e);
    bool negative = false;
    if (p < e && *p == '-') { negative = true; p++; }
    if (p >= e || *p < '0' || *p > '9') return NULL;
    int64_t value = 0;
    while (p < e && *p >= '0' && *p <= '9') value = value * 10 + (*p++ - '0');
    *out = negative ? -value : value;
    return p;
}
static bool p_get_int(const char *b, const char *e, const char *key, int64_t *out) {
    const char *p = p_find(b, e, key);
    return p != NULL && p_int_at(p, e, out) != NULL;
}
static bool p_get_bool(const char *b, const char *e, const char *key, bool *out) {
    const char *p = p_find(b, e, key);
    if (p == NULL) return false; p = p_skip(p, e);
    if (p + 4 <= e && memcmp(p, "true", 4) == 0) { *out = true; return true; }
    if (p + 5 <= e && memcmp(p, "false", 5) == 0) { *out = false; return true; }
    return false;
}
/* 码位 → UTF-8 字节（`\uXXXX` 转义必须还原成真正的 UTF-8，不得用占位符顶替）。 */
static bool p_put_utf8(char *out, size_t cap, size_t *used, int value) {
    unsigned char bytes[3];
    size_t n;
    if (value < 0x80) { bytes[0] = (unsigned char)value; n = 1; }
    else if (value < 0x800) { bytes[0] = (unsigned char)(0xC0 | (value >> 6)); bytes[1] = (unsigned char)(0x80 | (value & 0x3F)); n = 2; }
    else { bytes[0] = (unsigned char)(0xE0 | (value >> 12)); bytes[1] = (unsigned char)(0x80 | ((value >> 6) & 0x3F)); bytes[2] = (unsigned char)(0x80 | (value & 0x3F)); n = 3; }
    if (*used + n + 1 > cap) return false;
    for (size_t i = 0; i < n; i++) out[(*used)++] = (char)bytes[i];
    return true;
}
static bool p_get_str(const char *b, const char *e, const char *key, char *out, size_t cap) {
    const char *p = p_find(b, e, key);
    if (p == NULL) return false;
    p = p_skip(p, e);
    if (p >= e || *p != '"') return false;
    p++;
    size_t used = 0;
    while (p < e && *p != '"') {
        char c = *p;
        if (c == '\\' && p + 1 < e) {
            p++;
            c = *p;
            if (c == 'n') c = '\n';
            else if (c == 't') c = '\t';
            else if (c == 'r') c = '\r';
            else if (c == 'u') {
                if (p + 4 >= e) return false;
                int value = 0;
                for (int i = 1; i <= 4; i++) {
                    char h = (char)(p[i] | 0x20);
                    int digit = (h >= '0' && h <= '9') ? h - '0' : ((h >= 'a' && h <= 'f') ? h - 'a' + 10 : -1);
                    if (digit < 0) return false; /* 非法转义：显式失败，不静默改写 */
                    value = value * 16 + digit;
                }
                p += 4;
                if (!p_put_utf8(out, cap, &used, value)) return false;
                p++;
                continue;
            }
        }
        if (used + 1 < cap) out[used++] = c;
        p++;
    }
    if (p >= e) return false;
    out[used] = '\0';
    return true;
}
/* p 指向开引号，返回其闭引号之后的位置。 */
static const char *p_skip_string(const char *p, const char *e) {
    for (p++; p < e; p++) {
        if (*p == '\\') p++;
        else if (*p == '"') return p + 1;
    }
    return e;
}
/* 取 `"key": [ ... ]` 的数组内部区间（跳过字符串与嵌套括号）。 */
static bool p_array(const char *b, const char *e, const char *key, const char **cursor, const char **stop) {
    const char *p = p_find(b, e, key);
    if (p == NULL) return false;
    p = p_skip(p, e);
    if (p >= e || *p != '[') return false;
    const char *q = p + 1;
    int depth = 1;
    for (; q < e; q++) {
        if (*q == '"') { q = p_skip_string(q, e) - 1; continue; }
        if (*q == '[' || *q == '{') depth++;
        else if ((*q == ']' || *q == '}') && --depth == 0) break;
    }
    if (q >= e) return false;
    *cursor = p + 1; *stop = q;
    return true;
}
/* 取数组里的下一个扁平对象切片。 */
static bool p_next_object(const char **cursor, const char *stop, const char **begin, const char **end) {
    const char *p = *cursor;
    while (p < stop && *p != '{') p++;
    if (p >= stop) return false;
    const char *start = p;
    int depth = 0;
    for (; p < stop; p++) {
        if (*p == '"') { p = p_skip_string(p, stop) - 1; continue; }
        if (*p == '{') depth++;
        else if (*p == '}' && --depth == 0) {
            *begin = start;
            *end = p + 1;
            *cursor = p + 1;
            return true;
        }
    }
    return false;
}
/* 遍历 `"key": [ {...}, ... ]` 的每个扁平对象 */
#define P_FOREACH(body, end, key, item_begin, item_end)                    \
    const char *cursor = NULL, *stop = NULL, *item_begin = NULL, *item_end = NULL; \
    if (p_array(body, end, key, &cursor, &stop))                           \
        while (p_next_object(&cursor, stop, &item_begin, &item_end))
static bool p_put(char *out, size_t cap, size_t *used, const char *text) {
    size_t n = strlen(text);
    if (*used + n + 1 > cap) return false;
    memcpy(out + *used, text, n);
    *used += n; out[*used] = '\0';
    return true;
}
static bool p_put_int(char *out, size_t cap, size_t *used, int64_t value) {
    char buffer[24];
    size_t n = 0;
    bool negative = value < 0;
    uint64_t magnitude = negative ? (uint64_t)(-(value + 1)) + 1u : (uint64_t)value; /* INT64_MIN 安全 */
    do { buffer[n++] = (char)('0' + (magnitude % 10u)); magnitude /= 10u; } while (magnitude != 0);
    if (negative) buffer[n++] = '-';
    for (size_t i = 0, j = n - 1; i < j; i++, j--) { char swap = buffer[i]; buffer[i] = buffer[j]; buffer[j] = swap; }
    buffer[n] = '\0';
    return p_put(out, cap, used, buffer);
}
static bool p_put_quoted(char *out, size_t cap, size_t *used, const char *text) {
    static const char *kHex = "0123456789abcdef";
    if (!p_put(out, cap, used, "\"")) return false;
    for (const char *p = text; *p != '\0'; p++) {
        unsigned char c = (unsigned char)*p;
        char piece[8];
        if (c == '"' || c == '\\') { piece[0] = '\\'; piece[1] = (char)c; piece[2] = '\0'; }
        else if (c == '\n' || c == '\r' || c == '\t') { piece[0] = '\\'; piece[1] = (c == '\n') ? 'n' : ((c == '\r') ? 'r' : 't'); piece[2] = '\0'; }
        else if (c < 0x20) {
            piece[0] = '\\'; piece[1] = 'u'; piece[2] = '0'; piece[3] = '0';
            piece[4] = kHex[c >> 4]; piece[5] = kHex[c & 0x0Fu]; piece[6] = '\0';
        } else {
            piece[0] = (char)c; piece[1] = '\0';
        }
        if (!p_put(out, cap, used, piece)) return false;
    }
    return p_put(out, cap, used, "\"");
}
/* 写字段名（含逗号分隔），随后由 P_STR / P_INT / P_RAW 写值 */
static bool p_key(char *out, size_t cap, size_t *used, bool *first, const char *key) {
    if (!*first && !p_put(out, cap, used, ",")) return false;
    *first = false;
    return p_put(out, cap, used, "\"") && p_put(out, cap, used, key) && p_put(out, cap, used, "\":");
}
#define P_STR(k, v) do { if (!p_key(out, cap, &used, &first, (k)) || !p_put_quoted(out, cap, &used, (v))) return false; } while (0)
#define P_INT(k, v) do { if (!p_key(out, cap, &used, &first, (k)) || !p_put_int(out, cap, &used, (v))) return false; } while (0)

/* 请求构造（字段白名单：结构体里没有 stall_id / market_id） */
static bool p_lines(char *out, size_t cap, size_t *used, const proto_request_t *r, bool with_price) {
    if (!p_put(out, cap, used, "[")) return false;
    for (size_t i = 0; i < r->item_count; i++) {
        const proto_line_t *line = &r->items[i];
        if (i > 0 && !p_put(out, cap, used, ",")) return false;
        if (!p_put(out, cap, used, "{\"product_id\":") || !p_put_int(out, cap, used, line->product_id)) return false;
        if (with_price && (!p_put(out, cap, used, ",\"unit_price_cents\":") ||
                           !p_put_int(out, cap, used, line->unit_price_cents))) {
            return false;
        }
        if (!p_put(out, cap, used, ",\"weight_grams\":") || !p_put_int(out, cap, used, line->weight_grams)) return false;
        if (with_price && (!p_put(out, cap, used, ",\"amount_cents\":") ||
                           !p_put_int(out, cap, used, line->amount_cents))) {
            return false;
        }
        if (!p_put(out, cap, used, "}")) return false;
    }
    return p_put(out, cap, used, "]");
}
bool proto_build_request(proto_endpoint_t endpoint, const proto_request_t *request, char *out, size_t cap, size_t *out_len) {
    if (!route_ok(endpoint) || request == NULL || out == NULL || cap == 0) return false;
    if (out_len != NULL) *out_len = 0;
    out[0] = '\0';
    if (strcmp(kRoutes[endpoint - 1].method, "GET") == 0) return true; /* GET 无报文体 */
    if (kRoutes[endpoint - 1].idempotent && request->client_idempotency_key[0] == '\0') return false;
    size_t used = 0;
    bool first = true;
    if (!p_put(out, cap, &used, "{")) return false;
    switch (endpoint) {
        case PROTO_EP_DEVICES_ACTIVATE:
            P_STR("device_id", request->device_id);
            P_STR("firmware_version", request->firmware_version);
            P_STR("hardware_rev", request->hardware_rev);
            break;
        case PROTO_EP_DEVICES_HEARTBEAT:
            P_STR("device_id", request->device_id);
            P_INT("pending_count", request->pending_count);
            P_STR("firmware_version", request->firmware_version);
            break;
        case PROTO_EP_TRANSACTIONS_REPORT:
            if (request->item_count < 1 || request->item_count > PROTO_MAX_ITEMS) return false;
            P_STR("business_date", request->business_date);
            P_STR("captured_at", request->captured_at);
            P_STR("origin", request->offline_backfill ? "offline_backfill" : "online");
            P_INT("price_list_version", request->price_list_version);
            if (!p_key(out, cap, &used, &first, "items") || !p_lines(out, cap, &used, request, false)) return false;
            P_INT("amount_cents", request->amount_cents);
            if (!p_key(out, cap, &used, &first, "local_lines") || !p_lines(out, cap, &used, request, true)) return false;
            P_INT("round_off_cents", request->round_off_cents);
            P_STR("client_idempotency_key", request->client_idempotency_key);
            break;
        case PROTO_EP_TRANSACTION_SETTLE:
            P_STR("method", request->settle_cash ? "cash" : "qr");
            break;
        case PROTO_EP_TRANSACTION_REFUND:
            P_INT("amount_cents", request->refund_amount_cents);
            P_STR("reason", request->refund_reason);
            break;
        default:
            return false;
    }
    if (!p_put(out, cap, &used, "}")) return false;
    if (out_len != NULL) *out_len = used;
    return true;
}

/* 响应解析：只填白名单字段；`stall_id` / `market_id` 没有对应的读取代码 —— 不采信 */
#define G_STR(k, f) (void)p_get_str(body, end, (k), out->f, sizeof(out->f))
#define G_INT(k, f) (void)p_get_int(body, end, (k), &out->f)
#define G_BOOL(k, f) (void)p_get_bool(body, end, (k), &out->f)
bool proto_parse_response(proto_endpoint_t endpoint, const char *body, size_t length, proto_response_t *out) {
    if (!route_ok(endpoint) || body == NULL || out == NULL || length == 0) return false;
    memset(out, 0, sizeof(*out));
    const char *end = body + length;
    if (*p_skip(body, end) != '{') return false;
    switch (endpoint) {
        case PROTO_EP_DEVICES_ACTIVATE:
            G_STR("market_code", market_code);
            G_STR("stall_no", stall_no);
            G_STR("business_date", business_date);
            G_STR("server_time", server_time);
            G_INT("offline_warn_threshold", offline_warn_threshold);
            G_STR("customer_base_url", customer_base_url);
            G_INT("catalog_version", catalog_version);
            G_STR("price_list_business_date", price_list_business_date);
            G_INT("proto", proto_version);
            return out->market_code[0] != '\0' && out->stall_no[0] != '\0';
        case PROTO_EP_DEVICES_HEARTBEAT:
            G_STR("business_date", business_date);
            G_STR("server_time", server_time);
            G_BOOL("config_changed", config_changed);
            G_INT("catalog_version", catalog_version);
            return out->business_date[0] != '\0';
        case PROTO_EP_CATALOG:
            G_INT("catalog_version", catalog_version);
            {
                P_FOREACH(body, end, "products", ib, ie) {
                    if (out->product_count >= PROTO_MAX_PRODUCTS) break;
                    proto_product_t *product = &out->products[out->product_count++];
                    char status[16] = "";
                    (void)p_get_int(ib, ie, "product_id", &product->product_id);
                    (void)p_get_int(ib, ie, "category_id", &product->category_id);
                    (void)p_get_str(ib, ie, "status", status, sizeof(status));
                    (void)p_get_str(ib, ie, "name", product->name, sizeof(product->name));
                    product->active = strcmp(status, "active") == 0;
                }
            }
            {
                P_FOREACH(body, end, "categories", ib, ie) {
                    if (out->category_count >= PROTO_MAX_CATEGORIES) break;
                    proto_category_t *category = &out->categories[out->category_count++];
                    (void)p_get_int(ib, ie, "category_id", &category->category_id);
                    (void)p_get_str(ib, ie, "name", category->name, sizeof(category->name));
                }
            }
            return true;
        case PROTO_EP_PRICE_LIST:
            G_STR("business_date", business_date);
            G_INT("price_list_version", price_list_version);
            {
                P_FOREACH(body, end, "items", ib, ie) {
                    if (out->price_item_count >= PROTO_MAX_PRICE_ITEMS) break;
                    proto_price_item_t *item = &out->price_items[out->price_item_count++];
                    (void)p_get_int(ib, ie, "product_id", &item->product_id);
                    (void)p_get_int(ib, ie, "unit_price_cents", &item->unit_price_cents);
                }
            }
            return true;
        case PROTO_EP_TRANSACTIONS_REPORT:
            G_STR("transaction_no", transaction_no);
            G_STR("status", status);
            G_INT("authoritative_amount_cents", authoritative_amount_cents);
            G_INT("reported_amount_cents", reported_amount_cents);
            G_BOOL("amount_mismatch", amount_mismatch);
            G_BOOL("replayed", replayed);
            G_STR("receipt_url", receipt_url);
            return out->transaction_no[0] != '\0';
        case PROTO_EP_TRANSACTION_SETTLE:
            G_STR("transaction_no", transaction_no);
            G_STR("status", status);
            G_STR("qr_payload", qr_payload);
            G_STR("paid_at", paid_at);
            return out->transaction_no[0] != '\0';
        case PROTO_EP_TRANSACTION_REFUND:
            G_STR("transaction_no", transaction_no);
            G_STR("status", status);
            G_INT("refund_amount_cents", refund_amount_cents);
            return out->transaction_no[0] != '\0';
        default:
            return false;
    }
}

void proto_scope_init(proto_scope_t *scope) {
    if (scope != NULL) memset(scope, 0, sizeof(*scope));
}
bool proto_scope_apply_activate(const proto_response_t *response, proto_scope_t *scope) {
    if (response == NULL || scope == NULL) return false;
    if (response->market_code[0] == '\0' || response->stall_no[0] == '\0') return false;
    memset(scope, 0, sizeof(*scope));
    memcpy(scope->market_code, response->market_code, sizeof(scope->market_code) - 1);
    memcpy(scope->stall_no, response->stall_no, sizeof(scope->stall_no) - 1);
    return true;
}
bool proto_scope_is_active(const proto_scope_t *scope) {
    return scope != NULL && scope->market_code[0] != '\0' && scope->stall_no[0] != '\0';
}
bool proto_scope_matches_stall(const proto_scope_t *scope, const char *stall_no) {
    return proto_scope_is_active(scope) && stall_no != NULL && strcmp(scope->stall_no, stall_no) == 0;
}

static const char *kKnownCodes[] = {
    "MT-1001", "MT-1003", "MT-1004", "MT-1006", "MT-1008", "MT-1009",
    "MT-1012", "MT-2001", "MT-2002", "MT-2003", "MT-2004", "MT-2005",
};
bool proto_error_is_known(const char *code) {
    if (code == NULL) return false;
    for (size_t i = 0; i < sizeof(kKnownCodes) / sizeof(kKnownCodes[0]); i++) {
        if (strcmp(code, kKnownCodes[i]) == 0) return true;
    }
    return false;
}
proto_result_t proto_classify(const char *body, size_t length, int64_t http_status, proto_error_t *out_error) {
    if (out_error != NULL) memset(out_error, 0, sizeof(*out_error));
    if (body == NULL || length == 0) return PROTO_RESULT_MALFORMED;
    const char *end = body + length;
    const char *error = p_find(body, end, "error");
    if (error != NULL) {
        char code[16] = "";
        char message[64] = "";
        (void)p_get_str(error, end, "code", code, sizeof(code));
        (void)p_get_str(error, end, "message", message, sizeof(message));
        if (out_error != NULL) {
            out_error->http_status = http_status;
            memcpy(out_error->code, code, sizeof(out_error->code) - 1);
            memcpy(out_error->message, message, sizeof(out_error->message) - 1);
        }
        if (code[0] == '\0') return PROTO_RESULT_MALFORMED; /* 有 error 无 code：不得当成功 */
        return proto_error_is_known(code) ? PROTO_RESULT_ERROR_KNOWN : PROTO_RESULT_ERROR_UNKNOWN;
    }
    if (http_status >= 200 && http_status < 300) return PROTO_RESULT_OK;
    return PROTO_RESULT_MALFORMED;
}
