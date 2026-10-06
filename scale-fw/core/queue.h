/* 本地暂存队列（`plan.md` §4 `scale-fw/core/queue.h`；`REQ-040` / `REQ-041` / `NFR-013` / `NFR-014`）。
 *
 * 存在理由（`ADR-0006` §3 理由 2）：离线队列必须满足「绝不静默丢弃」（`RL-9`），
 * 把队列写成**定长记录 + CRC** 的追加写日志，比在解释器堆上操作文件更可审计，
 * 也更容易在断电场景下证明「不丢」。
 *
 * 断电语义（本文件的核心）：
 *   - 记录**定长**（`QUEUE_RECORD_BYTES`），每条自带 CRC-32；日志只追加、不原地修改长度；
 *   - 断电可能留下**尾部残片**：不足一条记录的字节，或 CRC 不符的半条记录；
 *   - 恢复（`queue_recover`）遇到第一条不完整/CRC 不符的记录即**停止**，把剩余字节计入
 *     `truncated_bytes` 并如实上报 —— 残片**不得**被当成一条待补传交易，也**不得**被静默忽略；
 *   - 写入失败（`QUEUE_ERR_IO`）时 `used` / `staged` 一律不变：**不留半条记录**（`REQ-030` 的秤端对应）。
 *
 * 三条状态：`staged`（待补传）/ `acked`（补传成功，本地副本已清除）/ `discarded`（显式丢弃，有计数）。
 * 本文件不硬编码任何阈值：告警阈值由调用方传入（来源见 `docs/standards/quality-gates.md` 与配置下发）。
 *
 * 标准头说明：`<string.h>` 仅为取得 `size_t`（ISO C 标准头，非 ESP-IDF）。
 */
#ifndef CAISHIC_SCALE_QUEUE_H
#define CAISHIC_SCALE_QUEUE_H

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

/* 定长记录总字节数：2 魔数 + 1 状态 + 1 版本 + 4×8 整数 + 40 幂等键 + 4 CRC = 80 */
#define QUEUE_RECORD_BYTES 80
/* 幂等键最大长度（含结尾 NUL；主契约要求 ≤64，秤端窄缓冲取 40 —— 由下发配置决定实际取值） */
#define QUEUE_IDEM_KEY_MAX 40
/* 记录格式版本（写进记录头，格式变更时可识别旧记录而不是猜着解析） */
#define QUEUE_FORMAT_VERSION 1

/* 定长记录的字节布局 —— 这是**持久化格式契约**（设备重启 / 断电后按它解析），故公开在头文件里。
 * 0-1 魔数 'S''Q' / 2 状态 / 3 版本 / 4-11 seq / 12-19 staged_at / 20-27 business_date /
 * 28-35 amount_cents / 36-75 幂等键(40) / 76-79 CRC-32（覆盖前 76 字节）。 */
#define QUEUE_MAGIC0 'S'
#define QUEUE_MAGIC1 'Q'
#define QUEUE_OFF_MAGIC0 0
#define QUEUE_OFF_MAGIC1 1
#define QUEUE_OFF_STATE 2
#define QUEUE_OFF_VERSION 3
#define QUEUE_OFF_SEQ 4
#define QUEUE_OFF_STAGED_AT 12
#define QUEUE_OFF_BUSINESS_DATE 20
#define QUEUE_OFF_AMOUNT 28
#define QUEUE_OFF_KEY 36
#define QUEUE_OFF_CRC 76
#define QUEUE_CRC_COVERED 76

typedef enum {
    QUEUE_STATE_STAGED = 0,    /* 待补传 */
    QUEUE_STATE_ACKED = 1,     /* 补传成功（本地副本已清除） */
    QUEUE_STATE_DISCARDED = 2  /* 显式丢弃（有计数，绝不静默） */
} queue_state_t;

typedef struct {
    int64_t seq;                            /* 单调序号（暂存时分配） */
    int64_t staged_at;                      /* 暂存时刻；补传按它升序（契约 §6 顺序不变量） */
    int64_t business_date;                  /* 暂存时所属营业日：补传必须用它，不是补传当天 */
    int64_t amount_cents;                   /* 本地计价金额（比对输入，**不是账口**，`REQ-038`） */
    uint8_t state;                          /* `queue_state_t` */
    char idempotency_key[QUEUE_IDEM_KEY_MAX]; /* 补传幂等键（重复上报由中台按它去重） */
} queue_record_t;

typedef enum {
    QUEUE_OK = 0,             /* 已落盘 */
    QUEUE_ALREADY_STAGED = 2, /* 同一幂等键已在队列中：不重复落盘，返回既有 seq */
    QUEUE_WARN_THRESHOLD = 1, /* 已落盘，但待补传条数达告警阈值：只告警、仍继续接受（NFR-014） */
    QUEUE_ERR_FULL = -1,      /* 无空间：**明确报错**（RL-9 绝不静默丢弃） */
    QUEUE_ERR_IO = -2,        /* 写入失败：**明确报错**，且不留半条记录 */
    QUEUE_ERR_INVALID = -3,   /* 入参非法（空键/超长键/空指针） */
    QUEUE_ERR_NOT_FOUND = -4  /* 指定 seq 不存在或状态不允许该操作 */
} queue_status_t;

/* 注入式写入回调：返回 false 表示写入失败（测试用它模拟断电 / 写坏块）。NULL = 默认内存写入。 */
typedef bool (*queue_write_fn)(void *ctx, size_t offset, const uint8_t *bytes, size_t length);

typedef struct {
    size_t staged;           /* 待补传条数 */
    size_t acked;            /* 已确认条数（本地副本已清除） */
    size_t discarded;        /* 已显式丢弃条数 */
    size_t truncated_bytes;  /* 尾部残片字节数（断电留下的不完整记录） */
    size_t invalid_records;  /* 完整长度但 CRC/魔数/版本不符的记录数 */
} queue_recovery_t;

typedef struct {
    uint8_t *log;              /* 追加写存储：主机侧为内存缓冲，设备侧为 LittleFS 上的记录文件 */
    size_t capacity;           /* 存储字节数 */
    size_t used;               /* 已提交的**完整**记录字节数（恒为 QUEUE_RECORD_BYTES 的整数倍） */
    queue_write_fn write;      /* 见 queue_write_fn */
    void *write_ctx;
    size_t warn_threshold;     /* 达此条数即告警，但仍继续接受 */
    size_t staged;
    size_t acked;
    size_t discarded;
    int64_t next_seq;
} queue_t;

/* ---------------- 记录编解码（定长 + CRC） ---------------- */

uint32_t queue_crc32(const uint8_t *data, size_t length);
bool queue_encode(const queue_record_t *record, uint8_t out[QUEUE_RECORD_BYTES]);
bool queue_decode(const uint8_t *bytes, size_t length, queue_record_t *out);

/* ---------------- 队列生命周期 ---------------- */

void queue_init(queue_t *q, uint8_t *log, size_t capacity, size_t warn_threshold);
void queue_set_writer(queue_t *q, queue_write_fn write, void *write_ctx);

/* 暂存一条交易。成功返回 `QUEUE_OK` / `QUEUE_WARN_THRESHOLD`；
 * 同一幂等键已在队列中 → `QUEUE_ALREADY_STAGED`（不新建记录，*out_seq 为既有 seq）；
 * 无空间 → `QUEUE_ERR_FULL`；写入失败 → `QUEUE_ERR_IO`。后两者**都不改变队列状态**。 */
queue_status_t queue_stage(queue_t *q, const queue_record_t *record, int64_t *out_seq);

size_t queue_pending_count(const queue_t *q);
bool queue_get(const queue_t *q, int64_t seq, queue_record_t *out);

/* 按 `staged_at` 升序（同刻按 seq 升序）取出待补传记录 —— 契约 §6 的顺序不变量。
 * 返回写入 out 的条数；`cap` 小于待补传条数时返回 0（不返回半份乱序结果）。 */
size_t queue_pending_order(const queue_t *q, queue_record_t *out, size_t cap);

/* 补传成功：置 acked 并**清除该条本地副本**（`NFR-013` / `RL-8`）。 */
queue_status_t queue_ack(queue_t *q, int64_t seq);
/* 显式丢弃：计入 discarded 计数（绝不静默丢弃的记账面）。 */
queue_status_t queue_discard(queue_t *q, int64_t seq);
/* 物理回收 acked / discarded 槽位，返回回收条数（回收后本地副本不再存在）。 */
size_t queue_compact(queue_t *q);

/* 从存储恢复队列（开机 / 断电后调用）：CRC 与魔数逐条校验，残片如实上报。
 * 调用顺序：先 `queue_init(q, log, capacity, warn_threshold)`（设定存储与阈值），再 `queue_recover`。
 * 恢复后 `used` 收缩到**可信前缀**的末尾，下一次暂存从该处续写（覆盖损坏尾部）。 */
bool queue_recover(queue_t *q, uint8_t *log, size_t used, queue_recovery_t *report);

#endif /* CAISHIC_SCALE_QUEUE_H */
