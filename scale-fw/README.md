# 秤端固件（ESP32-S3 + ESP-IDF，C）

摊位边缘终端：**选品 → 称重 → 本地计价 → 收款 → 本地留痕（离线也照常）→ 恢复联网后补传**。
本目录是 `specs/market-trade-flow/plan.md` §4 的 `scale-fw/` 实现。

## 1. 验证边界（**先读这一节**）

本机（WSL2）**没有 ESP-IDF、没有 ESP32 硬件**，所以这个仓库里两层的验证强度**完全不同**，
下面按目录分区写明。**任何地方都不得宣称 `main/**` "编译通过"或"已验证"。**

| 范围 | 本机做到了什么 | 证据 |
| --- | --- | --- |
| `core/**`（纯 C 业务核心） | **主机侧实测**：编译 + 单测 + 与 Python 权威实现的 golden vectors 逐位比对 | `test_pricing` 137 断言、`test_queue` 153 断言、`test_proto` 109 断言全绿；`zig cc -target x86_64-linux-musl` 零告警（`-std=c99` 与 `-std=c11` 双标准） |
| `main/net/sync.c` + `main/net/sync.h`（同步编排器） | **主机侧实测**：不依赖 ESP-IDF，故用**内存假端口**跑真断言 | `test_sync` **79 断言全绿**（含补传顺序、失败保持暂存、中台不可达仍营业、收款码来自中台） |
| `main/net/http_client.c`（WiFi + HTTP） | **仅静态检查 + 人工评审** | 未编译、未运行 |
| `main/store/store.{c,h}`（LittleFS + NVS） | **仅静态检查 + 人工评审** | 未编译、未运行 |
| `main/weigh/weigh.{c,h}`（HX711 / UART 仪表） | **仅静态检查 + 人工评审** | 未编译、未运行；**时序与报文格式必须在真机核对** |
| `main/ui/ui.{c,h}`（LVGL 界面） | **仅静态检查 + 人工评审** | 未编译、未运行 |
| `main/app_main.c`（组合根） | **仅静态检查 + 人工评审** | 未编译、未运行 |
| `scale-fw/CMakeLists.txt`、`main/CMakeLists.txt` | **仅静态检查 + 人工评审** | 未执行 `idf.py build` |
| `main/idf_component.yml`、`partitions.csv`（构建前置） | **仅静态检查 + 人工评审** | 未执行 `idf.py reconfigure` / `idf.py partition-table`；组件名与版本、分区表是否被 ESP-IDF 接受均**未核定** |

> **待办（阻塞性，须在装好 ESP-IDF 的环境补做并如实记录）**：
> `idf.py set-target esp32s3 && idf.py build` 必须真正跑通一次；在此之前，`main/**` 一律按"未验证"对待。
> 依据：`docs/adr/0006-秤端嵌入式技术栈.md` §4 负面清单、`specs/market-trade-flow/tasks.md` T-SCALE-13 验收 ④。

**没有硬件在环**（`Q-21`）：称重精度、触摸、WiFi 断连、真实掉电时序、屏幕可读性一律**未验证**。
本文档里所有涉及硬件的常数（引脚、标定系数、串口波特率）都是**待现场标定的占位值**，不是结论。

## 2. 目录与职责

```
scale-fw/
├── CMakeLists.txt          # ESP-IDF 工程入口
├── README.md               # 本文件
├── partitions.csv          # 分区表（假设 8MB Flash、不做 OTA；LittleFS 独立 storage 分区）
├── core/                   # 纯 C 业务核心：不依赖 ESP-IDF，主机侧可编译可单测
│   ├── money.h             # 定点类型（整数分 / 整数克）
│   ├── pricing.{c,h}       # 半进位取整、单价×重量、合计与抹零、改价确认阈值
│   ├── queue.{c,h}         # 定长 80 字节记录 + CRC-32 的追加型暂存队列（断电语义）
│   └── proto.{c,h}         # 7 端点报文窄编解码；越权字段不采信
├── main/                   # ESP-IDF 胶水层（薄）
│   ├── app_main.c          # 组合根：11 个端口回调接线 + 开机编排
│   ├── idf_component.yml   # 组件管理器依赖（littlefs/lvgl/esp_lvgl_port；组件名与版本**待核定**）
│   ├── net/                # sync.h/sync.c（可主机侧实测的编排器）+ http_client.c（WiFi+HTTP）
│   ├── store/              # LittleFS（队列日志/报文副本/字典缓存）+ NVS（设备身份）
│   ├── weigh/              # HX711 裸 GPIO 位翻转 + UART 仪表两路采样
│   └── ui/                 # LVGL：图标选品、计价、收款码（无文本输入）
└── test/                   # 主机侧单测（自制断言，不引测试框架）
    ├── test_pricing.c  test_queue.c  test_proto.c
    ├── test_sync.c         # 假端口驱动 sync.c 的真断言
    └── vectors/pricing_golden.json   # 产物：由 scripts/gen_pricing_vectors.py 从 Python 权威实现导出
```

## 3. 主机侧测试（本机已实测，可复现）

本机无 `gcc`，用 `zig cc` 作主机侧 C 编译器（`ADR-0006` §3.2 对冲 a）：

```bash
ZIG=~/.local/bin/zig cc
CORE="scale-fw/core/pricing.c scale-fw/core/queue.c scale-fw/core/proto.c"

# core 三件套
$ZIG -target x86_64-linux-musl -std=c11 -Wall -Wextra -Werror -I scale-fw/core \
     -o /tmp/test_pricing scale-fw/test/test_pricing.c scale-fw/core/pricing.c && /tmp/test_pricing scale-fw/test/vectors/pricing_golden.json
$ZIG -target x86_64-linux-musl -std=c11 -Wall -Wextra -Werror -I scale-fw/core \
     -o /tmp/test_queue scale-fw/test/test_queue.c scale-fw/core/queue.c && /tmp/test_queue
$ZIG -target x86_64-linux-musl -std=c11 -Wall -Wextra -Werror -I scale-fw/core \
     -o /tmp/test_proto scale-fw/test/test_proto.c scale-fw/core/proto.c && /tmp/test_proto

# 同步编排器（含 core）
$ZIG -target x86_64-linux-musl -std=c11 -Wall -Wextra -Werror \
     -I scale-fw/core -I scale-fw/main/net \
     -o /tmp/test_sync scale-fw/test/test_sync.c scale-fw/main/net/sync.c $CORE && /tmp/test_sync

# golden vectors 复算（删掉产物重跑必须逐字节还原）
python scripts/gen_pricing_vectors.py --check
```

`test_sync.c` 用**内存假端口**扮演 WiFi / 中台 / LittleFS，因此下面三条是**实测断言**而不是"读了一遍觉得对"：
① 补传按 `staged_at` 升序；② 补传失败该条保持暂存且不阻断后续；③ 中台不可达时仍能进入营业界面（`AC-025` 前半段）。

## 4. 构建前置（**未在本机执行**）

- ESP-IDF（目标 `esp32s3`）。
- 经组件管理器引入 `esp_littlefs`（`ADR-0006` §3.2 的落盘方案）与 LVGL + `esp_lvgl_port`（`ADR-0006` §3.4 的 UI 路径）：
  `main/idf_component.yml` 已声明这三个依赖，与 `main/CMakeLists.txt` 的 `REQUIRES` **逐项一致**。
  ⚠️ **组件名与版本号是"待核定"的占位**（本机没有 ESP-IDF、没有组件管理器、不联网，无从核定）：
  文件里故意写 `"*"`（任意版本），**没有**填一个看起来已验证、实则臆造的精确版本号。
  **须在装好 ESP-IDF 的环境按 `idf.py reconfigure` 的实际报错核定**，然后**同时**更新
  `idf_component.yml` 与 `main/CMakeLists.txt` 两处。最可能对不上的是 `littlefs`
  （注册表名 `joltwallet/littlefs`，组件名通常为 `joltwallet__littlefs`）。
- 分区表：`partitions.csv` 假设 **8MB Flash**（ESP32-S3-N8R8），**不做 OTA**，LittleFS 占独立 `littlefs`
  storage 分区（子类型写 `spiffs` 是通行做法，`esp_littlefs` 按**标签**查分区，与 `store.c` 的
  `partition_label = "littlefs"` 一致）。逐行累加自洽：`0x9000+0x6000=0xF000`、`0xF000+0x1000=0x10000`、
  `0x10000+0x400000=0x410000`、`0x410000+0x3F0000=0x800000`，恰好填满 8MB、无空洞无重叠。
  ⚠️ **是否真被 ESP-IDF 接受未核定**（须 `idf.py partition-table` 核对）。
- 设备身份（`device_id` / 设备令牌 / 中台 API 基址 / WiFi 口令）由 **NVS provisioning** 写入；
  **本固件不臆造默认值** —— 中台地址为空即视为不可达（离线营业），不会静默指向某个猜测的地址。
  ⚠️ provisioning 的写入流程（产线工具或首次配置入口）**尚未定义**，登记为缺口。
- 标定：`weigh_config.hx711_scale_num/den` 是**现场标定**后写入的值；`app_main.c` 里的 `1/1` 只是占位。

## 5. 已知边界与实现要点（评审核对用）

1. **错误码只在"一笔都不欠"时清除**（`sync.c` 的 `sync_settle_error`）。若按"任一次调用成功就清"，
   某笔卡住（如 MT-2004）后，后续几条成功会把"这笔为什么卡住"擦掉，界面再也看不到原因 ——
   这类缺陷只在**多笔 + 部分失败**的编排里才暴露，单笔单测抓不到。
2. **幂等键必须跨重启唯一**（`REQ-039`）：键 = `{营业日}-{开机时刻}-{持久化序号}`，序号落在本地存储里。
   撞键的后果不是报错，而是中台**返回别人那笔的结果**，故按正确性问题处理。
3. **写前日志**：先落"上报报文副本"、再落队列记录；任一步失败都**不留半条**并向上报错（`REQ-030` / `RL-9`）。
   补传重发的是**同一份字节**，故同一幂等键必然同体（不会误触 MT-1012）。
4. **补传成功即清除本地副本**（`NFR-013` / `RL-8`）：队列记录 ack（载荷清零）+ 报文副本删除，两处都清。
5. **离线不阻塞开机**（`AC-025`）：`sync_init` 只读本地，**零网络调用**；WiFi 最后才起、异步连接。
6. **界面无文本输入**（`AC-006`）：`ui.c` 不创建任何可编辑控件（`lv_keyboard` / `lv_textarea`），
   交互 2 次（选品 → 确认收款），称重计价自动。
7. **秤端不承载顾客页面**（`AC-033`）：二维码内容一律来自中台下发的 `qr_payload`（`sync_settle` 取回），
   固件里没有顾客页面的路由/模板/静态资源。
8. **秤端不做佣金 / 日聚合 / 结算 / 看板 / 退货冲正**（`NFR-015`）：这些一律在中台（`REQ-042`）。
9. **阈值不硬编码**：暂存告警阈值来自中台激活响应（`config.offline_warn_threshold`），秤端只转发给 `core`。
   唯一例外是重量上限 `SYNC_MAX_WEIGHT_GRAMS`（`sync.h` 有醒目注释）——中台不在激活响应里下发它，
   登记为**已知漂移风险**；中台若改为下发字段，此处应改为读配置。
10. **400 行规则**：`scale-fw/**` 的生产代码（`.c/.h`）全部 ≤400 行。规则原文的适用范围只枚举了
    `*.py/*.js/*.css/*.html/*.sh`（`docs/standards/quality-gates.md`），C 技术栈晚于该规则引入，
    故属**规则空档**；已按 Lead 裁定：生产代码守 400 行，**测试文件按覆盖度判**（`test_sync.c` 79 条断言，
    压到 400 行只能删断言）。裁定已记入 `docs/standards/quality-gates.md`。

## 6. 未验证 / 未完成的清单（如实，勿当已交付）

- `main/**` 全部**未编译、未运行**（本机无 ESP-IDF）。
- **无硬件在环**（`Q-21`）：称重精度与稳定性、触摸响应、WiFi 断连重连、真实掉电时序、屏幕强光可读性。
- 分量与分量口径的**真机标定**未做（`hx711_scale_num/den` 为占位）。
- `captured_at` 依赖本机墙钟；未接 SNTP/RTC 时是 1970 起的错误时刻（**营业日一律取中台值**，不影响记账口径）。
- `main/idf_component.yml` 与 `partitions.csv` **已创建但均未被 ESP-IDF 接受过**：组件名与版本待核定
  （见 §4），分区表是否被 `idf.py partition-table` 接受待核定。
- LVGL 中文字库、商品图标图片资源未接入（界面逻辑已留出位置）。
- provisioning（产线写入设备令牌/中台地址/WiFi）流程未定义。
- UART 仪表报文格式按"现场惯例"解析（三位小数即千克）；**具体仪表的协议须逐一核对**。
