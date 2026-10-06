# 秤端固件工程（ESP-IDF）。目录结构与职责见 `specs/market-trade-flow/plan.md` §4。
#
# ⚠️ 本文件**未在本机验证**（本机无 ESP-IDF）：只有静态检查 + 人工评审（见 README「验证边界」）。
# 目标芯片：ESP32-S3（`docs/adr/0006-秤端嵌入式技术栈.md`）。
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(caishic-scale)
