/* 平台端口的 ESP-IDF 实现之二：落盘（`T-SCALE-13`；`plan.md` §4「LittleFS/NVS 落盘」）。
 *
 * ⚠️ **未编译、未验证**（本机无 ESP-IDF、无硬件）：本文件只有**静态检查 + 人工评审**这一档边界，
 * 见 README「验证边界」。**不得**宣称"编译通过"或"已验证"。
 *
 * 存什么、为什么分三处：
 *   - **暂存队列日志**（LittleFS，整段读写）：定长记录 + CRC 的**追加型**日志，格式由 `core/queue.h`
 *     冻结。断电残片由 `queue_recover` 判无效（`RL-9`：绝不静默丢弃，也绝不把残片当交易）。
 *   - **上报报文副本**（LittleFS，按幂等键）：暂存时写、补传成功后删（`NFR-013` / `RL-8` 清除本地副本）。
 *     补传重发的是**同一份字节**，故同一幂等键必然同体。
 *   - **字典 / 价目表原始响应**（LittleFS）：断网时仍能选品与计价（`REQ-037`）；
 *     缓存的是中台原始响应体，启动时用同一个解析器读回（少一套序列化就少一个漂移点）。
 *   - **设备身份**（NVS）：`device_id` / 设备令牌 / 中台地址 / WiFi 口令 —— 出厂或首次配置写入。
 *
 * 写盘纪律：一律「先写 `.tmp` → `fsync` → `rename` 覆盖」，避免掉电留下半截文件。
 */
#ifndef CAISHIC_SCALE_STORE_H
#define CAISHIC_SCALE_STORE_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

/* 设备身份与配置（NVS）。`REQ-036`：秤端经设备注册绑定唯一「市场 + 摊位」，以设备令牌标识身份。 */
typedef struct {
    char device_id[24];
    char device_token[64];
    char api_base_url[96]; /* 中台 API 基址。**为空即视为不可达**，不臆造默认值 */
    char wifi_ssid[33];
    char wifi_password[65];
} scale_identity_t;

typedef struct scale_store scale_store_t; /* 不透明：定义在 store.c */

scale_store_t *scale_store_create(void);
void scale_store_destroy(scale_store_t *store);

/* 挂载 LittleFS 并打开 NVS。失败不阻塞开机：界面照常进（`AC-025`），但会明确提示无法暂存。 */
bool scale_store_mount(scale_store_t *store);

/* NVS 身份读写。`load` 缺字段时置空串并返回 false（调用方据此提示"设备未配置"）。 */
bool scale_store_load_identity(scale_store_t *store, scale_identity_t *identity);
bool scale_store_save_identity(scale_store_t *store, const scale_identity_t *identity);

/* `sync_port_t` 的 7 个存储回调（`sync.h`） */
bool scale_store_queue_read(scale_store_t *store, uint8_t *buffer, size_t capacity, size_t *length);
bool scale_store_queue_write(scale_store_t *store, const uint8_t *buffer, size_t length);
bool scale_store_blob_put(scale_store_t *store, const char *key, const char *body, size_t length);
bool scale_store_blob_get(scale_store_t *store, const char *key, char *out, size_t capacity, size_t *length);
bool scale_store_blob_drop(scale_store_t *store, const char *key);
bool scale_store_cache_read(scale_store_t *store, const char *name, char *out, size_t capacity,
                            size_t *length);
bool scale_store_cache_write(scale_store_t *store, const char *name, const char *body, size_t length);

#endif /* CAISHIC_SCALE_STORE_H */
