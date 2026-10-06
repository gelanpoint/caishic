# 秤端固件主组件（`T-SCALE-13`）。
#
# `SRCS` 与磁盘文件**一一对应**：列了不存在的文件 = 工程直接构建失败；
# 磁盘上有文件却没列 = 该文件根本不参与编译（比前者更隐蔽）。
# 对应证据见 README「SRCS 与磁盘文件一一对应」。
#
# ⚠️ 本文件**未在本机验证**（本机无 ESP-IDF）：只有静态检查 + 人工评审（见 README「验证边界」）。
idf_component_register(
    SRCS
        "app_main.c"
        "net/http_client.c"
        "net/sync.c"
        "store/store.c"
        "weigh/weigh.c"
        "ui/ui.c"
        # 纯 C 业务核心：`core/` 不是独立组件（它不依赖 ESP-IDF），随 main 一起编译；
        # 同一份源码同时用于主机侧 `zig cc` 单测 —— 这正是 ADR-0006 §3.2 对冲 (a) 的形态。
        "../core/pricing.c"
        "../core/queue.c"
        "../core/proto.c"
    INCLUDE_DIRS
        "."
        "net"
        "store"
        "weigh"
        "ui"
        "../core"
    REQUIRES
        driver
        esp_wifi
        esp_event
        esp_netif
        esp_http_client
        esp_timer
        nvs_flash
        # ⚠️ 以下两个是**第三方组件**（须经组件管理器引入，见 README「构建前置」）：
        #   littlefs      —— ADR-0006 §3.2 已接受的落盘方案（esp_littlefs）
        #   lvgl / esp_lvgl_port —— ADR-0006 §3.4 认可的 UI 路径
        # 组件名随版本可能变化（如 `littlefs` vs `joltwallet__littlefs`），
        # 须在装好 ESP-IDF 的环境按 `idf.py reconfigure` 的实际报错核定。
        littlefs
        lvgl
        esp_lvgl_port
)
