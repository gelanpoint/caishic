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

#: 仿真参数文件（`provenance` 的机器权威；出处校验见 `tests/sim/test_param_provenance.py`）
PARAMS_PATH = SIM_DIR / "calibration" / "params.json"

#: 《调研报告》原文 —— `provenance.ref` 里的 `结论 N` **唯一**指向的东西。
#: 放在这里而不是各用例文件各写一遍：同一路径写两遍就是下次漂移的种子
#: （`docs/PROJECT-STATE.md` 已记过一次"同一份事实存两遍"的事故）。
REPORT_PATH = REPO_ROOT / "docs" / "调研报告-现实情况.md"

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


# ---------------------------------------------------------------------------
# `T-SIM-06` 集成运行的**极小规模档**（真跑四个 Agent，但把规模压到"秒级"）
# ---------------------------------------------------------------------------
#: 集成运行的最小可用配置：**30 营业日**（= 1 个完整仿真月 ⇒ 月度事件齐全）、
#: 60 日到达 / 20 个消费者。用更短的 `days`（如 20）会**没有任何月频事件**
#: （`month_closed` / `consumer_period` / `market_cash_month`），于是 `M-01/02/03/06/15/17/18/19`
#: 全部分母为 0 —— 那种"绿"什么也没测。
TINY_RUN = {"days": 30, "arrivals": 60, "consumers": 20}


def scenario_by_id(scenario_id: str) -> dict:
    """按 id 取一个场景定义（`sim/scenarios/S*.json`）。"""
    from sim.bridge.scenario import load_scenario, scenario_files

    for path in scenario_files():
        scenario = load_scenario(path)
        if scenario["id"] == scenario_id:
            return scenario
    raise KeyError(f"没有场景 {scenario_id!r}")


def tiny_run(*, scenario_id: str = "S0", arm_index: int = 0, seed: int = 20261002, out_dir: Path,
             extra_overrides: dict | None = None, days: int | None = None, arrivals: int | None = None,
             consumers: int | None = None):
    """跑一次**极小规模**的集成运行，返回 `(result, events)`。

    它走的是**与 CLI 完全相同**的入口（`run_scenario`），只是把规模压小 ——
    于是用例断言的是产品路径，不是"测试自己搭的一个平行世界"。
    """
    from sim.bridge.model_adapter import read_events, run_scenario
    from sim.bridge.scenario import arm_flags, merged_overrides
    from sim.core.params import load_params

    scenario = scenario_by_id(scenario_id)
    arm = scenario["arms"][arm_index]
    overrides = merged_overrides(scenario, arm)
    overrides["daily_arrivals_per_market"] = arrivals if arrivals is not None else TINY_RUN["arrivals"]
    overrides["consumer_agent_count"] = consumers if consumers is not None else TINY_RUN["consumers"]
    if extra_overrides:
        overrides.update(extra_overrides)
    result = run_scenario(
        load_params(PARAMS_PATH),
        scenario_id=f"{scenario_id}::{arm['name']}",
        overrides=overrides,
        days=days if days is not None else TINY_RUN["days"],
        seed=seed,
        out_dir=Path(out_dir),
        self_funded=arm_flags(scenario, arm)["self_funded"],
    )
    return result, read_events(Path(out_dir) / "events.jsonl")


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
