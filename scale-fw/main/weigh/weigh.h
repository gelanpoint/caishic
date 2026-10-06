/* 称重采样（`T-SCALE-13`；`REQ-005` / `REQ-027`；`AC-006` 的"称重计价自动"）。
 *
 * ⚠️ **未编译、未验证**（本机无 ESP-IDF、无硬件）：只有**静态检查 + 人工评审**这一档边界（见 README）。
 * 硬件时序（HX711 的 24 位读出与增益选择、UART 仪表的报文格式）**必须**在真机上核对，见 README「待办」。
 *
 * 两路采样（`plan.md` §4「HX711 与 UART 仪表两路」）：
 *   - `WEIGH_SOURCE_HX711`：裸 GPIO 位翻转驱动（**不引第三方 HX711 库** —— 宪法 §1 秤端技术栈例外的
 *     第 4 条边界：不得引入白名单外的第三方 C 库）。
 *   - `WEIGH_SOURCE_UART_METER`：接现成电子秤仪表的串口输出（现场已有仪表的摊位用它，不动秤体）。
 *
 * 本层只输出**整数克**（`weight_grams_t`），绝不产生浮点：`core` 的计价全整数，
 * 这里一旦引入 `float` 就会把"分/克全整数"的口径从源头破掉。
 */
#ifndef CAISHIC_SCALE_WEIGH_H
#define CAISHIC_SCALE_WEIGH_H

#include <stdbool.h>
#include <stdint.h>

#include "money.h"

typedef enum {
    WEIGH_SOURCE_HX711 = 0,     /* 裸桥式传感器 + HX711 */
    WEIGH_SOURCE_UART_METER = 1 /* 现成仪表串口读数 */
} weigh_source_t;

/* 引脚（HX711）。`WEIGH_HX711_GAIN_A128` = A 通道增益 128（称重标准档）。 */
typedef struct {
    int dout_gpio;
    int sck_gpio;
} weigh_hx711_pins_t;

typedef struct {
    int uart_port;      /* 0/1/2 */
    int rx_gpio;
    int tx_gpio;        /* 只读仪表时可不接 */
    int baud_rate;      /* 常见 9600；按现场仪表说明书配置 */
} weigh_uart_config_t;

typedef struct {
    weigh_source_t source;
    weigh_hx711_pins_t hx711;
    weigh_uart_config_t uart;
    int64_t hx711_scale_num;  /* 克 = 原始码 / 分母 * 分子（整数，避免浮点） */
    int64_t hx711_scale_den;
    int64_t tare_grams;       /* 去皮偏移（含容器重量） */
    int stable_samples;       /* 连续多少个采样在容差内即判稳定 */
    int64_t stable_tolerance_grams;
} weigh_config_t;

typedef struct {
    weigh_config_t config;
    bool ready;
    int64_t last_raw;
    int64_t last_grams;
    int stable_count;
    int64_t previous_grams;
} weigh_t;

bool weigh_init(weigh_t *weigh, const weigh_config_t *config);

/* 读一次当前重量（已去皮、整数克）。返回 false = 本次读数无效（传感器未就绪 / 超时 / 报文不合法）。
 * ⚠️ **不做**范围判断：`<= 0` 与 `> 50 公斤` 一律由 `pricing_weight_is_priceable`（`core`）定夺（`REQ-027`），
 * 免得"合法范围"在秤端出现第二份实现。 */
bool weigh_read_grams(weigh_t *weigh, weight_grams_t *out);

/* 等到读数稳定（连续 `stable_samples` 次在容差内）或超时。返回 false = 超时未稳定（界面应提示"请稳定放置"）。 */
bool weigh_wait_stable(weigh_t *weigh, int64_t timeout_ms, weight_grams_t *out);

/* 去皮：把当前读数记为容器重量（摊主放上容器后按一次）。 */
bool weigh_tare(weigh_t *weigh);

#endif /* CAISHIC_SCALE_WEIGH_H */
