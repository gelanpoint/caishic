/* 称重采样实现（`T-SCALE-13`）。边界与两路采样的理由见 weigh.h。
 *
 * ⚠️ **未编译、未验证**（本机无 ESP-IDF、无硬件）：只有**静态检查 + 人工评审**这一档边界（见 README）。
 * 位翻转时序与 UART 报文解析**必须**在真机上核对：本文件里的时序常数、增益脉冲数、报文格式
 * 都取自器件手册与现场仪表说明书，**没有在本机跑过一秒**。
 *
 * 全整数纪律：原始码 → 克的换算走 `原始码 * 分子 / 分母`（整数），**不用 float**；
 * 与 `core` 的"分/克全整数"口径一致（宪法 §1 秤端例外第 2 条边界）。
 */
#include "weigh.h"

#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "scale-weigh";

#define HX711_CLOCK_PULSES 25     /* 24 位数据 + 1 个增益选择脉冲（A 通道 128 倍） */
#define HX711_CLOCK_HALF_US 1     /* 手册要求 SCK 高/低电平各约 1µs（<60µs 即可） */
#define HX711_READY_TIMEOUT_US 200000
#define UART_METER_LINE_MAX 48
#define UART_METER_READ_TIMEOUT_MS 200

/* ---- HX711：裸 GPIO 位翻转（不引第三方库） ---- */

static bool hx711_wait_ready(const weigh_config_t *config) {
    int64_t waited = 0;
    while (gpio_get_level((gpio_num_t)config->hx711.dout_gpio) != 0) { /* DOUT 拉低 = 数据就绪 */
        if (waited >= HX711_READY_TIMEOUT_US) return false;
        esp_rom_delay_us(10);
        waited += 10;
    }
    return true;
}

static bool hx711_read_raw(const weigh_config_t *config, int64_t *out_raw) {
    if (!hx711_wait_ready(config)) return false;
    uint32_t value = 0;
    for (int i = 0; i < HX711_CLOCK_PULSES; i++) {
        gpio_set_level((gpio_num_t)config->hx711.sck_gpio, 1);
        esp_rom_delay_us(HX711_CLOCK_HALF_US);
        if (i < 24) value = (value << 1) | (uint32_t)(gpio_get_level((gpio_num_t)config->hx711.dout_gpio) & 1);
        gpio_set_level((gpio_num_t)config->hx711.sck_gpio, 0);
        esp_rom_delay_us(HX711_CLOCK_HALF_US);
    }
    /* 24 位补码 → 有符号（溢出位在上方，故先左移再算术右移） */
    int32_t signed_value = (int32_t)(value << 8);
    *out_raw = (int64_t)(signed_value >> 8);
    return true;
}

/* ---- UART 仪表：按行读，取第一个数字（现场仪表多为 `ST,GS,+  1.234kg` 之类） ---- */

/* 把仪表文本里的数字解析成整数克：支持 `1234`（克）与 `1.234` / `1,234`（千克，三位小数）。
 * 整数实现：小数点后按位累加，**不经过 float**。返回 false = 这一行没有可用数字。 */
static bool uart_parse_grams(const char *line, int64_t *out_grams) {
    const char *p = line;
    while (*p != '\0' && !((*p >= '0' && *p <= '9') || *p == '+' || *p == '-' || *p == '.')) p++;
    bool negative = false;
    if (*p == '+' || *p == '-') {
        negative = (*p == '-');
        p++;
    }
    int64_t whole = 0;
    bool any_digit = false;
    while (*p >= '0' && *p <= '9') {
        whole = whole * 10 + (*p - '0');
        any_digit = true;
        p++;
    }
    int64_t grams = whole;
    if (*p == '.' || *p == ',') {
        p++;
        int64_t fraction = 0;
        int64_t divisor = 1;
        int digits = 0;
        while (*p >= '0' && *p <= '9' && digits < 4) {
            fraction = fraction * 10 + (*p - '0');
            divisor *= 10;
            digits++;
            p++;
        }
        /* 单位：`kg` 或 `g`。仪表若不带单位，按"三位小数即千克"这一现场惯例处理。 */
        bool is_kg = false;
        for (const char *q = p; *q != '\0' && q < p + 4; q++) {
            if (*q == 'k' || *q == 'K') is_kg = true;
        }
        if (!is_kg && digits == 3) is_kg = true;
        grams = is_kg ? (whole * 1000 + fraction * 1000 / divisor) : (whole * 1000 + fraction);
        any_digit = true;
    }
    if (!any_digit) return false;
    *out_grams = negative ? -grams : grams;
    return true;
}

static bool uart_read_grams(const weigh_config_t *config, int64_t *out_grams) {
    char line[UART_METER_LINE_MAX];
    size_t used = 0;
    int64_t deadline_ms = 0;
    while (deadline_ms < UART_METER_READ_TIMEOUT_MS) {
        uint8_t byte = 0;
        int read = uart_read_bytes((uart_port_t)config->uart.uart_port, &byte, 1,
                                   pdMS_TO_TICKS(20));
        deadline_ms += 20;
        if (read <= 0) continue;
        if (byte == '\r') continue;
        if (byte == '\n') {
            line[used] = '\0';
            if (used > 0 && uart_parse_grams(line, out_grams)) return true;
            used = 0;
            continue;
        }
        if (used + 1 < sizeof(line)) line[used++] = (char)byte;
    }
    return false;
}

/* ---- 对外 ---- */

static int64_t raw_to_grams(const weigh_config_t *config, int64_t raw) {
    if (config->hx711_scale_den == 0) return raw; /* 未标定：直接按码值（调试用，README 登记为待办） */
    return raw * config->hx711_scale_num / config->hx711_scale_den;
}

bool weigh_init(weigh_t *weigh, const weigh_config_t *config) {
    if (weigh == NULL || config == NULL) return false;
    memset(weigh, 0, sizeof(*weigh));
    weigh->config = *config;

    if (config->source == WEIGH_SOURCE_HX711) {
        gpio_config_t io;
        memset(&io, 0, sizeof(io));
        io.pin_bit_mask = (1ULL << config->hx711.dout_gpio) | (1ULL << config->hx711.sck_gpio);
        io.mode = GPIO_MODE_INPUT_OUTPUT;
        io.pull_up_en = GPIO_PULLUP_ENABLE; /* DOUT 空闲为高，SCK 低电平有效 */
        if (gpio_config(&io) != ESP_OK) return false;
        gpio_set_level((gpio_num_t)config->hx711.sck_gpio, 0);
    } else {
        uart_config_t uart;
        memset(&uart, 0, sizeof(uart));
        uart.baud_rate = config->uart.baud_rate;
        uart.data_bits = UART_DATA_8_BITS;
        uart.parity = UART_PARITY_DISABLE;
        uart.stop_bits = UART_STOP_BITS_1;
        uart.flow_ctrl = UART_HW_FLOWCTRL_DISABLE;
        if (uart_param_config((uart_port_t)config->uart.uart_port, &uart) != ESP_OK) return false;
        if (uart_set_pin((uart_port_t)config->uart.uart_port, config->uart.tx_gpio, config->uart.rx_gpio,
                         UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE) != ESP_OK) {
            return false;
        }
        if (uart_driver_install((uart_port_t)config->uart.uart_port, 512, 0, 0, NULL, 0) != ESP_OK) {
            return false;
        }
    }
    weigh->ready = true;
    ESP_LOGI(TAG, "称重通道已初始化（源=%s）", config->source == WEIGH_SOURCE_HX711 ? "HX711" : "UART 仪表");
    return true;
}

bool weigh_read_grams(weigh_t *weigh, weight_grams_t *out) {
    if (weigh == NULL || out == NULL || !weigh->ready) return false;
    int64_t grams = 0;
    if (weigh->config.source == WEIGH_SOURCE_HX711) {
        int64_t raw = 0;
        if (!hx711_read_raw(&weigh->config, &raw)) return false;
        weigh->last_raw = raw;
        grams = raw_to_grams(&weigh->config, raw);
    } else if (!uart_read_grams(&weigh->config, &grams)) {
        return false;
    }
    grams -= weigh->config.tare_grams;
    weigh->last_grams = grams;
    *out = (weight_grams_t)grams;
    return true;
}

bool weigh_wait_stable(weigh_t *weigh, int64_t timeout_ms, weight_grams_t *out) {
    if (weigh == NULL || out == NULL || !weigh->ready) return false;
    int64_t waited = 0;
    weigh->stable_count = 0;
    while (waited < timeout_ms) {
        weight_grams_t grams = 0;
        if (weigh_read_grams(weigh, &grams)) {
            int64_t delta = grams - weigh->previous_grams;
            if (delta < 0) delta = -delta;
            if (weigh->stable_count > 0 && delta <= weigh->config.stable_tolerance_grams) {
                weigh->stable_count++;
            } else {
                weigh->stable_count = 1;
            }
            weigh->previous_grams = grams;
            if (weigh->stable_count >= weigh->config.stable_samples) {
                *out = grams;
                return true;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(20));
        waited += 20;
    }
    return false; /* 超时未稳定：界面提示"请稳定放置"，不猜一个重量出来 */
}

bool weigh_tare(weigh_t *weigh) {
    if (weigh == NULL || !weigh->ready) return false;
    weight_grams_t grams = 0;
    if (!weigh_read_grams(weigh, &grams)) return false;
    weigh->config.tare_grams += (int64_t)grams; /* 当前读数即容器重量，此后读数都扣掉它 */
    return true;
}
