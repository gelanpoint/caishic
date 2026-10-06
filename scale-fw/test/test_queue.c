/* 主机侧单测：本地暂存队列（`T-SCALE-10`；`REQ-040` / `REQ-041` / `REQ-030` / `NFR-013` / `NFR-014`）。
 *
 * 纪律：**不引入任何测试框架依赖**（`plan.md` §4）——自制断言。
 * 本文件可以用标准头：它是主机侧测试，不在 `scale-fw/core` 目录下。
 *
 * 编译：
 *   ~/.local/bin/zig cc -target x86_64-linux-musl -std=c99 -Wall -Wextra -Werror \
 *       -I scale-fw/core -o /tmp/test_queue scale-fw/test/test_queue.c scale-fw/core/queue.c
 * 运行：/tmp/test_queue
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "queue.h"

#define CAPACITY (QUEUE_RECORD_BYTES * 4)
#define MAX_PENDING 8

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

static void set_key(queue_record_t *record, const char *key) {
    memset(record->idempotency_key, 0, sizeof(record->idempotency_key));
    memcpy(record->idempotency_key, key, strlen(key));
}

static queue_record_t make_record(const char *key, int64_t staged_at, int64_t amount) {
    queue_record_t record;
    memset(&record, 0, sizeof(record));
    set_key(&record, key);
    record.staged_at = staged_at;
    record.business_date = 20261006;
    record.amount_cents = amount;
    return record;
}

/* 在缓冲里数一个字节串出现的次数（核对「本地副本已清除」用）。 */
static size_t count_hits(const uint8_t *buffer, size_t length, const char *needle) {
    size_t needle_length = strlen(needle);
    size_t hits = 0;
    for (size_t i = 0; i + needle_length <= length; i++) {
        if (memcmp(buffer + i, needle, needle_length) == 0) {
            hits++;
        }
    }
    return hits;
}

/* 注入式故障写入：只写前一半字节就报告失败（模拟断电 / 写坏块）。 */
static bool half_write(void *ctx, size_t offset, const uint8_t *bytes, size_t length) {
    uint8_t *log = (uint8_t *)ctx;
    memcpy(log + offset, bytes, length / 2);
    return false;
}

static void test_codec(void) {
    queue_record_t record = make_record("SC-000123-20261006-0007", 1000, 250);
    record.seq = 7;
    record.state = QUEUE_STATE_STAGED;

    uint8_t bytes[QUEUE_RECORD_BYTES];
    CHECK(queue_encode(&record, bytes), "编码必须成功");
    CHECK(QUEUE_RECORD_BYTES == 80, "定长记录应为 80 字节（实得 %d）", QUEUE_RECORD_BYTES);

    queue_record_t decoded;
    CHECK(queue_decode(bytes, QUEUE_RECORD_BYTES, &decoded), "解码必须成功");
    CHECK(decoded.seq == 7 && decoded.staged_at == 1000 && decoded.business_date == 20261006 &&
              decoded.amount_cents == 250 && decoded.state == QUEUE_STATE_STAGED &&
              strcmp(decoded.idempotency_key, "SC-000123-20261006-0007") == 0,
          "编解码往返必须一致");

    uint8_t broken[QUEUE_RECORD_BYTES];
    for (size_t i = 0; i < QUEUE_RECORD_BYTES; i++) {
        memcpy(broken, bytes, sizeof(broken));
        broken[i] ^= 0x01u;
        CHECK(!queue_decode(broken, QUEUE_RECORD_BYTES, &decoded),
              "第 %d 字节翻转后必须判为无效（CRC/魔数/版本必须兜住）", (int)i);
    }
    CHECK(!queue_decode(bytes, QUEUE_RECORD_BYTES - 1, &decoded), "截断记录（少 1 字节）必须判为无效");
    CHECK(!queue_decode(bytes, QUEUE_RECORD_BYTES + 1, &decoded), "超长输入必须判为无效");
    CHECK(!queue_encode(NULL, bytes), "空记录必须被拒绝");
    CHECK(queue_crc32((const uint8_t *)"123456789", 9) == 0xCBF43926u,
          "CRC-32 必须与标准向量一致（实得 %08X）", queue_crc32((const uint8_t *)"123456789", 9));
}

static void test_stage_pending_and_idempotency(void) {
    uint8_t log[CAPACITY];
    queue_t q;
    queue_init(&q, log, sizeof(log), 3);
    CHECK(queue_pending_count(&q) == 0, "初始队列必须为空");

    int64_t seq = 0;
    queue_record_t first = make_record("key-a", 100, 250);
    CHECK(queue_stage(&q, &first, &seq) == QUEUE_OK && seq == 1, "第一条应落盘并取 seq=1（实得 %lld）",
          (long long)seq);
    queue_record_t second = make_record("key-b", 200, 130);
    CHECK(queue_stage(&q, &second, &seq) == QUEUE_OK && seq == 2, "第二条应取 seq=2");
    CHECK(queue_pending_count(&q) == 2, "待补传应为 2 条");
    CHECK(q.used == 2 * QUEUE_RECORD_BYTES, "used 应为 2 条记录长度");

    /* 幂等：同键重复暂存不新建记录 */
    queue_record_t duplicate = make_record("key-a", 999, 999);
    CHECK(queue_stage(&q, &duplicate, &seq) == QUEUE_ALREADY_STAGED && seq == 1,
          "同键重复暂存必须返回既有 seq=1（实得 %lld）", (long long)seq);
    CHECK(queue_pending_count(&q) == 2 && q.used == 2 * QUEUE_RECORD_BYTES,
          "同键重复暂存不得增加记录");

    queue_record_t empty;
    memset(&empty, 0, sizeof(empty));
    CHECK(queue_stage(&q, &empty, &seq) == QUEUE_ERR_INVALID, "空幂等键必须被拒绝");
    queue_record_t no_nul;
    memset(&no_nul, 'x', sizeof(no_nul));
    CHECK(queue_stage(&q, &no_nul, &seq) == QUEUE_ERR_INVALID, "未以 NUL 结尾的键必须被拒绝");
}

static void test_warn_threshold_and_full(void) {
    uint8_t log[2 * QUEUE_RECORD_BYTES];
    queue_t q;
    queue_init(&q, log, sizeof(log), 2);

    int64_t seq = 0;
    queue_record_t a = make_record("k1", 1, 10);
    queue_record_t b = make_record("k2", 2, 20);
    queue_record_t c = make_record("k3", 3, 30);
    CHECK(queue_stage(&q, &a, &seq) == QUEUE_OK, "第 1 条未达阈值：返回 OK");
    CHECK(queue_stage(&q, &b, &seq) == QUEUE_WARN_THRESHOLD,
          "第 2 条达告警阈值：返回 WARN 但仍接受（NFR-014）");
    CHECK(queue_pending_count(&q) == 2, "达阈值后仍继续接受新交易");

    size_t used_before = q.used;
    CHECK(queue_stage(&q, &c, &seq) == QUEUE_ERR_FULL, "无空间必须**明确报错**，不得静默丢弃（RL-9）");
    CHECK(queue_pending_count(&q) == 2 && q.used == used_before,
          "写满被拒后队列状态必须不变（第 3 条没有被记成成功）");
    CHECK(queue_get(&q, 3, &a) == false, "被拒的第 3 条不得存在于队列中");
}

static void test_io_failure_leaves_no_half_record(void) {
    uint8_t log[CAPACITY];
    queue_t q;
    queue_init(&q, log, sizeof(log), 8);
    queue_set_writer(&q, half_write, log);

    int64_t seq = 0;
    queue_record_t record = make_record("io-key", 10, 250);
    CHECK(queue_stage(&q, &record, &seq) == QUEUE_ERR_IO, "写入失败必须**明确报错**（REQ-030）");
    CHECK(q.used == 0 && queue_pending_count(&q) == 0, "写入失败后不得留下半条记录");

    /* 断电语义：存储里确实躺着半条残片 → 恢复必须判它无效，且如实上报字节数 */
    uint8_t raw[CAPACITY];
    memset(raw, 0, sizeof(raw));
    uint8_t encoded[QUEUE_RECORD_BYTES];
    queue_record_t fresh = make_record("io-key", 10, 250);
    fresh.seq = 1;
    CHECK(queue_encode(&fresh, encoded), "编码必须成功");
    memcpy(raw, encoded, QUEUE_RECORD_BYTES / 2);

    queue_t recovered;
    queue_init(&recovered, raw, sizeof(raw), 8);
    queue_recovery_t report;
    CHECK(queue_recover(&recovered, raw, QUEUE_RECORD_BYTES / 2, &report),
          "恢复必须成功返回（残片不是致命错误，但必须上报）");
    CHECK(report.staged == 0 && report.invalid_records == 0 &&
              report.truncated_bytes == QUEUE_RECORD_BYTES / 2,
          "尾部残片必须计入 truncated_bytes=%d（实得 %d），且不得当成一条交易",
          QUEUE_RECORD_BYTES / 2, (int)report.truncated_bytes);
    CHECK(recovered.used == 0, "恢复后可信前缀为空：下一次暂存从 0 续写");

    /* 完整长度但 CRC 不符（写坏块）：同样判无效，且可信前缀收缩到它之前 */
    uint8_t raw2[CAPACITY];
    memset(raw2, 0, sizeof(raw2));
    queue_record_t good = make_record("good-key", 10, 250);
    good.seq = 1;
    uint8_t good_bytes[QUEUE_RECORD_BYTES];
    CHECK(queue_encode(&good, good_bytes), "编码必须成功");
    memcpy(raw2, good_bytes, QUEUE_RECORD_BYTES);
    uint8_t bad_bytes[QUEUE_RECORD_BYTES];
    memcpy(bad_bytes, good_bytes, sizeof(bad_bytes));
    bad_bytes[QUEUE_OFF_STAGED_AT] ^= 0xFFu; /* 改载荷但不改 CRC → 必然校验失败 */
    memcpy(raw2 + QUEUE_RECORD_BYTES, bad_bytes, QUEUE_RECORD_BYTES);

    queue_t recovered2;
    queue_init(&recovered2, raw2, sizeof(raw2), 8);
    queue_recovery_t report2;
    CHECK(queue_recover(&recovered2, raw2, 2 * QUEUE_RECORD_BYTES, &report2), "恢复必须成功返回");
    CHECK(report2.staged == 1 && report2.invalid_records == 1 &&
              report2.truncated_bytes == QUEUE_RECORD_BYTES,
          "第 1 条应恢复为待补传、第 2 条判无效（实得 staged=%d invalid=%d truncated=%d）",
          (int)report2.staged, (int)report2.invalid_records, (int)report2.truncated_bytes);
    CHECK(recovered2.used == QUEUE_RECORD_BYTES, "可信前缀应收缩到第 1 条末尾");

    /* 收缩后可继续暂存：新记录写回被判定损坏的位置 */
    int64_t new_seq = 0;
    queue_record_t next = make_record("after-recovery", 20, 99);
    CHECK(queue_stage(&recovered2, &next, &new_seq) == QUEUE_OK && new_seq == 2,
          "恢复后应能从可信前缀续写（seq 应续为 2）");
    queue_t recovered3;
    queue_init(&recovered3, raw2, sizeof(raw2), 8);
    queue_recovery_t report3;
    CHECK(queue_recover(&recovered3, raw2, recovered2.used, &report3) && report3.staged == 2 &&
              report3.truncated_bytes == 0,
          "续写后整段日志应全部可信（staged=2，无残片）");
}

static void test_ack_clears_local_copy(void) {
    uint8_t log[CAPACITY];
    queue_t q;
    queue_init(&q, log, sizeof(log), 8);

    int64_t seq = 0;
    queue_record_t record = make_record("SC-000123-20261006-0007", 1000, 250);
    CHECK(queue_stage(&q, &record, &seq) == QUEUE_OK && seq == 1, "暂存必须成功");
    CHECK(count_hits(log, q.used, "SC-000123-20261006-0007") == 1, "暂存后本地副本存在（1 处命中）");

    CHECK(queue_ack(&q, seq) == QUEUE_OK, "补传成功后必须能确认");
    CHECK(queue_pending_count(&q) == 0, "确认后待补传必须为 0");
    CHECK(q.acked == 1, "已确认计数应为 1");

    queue_record_t tombstone;
    CHECK(queue_get(&q, seq, &tombstone) && tombstone.state == QUEUE_STATE_ACKED,
          "确认后应留下 ACKED 墓碑（序号可审计）");
    CHECK(tombstone.amount_cents == 0 && tombstone.idempotency_key[0] == '\0' &&
              tombstone.business_date == 0,
          "确认后本地副本字段必须清零（NFR-013 / RL-8）");
    CHECK(count_hits(log, q.used, "SC-000123-20261006-0007") == 0,
          "确认后全存储扫描不得再有幂等键残留（实得 %d 处）",
          (int)count_hits(log, q.used, "SC-000123-20261006-0007"));

    CHECK(queue_ack(&q, seq) == QUEUE_ERR_NOT_FOUND, "重复确认必须被拒绝（状态只允许迁移一次）");
    CHECK(queue_ack(&q, 999) == QUEUE_ERR_NOT_FOUND, "不存在的 seq 必须返回 NOT_FOUND");

    queue_t recovered;
    queue_init(&recovered, log, sizeof(log), 8);
    queue_recovery_t report;
    CHECK(queue_recover(&recovered, log, q.used, &report) && report.acked == 1 && report.staged == 0,
          "重启后应恢复为「已确认 1 条、待补传 0 条」");
}

static void test_discard_and_compact(void) {
    uint8_t log[CAPACITY];
    queue_t q;
    queue_init(&q, log, sizeof(log), 8);

    int64_t seq_a = 0, seq_b = 0;
    queue_record_t a = make_record("keep-key", 100, 250);
    queue_record_t b = make_record("drop-key", 200, 130);
    CHECK(queue_stage(&q, &a, &seq_a) == QUEUE_OK, "暂存 A 必须成功");
    CHECK(queue_stage(&q, &b, &seq_b) == QUEUE_OK, "暂存 B 必须成功");
    CHECK(queue_discard(&q, seq_b) == QUEUE_OK, "显式丢弃必须成功");
    CHECK(q.discarded == 1 && queue_pending_count(&q) == 1,
          "丢弃必须有计数（绝不静默）：discarded=1，待补传=1");

    size_t used_before = q.used;
    CHECK(queue_compact(&q) == 1, "压缩应回收 1 条");
    CHECK(q.used == used_before - QUEUE_RECORD_BYTES, "压缩后存储应少一条记录");
    CHECK(count_hits(log, q.used, "drop-key") == 0, "回收后不得留下被丢弃记录的字节");
    CHECK(count_hits(log, q.used, "keep-key") == 1, "回收后待补传记录必须完好");
    CHECK(queue_pending_count(&q) == 1 && queue_get(&q, seq_a, &a) && a.amount_cents == 250,
          "回收后待补传记录内容必须不变");

    queue_t recovered;
    queue_init(&recovered, log, sizeof(log), 8);
    queue_recovery_t report;
    CHECK(queue_recover(&recovered, log, q.used, &report) && report.staged == 1 &&
              report.acked == 0 && report.discarded == 0 && report.truncated_bytes == 0,
          "压缩后的日志必须能完整恢复（staged=1，无墓碑残留）");
}

static void test_pending_order_is_by_staged_at(void) {
    uint8_t log[CAPACITY];
    queue_t q;
    queue_init(&q, log, sizeof(log), 8);

    /* 故意乱序暂存：补传必须按 staged_at 升序（契约 §6 的顺序不变量） */
    int64_t seq = 0;
    queue_record_t late = make_record("order-late", 300, 30);
    queue_record_t early = make_record("order-early", 100, 10);
    queue_record_t middle = make_record("order-middle", 200, 20);
    queue_record_t tie = make_record("order-tie", 200, 21);
    CHECK(queue_stage(&q, &late, &seq) == QUEUE_OK && seq == 1, "乱序暂存第 1 条");
    CHECK(queue_stage(&q, &early, &seq) == QUEUE_OK && seq == 2, "乱序暂存第 2 条");
    CHECK(queue_stage(&q, &middle, &seq) == QUEUE_OK && seq == 3, "乱序暂存第 3 条");
    CHECK(queue_stage(&q, &tie, &seq) == QUEUE_OK && seq == 4, "乱序暂存第 4 条");

    queue_record_t ordered[MAX_PENDING];
    size_t count = queue_pending_order(&q, ordered, MAX_PENDING);
    CHECK(count == 4, "应取出 4 条待补传（实得 %d）", (int)count);
    CHECK(count == 4 && ordered[0].staged_at == 100 && ordered[1].staged_at == 200 &&
              ordered[2].staged_at == 200 && ordered[3].staged_at == 300,
          "补传顺序必须是 staged_at 升序");
    CHECK(count == 4 && ordered[1].seq == 3 && ordered[2].seq == 4,
          "同一 staged_at 时按 seq 升序（实得 %lld, %lld）", (long long)ordered[1].seq,
          (long long)ordered[2].seq);
    CHECK(queue_pending_order(&q, ordered, 2) == 0, "容量不足时必须返回 0，不得给出半份乱序结果");
}

static void test_recover_roundtrip(void) {
    uint8_t log[CAPACITY];
    queue_t q;
    queue_init(&q, log, sizeof(log), 8);
    int64_t seq = 0;
    for (int i = 0; i < 3; i++) {
        char key[16];
        key[0] = (char)('a' + i);
        key[1] = '\0';
        queue_record_t record = make_record(key, 100 + i, 10 * (i + 1));
        CHECK(queue_stage(&q, &record, &seq) == QUEUE_OK, "暂存第 %d 条必须成功", i + 1);
    }

    queue_t rebooted;
    queue_init(&rebooted, log, sizeof(log), 8);
    queue_recovery_t report;
    CHECK(queue_recover(&rebooted, log, q.used, &report) && report.staged == 3 &&
              report.truncated_bytes == 0 && report.invalid_records == 0,
          "重启恢复应得到 3 条待补传、无残片");
    CHECK(rebooted.next_seq == 4, "恢复后序号必须续上（实得 %lld）", (long long)rebooted.next_seq);
    queue_record_t fourth = make_record("d", 200, 40);
    CHECK(queue_stage(&rebooted, &fourth, &seq) == QUEUE_OK && seq == 4,
          "恢复后新暂存应取 seq=4（实得 %lld）", (long long)seq);
    CHECK(queue_pending_count(&rebooted) == 4, "恢复 + 新暂存应为 4 条");
    CHECK(queue_recover(&rebooted, NULL, 0, &report) == false, "空存储指针必须被拒绝");
    CHECK(queue_recover(&rebooted, log, CAPACITY + 1, &report) == false,
          "used 超过容量必须被拒绝");
}

int main(void) {
    test_codec();
    test_stage_pending_and_idempotency();
    test_warn_threshold_and_full();
    test_io_failure_leaves_no_half_record();
    test_ack_clears_local_copy();
    test_discard_and_compact();
    test_pending_order_is_by_staged_at();
    test_recover_roundtrip();
    printf("[%s] test_queue: 通过 %d，失败 %d\n", g_fail == 0 ? "PASS" : "FAIL", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
