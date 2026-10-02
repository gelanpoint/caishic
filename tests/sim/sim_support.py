"""`tests/sim/` 的共用支撑：**把仓库根放进 `sys.path`**，使 `import sim` 可用。

刻意**不放 pytest 夹具**：夹具跨文件可见要靠 conftest/plugin，而"多目录同名 conftest"正是本项目
踩过的坑（`cabe4e1`）。各用例文件用 `import sim_support` 显式取需要的东西，路径短、无隐式耦合。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SIM_DIR = REPO_ROOT / "sim"
PARAMS_PATH = SIM_DIR / "calibration" / "params.json"

#: 演示数据文件（仿真**绝不允许**触碰它；由用例做运行前后哈希比对）。
#: ⚠️ 文件名带**点**：`market_trade.sqlite3`。本常量第一版写成 `market_trade_sqlite3`（下划线），
#: 于是那条守卫**永远走 skip 分支**、永远不报信 —— 错的常量比没有常量更坏。
DEMO_DB = REPO_ROOT / "data" / "market_trade.sqlite3"

#: 标准库模块名的**粗粒度**白名单：只做"显然不是标准库"的拦截（`numpy`/`pandas`/`requests`…）。
#: 若走 `sys.stdlib_module_names` 精确判定，会把"本地相对导入"也一起卡住，反而容易被人放宽。
KNOWN_THIRD_PARTY = {
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "requests",
    "httpx",
    "yaml",
    "pytest",
    "flask",
    "statsmodels",
    "simpy",
    "networkx",
}

#: sim 允许 import 的仓库内顶层包（**只有自己**；出现 `app` / `run` 即违反隔离）
ALLOWED_LOCAL_ROOTS = {"sim"}

#: 相对导入的哨兵名（`from .core import x`）。**不能把相对导入直接映射成 `sim`** ——
#: 那样 `app/**` 里的相对导入也会被算作"引用了 sim"，反向隔离检查会全线假红。
RELATIVE_IMPORT = "<relative>"


def sim_python_files() -> list[Path]:
    """`sim/**` 的全部 Python 源文件（按路径排序，便于稳定输出）。"""
    return sorted(SIM_DIR.rglob("*.py"))


def imported_roots(source: str) -> set[str]:
    """解析源码里所有 `import X` / `from X import ...` 的**顶层包名**。**纯函数**。

    相对导入统一记作 `RELATIVE_IMPORT`，由调用方决定允许与否。
    """
    import ast

    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入
                roots.add(RELATIVE_IMPORT)
            elif node.module:
                roots.add(node.module.split(".")[0])
    return roots
