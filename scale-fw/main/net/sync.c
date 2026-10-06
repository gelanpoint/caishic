/* 秤端同步编排器（`T-SCALE-12`）。设计理由见 sync.h 头部。
 * 本文件**不依赖 ESP-IDF**（验收 ④）：只有 ISO C 标准头 + `core/` 冻结接口 + sync.h，故能被主机侧
 * `zig cc` 编译并由 `test_sync.c` 用假端口实测。三条硬口径：① 补传按 `staged_at` 升序（契约 §6）；
 * ② 补传失败该条保持暂存、不阻断后续（单条失败继续下一条，只有传输层断掉才结束本轮）；
 * ③ 中台不可达不得阻塞开机（`AC-025`：`sync_init` 只读本地，一个网络调用都没有）。 */
#include "sync.h"

#include <stdio.h>
#include <stdlib.h>

/* 报文 / 响应缓冲：四个对话函数顺序调用、不重入，故共用一份，省下三份 4KB 栈。 */
static char g_body[SYNC_BODY_MAX];
static char g_response[SYNC_RESPONSE_MAX];

/* 暂存告警阈值与幂等键序号都要跨重启保留：复用字典缓存同一个存储端口（少一个存储面就少一个漂移点）。 */
#define SYNC_CACHE_TX_COUNTER "tx_counter"

static void sync_copy(char *dst, size_t cap, const char *src) {
    if (dst == NULL || cap == 0) return;
    size_t i = 0;
    if (src != NULL) {
        for (; src[i] != '\0' && i + 1 < cap; i++) dst[i] = src[i];
    }
    dst[i] = '\0';
}
static int64_t sync_digits_number(const char *text) {
    int64_t value = 0;
    for (const char *p = text; p != NULL && *p != '\0'; p++) {
        if (*p >= '0' && *p <= '9') value = value * 10 + (*p - '0');
    }
    return value;
}
/* 错误码只在"一笔都不欠"时清除：否则某条成功会擦掉"这笔为什么卡住"，界面看不到原因。 */
static void sync_settle_error(sync_t *sync) {
    if (queue_pending_count(&sync->queue) == 0) sync->last_error[0] = '\0';
}

/* 队列日志整段落盘。写失败**不得**让交易变成"已成功"：调用方按返回值决定是否报错。 */
static bool sync_persist_queue(sync_t *sync) {
    if (sync->port.queue_log_write == NULL) return true;
    return sync->port.queue_log_write(sync->port.ctx, sync->queue_log, sync->queue.used);
}

/* ================= 启动：只读本地，绝不访问网络（`AC-025`） ================= */

static void sync_restore_activate(sync_t *sync, const char *body, size_t length) {
    proto_response_t parsed;
    if (!proto_parse_response(PROTO_EP_DEVICES_ACTIVATE, body, length, &parsed)) return;
    if (!proto_scope_apply_activate(&parsed, &sync->scope)) return; /* 授权范围只由激活响应建立 */
    sync_copy(sync->customer_base_url, sizeof(sync->customer_base_url), parsed.customer_base_url);
    sync_copy(sync->business_date, sizeof(sync->business_date), parsed.business_date);
    sync->offline_warn_threshold = parsed.offline_warn_threshold;
    sync->catalog_version = parsed.catalog_version;
    sync->queue.warn_threshold = (size_t)(parsed.offline_warn_threshold > 0 ? parsed.offline_warn_threshold : 0);
    sync->state = SYNC_STATE_READY;
}
static void sync_restore_cache(sync_t *sync) {
    size_t length = 0;
    if (sync->port.cache_read == NULL) return;

    if (sync->port.cache_read(sync->port.ctx, "activate", g_response, sizeof(g_response) - 1, &length)) sync_restore_activate(sync, g_response, length);
    proto_response_t parsed;
    if (sync->port.cache_read(sync->port.ctx, "catalog", g_response, sizeof(g_response) - 1, &length) &&
        proto_parse_response(PROTO_EP_CATALOG, g_response, length, &parsed)) {
        sync->product_count = parsed.product_count;
        memcpy(sync->products, parsed.products, parsed.product_count * sizeof(proto_product_t));
        if (parsed.catalog_version > 0) sync->catalog_version = parsed.catalog_version;
    }
    if (sync->port.cache_read(sync->port.ctx, "price_list", g_response, sizeof(g_response) - 1, &length) &&
        proto_parse_response(PROTO_EP_PRICE_LIST, g_response, length, &parsed)) {
        sync->price_count = parsed.price_item_count;
        memcpy(sync->prices, parsed.price_items, parsed.price_item_count * sizeof(proto_price_item_t));
        if (parsed.price_list_version > 0) sync->price_list_version = parsed.price_list_version;
        if (parsed.business_date[0] != '\0') sync_copy(sync->business_date, sizeof(sync->business_date), parsed.business_date);
    }
    /* 幂等键序号跨重启持久化：读不到就从 0 起（开机时刻仍参与构键）。 */
    if (sync->port.cache_read != NULL &&
        sync->port.cache_read(sync->port.ctx, SYNC_CACHE_TX_COUNTER, g_body, sizeof(g_body) - 1, &length)) {
        g_body[length] = '\0';
        sync->tx_counter = sync_digits_number(g_body);
    }
}

/* 开机时刻 `HHMMSS`（幂等键的一部分）。时钟未同步时端口可以给 000000，序号仍有持久化兜底。 */
static void sync_read_boot_stamp(sync_t *sync) {
    char stamp[PROTO_TIME_MAX];
    sync_copy(sync->boot_stamp, sizeof(sync->boot_stamp), "000000");
    if (sync->port.local_time_iso == NULL) return;
    if (!sync->port.local_time_iso(sync->port.ctx, stamp, sizeof(stamp))) return;
    const char *clock = strchr(stamp, ' ');
    clock = (clock != NULL) ? clock + 1 : stamp;
    size_t n = 0;
    for (const char *p = clock; *p != '\0' && n < 6; p++) {
        if (*p >= '0' && *p <= '9') sync->boot_stamp[n++] = *p;
    }
    sync->boot_stamp[6] = '\0';
}
void sync_init(sync_t *sync, const sync_port_t *port, const char *device_id, const char *device_token) {
    if (sync == NULL || port == NULL) return;
    memset(sync, 0, sizeof(*sync));
    sync->port = *port;
    sync_copy(sync->device_id, sizeof(sync->device_id), device_id);
    sync_copy(sync->device_token, sizeof(sync->device_token), device_token);
    sync->state = SYNC_STATE_UNACTIVATED;
    sync->link = (port->link_state != NULL) ? port->link_state(port->ctx) : SYNC_LINK_DOWN;
    sync_read_boot_stamp(sync);

    /* 读回日志 → 逐条校验 CRC：断电残片如实上报、绝不当成一条交易（`RL-9` / `NFR-014`）。 */
    queue_init(&sync->queue, sync->queue_log, sizeof(sync->queue_log), 0);
    size_t used = 0;
    if (sync->port.queue_log_read != NULL &&
        sync->port.queue_log_read(sync->port.ctx, sync->queue_log, sizeof(sync->queue_log), &used)) {
        queue_recovery_t report;
        (void)queue_recover(&sync->queue, sync->queue_log, used, &report);
    }
    sync_restore_cache(sync);
}
bool sync_is_online(const sync_t *sync) { return sync != NULL && sync->link == SYNC_LINK_UP; }

bool sync_is_operational(const sync_t *sync) {
    return sync != NULL && sync->state == SYNC_STATE_READY && proto_scope_is_active(&sync->scope);
}
void sync_set_link(sync_t *sync, sync_link_t link) {
    if (sync != NULL) sync->link = link;
}

/* ================= 本地计价（断网可用：`REQ-037` / `AC-028`） ================= */

bool sync_price_line(const sync_t *sync, int64_t product_id, weight_grams_t weight,
                     money_cents_t *amount) {
    if (sync == NULL || amount == NULL) return false;
    if (!pricing_weight_is_priceable(weight, SYNC_MAX_WEIGHT_GRAMS)) return false; /* `REQ-027` */
    for (size_t i = 0; i < sync->price_count; i++) {
        if (sync->prices[i].product_id == product_id) {
            return price_amount(sync->prices[i].unit_price_cents, weight, amount);
        }
    }
    return false; /* 无当日价目表 → 拒绝计价（MT-1006 语义），**不得按 0 元成交** */
}

/* ================= 上报与补传 ================= */

typedef enum {
    SYNC_SEND_OK = 0,     /* 中台已收下（2xx）→ 可清除本地副本 */
    SYNC_SEND_RETRY = 1,  /* 该条保持暂存，下一轮重试 */
    SYNC_SEND_OFFLINE = 2 /* 传输层断了：本轮到此为止（条目全部保持暂存） */
} sync_send_t;

static sync_send_t sync_send_record(sync_t *sync, const queue_record_t *record) {
    size_t body_length = 0;
    /* 上报体暂存时已落盘：补传重发同一份字节，同一幂等键必然同体（不会误触 MT-1012）。 */
    if (!sync->port.blob_get(sync->port.ctx, record->idempotency_key, g_body, sizeof(g_body) - 1, &body_length)) return SYNC_SEND_RETRY;
    g_body[body_length] = '\0';

    sync_http_result_t result;
    memset(&result, 0, sizeof(result));
    if (!sync->port.http_request(sync->port.ctx, "POST", proto_endpoint_path(PROTO_EP_TRANSACTIONS_REPORT),
                                 sync->device_token, record->idempotency_key, g_body, body_length,
                                 g_response, sizeof(g_response) - 1, &result)) return SYNC_SEND_OFFLINE;

    proto_error_t error;
    proto_result_t classified = proto_classify(g_response, result.body_length, result.http_status, &error);
    if (classified == PROTO_RESULT_OK) {
        /* 补传成功 → 清除本地副本：先 ack（core 把记录载荷清零），再丢掉报文副本（`NFR-013` / `RL-8`）。 */
        (void)queue_ack(&sync->queue, record->seq);
        (void)sync->port.blob_drop(sync->port.ctx, record->idempotency_key);
        sync->backfill_sent += 1;
        (void)sync_persist_queue(sync);
        sync_settle_error(sync);
        return SYNC_SEND_OK;
    }
    sync_copy(sync->last_error, sizeof(sync->last_error), error.code[0] != '\0' ? error.code : "MT-2xxx");
    if (classified == PROTO_RESULT_ERROR_KNOWN && strcmp(error.code, "MT-2003") == 0) {
        sync->proto_incompatible = true; /* 协议不兼容：**不得降级重试**（契约 §4） */
    }
    return SYNC_SEND_RETRY;
}
void sync_backfill(sync_t *sync) {
    if (sync == NULL || sync->link != SYNC_LINK_UP) return;
    size_t pending = queue_pending_count(&sync->queue);
    if (pending == 0) return;
    /* `queue_pending_order` 要求容量不小于待补传条数（否则不给半份乱序结果），故按 pending 分配。 */
    queue_record_t *batch = (queue_record_t *)malloc(pending * sizeof(queue_record_t));
    if (batch == NULL) return; /* 分配失败：一条都不丢，下一轮再来 */
    size_t count = queue_pending_order(&sync->queue, batch, pending);
    size_t budget = (count < SYNC_BACKFILL_PER_ROUND) ? count : SYNC_BACKFILL_PER_ROUND;
    for (size_t i = 0; i < budget; i++) {
        sync_send_t outcome = sync_send_record(sync, &batch[i]);
        if (outcome == SYNC_SEND_OFFLINE) {
            sync->backfill_failed += 1;
            break; /* 网络断了：本轮结束；其余条目保持暂存 */
        }
        if (outcome == SYNC_SEND_RETRY) sync->backfill_failed += 1; /* 保持暂存，**不阻断后续**（契约 §6） */
        if (sync->proto_incompatible) break;
    }
    free(batch);
}
void sync_make_idempotency_key(const sync_t *sync, int64_t local_seq, char *out, size_t cap) {
    if (sync == NULL || out == NULL || cap == 0) return;
    char day[9];
    size_t n = 0;
    for (const char *p = sync->business_date; *p != '\0' && n < 8; p++) {
        if (*p >= '0' && *p <= '9') day[n++] = *p;
    }
    while (n < 8) day[n++] = '0';
    day[8] = '\0';
    /* 营业日 + 开机时刻 + **跨重启持久化**的序号：重复的后果不是报错而是中台返回别人那笔的结果。 */
    (void)snprintf(out, cap, "%s-%s-%04d", day, sync->boot_stamp, (int)(local_seq % 10000));
}
queue_status_t sync_submit(sync_t *sync, const proto_line_t *lines, size_t line_count,
                           money_cents_t total, int64_t *out_seq) {
    if (sync == NULL || lines == NULL || out_seq == NULL) return QUEUE_ERR_INVALID;
    if (!pricing_items_count_ok(line_count, SYNC_MAX_ITEMS)) return QUEUE_ERR_INVALID;
    if (!sync_is_operational(sync)) return QUEUE_ERR_INVALID; /* 未激活：不得进入营业流程（`AC-027`） */

    proto_request_t request;
    memset(&request, 0, sizeof(request));
    sync->local_tx_seq += 1;
    sync->tx_counter += 1;
    sync_make_idempotency_key(sync, sync->tx_counter, request.client_idempotency_key, sizeof(request.client_idempotency_key));
    sync_copy(request.business_date, sizeof(request.business_date), sync->business_date);
    if (sync->port.local_time_iso == NULL) return QUEUE_ERR_INVALID;
    if (!sync->port.local_time_iso(sync->port.ctx, request.captured_at, sizeof(request.captured_at))) return QUEUE_ERR_INVALID;
    request.price_list_version = sync->price_list_version;
    request.amount_cents = total;
    request.round_off_cents = 0;
    /* `origin` 取**暂存时**的链路状态：此刻离线 → 补传（契约 §3.5 / §6）。 */
    request.offline_backfill = (sync->link != SYNC_LINK_UP);
    request.item_count = line_count;
    memcpy(request.items, lines, line_count * sizeof(proto_line_t));

    size_t body_length = 0;
    if (!proto_build_request(PROTO_EP_TRANSACTIONS_REPORT, &request, g_body, sizeof(g_body), &body_length)) {
        return QUEUE_ERR_INVALID;
    }
    /* 写前日志（先报文副本、后队列记录）：任一步失败都不得留半条，且必须明确报错（`REQ-030`）。 */
    if (sync->port.blob_put == NULL ||
        !sync->port.blob_put(sync->port.ctx, request.client_idempotency_key, g_body, body_length)) {
        return QUEUE_ERR_IO;
    }
    queue_record_t record = {0};
    sync_copy(record.idempotency_key, sizeof(record.idempotency_key), request.client_idempotency_key);
    record.staged_at = (sync->port.now_ms != NULL) ? sync->port.now_ms(sync->port.ctx) : 0;
    record.business_date = sync_digits_number(sync->business_date);
    record.amount_cents = total;

    int64_t seq = 0;
    queue_status_t status = queue_stage(&sync->queue, &record, &seq);
    if (status < 0) {
        (void)sync->port.blob_drop(sync->port.ctx, request.client_idempotency_key); /* 回滚：不留孤儿报文 */
        return status;
    }
    *out_seq = seq;
    if (!sync_persist_queue(sync)) return QUEUE_ERR_IO; /* 队列没落盘 → 不得当成功 */
    if (sync->port.cache_write != NULL) { /* 序号落盘：跨重启不复用幂等键（`REQ-039`） */
        char counter[24];
        int written = snprintf(counter, sizeof(counter), "%lld", (long long)sync->tx_counter);
        if (written > 0) (void)sync->port.cache_write(sync->port.ctx, SYNC_CACHE_TX_COUNTER, counter, (size_t)written);
    }
    if (status == QUEUE_WARN_THRESHOLD) sync->warn_threshold_reached = true; /* 只告警、仍继续接受（`NFR-014`） */
    if (sync->link == SYNC_LINK_UP) {
        /* 在线：立刻试发一次。发失败**不算交易失败**——它已持久暂存，留给补传（绝不静默丢弃）。 */
        queue_record_t staged;
        if (queue_get(&sync->queue, seq, &staged)) (void)sync_send_record(sync, &staged);
    }
    return status;
}

/* ================= 与中台的一次对话：激活 → 心跳 → 拉配置 ================= */

static bool sync_call(sync_t *sync, const char *method, const char *path, const char *idempotency_key,
                      const char *body, size_t body_length, sync_http_result_t *result,
                      proto_error_t *error) {
    memset(result, 0, sizeof(*result));
    if (!sync->port.http_request(sync->port.ctx, method, path, sync->device_token, idempotency_key, body,
                                 body_length, g_response, sizeof(g_response) - 1, result)) return false;
    proto_result_t classified = proto_classify(g_response, result->body_length, result->http_status, error);
    if (classified != PROTO_RESULT_OK) {
        sync_copy(sync->last_error, sizeof(sync->last_error), error->code[0] != '\0' ? error->code : "MT-2xxx");
        if (classified == PROTO_RESULT_ERROR_KNOWN && strcmp(error->code, "MT-2003") == 0) {
            sync->proto_incompatible = true;
        }
        return false;
    }
    sync_settle_error(sync);
    return true;
}
static void sync_activate(sync_t *sync) {
    proto_request_t request;
    memset(&request, 0, sizeof(request));
    sync_copy(request.device_id, sizeof(request.device_id), sync->device_id);
    sync_copy(request.firmware_version, sizeof(request.firmware_version), SYNC_FIRMWARE_VERSION);
    sync_copy(request.hardware_rev, sizeof(request.hardware_rev), SYNC_HARDWARE_REV);
    size_t body_length = 0;
    if (!proto_build_request(PROTO_EP_DEVICES_ACTIVATE, &request, g_body, sizeof(g_body), &body_length)) return;

    sync_http_result_t result;
    proto_error_t error;
    if (!sync_call(sync, "POST", proto_endpoint_path(PROTO_EP_DEVICES_ACTIVATE), NULL, g_body, body_length,
                   &result, &error)) return; /* 不可达 / 被拒：保持未激活，但**不阻塞开机**（`AC-025`） */
    /* 缓存中台**原始响应**：重启后由同一个解析器读回，不另造配置序列化。 */
    if (sync->port.cache_write != NULL) (void)sync->port.cache_write(sync->port.ctx, "activate", g_response, result.body_length);
    sync_restore_activate(sync, g_response, result.body_length);
    if (sync->price_count == 0) sync->config_dirty = true; /* 还没有价目表 → 下一轮拉 */
}
static void sync_heartbeat(sync_t *sync) {
    proto_request_t request;
    memset(&request, 0, sizeof(request));
    sync_copy(request.device_id, sizeof(request.device_id), sync->device_id);
    sync_copy(request.firmware_version, sizeof(request.firmware_version), SYNC_FIRMWARE_VERSION);
    /* `pending_count` 仅供中台观测"哪些摊位长期有未补传数据"，**不据此拒绝任何交易**（契约 §3.2）。 */
    request.pending_count = (int64_t)queue_pending_count(&sync->queue);
    size_t body_length = 0;
    if (!proto_build_request(PROTO_EP_DEVICES_HEARTBEAT, &request, g_body, sizeof(g_body), &body_length)) return;

    sync_http_result_t result;
    proto_error_t error;
    if (!sync_call(sync, "POST", proto_endpoint_path(PROTO_EP_DEVICES_HEARTBEAT), NULL, g_body, body_length,
                   &result, &error)) return;
    proto_response_t parsed;
    if (!proto_parse_response(PROTO_EP_DEVICES_HEARTBEAT, g_response, result.body_length, &parsed)) return;
    if (parsed.business_date[0] != '\0') {
        sync_copy(sync->business_date, sizeof(sync->business_date), parsed.business_date);
    }
    if (parsed.catalog_version > 0) sync->catalog_version = parsed.catalog_version;
    if (parsed.config_changed) sync->config_dirty = true;
}

/* 拉字典或价目表：两者只差端点、缓存名与落到哪个数组，故合成一处（少一处重复少一个漂移点）。 */
static void sync_fetch_list(sync_t *sync, proto_endpoint_t endpoint, const char *cache_name,
                            bool is_price_list) {
    const char *base = proto_endpoint_path(endpoint);
    char path[SYNC_PATH_MAX];
    if (endpoint == PROTO_EP_CATALOG) (void)snprintf(path, sizeof(path), "%s?since_version=%lld", base, (long long)sync->catalog_version);
    else if (sync->business_date[0] != '\0') (void)snprintf(path, sizeof(path), "%s?business_date=%s", base, sync->business_date);
    else (void)sync_copy(path, sizeof(path), base);
    sync_http_result_t result;
    proto_error_t error;
    if (!sync_call(sync, "GET", path, NULL, NULL, 0, &result, &error)) return;

    proto_response_t parsed;
    if (!proto_parse_response(endpoint, g_response, result.body_length, &parsed)) return;
    if (is_price_list) {
        /* 空价目表**不是错误**（契约 §3.4）：本地计价会拒绝并提示先设价，而不是按 0 元成交。 */
        sync->price_count = parsed.price_item_count;
        memcpy(sync->prices, parsed.price_items, parsed.price_item_count * sizeof(proto_price_item_t));
        if (parsed.price_list_version > 0) sync->price_list_version = parsed.price_list_version;
        if (parsed.business_date[0] != '\0') {
            sync_copy(sync->business_date, sizeof(sync->business_date), parsed.business_date);
        }
    } else {
        sync->product_count = parsed.product_count;
        memcpy(sync->products, parsed.products, parsed.product_count * sizeof(proto_product_t));
        if (parsed.catalog_version > 0) sync->catalog_version = parsed.catalog_version;
    }
    if (sync->port.cache_write != NULL) (void)sync->port.cache_write(sync->port.ctx, cache_name, g_response, result.body_length);
}
bool sync_settle(sync_t *sync, const char *transaction_no, bool cash, char *qr_payload, size_t cap) {
    if (sync == NULL || transaction_no == NULL || qr_payload == NULL || cap == 0) return false;
    qr_payload[0] = '\0';
    proto_request_t request;
    memset(&request, 0, sizeof(request));
    sync_copy(request.transaction_no, sizeof(request.transaction_no), transaction_no);
    /* 收款是幂等路由：`proto_build_request` 要求请求结构里**带着**幂等键（键走 `Idempotency-Key` 头）。 */
    sync_copy(request.client_idempotency_key, sizeof(request.client_idempotency_key), transaction_no);
    request.settle_cash = cash;
    size_t body_length = 0;
    if (!proto_build_request(PROTO_EP_TRANSACTION_SETTLE, &request, g_body, sizeof(g_body), &body_length)) return false;
    /* 端点路径含 `{transaction_no}` 占位：路径装配属传输层，在这里换成真实单号。
     * 幂等键用单号本身：同一次收款重发不会在中台记出第二笔（`REQ-026`）。 */
    const char *templ = proto_endpoint_path(PROTO_EP_TRANSACTION_SETTLE);
    const char *mark = strstr(templ, "{transaction_no}");
    if (mark == NULL) return false;
    char path[SYNC_PATH_MAX];
    (void)snprintf(path, sizeof(path), "%.*s%s%s", (int)(mark - templ), templ, transaction_no, mark + strlen("{transaction_no}"));
    sync_http_result_t result;
    proto_error_t error;
    if (!sync_call(sync, "POST", path, request.client_idempotency_key, g_body, body_length, &result, &error)) return false;
    proto_response_t parsed;
    if (!proto_parse_response(PROTO_EP_TRANSACTION_SETTLE, g_response, result.body_length, &parsed)) return false;
    sync_copy(qr_payload, cap, parsed.qr_payload);
    return true;
}
void sync_poll(sync_t *sync) {
    if (sync == NULL) return;
    sync->link = (sync->port.link_state != NULL) ? sync->port.link_state(sync->port.ctx) : SYNC_LINK_DOWN;
    if (sync->link != SYNC_LINK_UP) return; /* 离线：界面照常营业，不折腾 */
    if (sync->proto_incompatible) return;   /* 协议不兼容：明确提示升级，不重试（契约 §4） */

    if (!proto_scope_is_active(&sync->scope)) sync_activate(sync);
    if (proto_scope_is_active(&sync->scope)) {
        sync_heartbeat(sync);
        if (sync->config_dirty || sync->price_count == 0) {
            sync_fetch_list(sync, PROTO_EP_CATALOG, "catalog", false);
            sync_fetch_list(sync, PROTO_EP_PRICE_LIST, "price_list", true);
            if (sync->price_count > 0) sync->config_dirty = false;
        }
    }
    sync_backfill(sync);
}
