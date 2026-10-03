"""**跑一个臂 + 从臂里取值**（`T-SIM-08` 建；`2026-10-03` 从 `backtest.py` 按语义搬出）。

## 为什么搬（`quality-gates.md` §1.2 的 400 行门禁，`Q-16` 裁定：按语义拆、不放宽阈值）

`backtest.py` 一度长到 **485 行**。超的原因是它同时管两件事：

* **怎么把一个臂跑出来、怎么从臂里取一个数**（本模块：跑、复算、按期切序列、分位数区间）；
* **拿这些数怎么判**（留在 `backtest.py`：`judge_clause` / `verdict_of` / 稳健性扫描）。

两者语义正交：前者是**取数**，后者是**判定**。而"取出来的数对不对"与"判得对不对"必须能分开验 ——
本模块的 `measure` / `interval` 是**纯函数**（吃一个 `run_arm` 的返回值），
`tests/sim/test_verify_backtest.py` 的合成负例正是靠这一点直接喂构造出来的臂。

搬动是**纯移动**：函数体逐字保留，`backtest.py` 保留同名转出
（`calibration.py` 的 `from .backtest import run_arm` 一行不改），既有调用方与既有用例零改动。

## 内存纪律（搬过来时一并保留）

`run_arm` 算完指标就**释放事件明细**、只保留派生序列 —— `study.py` 踩过的 `MemoryError`
不重演：24 臂 × 每份 36 万事件的整份列表同时驻留会崩。
"""

from __future__ import annotations

from pathlib import Path

from ..bridge.model_adapter import read_events, run_scenario
from ..bridge.scenario import arm_flags, load_scenario, merged_overrides, scenario_files
from ..core.clock import DAYS_PER_MONTH
from ..observe.metrics import compute_all, survival_judgement
from ..observe.metric_util import pct

#: 重复区间用 p5/p95（与 `study.py` 的 `_summary` 同口径，不另立一套）
INTERVAL = (5, 95)


# ---------------------------------------------------------------------------
# 档位 → 运行参数 → 跑一个臂
# ---------------------------------------------------------------------------
def scenario_by_id(scenario_id: str) -> dict:
    for path in scenario_files():
        scenario = load_scenario(path)
        if scenario["id"] == scenario_id:
            return scenario
    raise KeyError(f"没有场景 {scenario_id!r}")


def arm_overrides(scenario: dict, arm: dict, prof: dict, extra: dict | None = None) -> dict:
    """某一臂在某一档下的**最终参数覆盖**（场景基线 + 该臂对照变量 + 档位规模 + 档位压力值）。"""
    out = merged_overrides(scenario, arm)
    out["daily_arrivals_per_market"] = prof["arrivals"]
    out["consumer_agent_count"] = prof["consumers"]
    out.update(prof.get("param_overrides") or {})
    out.update(extra or {})
    return out


def _period_of(events: list[dict]) -> dict:
    """营业日 → 仿真期号（`DAYS_PER_MONTH=30`）。仿真月与自然月不对齐，故按**日序号**切，不做日期算术。"""
    days = sorted({e["business_date"] for e in events if e.get("kind") == "day_arrivals_total"})
    return {day: index // DAYS_PER_MONTH for index, day in enumerate(days)}


def _scale_series(events: list[dict]) -> list[float]:
    """逐期走秤率序列（`M-04` 的按期切分）——「单调下降」这类判据必须有序列才判得了。

    切窗用**营业日序号**而不是月/日算术（与 `sim/observe/metrics.py::survival_window` 同纪律）：
    仿真月（`DAYS_PER_MONTH=30`）与自然月不对齐，用日期做算术会默默错位。
    """
    period_of = _period_of(events)
    buckets: dict[int, list[int]] = {}
    for event in events:
        if event.get("kind") != "txn":
            continue
        bucket = buckets.setdefault(period_of.get(event["business_date"], -1), [0, 0])
        bucket[1] += 1
        if event.get("channel") == "scale":
            bucket[0] += 1
    return [round(num / den, 6) for _period, (num, den) in sorted(buckets.items()) if den]


def _idle_series(events: list[dict]) -> list[float]:
    """逐期设备闲置率序列（`M-10` 口径①摊位-日的**按期切分**）。

    `m10()` 本身只给全期合计；而 `R3-c`（2026-10-03 规格修正后）判的是**加速上升**，
    必须有序列才判得了。本函数**按 `m10` 的同一口径复算**（同一对事件：`price_list` 与 `txn`），
    只是把窗口换成"按期" —— 口径不另立一处。
    """
    period_of = _period_of(events)
    used: dict[str, set] = {}
    for event in events:
        if event.get("kind") == "txn" and event.get("channel") == "scale":
            used.setdefault(event["business_date"], set()).add(event["stall_no"])
    buckets: dict[int, list[int]] = {}
    for event in events:
        if event.get("kind") != "price_list":
            continue
        bucket = buckets.setdefault(period_of.get(event["business_date"], -1), [0, 0])
        bucket[1] += 1
        if event["stall_no"] not in used.get(event["business_date"], set()):
            bucket[0] += 1
    return [round(num / den, 6) for _period, (num, den) in sorted(buckets.items()) if den]


def _series_of(events: list[dict], metrics: dict) -> dict:
    """从事件流/指标里抽出判据要用的**序列**（算完即丢事件明细）。"""
    maintenance = [float(e.get("maintenance_cents") or 0.0) for e in events if e.get("kind") == "market_admin_period"]
    upfront = max((float(e.get("upfront_cents") or 0.0) for e in events if e.get("kind") == "stall_adopted"),
                  default=None)
    idle_detail = (metrics.get("M-10") or {}).get("detail") or {}
    return {
        "M-01": [row["bp"] / 10000.0 for row in ((metrics.get("M-01") or {}).get("detail") or {}).get("by_month", [])
                 if row.get("denominator")],
        "M-04": _scale_series(events),
        "M-15": [row["mean"] for row in ((metrics.get("M-15") or {}).get("detail") or {}).get("by_period", [])],
        "M-17-B": maintenance,
        "R6-upfront": [upfront] if upfront is not None else [],
        "M-10-设备口径": [idle_detail.get("口径②设备-日(停摆>τ)", {})],
        "M-10-按期": _idle_series(events),
    }


def run_arm(params, *, scenario_id: str, arm_index: int, label: str, prof: dict,
            extra: dict | None = None, seed: int = 20261002, out_root: Path | str = "data/sim/backtest",
            self_funded: bool | None = None, progress=print) -> dict:
    """跑完一个臂的 `replications` 次重复，返回指标 + 派生序列（**不保留事件明细**）。"""
    scenario = scenario_by_id(scenario_id)
    arm = scenario["arms"][arm_index]
    overrides = arm_overrides(scenario, arm, prof, extra)
    flag = arm_flags(scenario, arm)["self_funded"] if self_funded is None else bool(self_funded)
    out_root = Path(out_root)
    rows = []
    for rep in range(prof["replications"]):
        target = out_root / f"{scenario_id}-{arm_index}-{rep:02d}"
        result = run_scenario(params, scenario_id=f"{scenario_id}::{arm['name']}", overrides=overrides,
                              days=prof["days"], seed=seed + rep * 101, out_dir=target, self_funded=flag)
        events = read_events(target / "events.jsonl")
        metrics = compute_all(events)
        rows.append({"rep": rep, "metrics": metrics, "series": _series_of(events, metrics), "out_dir": str(target)})
        del events
        progress(f"    · {label} rep{rep} 走秤率 {rows[-1]['metrics']['M-04']['ratio']}")
    return {"label": label, "scenario": scenario_id, "arm": arm["name"], "arm_index": arm_index,
            "overrides": overrides, "self_funded": flag, "profile": prof["id"], "rows": rows}


# ---------------------------------------------------------------------------
# 取值：measure → 标量 / 序列
# ---------------------------------------------------------------------------
def measure(run: dict, name: str):
    metrics = run["rows"][0]["metrics"]
    series = run["rows"][0].get("series") or {}
    if name in series:
        values = [v for v in series[name] if isinstance(v, (int, float))]
        return (values[0] if name in ("R6-upfront", "M-10-设备口径") else
                (sum(values) / len(values) if values else None)), series[name]
    if name.startswith("M-"):
        row = metrics.get(name) or {}
        return (row.get("value") if row.get("ratio_is_ratio") is False else row.get("ratio")), []
    if name.startswith("survival:"):
        rows = list(survival_judgement(metrics)["criteria"].values())
        return rows[int(name.split(":")[1]) - 1].get("value"), []
    raise KeyError(f"未登记的 measure：{name!r}")


def interval(run: dict, name: str) -> tuple[float | None, float | None]:
    """该 measure 在各次重复上的 p5/p95（**不许只报单次结果**，`§5.1`）。"""
    values = []
    for row in run["rows"]:
        value = measure({"rows": [row]}, name)[0]
        if isinstance(value, (int, float)):
            values.append(float(value))
    return (pct(values, INTERVAL[0]), pct(values, INTERVAL[1])) if values else (None, None)