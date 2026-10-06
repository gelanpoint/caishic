/* 定点计价的实现（`plan.md` §4 `scale-fw/core/pricing.c`；`REQ-038` / `AC-029`）。
 *
 * 与 `app/domain/pricing.py` 的对应关系见 pricing.h 的头部表。
 * 全程整数、无小数类型；不使用任何第三方库、不依赖 ESP-IDF。
 */
#include "pricing.h"

/* ------------------------------------------------------------------ */
/* 溢出判定（不依赖编译器内建，纯 C99 算术）                            */
/* ------------------------------------------------------------------ */

static bool add_overflows(int64_t a, int64_t b) {
    if (b > 0) {
        return a > INT64_MAX - b;
    }
    if (b < 0) {
        return a < INT64_MIN - b;
    }
    return false;
}

static bool sub_overflows(int64_t a, int64_t b) {
    if (b > 0) {
        return a < INT64_MIN + b;
    }
    if (b < 0) {
        return a > INT64_MAX + b;
    }
    return false;
}

static bool mul_overflows(int64_t a, int64_t b) {
    if (a == 0 || b == 0 || a == 1 || b == 1) {
        return false;
    }
    if (a == -1) {
        return b == INT64_MIN;
    }
    if (b == -1) {
        return a == INT64_MIN;
    }
    if (a > 0) {
        return (b > 0) ? (b > INT64_MAX / a) : (b < INT64_MIN / a);
    }
    return (b > 0) ? (a < INT64_MIN / b) : (a < INT64_MAX / b);
}

/* ------------------------------------------------------------------ */
/* 取整：与 Python 的 // 同义                                          */
/* ------------------------------------------------------------------ */

int64_t pricing_floor_div(int64_t numerator, int64_t denominator) {
    int64_t quotient = numerator / denominator;
    if ((numerator % denominator != 0) && ((numerator < 0) != (denominator < 0))) {
        quotient -= 1;
    }
    return quotient;
}

bool half_up_div_checked(int64_t numerator, int64_t denominator, int64_t *quotient) {
    if (denominator == 0 || quotient == NULL) {
        return false;
    }
    int64_t half = pricing_floor_div(denominator, 2);
    if (add_overflows(numerator, half)) {
        return false;
    }
    *quotient = pricing_floor_div(numerator + half, denominator);
    return true;
}

int64_t half_up_div(int64_t numerator, int64_t denominator) {
    int64_t quotient = 0;
    (void)half_up_div_checked(numerator, denominator, &quotient);
    return quotient;
}

/* ------------------------------------------------------------------ */
/* 计价                                                                */
/* ------------------------------------------------------------------ */

bool price_amount(money_cents_t unit_price_cents, weight_grams_t weight_grams, money_cents_t *out) {
    if (out == NULL) {
        return false;
    }
    if (mul_overflows(unit_price_cents, weight_grams)) {
        return false;
    }
    int64_t quotient = 0;
    if (!half_up_div_checked(unit_price_cents * weight_grams, MONEY_GRAMS_PER_KG, &quotient)) {
        return false;
    }
    *out = quotient;
    return true;
}

bool pricing_sum_amounts(const money_cents_t *line_amounts, size_t line_count, money_cents_t *out) {
    if (out == NULL || (line_amounts == NULL && line_count != 0)) {
        return false;
    }
    int64_t total = 0;
    for (size_t i = 0; i < line_count; i++) {
        if (add_overflows(total, line_amounts[i])) {
            return false;
        }
        total += line_amounts[i];
    }
    *out = total;
    return true;
}

money_cents_t pricing_total_after_round_off(money_cents_t items_total_cents,
                                            money_cents_t round_off_cents) {
    /* 对应 Python：total = max(0, int(items_total) - int(round_off))
     *
     * 前置条件（由上游校验保证）：items_total >= 0（非负单价 × 非负重量之和）、
     * round_off >= 0（`change_price` 对 `round_off_cents < 0` 抛 MT-1008）。
     * 两个非负 int64 相减不可能溢出，故此处无需溢出分支。
     * 越界输入按「拒绝」返回 0：C 侧不产生未定义行为，而 Python 侧该输入本就被 MT-1008 挡住。 */
    if (items_total_cents < 0 || round_off_cents < 0) {
        return 0;
    }
    money_cents_t total = items_total_cents - round_off_cents;
    return total < 0 ? 0 : total;
}

/* ------------------------------------------------------------------ */
/* 校验判据（阈值一律由调用方传入，本文件不硬编码任何数值）             */
/* ------------------------------------------------------------------ */

bool pricing_change_needs_confirm(money_cents_t original_unit_price_cents,
                                  money_cents_t final_unit_price_cents,
                                  int64_t threshold_percent) {
    /* 对应 Python：original > 0 and abs(final - original) * 100 > original * THRESHOLD */
    if (original_unit_price_cents <= 0) {
        return false;
    }
    if (sub_overflows(final_unit_price_cents, original_unit_price_cents)) {
        return true; /* 溢出按「需要确认」从宽处理 */
    }
    int64_t delta = final_unit_price_cents - original_unit_price_cents;
    if (delta < 0) {
        if (delta == INT64_MIN) {
            return true;
        }
        delta = -delta;
    }
    if (mul_overflows(delta, 100) || mul_overflows(original_unit_price_cents, threshold_percent)) {
        return true;
    }
    return delta * 100 > original_unit_price_cents * threshold_percent;
}

bool pricing_weight_is_priceable(weight_grams_t weight_grams, weight_grams_t max_weight_grams) {
    return weight_grams > 0 && weight_grams <= max_weight_grams;
}

bool pricing_items_count_ok(size_t item_count, size_t max_items) {
    return item_count >= 1 && item_count <= max_items;
}
