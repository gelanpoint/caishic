/* 落盘实现（`T-SCALE-13`）。头注里的分区理由见 store.h。
 *
 * ⚠️ **未编译、未验证**（本机无 ESP-IDF、无硬件）：只有**静态检查 + 人工评审**这一档边界（见 README）。
 *
 * 文件系统选型：**LittleFS**（`ADR-0006` §3.2 已接受的决策，理由是该设备会掉电、需要掉电安全的
 * 小文件系统）。本文件用 `esp_vfs_littlefs_register` 挂载，其余一律走 POSIX 文件 API ——
 * 这样 ESP-IDF 相关的表面只剩一个挂载调用，评审时一眼能看完（`esp_littlefs` 组件的引入方式见 README）。
 */
#include "store.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "esp_littlefs.h"
#include "esp_log.h"
#include "nvs.h"

static const char *TAG = "scale-store";

#define STORE_MOUNT_POINT "/littlefs"
#define STORE_QUEUE_PATH STORE_MOUNT_POINT "/queue.log"
#define STORE_BLOB_DIR STORE_MOUNT_POINT "/blob"
#define STORE_CACHE_DIR STORE_MOUNT_POINT "/cache"
#define STORE_NVS_NAMESPACE "scale"

struct scale_store {
    bool mounted;
    bool nvs_open;
};

/* 幂等键与缓存名都只含 `[0-9A-Za-z_-]`；仍做一次净化，避免任何路径穿越（防御性，不依赖上游约定）。
 * **放不下就返回 false，绝不静默截断** —— 截断会让两个不同幂等键落到同一个文件名，
 * `blob_get` 就可能读回**别人的上报体**（错在 `RL-9` 路径上）。与 `store_read_all` 的
 * "拒绝截断读取"同一条道理。 */
static bool store_safe_name(const char *input, char *out, size_t cap) {
    size_t n = 0;
    for (const char *p = input; p != NULL && *p != '\0'; p++) {
        if (n + 1 >= cap) return false;
        char c = *p;
        bool ok = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  c == '-' || c == '_';
        out[n++] = ok ? c : '_';
    }
    out[n] = '\0';
    return true;
}

/* 掉电安全写入：先写 `.tmp` → fsync → rename 覆盖。任何一步失败都不动原文件。 */
static bool store_write_atomic(const char *path, const void *data, size_t length) {
    char tmp[128];
    if (snprintf(tmp, sizeof(tmp), "%s.tmp", path) >= (int)sizeof(tmp)) return false; /* 宁可不写也不截断 */
    FILE *file = fopen(tmp, "wb");
    if (file == NULL) return false;
    bool ok = (length == 0) || (fwrite(data, 1, length, file) == length);
    if (ok) ok = (fflush(file) == 0) && (fsync(fileno(file)) == 0);
    if (fclose(file) != 0) ok = false;
    if (!ok) {
        (void)remove(tmp);
        return false;
    }
    if (rename(tmp, path) != 0) {
        (void)remove(tmp);
        return false;
    }
    return true;
}

static bool store_read_all(const char *path, void *out, size_t capacity, size_t *length) {
    FILE *file = fopen(path, "rb");
    if (file == NULL) return false;
    size_t read = fread(out, 1, capacity, file);
    bool more = (read == capacity) && (fgetc(file) != EOF);
    (void)fclose(file);
    if (more) {
        ESP_LOGE(TAG, "%s 超过缓冲（%u 字节）：拒绝截断读取", path, (unsigned)capacity);
        return false;
    }
    *length = read;
    return true;
}

bool scale_store_mount(scale_store_t *store) {
    if (store == NULL) return false;
    esp_vfs_littlefs_conf_t config;
    memset(&config, 0, sizeof(config));
    config.base_path = STORE_MOUNT_POINT;
    config.partition_label = "littlefs";
    config.format_if_mount_failed = true; /* 首次上电分区未格式化属正常 */
    esp_err_t err = esp_vfs_littlefs_register(&config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LittleFS 挂载失败: %s（界面照常进，但无法暂存）", esp_err_to_name(err));
        return false;
    }
    store->mounted = true;
    (void)mkdir(STORE_BLOB_DIR, 0777);
    (void)mkdir(STORE_CACHE_DIR, 0777);

    nvs_handle_t handle;
    if (nvs_open(STORE_NVS_NAMESPACE, NVS_READONLY, &handle) == ESP_OK) {
        nvs_close(handle);
        store->nvs_open = true;
    }
    return true;
}

/* ---- NVS：设备身份与配置 ---- */

static void store_nvs_get_str(nvs_handle_t handle, const char *key, char *out, size_t cap) {
    size_t length = cap;
    if (out == NULL || cap == 0) return;
    out[0] = '\0';
    if (nvs_get_str(handle, key, out, &length) != ESP_OK) out[0] = '\0';
}

bool scale_store_load_identity(scale_store_t *store, scale_identity_t *identity) {
    if (store == NULL || identity == NULL) return false;
    memset(identity, 0, sizeof(*identity));
    nvs_handle_t handle;
    if (nvs_open(STORE_NVS_NAMESPACE, NVS_READONLY, &handle) != ESP_OK) return false;
    store_nvs_get_str(handle, "device_id", identity->device_id, sizeof(identity->device_id));
    store_nvs_get_str(handle, "device_token", identity->device_token, sizeof(identity->device_token));
    store_nvs_get_str(handle, "api_base_url", identity->api_base_url, sizeof(identity->api_base_url));
    store_nvs_get_str(handle, "wifi_ssid", identity->wifi_ssid, sizeof(identity->wifi_ssid));
    store_nvs_get_str(handle, "wifi_password", identity->wifi_password, sizeof(identity->wifi_password));
    nvs_close(handle);
    return identity->device_id[0] != '\0' && identity->device_token[0] != '\0' &&
           identity->api_base_url[0] != '\0';
}

bool scale_store_save_identity(scale_store_t *store, const scale_identity_t *identity) {
    if (store == NULL || identity == NULL) return false;
    nvs_handle_t handle;
    if (nvs_open(STORE_NVS_NAMESPACE, NVS_READWRITE, &handle) != ESP_OK) return false;
    esp_err_t err = nvs_set_str(handle, "device_id", identity->device_id);
    if (err == ESP_OK) err = nvs_set_str(handle, "device_token", identity->device_token);
    if (err == ESP_OK) err = nvs_set_str(handle, "api_base_url", identity->api_base_url);
    if (err == ESP_OK) err = nvs_set_str(handle, "wifi_ssid", identity->wifi_ssid);
    if (err == ESP_OK) err = nvs_set_str(handle, "wifi_password", identity->wifi_password);
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    if (err != ESP_OK) ESP_LOGE(TAG, "身份写入失败: %s", esp_err_to_name(err));
    return err == ESP_OK;
}

/* ---- 暂存队列日志（整段读写，记录格式由 core/queue.h 冻结） ---- */

bool scale_store_queue_read(scale_store_t *store, uint8_t *buffer, size_t capacity, size_t *length) {
    if (store == NULL || !store->mounted) return false;
    return store_read_all(STORE_QUEUE_PATH, buffer, capacity, length);
}

bool scale_store_queue_write(scale_store_t *store, const uint8_t *buffer, size_t length) {
    if (store == NULL || !store->mounted) return false;
    /* 写失败必须回 false：调用方据此明确报错，**不得**把该笔记为已成功（`REQ-030` / `RL-9`）。 */
    return store_write_atomic(STORE_QUEUE_PATH, buffer, length);
}

/* ---- 上报报文副本（按幂等键；补传成功后必须删掉） ---- */

static bool store_blob_path(const char *key, char *out, size_t cap) {
    char safe[48];
    if (!store_safe_name(key, safe, sizeof(safe))) return false;
    return snprintf(out, cap, "%s/%s", STORE_BLOB_DIR, safe) < (int)cap;
}

bool scale_store_blob_put(scale_store_t *store, const char *key, const char *body, size_t length) {
    if (store == NULL || !store->mounted || key == NULL) return false;
    char path[128];
    if (!store_blob_path(key, path, sizeof(path))) return false;
    return store_write_atomic(path, body, length);
}

bool scale_store_blob_get(scale_store_t *store, const char *key, char *out, size_t capacity,
                          size_t *length) {
    if (store == NULL || !store->mounted || key == NULL) return false;
    char path[128];
    if (!store_blob_path(key, path, sizeof(path))) return false;
    if (!store_read_all(path, out, capacity - 1, length)) return false;
    out[*length] = '\0';
    return true;
}

bool scale_store_blob_drop(scale_store_t *store, const char *key) {
    if (store == NULL || !store->mounted || key == NULL) return false;
    char path[128];
    if (!store_blob_path(key, path, sizeof(path))) return false;
    return remove(path) == 0;
}

/* ---- 字典 / 价目表缓存（中台原始响应体） ---- */

static bool store_cache_path(const char *name, char *out, size_t cap) {
    char safe[24];
    if (!store_safe_name(name, safe, sizeof(safe))) return false;
    return snprintf(out, cap, "%s/%s", STORE_CACHE_DIR, safe) < (int)cap;
}

bool scale_store_cache_read(scale_store_t *store, const char *name, char *out, size_t capacity,
                            size_t *length) {
    if (store == NULL || !store->mounted || name == NULL) return false;
    char path[96];
    if (!store_cache_path(name, path, sizeof(path))) return false;
    if (!store_read_all(path, out, capacity - 1, length)) return false;
    out[*length] = '\0';
    return true;
}

bool scale_store_cache_write(scale_store_t *store, const char *name, const char *body, size_t length) {
    if (store == NULL || !store->mounted || name == NULL) return false;
    char path[96];
    if (!store_cache_path(name, path, sizeof(path))) return false;
    return store_write_atomic(path, body, length);
}

scale_store_t *scale_store_create(void) { return (scale_store_t *)calloc(1, sizeof(scale_store_t)); }

void scale_store_destroy(scale_store_t *store) {
    if (store == NULL) return;
    if (store->mounted) (void)esp_vfs_littlefs_unregister("littlefs");
    free(store);
}
