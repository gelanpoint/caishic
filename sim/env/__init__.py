"""`sim/env/`：仿真环境层（市场 / 客流 / 设备）。

**唯一耦合面是 HTTP 契约的 38 个端点** —— 本层只构造"环境事实"，
不调用主系统任何函数（由 `tests/sim/test_sim_no_app_import.py` 机械强制）。
"""

from __future__ import annotations
