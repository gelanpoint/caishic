/* 秤端同步编排器（`T-SCALE-12`；`REQ-034` / `REQ-037` / `REQ-040` / `REQ-041`；`AC-025` / `AC-028`）。
 *
 * **本头文件与 sync.c 不依赖 ESP-IDF**：所有网络 / 存储 / 时钟动作都经下面的**平台端口**（`sync_port_t`）。
 * 理由不是好看：T-SCALE-12 的三条验收（① 补传按 `staged_at` 升序；② 补传失败该条保持暂存且不阻断后续；
 * ③ 中台不可达时仍能进入营业界面）**是纯逻辑**，若直接依赖 ESP-IDF，本机（无 ESP-IDF、无硬件）
 * 一条都验不了 —— 那就成了本项目明令拒绝的"不可复现的验收"。这与 `ADR-0006` §3.2 对冲 a
 * （核心不依赖 ESP-IDF 才能在主机侧被验证）是同一条纪律，只是延伸到了编排层。
 *
 * 端点数与字段一律以 `specs/market-trade-flow/contracts/scale-midplatform.md` 为准；
 * 报文编解码、计价、暂存队列一律复用已冻结的 `core/`（本层不重复实现任何口径）。
 * ESP-IDF 实现见：`main/net/http_client.c`（WiFi + HTTP 端口）、`main/store/store.c`（落盘端口）。
 */
#ifndef CAISHIC_SCALE_SYNC_H
#define CAISHIC_SCALE_SYNC_H

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "pricing.h"
#include "proto.h"
#include "queue.h"

/* 重量上限（`REQ-027` / `MT-1002`）：唯一事实来源是 `specs/market-trade-flow/data-model.md` §2.9 的 CHECK 值。
 * ⚠️ 这是本层唯一一处**复述**域常量（中台不在激活响应里下发它），登记为已知漂移风险；
 * 中台若把它改成下发字段，此处应改为读配置。core 侧不硬编码它（见 pricing.h）。 */
#define SYNC_MAX_WEIGHT_GRAMS ((weight_grams_t)50000)
/* 明细行数上限（主契约 §3.6：1~20）。 */
#define SYNC_MAX_ITEMS PROTO_MAX_ITEMS

#define SYNC_DEVICE_ID_MAX PROTO_DEVICE_ID_MAX
#define SYNC_TOKEN_MAX 64
#define SYNC_FIRMWARE_VERSION "0.1.0"
#define SYNC_HARDWARE_REV "esp32s3-n8r8"

/* 缓冲（窄实现：报文与响应全定长；只有补传批次按待补传条数临时分配） */
#define SYNC_QUEUE_LOG_BYTES 16384  /* 204 条定长记录（80 字节/条）：够放一天离线交易 */
#define SYNC_BACKFILL_PER_ROUND 16  /* 单轮最多上报条数：别让补传把界面卡住，剩下的下一轮 */
#define SYNC_BODY_MAX 1024
#define SYNC_RESPONSE_MAX 4096
#define SYNC_PATH_MAX 128

typedef enum {
    SYNC_LINK_DOWN = 0,
    SYNC_LINK_UP = 1
} sync_link_t;

typedef enum {
    SYNC_STATE_UNACTIVATED = 0, /* 无本地绑定：界面提示联系运维，**不得进入营业流程**（`AC-027`） */
    SYNC_STATE_READY = 1        /* 已有绑定与价目表缓存：在线离线都能营业（`AC-025` / `AC-028`） */
} sync_state_t;

typedef struct {
    bool transport_ok;   /* 传输层是否成功（不可达 / 超时 = false） */
    int64_t http_status; /* 传输层成功时的 HTTP 状态码 */
    size_t body_length;  /* 写进响应缓冲的字节数 */
} sync_http_result_t;

/* 平台端口：ESP-IDF 只出现在这些回调的实现里（见 http_client.c / store.c）。 */
typedef struct sync_port {
    void *ctx;

    sync_link_t (*link_state)(void *ctx);
    /* 同步 HTTP。返回 false = **传输层失败**（不是 HTTP 错误码）。
     * 端口只收**语义输入**（设备令牌、幂等键），由端口实现装配 `X-Device-Token` /
     * `X-Scale-Proto` / `Idempotency-Key` 三个头 —— 编排层不拼 HTTP 头，也不见 ESP-IDF 类型。
     * `idempotency_key` 为 NULL 表示该端点不需要（GET 与激活/心跳）。 */
    bool (*http_request)(void *ctx, const char *method, const char *path, const char *device_token,
                         const char *idempotency_key, const char *body, size_t body_length,
                         char *response, size_t response_cap, sync_http_result_t *result);
    /* 本机墙钟 → `YYYY-MM-DD HH:MM:SS`（`captured_at` 用；营业日一律用中台下发的值） */
    bool (*local_time_iso)(void *ctx, char *out, size_t cap);
    /* 单调毫秒：暂存序号 `staged_at` 的取值来源（**必须是单调的**，否则补传顺序会被墙钟回拨打乱） */
    int64_t (*now_ms)(void *ctx);

    /* 暂存队列日志：整段读写（记录格式由 core/queue.h 定义，定长 + CRC） */
    bool (*queue_log_read)(void *ctx, uint8_t *buffer, size_t capacity, size_t *length);
    bool (*queue_log_write)(void *ctx, const uint8_t *buffer, size_t length);

    /* 上报报文副本（按幂等键存取）：补传成功后必须 drop —— 清除本地副本（`NFR-013` / `RL-8`） */
    bool (*blob_put)(void *ctx, const char *key, const char *body, size_t length);
    bool (*blob_get)(void *ctx, const char *key, char *buffer, size_t capacity, size_t *length);
    bool (*blob_drop)(void *ctx, const char *key);

    /* 字典 / 价目表的**原始响应**缓存（`REQ-037`）：断网时仍能选品与计价。
     * 缓存的是中台原始响应体，启动时用同一个解析器读回 —— 少一套序列化就少一个漂移点。 */
    bool (*cache_read)(void *ctx, const char *name, char *buffer, size_t capacity, size_t *length);
    bool (*cache_write)(void *ctx, const char *name, const char *body, size_t length);
} sync_port_t;

typedef struct {
    sync_port_t port;

    char device_id[SYNC_DEVICE_ID_MAX];
    char device_token[SYNC_TOKEN_MAX];

    proto_scope_t scope;                    /* 只由激活响应建立（`proto.h`），响应里的越权字段不采信 */
    char customer_base_url[PROTO_URL_MAX];  /* 顾客扫码页基址（中台下发，秤端不承载顾客页） */
    char business_date[PROTO_DATE_MAX];     /* 营业日：一律取中台值 */
    int64_t offline_warn_threshold;         /* 暂存告警阈值：中台下发，**不在秤端硬编码** */
    int64_t catalog_version;
    int64_t price_list_version;
    bool proto_incompatible;                /* MT-2003：不得降级重试（契约 §4） */

    proto_price_item_t prices[PROTO_MAX_PRICE_ITEMS];
    size_t price_count;
    proto_product_t products[PROTO_MAX_PRODUCTS];
    size_t product_count;

    uint8_t queue_log[SYNC_QUEUE_LOG_BYTES];
    queue_t queue;

    sync_state_t state;
    sync_link_t link;
    bool config_dirty;         /* 心跳说配置变了 → 下一轮拉字典与价目表 */
    char boot_stamp[8];        /* 开机时刻 `HHMMSS`（幂等键的一部分；时钟未同步时可能为 000000） */
    int64_t tx_counter;        /* 幂等键的本地序号，**跨重启持久化**（`REQ-039`：键必须唯一，否则中台会返回别人那笔的结果） */
    int64_t local_tx_seq;      /* 本次开机内的自增序号（仅用于 key 的调试可读性） */
    size_t backfill_sent;
    size_t backfill_failed;
    bool warn_threshold_reached;
    char last_error[16];       /* 最近一次中台错误码（供界面显示） */
} sync_t;

/* 开机初始化：恢复队列与本地缓存。**绝不访问网络**，故中台不可达也能进入营业界面（`AC-025`）。 */
void sync_init(sync_t *sync, const sync_port_t *port, const char *device_id, const char *device_token);

bool sync_is_online(const sync_t *sync);
bool sync_is_operational(const sync_t *sync); /* 已激活且价目表可用 → 允许进入营业流程 */
void sync_set_link(sync_t *sync, sync_link_t link);

/* 一次同步轮询（周期调用）：激活 → 心跳 → 拉配置 → 补传。离线时只更新状态，不阻塞界面。 */
void sync_poll(sync_t *sync);

/* 本地计价（`REQ-037` / `REQ-038` / `AC-028`）：只用本地价目表 + `core` 的 `price_amount`。
 * 无该商品当日价目表 → false（**不得按 0 元成交**，MT-1006 语义）；重量越界 → false（`REQ-027`）。 */
bool sync_price_line(const sync_t *sync, int64_t product_id, weight_grams_t weight,
                     money_cents_t *amount);

/* 暂存一笔本地计价的交易并尝试上报。返回值即 `queue_status_t`：
 * `QUEUE_WARN_THRESHOLD` = 已落盘但达告警阈值（只告警、仍继续接受，`REQ-016` / `NFR-014`）；
 * `QUEUE_ERR_FULL` / `QUEUE_ERR_IO` = **必须向操作者明确报错且不得记为成功**（`REQ-030` / `RL-9`）。 */
queue_status_t sync_submit(sync_t *sync, const proto_line_t *lines, size_t line_count,
                           money_cents_t total, int64_t *out_seq);

/* 补传一轮：按 `staged_at` 升序逐条上报；成功即清除本地副本，失败该条保持暂存且不阻断后续。 */
void sync_backfill(sync_t *sync);

/* 收款（`REQ-009` / `REQ-043`）：`cash=true` 只记现金；否则取回中台下发的 `qr_payload`。
 * **二维码内容一律来自中台**，秤端只负责本地画码、不生成顾客页面（`AC-033`）。 */
bool sync_settle(sync_t *sync, const char *transaction_no, bool cash, char *qr_payload, size_t cap);

/* 客户端幂等键（`REQ-039`）：`{YYYYMMDD}-{HHMMSS}-{4 位持久化序号}`（契约 §3.5 的样例同形）。
 * 序号跨重启持久化，故重启后不会与上次开机的键相撞 —— 撞键的后果不是报错，而是中台把新交易
 * 当成旧交易并返回首次结果（`REQ-039`），故这是正确性问题而非美观问题。 */
void sync_make_idempotency_key(const sync_t *sync, int64_t local_seq, char *out, size_t cap);

/* ================= 平台端口的 ESP-IDF 实现（`main/net/http_client.c`） =================
 * ⚠️ 本区**只是声明**：实现在 http_client.c；本机没有 ESP-IDF，故该实现**未编译、未验证**
 * （见 README「验证边界」）。声明里刻意不出现任何 ESP-IDF 类型（不透明句柄 + C 基本类型），
 * 这样 test_sync.c 仍然只依赖本头文件就能编译。 */
typedef struct scale_net scale_net_t; /* 不透明：定义在 http_client.c */

scale_net_t *scale_net_create(const char *api_base_url);
void scale_net_destroy(scale_net_t *net);
/* 启动 WiFi STA 并注册事件；返回 false = 配置阶段就失败（**不阻塞开机**，`AC-025`） */
bool scale_net_start_wifi(scale_net_t *net, const char *ssid, const char *password);
sync_link_t scale_net_link_state(scale_net_t *net);
bool scale_net_http_request(scale_net_t *net, const char *method, const char *path,
                            const char *device_token, const char *idempotency_key, const char *body,
                            size_t body_length, char *response, size_t cap, sync_http_result_t *result);
bool scale_net_local_time_iso(scale_net_t *net, char *out, size_t cap);
int64_t scale_net_now_ms(scale_net_t *net);

#endif /* CAISHIC_SCALE_SYNC_H */
