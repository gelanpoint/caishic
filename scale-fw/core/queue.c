/* 本地暂存队列的实现（`plan.md` §4 `scale-fw/core/queue.c`）。
 *
 * 断电语义与三条状态的说明见 queue.h 头部。本文件不依赖 ESP-IDF、不引入第三方库。
 */
#include "queue.h"

/* 定长记录的字节布局见 queue.h（持久化格式契约）。 */

static void put_u64(uint8_t *out, int64_t value) {
    uint64_t raw = (uint64_t)value;
    for (int i = 0; i < 8; i++) {
        out[i] = (uint8_t)((raw >> (8 * i)) & 0xFFu);
    }
}

static int64_t get_u64(const uint8_t *in) {
    uint64_t raw = 0;
    for (int i = 0; i < 8; i++) {
        raw |= ((uint64_t)in[i]) << (8 * i);
    }
    return (int64_t)raw;
}

uint32_t queue_crc32(const uint8_t *data, size_t length) {
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < length; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++) {
            uint32_t mask = (uint32_t)(-(int32_t)(crc & 1u));
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
        }
    }
    return ~crc;
}

bool queue_encode(const queue_record_t *record, uint8_t out[QUEUE_RECORD_BYTES]) {
    if (record == NULL || out == NULL) {
        return false;
    }
    memset(out, 0, QUEUE_RECORD_BYTES);
    out[QUEUE_OFF_MAGIC0] = QUEUE_MAGIC0;
    out[QUEUE_OFF_MAGIC1] = QUEUE_MAGIC1;
    out[QUEUE_OFF_STATE] = record->state;
    out[QUEUE_OFF_VERSION] = QUEUE_FORMAT_VERSION;
    put_u64(out + QUEUE_OFF_SEQ, record->seq);
    put_u64(out + QUEUE_OFF_STAGED_AT, record->staged_at);
    put_u64(out + QUEUE_OFF_BUSINESS_DATE, record->business_date);
    put_u64(out + QUEUE_OFF_AMOUNT, record->amount_cents);
    memcpy(out + QUEUE_OFF_KEY, record->idempotency_key, QUEUE_IDEM_KEY_MAX);
    put_u64(out + QUEUE_OFF_CRC, (int64_t)queue_crc32(out, QUEUE_CRC_COVERED));
    return true;
}

bool queue_decode(const uint8_t *bytes, size_t length, queue_record_t *out) {
    if (bytes == NULL || out == NULL || length != QUEUE_RECORD_BYTES) {
        return false;
    }
    if (bytes[QUEUE_OFF_MAGIC0] != QUEUE_MAGIC0 || bytes[QUEUE_OFF_MAGIC1] != QUEUE_MAGIC1) {
        return false;
    }
    if (bytes[QUEUE_OFF_VERSION] != QUEUE_FORMAT_VERSION) {
        return false; /* 版本不符：显式拒绝，不猜着解析 */
    }
    uint32_t expected = (uint32_t)get_u64(bytes + QUEUE_OFF_CRC);
    if (queue_crc32(bytes, QUEUE_CRC_COVERED) != expected) {
        return false;
    }
    if (bytes[QUEUE_OFF_STATE] > QUEUE_STATE_DISCARDED) {
        return false;
    }
    memset(out, 0, sizeof(*out));
    out->state = bytes[QUEUE_OFF_STATE];
    out->seq = get_u64(bytes + QUEUE_OFF_SEQ);
    out->staged_at = get_u64(bytes + QUEUE_OFF_STAGED_AT);
    out->business_date = get_u64(bytes + QUEUE_OFF_BUSINESS_DATE);
    out->amount_cents = get_u64(bytes + QUEUE_OFF_AMOUNT);
    memcpy(out->idempotency_key, bytes + QUEUE_OFF_KEY, QUEUE_IDEM_KEY_MAX);
    out->idempotency_key[QUEUE_IDEM_KEY_MAX - 1] = '\0';
    return true;
}

/* ---------------- 生命周期 ---------------- */

void queue_init(queue_t *q, uint8_t *log, size_t capacity, size_t warn_threshold) {
    if (q == NULL) {
        return;
    }
    memset(q, 0, sizeof(*q));
    q->log = log;
    q->capacity = capacity;
    q->warn_threshold = warn_threshold;
    q->next_seq = 1;
}

void queue_set_writer(queue_t *q, queue_write_fn write, void *write_ctx) {
    if (q == NULL) {
        return;
    }
    q->write = write;
    q->write_ctx = write_ctx;
}

static bool queue_write_at(queue_t *q, size_t offset, const uint8_t *bytes, size_t length) {
    if (q->write != NULL) {
        return q->write(q->write_ctx, offset, bytes, length);
    }
    if (q->log == NULL || offset + length > q->capacity) {
        return false;
    }
    memcpy(q->log + offset, bytes, length);
    return true;
}

/* 逐条扫描**可信前缀**；遇到解码失败即停止（之后的字节不可信）。 */
#define QUEUE_SCAN(q, off, rec)                                                            \
    for ((off) = 0; (off) + QUEUE_RECORD_BYTES <= (q)->used; (off) += QUEUE_RECORD_BYTES)   \
        if (!queue_decode((q)->log + (off), QUEUE_RECORD_BYTES, &(rec))) {                  \
            break;                                                                          \
        } else

static bool queue_find(const queue_t *q, int64_t seq, size_t *out_offset, queue_record_t *out) {
    size_t offset;
    queue_record_t rec;
    QUEUE_SCAN(q, offset, rec) {
        if (rec.seq == seq) {
            if (out_offset != NULL) {
                *out_offset = offset;
            }
            if (out != NULL) {
                *out = rec;
            }
            return true;
        }
    }
    return false;
}

queue_status_t queue_stage(queue_t *q, const queue_record_t *record, int64_t *out_seq) {
    if (q == NULL || q->log == NULL || record == NULL) {
        return QUEUE_ERR_INVALID;
    }
    const char *terminator = (const char *)memchr(record->idempotency_key, '\0', QUEUE_IDEM_KEY_MAX);
    if (terminator == NULL || terminator == record->idempotency_key) {
        return QUEUE_ERR_INVALID; /* 空键或未以 NUL 结尾 */
    }

    /* 幂等：同一键已在待补传队列中 → 返回既有 seq，不重复落盘 */
    size_t offset;
    queue_record_t existing;
    QUEUE_SCAN(q, offset, existing) {
        if (existing.state == QUEUE_STATE_STAGED &&
            strcmp(existing.idempotency_key, record->idempotency_key) == 0) {
            if (out_seq != NULL) {
                *out_seq = existing.seq;
            }
            return QUEUE_ALREADY_STAGED;
        }
    }

    if (q->used + QUEUE_RECORD_BYTES > q->capacity) {
        return QUEUE_ERR_FULL; /* 明确报错，绝不静默丢弃（RL-9） */
    }

    queue_record_t fresh = *record;
    fresh.seq = q->next_seq;
    fresh.state = QUEUE_STATE_STAGED;
    uint8_t bytes[QUEUE_RECORD_BYTES];
    if (!queue_encode(&fresh, bytes)) {
        return QUEUE_ERR_INVALID;
    }
    /* 一次性写入整条记录：写失败时 used / staged 均不变 —— **不留半条记录** */
    if (!queue_write_at(q, q->used, bytes, QUEUE_RECORD_BYTES)) {
        return QUEUE_ERR_IO;
    }
    q->used += QUEUE_RECORD_BYTES;
    q->next_seq += 1;
    q->staged += 1;
    if (out_seq != NULL) {
        *out_seq = fresh.seq;
    }
    if (q->warn_threshold > 0 && q->staged >= q->warn_threshold) {
        return QUEUE_WARN_THRESHOLD; /* 只告警，仍继续接受新交易（NFR-014） */
    }
    return QUEUE_OK;
}

size_t queue_pending_count(const queue_t *q) {
    return (q == NULL) ? 0 : q->staged;
}

bool queue_get(const queue_t *q, int64_t seq, queue_record_t *out) {
    if (q == NULL || out == NULL) {
        return false;
    }
    return queue_find(q, seq, NULL, out);
}

size_t queue_pending_order(const queue_t *q, queue_record_t *out, size_t cap) {
    if (q == NULL || out == NULL || cap < q->staged) {
        return 0; /* 容量不足时返回 0：不返回半份乱序结果 */
    }
    size_t count = 0;
    size_t offset;
    queue_record_t rec;
    QUEUE_SCAN(q, offset, rec) {
        if (rec.state != QUEUE_STATE_STAGED) {
            continue;
        }
        size_t i = count;
        while (i > 0 && (out[i - 1].staged_at > rec.staged_at ||
                         (out[i - 1].staged_at == rec.staged_at && out[i - 1].seq > rec.seq))) {
            out[i] = out[i - 1];
            i -= 1;
        }
        out[i] = rec;
        count += 1;
    }
    return count;
}

static queue_status_t queue_set_state(queue_t *q, int64_t seq, uint8_t state) {
    if (q == NULL || q->log == NULL) {
        return QUEUE_ERR_INVALID;
    }
    size_t offset = 0;
    queue_record_t rec;
    if (!queue_find(q, seq, &offset, &rec)) {
        return QUEUE_ERR_NOT_FOUND;
    }
    if (rec.state != QUEUE_STATE_STAGED) {
        return QUEUE_ERR_NOT_FOUND; /* 状态只允许从 staged 迁移一次 */
    }
    queue_record_t tombstone = rec;
    tombstone.state = state;
    /* 清除本地副本（NFR-013 / RL-8）：只保留序号与状态（可审计），其余字段一律清零 */
    tombstone.staged_at = 0;
    tombstone.business_date = 0;
    tombstone.amount_cents = 0;
    memset(tombstone.idempotency_key, 0, sizeof(tombstone.idempotency_key));

    uint8_t bytes[QUEUE_RECORD_BYTES];
    if (!queue_encode(&tombstone, bytes)) {
        return QUEUE_ERR_INVALID;
    }
    if (!queue_write_at(q, offset, bytes, QUEUE_RECORD_BYTES)) {
        return QUEUE_ERR_IO; /* 写入失败：该条保持 staged，下一轮重试（契约 §6） */
    }
    q->staged -= 1;
    if (state == QUEUE_STATE_ACKED) {
        q->acked += 1;
    } else {
        q->discarded += 1;
    }
    return QUEUE_OK;
}

queue_status_t queue_ack(queue_t *q, int64_t seq) {
    return queue_set_state(q, seq, QUEUE_STATE_ACKED);
}

queue_status_t queue_discard(queue_t *q, int64_t seq) {
    return queue_set_state(q, seq, QUEUE_STATE_DISCARDED);
}

size_t queue_compact(queue_t *q) {
    if (q == NULL || q->log == NULL) {
        return 0;
    }
    size_t write_offset = 0;
    size_t reclaimed = 0;
    size_t offset;
    queue_record_t rec;
    QUEUE_SCAN(q, offset, rec) {
        if (rec.state == QUEUE_STATE_STAGED) {
            if (write_offset != offset) {
                memmove(q->log + write_offset, q->log + offset, QUEUE_RECORD_BYTES);
            }
            write_offset += QUEUE_RECORD_BYTES;
        } else {
            reclaimed += 1;
        }
    }
    if (write_offset < q->used) {
        memset(q->log + write_offset, 0, q->used - write_offset); /* 回收区不留任何残留字节 */
    }
    q->used = write_offset;
    q->acked = 0;
    q->discarded = 0;
    return reclaimed;
}

bool queue_recover(queue_t *q, uint8_t *log, size_t used, queue_recovery_t *report) {
    if (q == NULL || log == NULL || report == NULL || used > q->capacity) {
        return false;
    }
    memset(report, 0, sizeof(*report));
    q->log = log;

    size_t offset = 0;
    while (offset + QUEUE_RECORD_BYTES <= used) {
        queue_record_t rec;
        if (!queue_decode(log + offset, QUEUE_RECORD_BYTES, &rec)) {
            report->invalid_records += 1; /* 完整长度但校验不过：可信前缀到此为止 */
            break;
        }
        if (rec.state == QUEUE_STATE_STAGED) {
            report->staged += 1;
        } else if (rec.state == QUEUE_STATE_ACKED) {
            report->acked += 1;
        } else {
            report->discarded += 1;
        }
        if (rec.seq >= q->next_seq) {
            q->next_seq = rec.seq + 1;
        }
        offset += QUEUE_RECORD_BYTES;
    }
    if (offset < used) {
        /* 断电留下的尾部残片（含刚判定失败的那条整记录）：如实上报，不当成交易、也不静默忽略 */
        report->truncated_bytes = used - offset;
    }

    q->used = offset; /* 收缩到可信前缀：下一次暂存从这里续写，覆盖损坏尾部 */
    q->staged = report->staged;
    q->acked = report->acked;
    q->discarded = report->discarded;
    return true;
}
