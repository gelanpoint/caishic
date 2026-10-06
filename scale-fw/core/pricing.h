/* 定点计价（`plan.md` §4 `scale-fw/core/pricing.h`；`REQ-038` / `AC-029`）。
 *
 * 本文件声明的函数与权威实现 `app/domain/pricing.py` 一一对应，**逐位一致**：
 *
 *   C 侧                              Python 权威侧                       语义
 *   half_up_div()                     half_up_div()                       整数四舍五入除法
 *   price_amount()                    price_amount()                      单价(分/公斤) × 重量(克) → 金额(分)
 *   pricing_sum_amounts()             sum(line[5] for line in lines)      合计 = 明细金额之和
 *   pricing_total_after_round_off()   _recompute_total()                  总额 = max(0, 合计 − 抹零)
 *   pricing_change_needs_confirm()    change_price() 的 MT-1011 判据       改价幅度超阈值需确认（不阻止）
 *   pricing_weight_is_priceable()     _parse_items() 的 MT-1002 判据       重量越界即拒绝
 *   pricing_items_count_ok()          _parse_items() 的 MT-1008 判据       items 长度 1~MAX_ITEMS
 *
 * **取整方式不是银行家舍入**：Python 侧用 `(a + b//2)//b` 而不是内置 `round`，
 * 故 C 侧也必须按「加半个除数后向下取整」实现（负数亦与 Python 的 `//` 同义）。
 * 这不是可自由选择的实现细节，而是 golden vectors 逐条钉住的契约（`AC-029`）。
 *
 * 阈值与上限（重量上限、明细行数上限、改价确认幅度）**一律不由本文件硬编码**：
 * 它们由调用方传入，取值来源是 Python 权威实现导出的 golden vectors 文件。
 * 依据：宪法 §1 例外边界 5「秤端固件只依赖阈值定义处，不复制任何数值」。
 *
 * 标准头说明：`<string.h>` 仅为取得 `size_t`（ISO C 标准头，非 ESP-IDF）。
 */
#ifndef CAISHIC_SCALE_PRICING_H
#define CAISHIC_SCALE_PRICING_H

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "money.h"

/* 向下取整除法：与 Python 的 `//` 同义（含负数）。
 * 前置条件：denominator != 0。 */
int64_t pricing_floor_div(int64_t numerator, int64_t denominator);

/* 整数四舍五入除法：与 Python `half_up_div(n, d) = (n + d//2)//d` 逐位一致。
 * 前置条件：denominator > 0，且 numerator + denominator/2 不溢出。 */
int64_t half_up_div(int64_t numerator, int64_t denominator);

/* `half_up_div` 的安全版本：denominator == 0 或中间量溢出时返回 false 且不写 *quotient。 */
bool half_up_div_checked(int64_t numerator, int64_t denominator, int64_t *quotient);

/* 金额(分) = half_up_div(单价(分/公斤) × 重量(克), 1000)。
 * 与 Python `price_amount` 逐位一致；乘积超出 int64 表示范围时返回 false
 * （Python 侧为大整数、无对应物 —— 该边界见 test_pricing 的 overflow 用例与交付说明）。 */
bool price_amount(money_cents_t unit_price_cents, weight_grams_t weight_grams, money_cents_t *out);

/* 合计 = 明细金额之和（对应 Python `total = sum(line[5] for line in lines)`）。溢出返回 false。 */
bool pricing_sum_amounts(const money_cents_t *line_amounts, size_t line_count, money_cents_t *out);

/* 总额 = max(0, 合计 − 抹零)，对应 Python `_recompute_total` 的 `max(0, items_total - round_off)`。
 * 抹零**只减总额**，不改变任何明细行的标价（`REQ-008` / `AC-008`）。 */
money_cents_t pricing_total_after_round_off(money_cents_t items_total_cents,
                                            money_cents_t round_off_cents);

/* 改价是否需要摊主确认，对应 `change_price` 的 MT-1011 判据：
 * `original > 0 && |final - original| * 100 > original * threshold_percent`。
 * **只要求确认、不阻止操作**（`REQ-007`）；幅度恰好等于阈值时**不需要**确认（严格大于）。
 * 幅度乘积溢出时按「需要确认」从宽处理（宁可多问一次，不可漏问）。 */
bool pricing_change_needs_confirm(money_cents_t original_unit_price_cents,
                                  money_cents_t final_unit_price_cents,
                                  int64_t threshold_percent);

/* 重量是否在可计价区间内（`REQ-027`：≤0 或 > 上限一律拒绝计价，`MT-1002`）。
 * 上限由调用方传入（来源：权威实现导出的向量文件）。 */
bool pricing_weight_is_priceable(weight_grams_t weight_grams, weight_grams_t max_weight_grams);

/* `items` 长度是否合法（主契约 §3.6：1~MAX_ITEMS，`MT-1008`）。上限由调用方传入。 */
bool pricing_items_count_ok(size_t item_count, size_t max_items);

#endif /* CAISHIC_SCALE_PRICING_H */
