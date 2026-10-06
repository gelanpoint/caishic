/* 定点金额与重量类型（`plan.md` §4 `scale-fw/core/money.h`）。
 *
 * 两条口径直接来自权威计价实现 `app/domain/pricing.py` 的模块说明，逐条对应：
 *   1. 金额一律整数「分」、重量一律整数「克」，**全程整数运算**（引入小数会让对账等式时对时错）；
 *   2. 金额与重量的换算基数是 `MONEY_GRAMS_PER_KG`（1 公斤 = 1000 克）。
 *
 * 硬边界（宪法 §1「秤端技术栈例外」边界 3）：本目录只允许 ISO C 标准头，
 * **不得** include 任何 ESP-IDF 头文件 —— 这是「无硬件也能验证核心逻辑」的前提（`Q-21`）。
 * 因此本文件只包含 `<stdint.h>`。
 */
#ifndef CAISHIC_SCALE_MONEY_H
#define CAISHIC_SCALE_MONEY_H

#include <stdint.h>

/* 金额：整数「分」。不用小数类型，见文件头口径 1。 */
typedef int64_t money_cents_t;

/* 重量：整数「克」。 */
typedef int64_t weight_grams_t;

/* 换算基数：1 公斤 = 1000 克（`data-model.md` §0 计价公式的分母）。 */
#define MONEY_GRAMS_PER_KG ((int64_t)1000)

#endif /* CAISHIC_SCALE_MONEY_H */
