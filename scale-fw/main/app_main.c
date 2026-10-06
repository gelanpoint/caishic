/* 开机编排与端口接线（`T-SCALE-13`；`REQ-034` / `REQ-040`；`AC-025`）。
 *
 * ⚠️ **未编译、未验证**（本机无 ESP-IDF、无硬件）：只有**静态检查 + 人工评审**这一档边界（见 README）。
 *
 * 本文件是**组合根**：`main/net`（WiFi + HTTP）与 `main/store`（LittleFS + NVS）两个实现，
 * 在这里缝成编排层要的那**一个** `sync_port_t`（11 个回调，`ctx` 用同一个组合对象贯穿）。
 * 编排层（`sync.c`）本身不依赖 ESP-IDF，故它的逻辑在主机侧已由 `test_sync.c` 实测。
 *
 * 开机顺序（`AC-025` 的硬要求：**中台不可达不得导致秤端无法开机**）：
 *   1. 读身份（NVS）→ 挂载 LittleFS → 建界面 → **界面已可用**；
 *   2. 恢复暂存队列与本地字典/价目表（`sync_init`，只读本地，零网络调用）；
 *   3. 最后才启动 WiFi（异步、不等待连接结果）。
 * 即：网络是"后加的能力"，不是开机的必要条件。
 */
#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "store.h"
#include "sync.h"
#include "ui.h"
#include "weigh.h"

static const char *TAG = "scale-main";

#define SYNC_POLL_INTERVAL_MS 2000
#define UI_TICK_INTERVAL_MS 100

/* 组合对象：两个端口实现 + 设备身份。**同一个指针**贯穿 11 个回调（`ctx`）。 */
typedef struct {
    scale_net_t *net;
    scale_store_t *store;
    scale_identity_t identity;
} scale_app_t;

static scale_app_t g_app;
static sync_t g_sync;
static weigh_t g_weigh;
static ui_t *g_ui;

/* ---- 11 个端口回调：每个都只做"转调 + 类型转换"，不含业务逻辑 ---- */

static sync_link_t glue_link_state(void *ctx) { return scale_net_link_state(((scale_app_t *)ctx)->net); }

static bool glue_http_request(void *ctx, const char *method, const char *path, const char *device_token,
                              const char *idempotency_key, const char *body, size_t body_length,
                              char *response, size_t cap, sync_http_result_t *result) {
    return scale_net_http_request(((scale_app_t *)ctx)->net, method, path, device_token, idempotency_key,
                                  body, body_length, response, cap, result);
}

static bool glue_local_time_iso(void *ctx, char *out, size_t cap) {
    return scale_net_local_time_iso(((scale_app_t *)ctx)->net, out, cap);
}

static int64_t glue_now_ms(void *ctx) { return scale_net_now_ms(((scale_app_t *)ctx)->net); }

static bool glue_queue_read(void *ctx, uint8_t *buffer, size_t capacity, size_t *length) {
    return scale_store_queue_read(((scale_app_t *)ctx)->store, buffer, capacity, length);
}

static bool glue_queue_write(void *ctx, const uint8_t *buffer, size_t length) {
    return scale_store_queue_write(((scale_app_t *)ctx)->store, buffer, length);
}

static bool glue_blob_put(void *ctx, const char *key, const char *body, size_t length) {
    return scale_store_blob_put(((scale_app_t *)ctx)->store, key, body, length);
}

static bool glue_blob_get(void *ctx, const char *key, char *out, size_t cap, size_t *length) {
    return scale_store_blob_get(((scale_app_t *)ctx)->store, key, out, cap, length);
}

static bool glue_blob_drop(void *ctx, const char *key) {
    return scale_store_blob_drop(((scale_app_t *)ctx)->store, key);
}

static bool glue_cache_read(void *ctx, const char *name, char *out, size_t cap, size_t *length) {
    return scale_store_cache_read(((scale_app_t *)ctx)->store, name, out, cap, length);
}

static bool glue_cache_write(void *ctx, const char *name, const char *body, size_t length) {
    return scale_store_cache_write(((scale_app_t *)ctx)->store, name, body, length);
}

/* 端口装配：11 个字段一个不少（漏填 = 运行时 NULL 回调 = 静默 no-op，故这里全部显式赋值）。 */
static sync_port_t make_sync_port(scale_app_t *app) {
    sync_port_t port;
    memset(&port, 0, sizeof(port));
    port.ctx = app;
    port.link_state = glue_link_state;
    port.http_request = glue_http_request;
    port.local_time_iso = glue_local_time_iso;
    port.now_ms = glue_now_ms;
    port.queue_log_read = glue_queue_read;
    port.queue_log_write = glue_queue_write;
    port.blob_put = glue_blob_put;
    port.blob_get = glue_blob_get;
    port.blob_drop = glue_blob_drop;
    port.cache_read = glue_cache_read;
    port.cache_write = glue_cache_write;
    return port;
}

/* ---- 后台任务 ---- */

static void sync_task(void *arg) {
    sync_t *sync = (sync_t *)arg;
    for (;;) {
        sync_poll(sync); /* 在线时：激活 → 心跳 → 拉配置 → 补传；离线时只更新链路状态 */
        vTaskDelay(pdMS_TO_TICKS(SYNC_POLL_INTERVAL_MS));
    }
}

static void ui_task(void *arg) {
    ui_t *ui = (ui_t *)arg;
    for (;;) {
        ui_tick(ui);
        vTaskDelay(pdMS_TO_TICKS(UI_TICK_INTERVAL_MS));
    }
}

void app_main(void) {
    memset(&g_app, 0, sizeof(g_app));

    /* 1) 身份：读不到也要能开机 —— 界面提示"设备未配置"，而不是卡死（`AC-025` 的精神）。 */
    g_app.store = scale_store_create();
    if (g_app.store == NULL) {
        ESP_LOGE(TAG, "存储对象创建失败");
        return;
    }
    if (!scale_store_mount(g_app.store)) {
        ESP_LOGW(TAG, "LittleFS 未挂载：交易将无法暂存（界面会明确报错，不会静默丢）");
    }
    if (!scale_store_load_identity(g_app.store, &g_app.identity)) {
        ESP_LOGW(TAG, "设备未配置（缺 device_id / 令牌 / 中台地址）：界面提示联系运维");
    }

    /* 2) 端口与编排器：`sync_init` 只读本地，**一个网络调用都没有**。 */
    g_app.net = scale_net_create(g_app.identity.api_base_url);
    if (g_app.net == NULL) {
        ESP_LOGE(TAG, "网络对象创建失败");
        return;
    }
    sync_port_t port = make_sync_port(&g_app);
    sync_init(&g_sync, &port, g_app.identity.device_id, g_app.identity.device_token);

    /* 3) 称重与界面：界面先立起来，摊主立刻能用（离线也能选品计价与本地暂存）。 */
    weigh_config_t weigh_config;
    memset(&weigh_config, 0, sizeof(weigh_config));
    weigh_config.source = WEIGH_SOURCE_HX711;
    weigh_config.hx711.dout_gpio = 4;
    weigh_config.hx711.sck_gpio = 5;
    weigh_config.hx711_scale_num = 1;
    weigh_config.hx711_scale_den = 1; /* ⚠️ 标定系数须现场标定后写入 NVS，见 README「待办」 */
    weigh_config.stable_samples = 5;
    weigh_config.stable_tolerance_grams = 2;
    if (!weigh_init(&g_weigh, &weigh_config)) {
        ESP_LOGW(TAG, "称重通道初始化失败：界面会提示不可计价");
    }
    g_ui = ui_create(&g_sync, &g_weigh);
    if (g_ui == NULL) {
        ESP_LOGE(TAG, "界面创建失败");
        return;
    }

    /* 4) 任务：界面刷新 + 同步轮询。 */
    (void)xTaskCreate(ui_task, "scale_ui", 4096, g_ui, 4, NULL);
    (void)xTaskCreate(sync_task, "scale_sync", 8192, &g_sync, 3, NULL);

    /* 5) **最后**才起 WiFi：异步连接，不阻塞开机（中台/路由器不可达也照常营业）。 */
    if (g_app.identity.wifi_ssid[0] != '\0') {
        if (!scale_net_start_wifi(g_app.net, g_app.identity.wifi_ssid, g_app.identity.wifi_password)) {
            ESP_LOGW(TAG, "WiFi 启动失败：离线营业，交易本地暂存");
        }
    } else {
        ESP_LOGW(TAG, "未配置 WiFi：离线营业，交易本地暂存");
    }
    ESP_LOGI(TAG, "开机完成（营业界面已就绪；网络状态见界面上的离线/在线标识）");
}
