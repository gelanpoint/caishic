/* 界面（`T-SCALE-13`；`REQ-004` / `REQ-005` / `REQ-014` / `REQ-016` / `REQ-043`；`AC-006` / `AC-033`）。
 *
 * ⚠️ **未编译、未验证**（本机无 ESP-IDF、无硬件）：只有**静态检查 + 人工评审**这一档边界（见 README）。
 * LVGL 的调用签名与组件版本**必须**在真机/装好 ESP-IDF 的环境核对。
 *
 * 三条界面硬约束（本文件靠"没有某样东西"来满足，故写成注释便于评审核对）：
 *   1. `AC-006` —— **全程无文本输入**：本模块**不创建** `lv_keyboard` / `lv_textarea` /
 *      任何可编辑控件，也不调用 `lv_textarea_set_text`；摊主只需"点图标 → 看金额 → 点确认"（2 次交互）。
 *   2. `AC-033` —— **秤端不承载顾客页面**：本模块只把中台下发的 `qr_payload` 画成本地二维码
 *      （`sync_settle` 取回），**没有**任何顾客页面的路由/模板/静态资源。
 *   3. `NFR-015` —— 本模块**不含**佣金 / 日聚合 / 结算 / 看板 / 退货冲正计算，只显示"本笔金额"。
 */
#ifndef CAISHIC_SCALE_UI_H
#define CAISHIC_SCALE_UI_H

#include <stdbool.h>
#include <stdint.h>

#include "sync.h"
#include "weigh.h"

typedef struct ui ui_t; /* 不透明：定义在 ui.c */

/* 建界面并绑定编排器与称重通道。**不访问网络、不等待硬件**：中台不可达也照常进营业界面（`AC-025`）。 */
ui_t *ui_create(sync_t *sync, weigh_t *weigh);
void ui_destroy(ui_t *ui);

/* 周期刷新（由主循环每 ~100ms 调一次）：重量、金额、离线/在线标识、暂存告警、补传进度。 */
void ui_tick(ui_t *ui);

/* 显式触发一次链路重试（`REQ-014`：离线/在线状态切换要能在界面上显式触发并可复现）。 */
void ui_trigger_reconnect(ui_t *ui);

/* 收款码：把中台下发的载荷画到屏幕上（`AC-033`）。载荷为空表示现金收款，不画码。 */
void ui_show_payment(ui_t *ui, const char *qr_payload);

#endif /* CAISHIC_SCALE_UI_H */
