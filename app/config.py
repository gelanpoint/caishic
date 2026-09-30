"""运行期配置。

约定（`specs/market-trade-flow/plan.md` §4）：
- 业务数值（如离线暂存告警阈值）**只引用 spec 的来源，不在代码里另立一套**；
- 质量阈值**不在本文件**，唯一权威是 `docs/standards/quality-gates.md`；
- 本文件不读取任何密钥（本项目运行期不需要密钥，宪法 §2）。
"""

from __future__ import annotations

import os
from pathlib import Path

# 仓库根目录（app/config.py → app/ → 仓库根）
BASE_DIR = Path(__file__).resolve().parent.parent

# 运行期数据目录（.gitignore 已忽略；演示数据可由 scripts/reset_demo.py 重建）
DATA_DIR = Path(os.environ.get("MT_DATA_DIR") or (BASE_DIR / "data"))
DB_PATH = DATA_DIR / "market_trade.sqlite3"
OFFLINE_STAGING_DIR = DATA_DIR / "offline_staging"
LOG_DIR = DATA_DIR / "logs"

# 迁移脚本目录（按文件名顺序执行）
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# 静态前端与种子数据目录
STATIC_DIR = Path(__file__).resolve().parent / "static"
SEED_FILE = Path(__file__).resolve().parent / "seed_data" / "seed.json"

APP_NAME = "菜市场数字化交易与佣金系统 MVP"

# 监听地址：默认监听所有网卡，便于用局域网 IP 从另一台设备打开（AGENTS.md §3 硬要求 1/2）
HOST = os.environ.get("MT_HOST") or "0.0.0.0"
PORT = int(os.environ.get("MT_PORT") or "8000")

# 离线暂存告警阈值（笔）：**告警阈值，不是硬上限** —— 达阈值只提示摊主联系运维、
# 并**继续接受新交易**（行为见 `specs/market-trade-flow/spec.md` `REQ-016`；`NFR-014` 规定绝不静默丢弃）。
# 取值 `200` 的**实际出处**：`specs/market-trade-flow/discovery.md` D-12（Q2.3 访谈决策留痕）。
#   ⚠️ 已知缺口（已上报父代理，未自行改规格）：`discovery.md` 自定规则为"需求取值一旦写入 `spec.md`
#   即以 `spec.md` 为该取值的唯一权威载体"，但 `spec.md` `REQ-016` **只写了行为、未写数值**，
#   故本文件暂时引用 D-12；待父代理裁定后按 `RL-2`（规格错了先改规格）把取值落进 `spec.md`，再改回引用 `spec.md`。
OFFLINE_WARN_THRESHOLD = 200
