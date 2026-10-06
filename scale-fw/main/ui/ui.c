/* 界面实现（`T-SCALE-13`）。边界与三条硬约束见 ui.h。
 *
 * ⚠️ **未编译、未验证**（本机无 ESP-IDF、无硬件）：只有**静态检查 + 人工评审**这一档边界（见 README）。
 * 依赖：LVGL（`ADR-0006` §3.4 认可的 UI 路径）+ `esp_lvgl_port` 显示/触摸移植层 + LVGL 自带 qrcode 库。
 * 引入方式见 README「构建前置」（本机无法执行，故未验证）。
 */
#include "ui.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "lvgl.h"

static const char *TAG = "scale-ui";

/* 尺寸与配色：现场强光下要大字号、高对比（调研结论：字小、按键慢是弃用主因）。 */
#define UI_WEIGHT_FONT_SCALE 4
#define UI_AMOUNT_FONT_SCALE 5
#define UI_TILE_SIZE 120
#define UI_QR_SIZE 200

struct ui {
    sync_t *sync;
    weigh_t *weigh;

    lv_obj_t *weight_label;
    lv_obj_t *amount_label;
    lv_obj_t *link_label;    /* 离线 / 在线：明确可视标识（`REQ-014`） */
    lv_obj_t *pending_label; /* 待补传条数 + 达阈值提示（`REQ-016`） */
    lv_obj_t *hint_label;    /* 明确报错用（`REQ-030`：写入失败不得当成功） */
    lv_obj_t *confirm_button;
    lv_obj_t *qr_code;

    int64_t selected_product;
    int64_t amount_cents;
    bool amount_ready;
    int64_t last_pending_seen;
};

/* ---- 小工具 ---- */

static lv_obj_t *ui_make_label(lv_obj_t *parent, int font_scale, lv_color_t color) {
    lv_obj_t *label = lv_label_create(parent);
    lv_obj_set_style_text_font(label, LV_FONT_DEFAULT, 0);
    /* ⚠️ 真机需换成对应字号的中文字库（LVGL 内置字体不含中文）；此处只留设置入口。 */
    (void)font_scale;
    lv_obj_set_style_text_color(label, color, 0);
    return label;
}

static void ui_set_hint(ui_t *ui, const char *text) {
    if (ui->hint_label == NULL) return;
    lv_label_set_text(ui->hint_label, text);
}

/* ---- 交互：选品（第 1 次交互）与确认收款（第 2 次交互） ---- */

static void ui_on_product_selected(lv_event_t *event) {
    ui_t *ui = (ui_t *)lv_event_get_user_data(event);
    lv_obj_t *target = lv_event_get_target(event);
    ui->selected_product = (int64_t)(intptr_t)lv_obj_get_user_data(target);
    ui->amount_ready = false;
    ui->amount_cents = 0;
    lv_label_set_text(ui->amount_label, "--");
    ui_set_hint(ui, "请把商品放上秤台"); /* 称重与计价随后自动进行（`AC-006`） */
    ESP_LOGI(TAG, "选品 product_id=%lld", (long long)ui->selected_product);
}

static void ui_on_confirm(lv_event_t *event) {
    ui_t *ui = (ui_t *)lv_event_get_user_data(event);
    if (!ui->amount_ready) {
        ui_set_hint(ui, "尚未计价");
        return;
    }
    if (!sync_is_operational(ui->sync)) {
        ui_set_hint(ui, "设备未激活，请联系运维"); /* `AC-027` */
        return;
    }
    proto_line_t line;
    memset(&line, 0, sizeof(line));
    line.product_id = ui->selected_product;
    line.weight_grams = ui->weigh->last_grams;
    line.amount_cents = ui->amount_cents;
    /* 单价从本地价目表取（与计价同源，避免"显示一个价、上报另一个价"）。 */
    for (size_t i = 0; i < ui->sync->price_count; i++) {
        if (ui->sync->prices[i].product_id == ui->selected_product) {
            line.unit_price_cents = ui->sync->prices[i].unit_price_cents;
            break;
        }
    }
    int64_t seq = 0;
    queue_status_t status = sync_submit(ui->sync, &line, 1, ui->amount_cents, &seq);
    if (status == QUEUE_ERR_FULL || status == QUEUE_ERR_IO) {
        /* `REQ-030` / `RL-9`：必须明确报错，**不得**记为成功。 */
        ui_set_hint(ui, status == QUEUE_ERR_FULL ? "本地暂存已满，请联系运维" : "本地暂存写入失败，请重试");
        return;
    }
    if (status == QUEUE_WARN_THRESHOLD) {
        ui_set_hint(ui, "暂存较多，请联系运维（仍可继续收款）"); /* `REQ-016`：只告警、不阻断 */
    }
    ui_show_payment(ui, NULL); /* 现金/扫码由收款页选择；扫码时再取中台载荷 */
    (void)seq;
}

/* ---- 收款码（`AC-033`：内容来自中台，秤端只负责画） ---- */

void ui_show_payment(ui_t *ui, const char *qr_payload) {
    if (ui == NULL) return;
    if (qr_payload == NULL || qr_payload[0] == '\0') {
        ui_set_hint(ui, "已记现金收款");
        if (ui->qr_code != NULL) lv_obj_add_flag(ui->qr_code, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    /* 秤端**只**把中台给的载荷画成二维码；不生成、不改写、不承载顾客页面（`AC-033`）。 */
    if (ui->qr_code == NULL) {
        ui->qr_code = lv_qrcode_create(lv_scr_act(), UI_QR_SIZE, lv_color_black(), lv_color_white());
        lv_obj_align(ui->qr_code, LV_ALIGN_CENTER, 0, 0);
    }
    lv_obj_clear_flag(ui->qr_code, LV_OBJ_FLAG_HIDDEN);
    if (lv_qrcode_update(ui->qr_code, qr_payload, strlen(qr_payload)) != LV_RES_OK) {
        ui_set_hint(ui, "收款码生成失败，请改用现金");
    }
    ui_set_hint(ui, "请顾客扫码支付");
}

/* ---- 建界面 ---- */

ui_t *ui_create(sync_t *sync, weigh_t *weigh) {
    ui_t *ui = (ui_t *)lv_mem_alloc(sizeof(ui_t));
    if (ui == NULL) return NULL;
    memset(ui, 0, sizeof(*ui));
    ui->sync = sync;
    ui->weigh = weigh;

    lv_obj_t *screen = lv_scr_act();
    lv_obj_set_style_bg_color(screen, lv_color_white(), 0);

    /* 商品图标墙：只从**本地缓存的字典**生成，故断网也能选品（`REQ-037`）。 */
    lv_obj_t *tiles = lv_obj_create(screen);
    lv_obj_set_size(tiles, LV_PCT(100), 200);
    lv_obj_set_flex_flow(tiles, LV_FLEX_FLOW_ROW_WRAP);
    size_t shown = sync->product_count;
    for (size_t i = 0; i < shown; i++) {
        const proto_product_t *product = &sync->products[i];
        if (!product->active) continue;
        lv_obj_t *tile = lv_btn_create(tiles);
        lv_obj_set_size(tile, UI_TILE_SIZE, UI_TILE_SIZE);
        lv_obj_set_user_data(tile, (void *)(intptr_t)product->product_id);
        lv_obj_add_event_cb(tile, ui_on_product_selected, LV_EVENT_CLICKED, ui);
        lv_obj_t *name = ui_make_label(tile, 2, lv_color_black());
        lv_label_set_text(name, product->name);
        lv_obj_center(name);
        /* ⚠️ 真机应换成商品图标图片（`lv_img`）+ 名称标签；此处用名称标签保证无图片资源也能评审逻辑。 */
    }

    ui->weight_label = ui_make_label(screen, UI_WEIGHT_FONT_SCALE, lv_color_black());
    lv_label_set_text(ui->weight_label, "0 克");
    lv_obj_align(ui->weight_label, LV_ALIGN_TOP_LEFT, 12, 210);

    ui->amount_label = ui_make_label(screen, UI_AMOUNT_FONT_SCALE, lv_color_make(0xC0, 0x20, 0x20));
    lv_label_set_text(ui->amount_label, "--");
    lv_obj_align(ui->amount_label, LV_ALIGN_TOP_RIGHT, -12, 210);

    ui->link_label = ui_make_label(screen, 2, lv_color_make(0x80, 0x80, 0x80));
    lv_label_set_text(ui->link_label, "离线");
    lv_obj_align(ui->link_label, LV_ALIGN_BOTTOM_LEFT, 12, -12);

    ui->pending_label = ui_make_label(screen, 2, lv_color_make(0x80, 0x80, 0x80));
    lv_label_set_text(ui->pending_label, "");
    lv_obj_align(ui->pending_label, LV_ALIGN_BOTTOM_MID, 0, -12);

    ui->hint_label = ui_make_label(screen, 2, lv_color_make(0x20, 0x60, 0xC0));
    lv_label_set_text(ui->hint_label, "");
    lv_obj_align(ui->hint_label, LV_ALIGN_BOTTOM_RIGHT, -12, -12);
    if (sync->product_count == 0) ui_set_hint(ui, "暂无商品字典，请联网同步或联系运维");

    ui->confirm_button = lv_btn_create(screen);
    lv_obj_set_size(ui->confirm_button, 220, 90);
    lv_obj_align(ui->confirm_button, LV_ALIGN_CENTER, 0, 120);
    lv_obj_add_event_cb(ui->confirm_button, ui_on_confirm, LV_EVENT_CLICKED, ui);
    lv_obj_t *confirm_text = ui_make_label(ui->confirm_button, 3, lv_color_white());
    lv_label_set_text(confirm_text, "确认收款");
    lv_obj_center(confirm_text);

    /* `AC-006`：到这里为止，本模块创建的控件里**没有任何**可输入文本的控件 —— 这是刻意的。 */
    ESP_LOGI(TAG, "界面已就绪（无文本输入控件；离线也照常进营业界面）");
    return ui;
}

void ui_destroy(ui_t *ui) {
    if (ui == NULL) return;
    if (ui->qr_code != NULL) lv_obj_del(ui->qr_code);
    lv_mem_free(ui);
}

void ui_trigger_reconnect(ui_t *ui) {
    if (ui == NULL) return;
    /* 显式触发一次同步：链路状态会随之刷新（`REQ-014`：状态切换可显式触发、可复现）。 */
    sync_poll(ui->sync);
    ui_set_hint(ui, sync_is_online(ui->sync) ? "已连接中台" : "仍未连上中台");
}

void ui_tick(ui_t *ui) {
    if (ui == NULL) return;
    weight_grams_t grams = 0;
    if (weigh_read_grams(ui->weigh, &grams)) {
        lv_label_set_text_fmt(ui->weight_label, "%lld 克", (long long)grams);
        if (ui->selected_product != 0 && !ui->amount_ready) {
            money_cents_t amount = 0;
            /* 本地计价（`REQ-037` / `AC-028`）：口径来自 `core`，界面不做第二次实现。 */
            if (sync_price_line(ui->sync, ui->selected_product, grams, &amount)) {
                ui->amount_cents = amount;
                ui->amount_ready = true;
                lv_label_set_text_fmt(ui->amount_label, "%lld.%02lld 元", (long long)(amount / 100),
                                      (long long)(amount % 100));
                ui_set_hint(ui, "请确认收款");
            } else if (grams <= 0 || grams > SYNC_MAX_WEIGHT_GRAMS) {
                ui_set_hint(ui, "重量超出可计价范围"); /* `REQ-027` */
            } else {
                ui_set_hint(ui, "该商品今日未设价，请先设价");
            }
        }
    }

    lv_label_set_text(ui->link_label, sync_is_online(ui->sync) ? "在线" : "离线");
    size_t pending = queue_pending_count(&ui->sync->queue);
    if (pending != (size_t)ui->last_pending_seen) {
        ui->last_pending_seen = (int64_t)pending;
        lv_label_set_text_fmt(ui->pending_label, "待补传 %u 笔", (unsigned)pending);
    }
    if (ui->sync->warn_threshold_reached) {
        ui_set_hint(ui, "暂存较多，请联系运维（仍可继续收款）");
    }
    if (ui->sync->last_error[0] != '\0') {
        /* 把中台错误码如实显示出来（摊主据此报给运维），但不阻断营业。 */
        lv_label_set_text_fmt(ui->pending_label, "待补传 %u 笔（%s）", (unsigned)pending,
                              ui->sync->last_error);
    }
}
