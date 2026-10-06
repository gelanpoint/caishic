/* 主机侧单测：同步编排器（`T-SCALE-12` 验收 ①②③；`AC-025` / `AC-027` / `AC-028`；`NFR-013` / `NFR-014` / `REQ-030`）。
 *
 * 为什么有这个文件（`tasks.md` T-SCALE-12 验收 ④ 的由来）：`sync.c` 刻意不依赖 ESP-IDF，只依赖
 * `core/` 冻结接口 + `sync.h` 自声明的平台端口，于是它的三条验收从"静态检查"升级为**主机侧实测断言**
 * —— 用内存假端口扮演 WiFi / 中台 / LittleFS，把「补传顺序」「失败保持暂存」「中台不可达仍能营业」真跑出来。
 * 纪律：**不引入任何测试框架依赖**（`plan.md` §4），自制断言。编译运行命令见 README。
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "sync.h"

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

/* ================= 内存假端口：扮演 WiFi + 中台 + LittleFS ================= */

#define KV_MAX 10
#define KV_BODY_MAX 4096
#define RPT_MAX 8
enum { KV_CACHE = 0, KV_BLOB = 1 };

typedef struct {
    char key[24];
    char body[KV_BODY_MAX];
    size_t length;
    int kind;
    bool used;
} kv_t;

typedef struct {
    sync_link_t link;
    int64_t clock[RPT_MAX];
    size_t clock_count, clock_index;
    size_t http_calls;
    char last_token[64];
    const char *activate_body, *heartbeat_body, *catalog_body, *price_list_body;
    char activate_error[16];
    int64_t report_status[RPT_MAX];
    char report_code[RPT_MAX][16];
    bool report_transport_fail[RPT_MAX];
    size_t report_index;
    char sent_key[RPT_MAX][24]; /* 补传顺序的取证 */
    size_t sent_count;
    size_t settle_count;
    char last_settle_key[40];
    char last_settle_body[64];
    bool blob_put_fails, queue_write_fails;
    kv_t kv[KV_MAX];
    uint8_t qlog[SYNC_QUEUE_LOG_BYTES];
    size_t qlog_length;
    bool qlog_used;
} fake_t;

static void fake_reset(fake_t *f) { memset(f, 0, sizeof(*f)); }

static kv_t *kv_find(fake_t *f, const char *key) {
    for (size_t i = 0; i < KV_MAX; i++) {
        if (f->kv[i].used && strcmp(f->kv[i].key, key) == 0) return &f->kv[i];
    }
    return NULL;
}

static size_t kv_count_blobs(const fake_t *f) {
    size_t n = 0;
    for (size_t i = 0; i < KV_MAX; i++) {
        if (f->kv[i].used && f->kv[i].kind == KV_BLOB) n++;
    }
    return n;
}

static bool kv_put(fake_t *f, const char *key, int kind, const void *data, size_t length) {
    kv_t *slot = kv_find(f, key);
    for (size_t i = 0; slot == NULL && i < KV_MAX; i++) {
        if (!f->kv[i].used) slot = &f->kv[i];
    }
    if (slot == NULL || length >= KV_BODY_MAX) return false;
    memset(slot, 0, sizeof(*slot));
    slot->used = true;
    slot->kind = kind;
    snprintf(slot->key, sizeof(slot->key), "%s", key);
    memcpy(slot->body, data, length);
    slot->length = length;
    return true;
}

static bool kv_get(fake_t *f, const char *key, char *out, size_t cap, size_t *length) {
    kv_t *slot = kv_find(f, key);
    if (slot == NULL || slot->length >= cap) return false;
    memcpy(out, slot->body, slot->length);
    out[slot->length] = '\0';
    *length = slot->length;
    return true;
}

static sync_link_t fake_link(void *ctx) { return ((fake_t *)ctx)->link; }

static int64_t fake_now_ms(void *ctx) {
    fake_t *f = (fake_t *)ctx;
    if (f->clock_count == 0) return 0;
    size_t i = f->clock_index < f->clock_count ? f->clock_index : f->clock_count - 1;
    f->clock_index++;
    return f->clock[i];
}

static bool fake_local_time(void *ctx, char *out, size_t cap) {
    (void)ctx;
    if (out == NULL || cap < 20) return false;
    snprintf(out, cap, "2026-10-06 08:20:11");
    return true;
}

static void error_body(const char *code, char *out, size_t cap) {
    snprintf(out, cap, "{\"error\":{\"code\":\"%s\",\"message\":\"测试构造\"}}", code);
}

static bool fake_http(void *ctx, const char *method, const char *path, const char *device_token,
                      const char *idem, const char *body, size_t body_length, char *response, size_t cap,
                      sync_http_result_t *result) {
    fake_t *f = (fake_t *)ctx;
    f->http_calls++;
    snprintf(f->last_token, sizeof(f->last_token), "%s", device_token == NULL ? "" : device_token);
    result->transport_ok = true;
    result->http_status = 200;
    result->body_length = 0;
    const char *source = NULL;
    const char *error = NULL;
    if (strncmp(path, "/api/scale/v1/devices/activate", 30) == 0) {
        source = f->activate_body;
        error = f->activate_error;
    } else if (strncmp(path, "/api/scale/v1/devices/heartbeat", 31) == 0) {
        source = f->heartbeat_body;
    } else if (strncmp(path, "/api/scale/v1/catalog", 21) == 0) {
        source = f->catalog_body;
    } else if (strncmp(path, "/api/scale/v1/price-list", 24) == 0) {
        source = f->price_list_body;
    } else if (strstr(path, "/settle") != NULL) {
        if (idem == NULL || body == NULL) return false;
        snprintf(f->last_settle_key, sizeof(f->last_settle_key), "%s", idem);
        snprintf(f->last_settle_body, sizeof(f->last_settle_body), "%s", body);
        f->settle_count++;
        if (strstr(body, "\"method\":\"qr\"") != NULL) {
            snprintf(response, cap, "{\"transaction_no\":\"T-20261006-0001\",\"status\":\"pending\","
                                   "\"qr_payload\":\"http://192.168.1.10:8000/customer/pay?t=abc123\"}");
        } else {
            snprintf(response, cap, "{\"transaction_no\":\"T-20261006-0001\",\"status\":\"settled\","
                                   "\"paid_at\":\"2026-10-06 08:31:00\"}");
        }
        result->body_length = strlen(response);
        return true;
    } else if (strncmp(path, "/api/scale/v1/transactions", 26) == 0) {
        /* 上报必须 POST、必须带幂等键与非空报文（否则中台无法去重） */
        if (strcmp(method, "POST") != 0 || idem == NULL || body == NULL || body_length == 0) return false;
        size_t i = f->report_index++;
        if (i >= RPT_MAX || f->report_transport_fail[i]) return false;
        if (f->sent_count < RPT_MAX) {
            snprintf(f->sent_key[f->sent_count], sizeof(f->sent_key[0]), "%s", idem);
            f->sent_count++;
        }
        if (f->report_code[i][0] != '\0') {
            error_body(f->report_code[i], response, cap);
            result->http_status = f->report_status[i] != 0 ? f->report_status[i] : 422;
        } else {
            snprintf(response, cap, "{\"transaction_no\":\"T-20261006-0001\",\"status\":\"priced\","
                                   "\"authoritative_amount_cents\":250,\"reported_amount_cents\":250,"
                                   "\"amount_mismatch\":false,\"replayed\":false}");
        }
        result->body_length = strlen(response);
        return true;
    } else {
        return false;
    }
    if (error != NULL && error[0] != '\0') {
        error_body(error, response, cap);
        result->http_status = 409;
    } else if (source == NULL) {
        return false;
    } else {
        snprintf(response, cap, "%s", source);
    }
    result->body_length = strlen(response);
    return true;
}

static bool fake_queue_read(void *ctx, uint8_t *buffer, size_t capacity, size_t *length) {
    fake_t *f = (fake_t *)ctx;
    if (!f->qlog_used || f->qlog_length > capacity) return false;
    memcpy(buffer, f->qlog, f->qlog_length);
    *length = f->qlog_length;
    return true;
}

static bool fake_queue_write(void *ctx, const uint8_t *buffer, size_t length) {
    fake_t *f = (fake_t *)ctx;
    if (f->queue_write_fails || length > sizeof(f->qlog)) return false;
    memcpy(f->qlog, buffer, length);
    f->qlog_length = length;
    f->qlog_used = true;
    return true;
}

static bool fake_blob_put(void *ctx, const char *key, const char *body, size_t length) {
    fake_t *f = (fake_t *)ctx;
    return !f->blob_put_fails && kv_put(f, key, KV_BLOB, body, length);
}
static bool fake_blob_get(void *ctx, const char *key, char *out, size_t cap, size_t *length) {
    return kv_get((fake_t *)ctx, key, out, cap, length);
}
static bool fake_blob_drop(void *ctx, const char *key) {
    kv_t *slot = kv_find((fake_t *)ctx, key);
    if (slot == NULL) return false;
    memset(slot, 0, sizeof(*slot));
    return true;
}
static bool fake_cache_read(void *ctx, const char *name, char *out, size_t cap, size_t *length) {
    return kv_get((fake_t *)ctx, name, out, cap, length);
}
static bool fake_cache_write(void *ctx, const char *name, const char *body, size_t length) {
    return kv_put((fake_t *)ctx, name, KV_CACHE, body, length);
}

static sync_port_t make_port(fake_t *f) {
    sync_port_t port;
    memset(&port, 0, sizeof(port));
    port.ctx = f;
    port.link_state = fake_link;
    port.http_request = fake_http;
    port.local_time_iso = fake_local_time;
    port.now_ms = fake_now_ms;
    port.queue_log_read = fake_queue_read;
    port.queue_log_write = fake_queue_write;
    port.blob_put = fake_blob_put;
    port.blob_get = fake_blob_get;
    port.blob_drop = fake_blob_drop;
    port.cache_read = fake_cache_read;
    port.cache_write = fake_cache_write;
    return port;
}

/* 中台侧样例响应（字段与 `contracts/scale-midplatform.md` 一致） */
#define ACTIVATE_OK                                                                   \
    "{\"market\":{\"market_code\":\"M-0001\"},\"stall\":{\"stall_no\":\"A-012\"},"     \
    "\"business_date\":\"2026-10-06\",\"server_time\":\"2026-10-06 08:12:33\","       \
    "\"config\":{\"offline_warn_threshold\":2,\"customer_base_url\":"                 \
    "\"http://192.168.1.10:8000/customer\",\"catalog_version\":7,"                    \
    "\"price_list_business_date\":\"2026-10-06\",\"proto\":1}}"
#define HEARTBEAT_OK                                                                  \
    "{\"business_date\":\"2026-10-06\",\"server_time\":\"2026-10-06 08:13:00\","      \
    "\"config_changed\":false,\"catalog_version\":7}"
#define CATALOG_OK                                                                    \
    "{\"catalog_version\":8,\"products\":[{\"product_id\":1234,\"name\":\"本地小白菜\"," \
    "\"category_id\":3,\"status\":\"active\"}]}"
#define PRICE_LIST_OK                                                                 \
    "{\"business_date\":\"2026-10-06\",\"items\":[{\"product_id\":1234,"              \
    "\"unit_price_cents\":320}],\"price_list_version\":11}"

/* 开机：清空假端口、按需铺"上次开机留下的缓存"（`REQ-037`）、初始化编排器。
 * `cached=false` = 这台秤此前没缓存过（首次开机），此时三个缓存一个都不能铺 ——
 * 否则在线激活用例不会走到"拉字典与价目表"，激活响应里的版本号会把真实拉取结果盖掉。 */
static void boot(sync_t *sync, fake_t *f, sync_link_t link, bool cached) {
    fake_reset(f);
    f->link = link;
    if (cached) {
        (void)kv_put(f, "activate", KV_CACHE, ACTIVATE_OK, strlen(ACTIVATE_OK));
        (void)kv_put(f, "catalog", KV_CACHE, CATALOG_OK, strlen(CATALOG_OK));
        (void)kv_put(f, "price_list", KV_CACHE, PRICE_LIST_OK, strlen(PRICE_LIST_OK));
    }
    sync_port_t port = make_port(f);
    sync_init(sync, &port, "SC-000123", "token-abc");
}

static proto_line_t line_of(void) {
    proto_line_t line;
    memset(&line, 0, sizeof(line));
    line.product_id = 1234;
    line.weight_grams = 780;
    line.unit_price_cents = 320;
    line.amount_cents = 250;
    return line;
}

/* 离线暂存 `count` 条；`keys` 非空时取回各条幂等键（供补传顺序取证） */
static void stage_offline(sync_t *sync, size_t count, char keys[][24]) {
    proto_line_t line = line_of();
    for (size_t i = 0; i < count; i++) {
        int64_t seq = 0;
        queue_status_t status = sync_submit(sync, &line, 1, 250, &seq);
        CHECK(status == QUEUE_OK || status == QUEUE_WARN_THRESHOLD, "离线暂存第 %d 条", (int)(i + 1));
        if (keys == NULL) continue;
        queue_record_t record;
        CHECK(queue_get(&sync->queue, seq, &record), "取回第 %d 条", (int)(i + 1));
        snprintf(keys[i], 24, "%s", record.idempotency_key);
    }
}

/* ① 中台不可达仍能进营业界面（`AC-025` 前半段）+ 离线计价（`AC-028`）+ 未激活不得营业（`AC-027`） */
static void test_boot_offline(void) {
    fake_t f;
    sync_t sync;
    boot(&sync, &f, SYNC_LINK_DOWN, true);
    CHECK(f.http_calls == 0, "开机初始化不得联网（实得 %d 次）", (int)f.http_calls);
    CHECK(sync_is_operational(&sync), "有缓存时中台不可达也必须能进营业界面");
    CHECK(!sync_is_online(&sync), "链路应为离线");
    CHECK(strcmp(sync.scope.market_code, "M-0001") == 0 && strcmp(sync.scope.stall_no, "A-012") == 0,
          "绑定应从缓存恢复");
    CHECK(sync.queue.warn_threshold == 2, "告警阈值须来自中台下发（实得 %d）", (int)sync.queue.warn_threshold);

    /* 离线计价与权威 golden vector `amount-001`（320 分 × 780 克 = 250 分）逐位一致 */
    money_cents_t amount = 0;
    CHECK(sync_price_line(&sync, 1234, 780, &amount) && amount == 250, "离线计价应为 250（实得 %lld）",
          (long long)amount);
    CHECK(!sync_price_line(&sync, 9999, 780, &amount), "无当日价目表必须拒绝计价");
    CHECK(!sync_price_line(&sync, 1234, 0, &amount), "重量 0 必须拒绝（`REQ-027`）");
    CHECK(!sync_price_line(&sync, 1234, SYNC_MAX_WEIGHT_GRAMS + 1, &amount), "超上限必须拒绝（`REQ-027`）");
    CHECK(sync_price_line(&sync, 1234, SYNC_MAX_WEIGHT_GRAMS, &amount) && amount == 16000,
          "上限 50 公斤须可计价（320×50000/1000）");

    sync_poll(&sync);
    CHECK(f.http_calls == 0 && sync_is_operational(&sync), "离线轮询不得发起调用、不得影响营业能力");
    stage_offline(&sync, 1, NULL);
    CHECK(queue_pending_count(&sync.queue) == 1, "离线成交必须能本地暂存");
    CHECK(sync.last_error[0] == '\0', "离线不应产生中台错误码");

    /* 从未激活的设备：能开机、但不许进入营业流程（`AC-027`） */
    fake_t blank;
    sync_t unactivated;
    boot(&unactivated, &blank, SYNC_LINK_DOWN, false);
    CHECK(blank.http_calls == 0, "无缓存时初始化同样不得联网");
    CHECK(!sync_is_operational(&unactivated), "从未激活的设备不得进入营业流程");
    proto_line_t line = line_of();
    int64_t seq = 0;
    CHECK(sync_submit(&unactivated, &line, 1, 250, &seq) == QUEUE_ERR_INVALID, "未激活设备不得暂存交易");
}

/* ② 补传按 `staged_at` 升序（契约 §6 顺序不变量） */
static void test_backfill_order(void) {
    fake_t f;
    sync_t sync;
    boot(&sync, &f, SYNC_LINK_DOWN, true);
    /* 故意让暂存时刻乱序：第 1 条 3000、第 2 条 1000、第 3 条 2000（seq 仍是 1/2/3） */
    f.clock[0] = 3000;
    f.clock[1] = 1000;
    f.clock[2] = 2000;
    f.clock_count = 3;
    char keys[3][24];
    stage_offline(&sync, 3, keys);

    f.link = SYNC_LINK_UP;
    sync_set_link(&sync, SYNC_LINK_UP);
    sync_backfill(&sync);
    CHECK(f.sent_count == 3, "三条都该被补传（实得 %d）", (int)f.sent_count);
    CHECK(f.sent_count == 3 && strcmp(f.sent_key[0], keys[1]) == 0 && strcmp(f.sent_key[1], keys[2]) == 0 &&
              strcmp(f.sent_key[2], keys[0]) == 0,
          "补传必须按 staged_at 升序（期望 2,3,1 实得 %s,%s,%s）", f.sent_count > 0 ? f.sent_key[0] : "-",
          f.sent_count > 1 ? f.sent_key[1] : "-", f.sent_count > 2 ? f.sent_key[2] : "-");
    CHECK(queue_pending_count(&sync.queue) == 0, "全部成功后待补传应为 0");
    CHECK(kv_count_blobs(&f) == 0, "补传成功后本地副本必须为空（`NFR-013` / `RL-8`）");
    CHECK(sync.backfill_sent == 3, "补传成功计数应为 3（实得 %d）", (int)sync.backfill_sent);
}

/* ③ 补传失败该条保持暂存、不阻断后续；传输层断掉则结束本轮但一条都不丢（契约 §6） */
static void test_backfill_failure_keeps_staged(void) {
    fake_t f;
    sync_t sync;
    boot(&sync, &f, SYNC_LINK_DOWN, true);
    stage_offline(&sync, 3, NULL);

    /* 第 1 条被判载荷不合法（MT-2004），第 2、3 条成功 */
    f.report_status[0] = 422;
    snprintf(f.report_code[0], sizeof(f.report_code[0]), "MT-2004");
    f.link = SYNC_LINK_UP;
    sync_set_link(&sync, SYNC_LINK_UP);
    sync_backfill(&sync);
    CHECK(f.sent_count == 3, "失败一条不得阻断后续（应发出 3 条，实得 %d）", (int)f.sent_count);
    CHECK(queue_pending_count(&sync.queue) == 1, "失败那条必须保持暂存（实得 %d）",
          (int)queue_pending_count(&sync.queue));
    CHECK(sync.backfill_sent == 2 && sync.backfill_failed == 1, "成功 2 条、失败 1 条（实得 %d/%d）",
          (int)sync.backfill_sent, (int)sync.backfill_failed);
    CHECK(strcmp(sync.last_error, "MT-2004") == 0, "中台错误码须如实带出（实得 %s）", sync.last_error);
    queue_record_t record;
    CHECK(queue_get(&sync.queue, 1, &record) && record.state == QUEUE_STATE_STAGED, "被拒那条应仍 staged");
    CHECK(queue_get(&sync.queue, 2, &record) && record.state == QUEUE_STATE_ACKED, "成功那条应已 acked");

    f.report_index = 0;
    f.report_code[0][0] = '\0';
    size_t before = f.sent_count;
    sync_backfill(&sync);
    CHECK(f.sent_count == before + 1, "下一轮只应重试剩下 1 条（实得新增 %d）", (int)(f.sent_count - before));
    CHECK(queue_pending_count(&sync.queue) == 0 && kv_count_blobs(&f) == 0, "重试成功后队列与副本都须清空");

    /* 传输层不可达：条目与本地副本一律保留，网络恢复后仍能补传成功 */
    fake_t down;
    sync_t offline;
    boot(&offline, &down, SYNC_LINK_DOWN, true);
    stage_offline(&offline, 1, NULL);
    down.link = SYNC_LINK_UP;
    sync_set_link(&offline, SYNC_LINK_UP);
    down.report_transport_fail[0] = true;
    sync_backfill(&offline);
    CHECK(queue_pending_count(&offline.queue) == 1, "传输层失败时条目必须保持暂存");
    CHECK(kv_count_blobs(&down) == 1 && offline.backfill_failed == 1, "传输层失败时不得清除本地副本");
    down.report_transport_fail[0] = false;
    down.report_index = 0;
    sync_backfill(&offline);
    CHECK(queue_pending_count(&offline.queue) == 0 && kv_count_blobs(&down) == 0,
          "网络恢复后必须补传成功并清除本地副本");
}

/* 阈值只告警仍继续接受（`REQ-016` / `NFR-014`）+ 写入失败必须明确报错（`REQ-030` / `RL-9`） */
static void test_threshold_and_write_failures(void) {
    fake_t f;
    sync_t sync;
    boot(&sync, &f, SYNC_LINK_DOWN, true); /* offline_warn_threshold = 2 */
    proto_line_t line = line_of();
    int64_t seq = 0;
    CHECK(sync_submit(&sync, &line, 1, 250, &seq) == QUEUE_OK, "第 1 条未达阈值");
    CHECK(sync_submit(&sync, &line, 1, 250, &seq) == QUEUE_WARN_THRESHOLD, "第 2 条达阈值 → 返回 WARN");
    CHECK(sync.warn_threshold_reached, "界面应能看到告警状态");
    CHECK(sync_submit(&sync, &line, 1, 250, &seq) == QUEUE_WARN_THRESHOLD &&
              queue_pending_count(&sync.queue) == 3,
          "达阈值后必须继续接受新交易，绝不静默丢弃");

    fake_t bad;
    sync_t sync2;
    boot(&sync2, &bad, SYNC_LINK_DOWN, true);
    bad.blob_put_fails = true;
    CHECK(sync_submit(&sync2, &line, 1, 250, &seq) == QUEUE_ERR_IO, "报文副本写失败必须明确报错");
    CHECK(queue_pending_count(&sync2.queue) == 0 && kv_count_blobs(&bad) == 0, "写失败后不得留半条或孤儿报文");
    bad.blob_put_fails = false;
    bad.queue_write_fails = true;
    CHECK(sync_submit(&sync2, &line, 1, 250, &seq) == QUEUE_ERR_IO, "队列落盘失败必须明确报错");
    bad.queue_write_fails = false;
    CHECK(queue_pending_count(&sync2.queue) == 1, "内存中那条仍在，下一轮持久化会带上它");
}

/* 幂等键必须跨重启唯一（`REQ-039`）：重复的后果不是报错，而是中台返回别人那笔的结果 */
static void test_idempotency_key_unique_across_reboots(void) {
    fake_t f;
    sync_t first;
    boot(&first, &f, SYNC_LINK_DOWN, true);
    proto_line_t line = line_of();
    int64_t seq = 0;
    CHECK(sync_submit(&first, &line, 1, 250, &seq) == QUEUE_OK, "第 1 次开机暂存");
    queue_record_t record;
    CHECK(queue_get(&first.queue, seq, &record), "取第 1 条记录");
    char key1[24];
    snprintf(key1, sizeof(key1), "%s", record.idempotency_key);

    sync_port_t port = make_port(&f); /* 模拟重启：同一个存储重新 init */
    sync_t second;
    sync_init(&second, &port, "SC-000123", "token-abc");
    queue_status_t status = sync_submit(&second, &line, 1, 250, &seq);
    CHECK(status == QUEUE_OK || status == QUEUE_WARN_THRESHOLD, "重启后再暂存（实得 %d）", (int)status);
    CHECK(queue_get(&second.queue, seq, &record), "取第 2 条记录");
    char key2[24];
    snprintf(key2, sizeof(key2), "%s", record.idempotency_key);

    CHECK(strcmp(key1, key2) != 0, "跨重启幂等键必须不同（实得 %s 与 %s）", key1, key2);
    CHECK(strlen(key2) < QUEUE_IDEM_KEY_MAX, "幂等键须放得进 core 定长字段（%d < %d）", (int)strlen(key2),
          QUEUE_IDEM_KEY_MAX);
    CHECK(strstr(key2, "20261006") != NULL, "幂等键应含营业日（实得 %s）", key2);
}

/* 协议不兼容不得降级重试（契约 §4） */
static void test_protocol_incompatible_not_retried(void) {
    fake_t f;
    sync_t sync;
    boot(&sync, &f, SYNC_LINK_UP, false);
    snprintf(f.activate_error, sizeof(f.activate_error), "MT-2003");
    sync_poll(&sync);
    size_t after_first = f.http_calls;
    CHECK(sync.proto_incompatible, "MT-2003 必须被识别为协议不兼容");
    CHECK(strcmp(sync.last_error, "MT-2003") == 0, "错误码须如实带出（实得 %s）", sync.last_error);
    CHECK(!sync_is_operational(&sync), "协议不兼容时不得进入营业流程");
    sync_poll(&sync);
    sync_poll(&sync);
    CHECK(f.http_calls == after_first, "协议不兼容不得降级重试：调用数不得增加");
}

/* 在线激活 → 拉字典与价目表 → 缓存 → 重启后断网仍可计价 → 断电不丢暂存（`REQ-037` / `NFR-014`） */
static void test_online_activation_then_reboot(void) {
    fake_t f;
    sync_t sync;
    boot(&sync, &f, SYNC_LINK_UP, false);
    CHECK(f.http_calls == 0, "初始化不联网");
    f.activate_body = ACTIVATE_OK;
    f.heartbeat_body = HEARTBEAT_OK;
    f.catalog_body = CATALOG_OK;
    f.price_list_body = PRICE_LIST_OK;
    sync_poll(&sync);
    CHECK(sync_is_operational(&sync), "在线激活后应可营业");
    CHECK(strcmp(f.last_token, "token-abc") == 0, "授权只来自设备令牌：端口须收到令牌（实得 %s）",
          f.last_token);
    CHECK(sync.price_count == 1 && sync.product_count == 1, "字典与价目表应已拉取并缓存");
    CHECK(sync.catalog_version == 8 && sync.price_list_version == 11,
          "版本号应更新（实得 catalog=%lld price=%lld）", (long long)sync.catalog_version,
          (long long)sync.price_list_version);

    sync_port_t port = make_port(&f);
    sync_t rebooted;
    sync_init(&rebooted, &port, "SC-000123", "token-abc");
    CHECK(sync_is_operational(&rebooted) && rebooted.price_count == 1, "重启后应能从缓存恢复价目表");

    f.link = SYNC_LINK_DOWN;
    sync_set_link(&rebooted, SYNC_LINK_DOWN);
    stage_offline(&rebooted, 1, NULL);
    sync_t after_power_loss;
    sync_init(&after_power_loss, &port, "SC-000123", "token-abc");
    CHECK(queue_pending_count(&after_power_loss.queue) == 1, "断电重启后暂存必须还在（`NFR-014`）");
    f.link = SYNC_LINK_UP;
    sync_set_link(&after_power_loss, SYNC_LINK_UP);
    sync_backfill(&after_power_loss);
    CHECK(queue_pending_count(&after_power_loss.queue) == 0 && kv_count_blobs(&f) == 0,
          "重启后补传成功并清除本地副本（`NFR-013`）");
}

/* 收款（`REQ-009` / `REQ-043` / `AC-033`）：二维码内容必须来自中台，秤端只负责本地画码 */
static void test_settle_qr_from_midplatform(void) {
    fake_t f;
    sync_t sync;
    boot(&sync, &f, SYNC_LINK_UP, true);
    char qr[PROTO_URL_MAX] = "";
    CHECK(sync_settle(&sync, "T-20261006-0001", false, qr, sizeof(qr)), "扫码收款应取回中台载荷");
    CHECK(strstr(qr, "/customer/pay") != NULL, "二维码内容必须来自中台（实得 %s）", qr);
    CHECK(strstr(qr, "localhost") == NULL && strstr(qr, "127.0.0.1") == NULL, "不得指向秤端自身（`AC-033`）");
    CHECK(strcmp(f.last_settle_key, "T-20261006-0001") == 0, "收款请求须带单号作幂等键（`REQ-026`）");
    CHECK(strcmp(f.last_settle_body, "{\"method\":\"qr\"}") == 0, "扫码收款报文应为 method=qr（实得 %s）",
          f.last_settle_body);
    char cash[PROTO_URL_MAX] = "x";
    CHECK(sync_settle(&sync, "T-20261006-0001", true, cash, sizeof(cash)), "现金收款也要中台留痕（`REQ-010`）");
    CHECK(cash[0] == '\0', "现金收款没有二维码载荷（实得 %s）", cash);
    CHECK(!sync_settle(&sync, NULL, false, qr, sizeof(qr)), "空单号必须拒绝");
}

int main(void) {
    test_boot_offline();
    test_backfill_order();
    test_backfill_failure_keeps_staged();
    test_threshold_and_write_failures();
    test_idempotency_key_unique_across_reboots();
    test_protocol_incompatible_not_retried();
    test_online_activation_then_reboot();
    test_settle_qr_from_midplatform();
    printf("[%s] test_sync: 通过 %d，失败 %d\n", g_fail == 0 ? "PASS" : "FAIL", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
