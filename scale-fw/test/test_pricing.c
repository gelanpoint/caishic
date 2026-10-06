/* 主机侧单测：golden vectors 逐条比对（`T-SCALE-08` / `T-SCALE-09`；`AC-029`）。
 *
 * 纪律：**不引入任何测试框架依赖**（`plan.md` §4）——自制断言 + 自制极简 JSON 读取器。
 * 向量文件是**产物**（`scripts/gen_pricing_vectors.py` 生成，禁止手改），本文件只读它、不改它。
 *
 * 编译（本机无 gcc / make / cmake，用 zig 自带 C 编译器）：
 *   ~/.local/bin/zig cc -target x86_64-linux-musl -std=c99 -Wall -Wextra -Werror \
 *       -I scale-fw/core -o /tmp/test_pricing scale-fw/test/test_pricing.c scale-fw/core/pricing.c
 * 运行：/tmp/test_pricing scale-fw/test/vectors/pricing_golden.json
 *
 * 本文件可以用 `<stdio.h>` 等标准头：它是**主机侧测试**，不在 `scale-fw/core` 目录下，
 * 不受「core 只用标准头、不得依赖 ESP-IDF」那条边界约束（宪法 §1 边界 3 约束的是 core）。
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pricing.h"

#define DOC_CAP (256 * 1024)
#define MAX_LINES 16

static int g_pass = 0;
static int g_fail = 0;

#define CHECK(cond, ...)           \
    do {                           \
        if (cond) {                \
            g_pass++;              \
        } else {                   \
            g_fail++;              \
            printf("[FAIL] ");     \
            printf(__VA_ARGS__);   \
            printf("\n");          \
        }                          \
    } while (0)

/* ---------------- 极简 JSON 读取器（只支持本向量文件的固定 schema） ----------------
 * 支持：对象、字符串、整数、布尔、整数数组。不支持：嵌套对象、\u 转义。
 * 够用即可 —— 向量文件的 schema 由本仓库的生成器独占控制。 */

static bool is_space(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

static const char *skip_space(const char *p, const char *end) {
    while (p < end && is_space(*p)) p++;
    return p;
}

static const char *find_key(const char *begin, const char *end, const char *key) {
    size_t klen = strlen(key);
    for (const char *p = begin; p + klen + 2 <= end; p++) {
        if (*p != '"' || memcmp(p + 1, key, klen) != 0 || p[1 + klen] != '"') continue;
        const char *q = skip_space(p + 2 + klen, end);
        if (q < end && *q == ':') return q + 1;
    }
    return NULL;
}

/* 读一个整数；失败返回 NULL，成功返回指向数字之后的指针。 */
static const char *read_int(const char *p, const char *end, int64_t *out) {
    p = skip_space(p, end);
    bool negative = false;
    if (p < end && *p == '-') {
        negative = true;
        p++;
    }
    if (p >= end || *p < '0' || *p > '9') return NULL;
    int64_t value = 0;
    while (p < end && *p >= '0' && *p <= '9') value = value * 10 + (*p++ - '0');
    *out = negative ? -value : value;
    return p;
}

static bool get_int64(const char *begin, const char *end, const char *key, int64_t *out) {
    const char *p = find_key(begin, end, key);
    return p != NULL && read_int(p, end, out) != NULL;
}

static bool get_bool(const char *begin, const char *end, const char *key, bool *out) {
    const char *p = find_key(begin, end, key);
    if (p == NULL) return false;
    p = skip_space(p, end);
    if (p + 4 <= end && memcmp(p, "true", 4) == 0) {
        *out = true;
        return true;
    }
    if (p + 5 <= end && memcmp(p, "false", 5) == 0) {
        *out = false;
        return true;
    }
    return false;
}

static bool get_string(const char *begin, const char *end, const char *key, char *out, size_t cap) {
    const char *p = find_key(begin, end, key);
    if (p == NULL) return false;
    p = skip_space(p, end);
    if (p >= end || *p != '"') return false;
    p++;
    size_t used = 0;
    while (p < end && *p != '"') {
        if (used + 1 < cap) out[used++] = *p;
        p++;
    }
    if (p >= end) return false;
    out[used] = '\0';
    return true;
}

static bool get_int_array(const char *begin, const char *end, const char *key, int64_t *out,
                          size_t cap, size_t *count) {
    const char *p = find_key(begin, end, key);
    if (p == NULL) return false;
    p = skip_space(p, end);
    if (p >= end || *p != '[') return false;
    p++;
    size_t used = 0;
    for (;;) {
        p = skip_space(p, end);
        if (p >= end) return false;
        if (*p == ']') break;
        if (used >= cap) return false;
        p = read_int(p, end, &out[used++]);
        if (p == NULL) return false;
        p = skip_space(p, end);
        if (p < end && *p == ',') p++;
    }
    *count = used;
    return true;
}

/* 取下一个顶层对象，并把游标推进到对象之后。 */
static bool next_object(const char **cursor, const char *end, const char **obj_begin,
                        const char **obj_end) {
    const char *p = *cursor;
    while (p < end && *p != '{') p++;
    if (p >= end) return false;
    const char *start = p;
    int depth = 0;
    bool in_string = false;
    for (; p < end; p++) {
        if (in_string) {
            if (*p == '\\') p++;
            else if (*p == '"') in_string = false;
            continue;
        }
        if (*p == '"') in_string = true;
        else if (*p == '{') depth++;
        else if (*p == '}' && --depth == 0) {
            *obj_begin = start;
            *obj_end = p + 1;
            *cursor = p + 1;
            return true;
        }
    }
    return false;
}

/* ---------------- 向量逐条比对 ---------------- */

typedef struct {
    int64_t max_items;
    int64_t max_weight_grams;
    int64_t confirm_threshold_percent;
} limits_t;

static void check_vector(const char *obj, const char *end, const limits_t *limits) {
    char id[64] = "?";
    char kind[32] = "?";
    (void)get_string(obj, end, "id", id, sizeof(id));
    if (!get_string(obj, end, "kind", kind, sizeof(kind))) {
        CHECK(false, "向量缺少 kind：%s", id);
        return;
    }

    if (strcmp(kind, "half_up") == 0) {
        int64_t n = 0, d = 0, expected = 0;
        bool ok = get_int64(obj, end, "numerator", &n) && get_int64(obj, end, "denominator", &d) &&
                  get_int64(obj, end, "quotient", &expected) && half_up_div(n, d) == expected;
        CHECK(ok, "half_up 不一致：%s（%lld / %lld，期望 %lld）", id, (long long)n, (long long)d,
              (long long)expected);
    } else if (strcmp(kind, "amount") == 0) {
        int64_t unit = 0, weight = 0, expected = 0, got = 0;
        bool ok = get_int64(obj, end, "unit_price_cents", &unit) &&
                  get_int64(obj, end, "weight_grams", &weight) &&
                  get_int64(obj, end, "amount_cents", &expected) && price_amount(unit, weight, &got) &&
                  got == expected;
        CHECK(ok, "amount 不一致：%s（单价 %lld × %lld 克，期望 %lld，实得 %lld）", id,
              (long long)unit, (long long)weight, (long long)expected, (long long)got);
    } else if (strcmp(kind, "total") == 0 || strcmp(kind, "round_off") == 0) {
        int64_t lines[MAX_LINES];
        size_t line_count = 0;
        int64_t round_off = 0, items_total = 0, total = 0, sum = 0;
        bool ok = get_int_array(obj, end, "line_amounts", lines, MAX_LINES, &line_count) &&
                  get_int64(obj, end, "round_off_cents", &round_off) &&
                  get_int64(obj, end, "items_total_cents", &items_total) &&
                  get_int64(obj, end, "total_cents", &total) &&
                  pricing_sum_amounts(lines, line_count, &sum) && sum == items_total &&
                  pricing_total_after_round_off(items_total, round_off) == total;
        CHECK(ok, "合计/抹零不一致：%s（明细和 %lld，期望合计 %lld，抹零 %lld，期望总额 %lld）", id,
              (long long)sum, (long long)items_total, (long long)round_off, (long long)total);
    } else if (strcmp(kind, "price_change") == 0) {
        int64_t original = 0, final_price = 0, weight = 0, expected = 0, got = 0, total = 0;
        bool expected_confirm = false;
        bool ok = get_int64(obj, end, "original_unit_price_cents", &original) &&
                  get_int64(obj, end, "final_unit_price_cents", &final_price) &&
                  get_int64(obj, end, "weight_grams", &weight) &&
                  get_int64(obj, end, "final_amount_cents", &expected) &&
                  get_int64(obj, end, "total_cents", &total) &&
                  get_bool(obj, end, "needs_confirm", &expected_confirm) &&
                  price_amount(final_price, weight, &got) && got == expected && got == total;
        bool confirm = pricing_change_needs_confirm(original, final_price,
                                                   limits->confirm_threshold_percent);
        CHECK(ok && confirm == expected_confirm,
              "改价不一致：%s（原价 %lld → %lld，%lld 克，期望金额 %lld，实得 %lld，"
              "需确认 期望 %d 实得 %d）",
              id, (long long)original, (long long)final_price, (long long)weight, (long long)expected,
              (long long)got, (int)expected_confirm, (int)confirm);
    } else if (strcmp(kind, "weight_range") == 0) {
        int64_t weight = 0, max_weight = 0;
        bool expected = false;
        bool ok = get_int64(obj, end, "weight_grams", &weight) &&
                  get_int64(obj, end, "max_weight_grams", &max_weight) &&
                  get_bool(obj, end, "priceable", &expected) && max_weight == limits->max_weight_grams &&
                  pricing_weight_is_priceable(weight, max_weight) == expected;
        CHECK(ok, "重量区间不一致：%s（%lld 克，上限 %lld，期望可计价 %d）", id, (long long)weight,
              (long long)max_weight, (int)expected);
    } else if (strcmp(kind, "items_count") == 0) {
        int64_t count = 0, max_items = 0;
        bool expected = false;
        bool ok = get_int64(obj, end, "item_count", &count) && get_int64(obj, end, "max_items", &max_items) &&
                  get_bool(obj, end, "ok", &expected) && max_items == limits->max_items &&
                  pricing_items_count_ok((size_t)count, (size_t)max_items) == expected;
        CHECK(ok, "明细行数上限不一致：%s（%lld 行，上限 %lld，期望合法 %d）", id, (long long)count,
              (long long)max_items, (int)expected);
    } else {
        CHECK(false, "未知向量类型：%s（%s）—— 生成器新增类型后本测试必须同步", id, kind);
    }
}

/* ---------------- 向量文件之外的本地边界用例 ---------------- */

static void check_local_boundaries(const limits_t *limits) {
    int64_t out = 0, quotient = 0;
    const int64_t threshold = limits->confirm_threshold_percent;

    CHECK(half_up_div_checked(1, 0, &quotient) == false, "除数为 0 必须被拒绝");
    CHECK(half_up_div_checked(1, 1000, NULL) == false, "输出指针为空必须被拒绝");
    CHECK(price_amount(10, 10, NULL) == false, "输出指针为空必须被拒绝");

    /* 溢出边界：Python 侧是大整数、无对应物，C 侧必须**显式失败**而不是给出错误答案。 */
    CHECK(price_amount(INT64_MAX, 2, &out) == false, "乘法溢出必须显式失败");
    CHECK(price_amount(INT64_MAX, 1, &out) == false, "加半值溢出必须显式失败");
    CHECK(price_amount(INT64_MAX - 1000, 1, &out) == true && out > 0,
          "未溢出的大值必须算得出来（实得 %lld）", (long long)out);
    CHECK(price_amount(0, INT64_MAX, &out) == true && out == 0, "0 单价 × 任意重量 = 0");

    /* 非法输入的拒绝路径：返回 0，不产生未定义行为。 */
    CHECK(pricing_total_after_round_off(-1, 0) == 0, "负合计（非法输入）必须被拒绝");
    CHECK(pricing_total_after_round_off(100, -1) == 0, "负抹零（非法输入）必须被拒绝");

    /* 改价确认判据的边界：恰好等于阈值不需确认，多一分即需确认。 */
    CHECK(pricing_change_needs_confirm(0, 100, threshold) == false, "原价为 0 时判据被短路");
    CHECK(pricing_change_needs_confirm(-5, 100, threshold) == false, "负原价时判据被短路");
    CHECK(pricing_change_needs_confirm(100, 100, threshold) == false, "不改价不需确认");
    CHECK(pricing_change_needs_confirm(100, 100 + threshold, threshold) == false,
          "恰好 +阈值 不需确认（判据是严格大于）");
    CHECK(pricing_change_needs_confirm(100, 100 + threshold + 1, threshold) == true,
          "超过阈值一分即需确认");
    CHECK(pricing_change_needs_confirm(100, 100 - threshold, threshold) == false, "恰好 −阈值 不需确认");
    CHECK(pricing_change_needs_confirm(100, 100 - threshold - 1, threshold) == true,
          "低于阈值一分即需确认");

    /* 与 Python 的 // 同义（向下取整），不是 C 的截断除法。 */
    CHECK(pricing_floor_div(-3, 2) == -2, "负数向下取整：-3//2 应为 -2（截断除法会给 -1）");
    CHECK(half_up_div(-2500, 1000) == -2, "负数四舍五入：-2500/1000 应为 -2");
}

int main(int argc, char **argv) {
    const char *path = (argc > 1) ? argv[1] : "scale-fw/test/vectors/pricing_golden.json";

    FILE *handle = fopen(path, "rb");
    if (handle == NULL) {
        printf("[FAIL] 无法打开向量文件：%s\n", path);
        return 1;
    }
    static char doc[DOC_CAP];
    size_t length = fread(doc, 1, sizeof(doc) - 1, handle);
    fclose(handle);
    if (length == 0 || length >= sizeof(doc) - 1) {
        printf("[FAIL] 向量文件为空或超过 %d 字节：%s\n", DOC_CAP, path);
        return 1;
    }
    doc[length] = '\0';
    const char *doc_end = doc + length;

    char format[64] = "";
    if (!get_string(doc, doc_end, "format", format, sizeof(format)) ||
        strcmp(format, "caishic-scale-pricing-golden/v1") != 0) {
        printf("[FAIL] 向量文件格式头不匹配（实得 \"%s\"）：%s\n", format, path);
        return 1;
    }

    limits_t limits = {0, 0, 0};
    const char *limits_at = find_key(doc, doc_end, "limits");
    CHECK(limits_at != NULL && get_int64(limits_at, doc_end, "max_items", &limits.max_items) &&
              get_int64(limits_at, doc_end, "max_weight_grams", &limits.max_weight_grams) &&
              get_int64(limits_at, doc_end, "confirm_threshold_percent",
                        &limits.confirm_threshold_percent),
          "向量文件缺少 limits（阈值必须来自权威实现，不在 C 侧硬编码）");

    int64_t declared = 0;
    CHECK(get_int64(doc, doc_end, "vector_count", &declared), "向量文件缺少 vector_count");

    const char *cursor = find_key(doc, doc_end, "vectors");
    CHECK(cursor != NULL, "向量文件缺少 vectors 数组");
    if (cursor != NULL) {
        const char *obj_begin = NULL;
        const char *obj_end = NULL;
        int64_t seen = 0;
        while (next_object(&cursor, doc_end, &obj_begin, &obj_end)) {
            check_vector(obj_begin, obj_end, &limits);
            seen++;
        }
        CHECK(seen == declared, "实际读取 %lld 条向量，文件声明 %lld 条", (long long)seen,
              (long long)declared);
        printf("[INFO] 向量文件 %s：读取 %lld 条\n", path, (long long)seen);
    }

    check_local_boundaries(&limits);

    printf("[%s] test_pricing: 通过 %d，失败 %d\n", g_fail == 0 ? "PASS" : "FAIL", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
