/* 平台端口的 ESP-IDF 实现之一：WiFi 链路 + HTTP 客户端（`T-SCALE-12`；契约 7 端点的传输层）。
 *
 * ⚠️ **未编译、未验证**（`ADR-0006` §4 负面清单 / `Q-21`）：本机没有 ESP-IDF、没有硬件，
 * 本文件只能做到**静态检查 + 人工评审**。**不得**据此宣称"编译通过"或"已验证"。
 * 须在装好 ESP-IDF（`idf.py set-target esp32s3`）的环境补做构建验证，见 README「验证边界」。
 *
 * 职责边界（薄）：
 *   - 只做**传输**：装配 URL 与三个头（`X-Device-Token` / `X-Scale-Proto` / `Idempotency-Key`）、
 *     读响应体、给出 HTTP 状态码。**不做**任何业务判断 —— 成功/失败一律交回 `sync.c`
 *     用 `proto_classify` 判定（窄口径：HTTP 200 也可能带错误码）。
 *   - 断线自动重连；链路状态经 `sync_port_t.link_state` 暴露给编排层。
 *   - 路径 / 查询串由编排层给出（`proto.h` 只管报文体，见其头注）。
 */
#include "sync.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

static const char *TAG = "scale-net";

#define SCALE_HTTP_TIMEOUT_MS 8000 /* 中台不可达时别把界面卡住；超时后由补传下一轮再试 */

struct scale_net {
    char api_base[PROTO_URL_MAX];
    char url[PROTO_URL_MAX + SYNC_PATH_MAX];
    volatile bool link_up;
    bool wifi_started;
};

/* ---- 链路状态：由 WiFi / IP 事件维护 ---- */

static void net_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data) {
    scale_net_t *net = (scale_net_t *)arg;
    if (net == NULL) return;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        (void)esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        net->link_up = false;
        ESP_LOGW(TAG, "WiFi 断开，自动重连（离线期间交易照常本地暂存）");
        (void)esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        net->link_up = true;
        ESP_LOGI(TAG, "链路已就绪");
    }
}

bool scale_net_start_wifi(scale_net_t *net, const char *ssid, const char *password) {
    if (net == NULL || ssid == NULL || ssid[0] == '\0') return false;
    if (net->wifi_started) return true;

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        (void)nvs_flash_erase();
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_flash_init 失败: %s", esp_err_to_name(err));
        return false;
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    if (esp_netif_create_default_wifi_sta() == NULL) return false;

    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_config));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, net_event_handler,
                                                       net, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, net_event_handler,
                                                       net, NULL));

    wifi_config_t wifi_config;
    memset(&wifi_config, 0, sizeof(wifi_config));
    snprintf((char *)wifi_config.sta.ssid, sizeof(wifi_config.sta.ssid), "%s", ssid);
    snprintf((char *)wifi_config.sta.password, sizeof(wifi_config.sta.password), "%s",
             password == NULL ? "" : password);
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    net->wifi_started = true;
    ESP_LOGI(TAG, "WiFi STA 已启动（SSID 已配置；连接结果由事件驱动，不阻塞开机）");
    return true;
}

sync_link_t scale_net_link_state(scale_net_t *net) {
    return (net != NULL && net->link_up) ? SYNC_LINK_UP : SYNC_LINK_DOWN;
}

int64_t scale_net_now_ms(scale_net_t *net) {
    (void)net;
    return (int64_t)(esp_timer_get_time() / 1000); /* 单调：`staged_at` 用它，墙钟回拨不会打乱补传顺序 */
}

bool scale_net_local_time_iso(scale_net_t *net, char *out, size_t cap) {
    (void)net;
    if (out == NULL || cap < 20) return false;
    time_t now = time(NULL);
    struct tm local;
    if (localtime_r(&now, &local) == NULL) return false;
    /* ⚠️ 未接 SNTP/RTC 时这是 1970 起的错误时刻。`captured_at` 仅作留痕，
     * **营业日一律取中台下发的 `business_date`**（契约 §3.1），故不影响记账口径。 */
    return strftime(out, cap, "%Y-%m-%d %H:%M:%S", &local) > 0;
}

/* ---- HTTP：只做传输，成功/失败交回编排层判定 ---- */

bool scale_net_http_request(scale_net_t *net, const char *method, const char *path,
                            const char *device_token, const char *idempotency_key, const char *body,
                            size_t body_length, char *response, size_t cap, sync_http_result_t *result) {
    if (net == NULL || method == NULL || path == NULL || response == NULL || result == NULL) return false;
    memset(result, 0, sizeof(*result));
    if (net->api_base[0] == '\0') {
        ESP_LOGW(TAG, "未配置中台地址（provisioning 未写入）→ 视为不可达");
        return false;
    }
    snprintf(net->url, sizeof(net->url), "%s%s", net->api_base, path);

    esp_http_client_config_t config;
    memset(&config, 0, sizeof(config));
    config.url = net->url;
    config.method = (strcmp(method, "POST") == 0) ? HTTP_METHOD_POST : HTTP_METHOD_GET;
    config.timeout_ms = SCALE_HTTP_TIMEOUT_MS;
    config.disable_auto_redirect = true;

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) return false;

    /* 三个头在这里装配（`proto.h` 只管报文体，不拼传输细节） */
    if (device_token != NULL && device_token[0] != '\0') {
        (void)esp_http_client_set_header(client, "X-Device-Token", device_token);
    }
    char proto_header[8];
    snprintf(proto_header, sizeof(proto_header), "%d", PROTO_VERSION);
    (void)esp_http_client_set_header(client, "X-Scale-Proto", proto_header);
    if (idempotency_key != NULL && idempotency_key[0] != '\0') {
        (void)esp_http_client_set_header(client, "Idempotency-Key", idempotency_key);
    }
    if (body != NULL && body_length > 0) {
        (void)esp_http_client_set_header(client, "Content-Type", "application/json");
        (void)esp_http_client_set_post_field(client, body, (int)body_length);
    }

    esp_err_t err = esp_http_client_perform(client);
    if (err != ESP_OK) {
        /* 传输层失败（不可达 / 超时）：不是 HTTP 错误码，编排层据此保持暂存并下一轮重试 */
        ESP_LOGW(TAG, "%s %s 传输失败: %s", method, path, esp_err_to_name(err));
        (void)esp_http_client_cleanup(client);
        return false;
    }

    result->transport_ok = true;
    result->http_status = esp_http_client_get_status_code(client);
    result->body_length = 0;
    int read = 0;
    while ((read = esp_http_client_read(client, response + result->body_length,
                                        (int)(cap - 1 - result->body_length))) > 0) {
        result->body_length += (size_t)read;
        if (result->body_length >= cap - 1) break;
    }
    response[result->body_length] = '\0';
    (void)esp_http_client_cleanup(client);

    /* 窄口径：这里**不**判断业务成功 —— 2xx 也可能是带错误码的响应，由 `proto_classify` 定夺。 */
    ESP_LOGI(TAG, "%s %s → HTTP %d，响应 %u 字节", method, path, (int)result->http_status,
             (unsigned)result->body_length);
    return true;
}

scale_net_t *scale_net_create(const char *api_base_url) {
    scale_net_t *net = (scale_net_t *)calloc(1, sizeof(scale_net_t));
    if (net == NULL) return NULL;
    if (api_base_url != NULL) snprintf(net->api_base, sizeof(net->api_base), "%s", api_base_url);
    return net;
}

void scale_net_destroy(scale_net_t *net) { free(net); }
