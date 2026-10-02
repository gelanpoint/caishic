"""场景驱动器（`T-SIM-06`）：把 7 个场景的每一臂跑完 → 指标 → 敏感性 → 报告。

## 它回答什么、不回答什么

* **回答**：每个场景的对照实验**结果**（含分子/分母与重复区间）、**每条结论对哪些参数敏感**、
  以及设计 §7.1 的 `R1`~`R6` 里**哪些被复现、哪些没有**（`R6` 是重点，见下）。
* **不回答**：现实。一切结论都是**关于参数的命题**；无数据的参数只给序关系。

## `R6` 反例（硬约束）

`docs/sim-design.md` §7.1 的 `R6`：**商户自费购秤 ⇒ 走秤率必须显著低于市场方出资组**；
若两组一样好，则说明"模型里的自费没有真实成本"，`结论 26` 的断言**无法被检验**。
本模块对 `R6` 的处置是**给三段证据**，而不是给一个布尔值：

1. `S5` 两臂的走秤率与重复区间（**区间是否重叠**是第一层判据）；
2. `merchant_adoption_cost_weight`（A-18，**无出处**）的 OAT 扫描 —— 它决定"要多强的心理权重才分得开"；
3. `gross_margin_rate`（A-01，**无出处**）的 OAT 扫描 —— 它决定"财务通道在什么毛利率下才起作用"。

三段都进报告，**不许只挑对自己有利的一段**。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from ..core.streams import derive_seed
from ..observe import report as report_mod
from ..observe.metrics import compute_all, survival_judgement
from ..observe.metrics_check import cross_check_problems, mutation_sensitivity_problems, verify_against_declared
from ..observe.metric_util import pct
from ..verify.sensitivity import SENSITIVITY_PROFILE, latin_hypercube, oat_scan, ordering_stability, rank_correlations, stratum_coverage
from .model_adapter import arm_flags, load_scenario, merged_overrides, read_events, run_scenario, scenario_files, scenario_problems

#: 每个场景的**主结论**（哪两臂、比哪个指标、期望方向）——全部是**序关系**，不是绝对阈值
SCENARIO_CONCLUSIONS = {
    "S0": None,
    "S1": {"a": 0, "b": 1, "metric": "M-04", "expected": "A<B",
           "statement": "向商户抽 200bp 的走秤率低于不抽佣（R1 的序关系判据；2% 非实测费率）"},
    "S2": {"a": 0, "b": 5, "metric": "M-12", "expected": "A>B",
           "statement": "抽检率与罚款同时提高后，八两秤发生率下降（R2/结论 13 的序关系判据，无绝对阈值）"},
    "S3": {"a": 0, "b": 4, "metric": "M-04", "expected": "A>B",
           "statement": "维护预算 0.3× 且无保底时，走秤率低于足额预算（R3 的序关系判据）"},
    "S4": {"a": 0, "b": 2, "metric": "M-15", "expected": "A<B",
           "statement": "有价格公示（电子屏）时信任存量高于无公示（R4 的序关系判据）"},
    "S5": {"a": 0, "b": 1, "metric": "M-04", "expected": "A>B",
           "statement": "**R6 反例**：商户自费的走秤率**必须**低于市场方出资组；若两组一样好 ⇒ 自费没有真实成本"},
    "S6": {"a": 0, "b": 1, "metric": "M-04", "expected": "A>B",
           "statement": "堵死私下交易后走秤率高于不堵（结论 13/12 的序关系判据）"},
}

#: 全局 OAT 的扫描网格（`§5.1`：参数逐项取 {p10,p50,p90}，**其余固定在 S0**）
OAT_KEYS = (
    "commission_rate_bp", "daily_inspection_rate", "merchant_fine_short_cents", "gross_margin_rate",
    "device_mtbf_days", "repair_cost_cents", "market_budget_initial_cents", "stall_fee_cents_per_month",
    "merchant_adoption_cost_weight", "short_weight_propensity_share", "short_weight_on_scale_probability",
    "info_real_share", "trust_update_eta_neg", "trust_decay_delta", "evade_feasibility",
    "daily_arrivals_per_market", "cash_payment_share", "bank_funding_cents_3y",
)

#: 敏感性阶段只扫这些参数的**范围**（有 range 的取范围两端；只有 value 的按 ±50%）
LHS_KEYS = (
    "commission_rate_bp", "daily_inspection_rate", "merchant_fine_short_cents", "gross_margin_rate",
    "device_mtbf_days", "short_weight_propensity_share", "info_real_share", "trust_update_eta_neg",
    "trust_decay_delta", "merchant_adoption_cost_weight", "stall_fee_cents_per_month",
)


#: 本场景 own-OAT 的**固定配置取自哪一臂**（缺省 = 结论的 A 臂）。
#: `S5` 必须取**自费臂**：`R6` 问的是"自费是否导致弃用"，而 `adopt_decision` 里的
#: `cost_weight × upfront` 只有在 `upfront > 0`（自费）时才有意义 —— 在出资臂上扫描该权重
#: 恒等于"扫一个乘 0 的数"，会得到一条**假的平线**（第一版就是这么扫的）。
SCENARIO_OAT_ARM = {"S5": 1}

#: 每个场景**必须额外扫描的"适用条件"键**：回答"这条结论在什么条件下才可观测"。
#: 例：`S3`（维护预算）只有在**真的坏设备**时才可观测，而 MTBF 无出处（A-11）⇒
#: 必须把 `device_mtbf_days` 一起扫出来，否则一条平线会被读成"维护预算不重要"。
SCENARIO_CONDITION_KEYS = {
    "S1": ("gross_margin_rate", "device_mtbf_days"),
    "S2": ("device_mtbf_days", "short_weight_propensity_share"),
    "S3": ("device_mtbf_days",),
    "S4": ("info_real_share",),
    "S5": ("merchant_adoption_cost_weight",),
    "S6": ("evade_feasibility", "daily_inspection_rate"),
}

#: 少数键要**细扫**（`§5.1` 的 `{p10,p50,p90}` 三点网格在这些键上分辨率不够）。
#: `R6` 的成本权重是硬约束要求"必须执行并如实报告"的反例，而它在 3 点网格上只能得到
#: "首次低于出资臂的权重点 = 200.5"这种粗结论 —— **实测翻转发生在 1 与 50 之间**，
#: 粗网格会把"窗口有多大"这件事掩盖掉（`data/` 下的探针实测：权重 1 采用 10/10、
#: 权重 50 采用 0/10）。故这一项用 7 点细扫，报告里给出**窗口**而不是一个点。
SCENARIO_OAT_POINTS = {
    "S5": {"merchant_adoption_cost_weight": (1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 200.0)},
}


def _range_of(params, key: str) -> tuple[float, float]:
    entry = params.parameters()[key]
    if "range" in entry:
        low, high = entry["range"]
        return float(low), float(high)
    value = float(entry["value"])
    return value * 0.5, value * 1.5


def _scalar(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _summary(rows: list[dict]) -> dict:
    """把多次重复压成"均值 + 区间"（**不许只报单次结果**，设计 §5.1）。"""
    out: dict[str, dict] = {}
    for metric_id in ("M-04", "M-05", "M-09", "M-12", "M-15", "M-18"):
        values = []
        for row in rows:
            metric = row["metrics"].get(metric_id, {})
            value = metric.get("value") if metric.get("ratio_is_ratio") is False else metric.get("ratio")
            if isinstance(value, (int, float)):
                values.append(float(value))
        if values:
            out[metric_id] = {
                "replications": len(values),
                "mean": round(sum(values) / len(values), 6),
                "min": round(min(values), 6),
                "max": round(max(values), 6),
                "p5": pct(values, 5),
                "p95": pct(values, 95),
                "interval_overlaps_note": "区间 = 重复样本的 p5/p95；R 越小越宽，报告必须写明 R",
            }
    return out


def run_arm(params, scenario: dict, arm: dict, *, days: int, replications: int, seed: int, out_root: Path,
            arrivals: int | None = None, consumers: int | None = None, progress=print) -> dict:
    """跑一个臂的 `replications` 次重复，返回"均值 + 区间 + 一次完整指标"。

    ⚠️ **本函数刻意不把事件明细留在返回值里**（只留 `events_path` + 当场算好的三项复算自检）。
    第一版把 `read_events(...)` 的整份列表塞进结果行，于是 24 个臂 × 每份 360 日事件
    （137 MB JSONL ⇒ 解析后 ~1.5 GB 对象）**同时驻留内存**，实测在跑到第 20 多个臂时
    `MemoryError` 崩掉、前面半小时白跑。事件明细只被"三项自检"用一次，算完就该释放。
    """
    overrides = merged_overrides(scenario, arm)
    if arrivals is not None:
        overrides["daily_arrivals_per_market"] = arrivals
    if consumers is not None:
        overrides["consumer_agent_count"] = consumers
    flags = arm_flags(scenario, arm)
    rows = []
    for rep in range(replications):
        slug = "".join(ch if ch.isalnum() else "-" for ch in arm["name"])[:40]
        out_dir = out_root / f"{scenario['id']}-{slug}-rep{rep:02d}"
        result = run_scenario(params, scenario_id=f"{scenario['id']}::{arm['name']}", overrides=overrides,
                              days=days, seed=seed + rep * 101, out_dir=out_dir, self_funded=flags["self_funded"])
        rows.append(result)
        progress(f"    · {scenario['id']}/{arm['name']} rep{rep} → 走秤率 {result['metrics']['M-04']['ratio']}")
    metrics = rows[0]["metrics"]
    events_path = Path(rows[0]["out_dir"]) / "events.jsonl"
    checks = self_check(metrics, events_path)
    return {
        "scenario": scenario["id"], "arm": arm["name"], "overrides": overrides, "flags": flags,
        "days": days, "replications": replications, "out_dir": rows[0]["out_dir"],
        "events_path": str(events_path), "checks": checks,
        "metrics": metrics, "summary": _summary(rows),
        "survival": survival_judgement(metrics),
    }


def self_check(metrics: dict, events_path: Path) -> dict:
    """三项复算自检（**算完就释放事件明细**）：两条路径对账 / 产物↔事件流 / 非退化（抗写死）。"""
    from ..observe.metrics_check import cross_check_problems, mutation_sensitivity_problems, verify_against_declared

    events = read_events(events_path)
    out = {
        "events_path": str(events_path),
        "cross": cross_check_problems(events),
        "declared": verify_against_declared(metrics, events),
        "degeneracy": mutation_sensitivity_problems(events),
    }
    del events
    return out


def _contrast_value(result: dict, metric_id: str) -> float | None:
    metric = result["metrics"].get(metric_id, {})
    if metric.get("ratio_is_ratio") is False:
        value = metric.get("value", metric.get("numerator"))
    else:
        value = metric.get("ratio")
    return _scalar(value)


def run_study(params, *, days: int = 360, replications: int = 3, seed: int = 20261002,
              out_root: Path | str = "data/sim/study", scenario_ids=None, arrivals: int | None = None,
              consumers: int | None = None, sensitivity_days: int | None = None,
              sensitivity_arrivals: int | None = None, lhs_samples: int = 64, with_sensitivity: bool = True,
              progress=print) -> dict:
    """把 7 个场景跑完并产出报告所需的一切（结果 / 敏感性 / 疑问 / 检验结论）。

    `with_sensitivity=False`（`--no-study`）**真的跳过** OAT/LHS/稳健性 —— 第一版只打印一句
    "本次仍完整跑了敏感性"，于是那个开关是个摆设（`--no-study` 与不传它的耗时逐秒相同）。
    """
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    scenarios = [load_scenario(path) for path in scenario_files()]
    if scenario_ids:
        scenarios = [s for s in scenarios if s["id"] in set(scenario_ids)]
    problems = [(s["id"], scenario_problems(s)) for s in scenarios if scenario_problems(s)]
    if problems:
        raise SystemExit(f"场景结构不合规：{problems}")

    results: list[dict] = []
    for scenario in scenarios:
        progress(f"  [{scenario['id']}] {scenario['title']}")
        for arm in scenario["arms"]:
            results.append(run_arm(params, scenario, arm, days=days, replications=replications,
                                   seed=seed, out_root=out_root, arrivals=arrivals, consumers=consumers,
                                   progress=progress))

    if with_sensitivity:
        sensitivity = run_sensitivity(params, scenarios, results, days=sensitivity_days or SENSITIVITY_PROFILE["days"],
                                      arrivals=sensitivity_arrivals, seed=seed + 90000, lhs_samples=lhs_samples,
                                      progress=progress)
    else:
        progress("  [--no-study] 跳过全局 OAT / LHS / 结论稳健性（**只跑场景臂**）；"
                 "报告里的敏感性一节将如实标为『本次未跑』")
        sensitivity = {"skipped": True, "global_oat": None, "rank_correlations": [], "by_scenario": {},
                       "top_keys": [],
                       "note": "本次以 `with_sensitivity=False` 运行：**敏感性一节没有数据**，"
                               "不是『跑了没发现敏感性』。要看敏感性请不带 --no-study 重跑。"}
    doubts, r6_lines, checks = _assemble(params, results, sensitivity)
    context = {
        "scenarios": [s["id"] for s in scenarios],
        "scenario_meta": scenarios,
        "days": days, "replications": replications, "seed": seed, "out_root": str(out_root),
        "params_path": params.source, "r6_lines": r6_lines,
    }
    baseline_metrics = next((row["metrics"] for row in results if row["scenario"] == "S0"),
                            results[0]["metrics"] if results else {})
    text = report_mod.render_report(context=context, results=results, sensitivity=sensitivity, doubts=doubts,
                                    declared_vs_recomputed=checks["declared"], degeneracy=checks["degeneracy"],
                                    cross_checks=checks["cross"])
    paths = report_mod.write_report(out_root, text, baseline_metrics,
                                    {"results": [{k: v for k, v in row.items() if k != "events"} for row in results]},
                                    sensitivity)
    return {"results": results, "sensitivity": sensitivity, "report": text, "paths": paths}


def _numeric_keys(params, keys) -> list[str]:
    """本场景的对照变量里**能按数值扫描**的那些（布尔/字符串开关不能取 p10/p50/p90）。"""
    out = []
    for key in keys:
        entry = params.parameters().get(key)
        if entry is None:
            continue
        if isinstance(entry.get("value"), bool):
            continue
        if "range" in entry or isinstance(entry.get("value"), (int, float)):
            out.append(key)
    return out


def lhs_rng(space: dict, seed: int):
    """LHS 抽样用的随机流 —— **由 `sha256` 派生，绝不用内置 `hash()`**。

    ## 这里曾经有一个真缺陷（`T-SIM-06` 收口时发现并修掉）

    原实现是 `random.Random(hash(rng_seed) % (2**32))`。内置 `hash()` 对字符串**按
    `PYTHONHASHSEED` 加盐、跨进程不稳定** —— 正是 `sim/core/streams.py` 的模块 docstring
    明令禁止的那件事（"内置 `hash()` ... 而『同 seed 逐字节可复现』是本仿真的地基判据"）。
    后果有两条，且都不可接受：

    1. **同一 `--seed` 跑两次，LHS 样本集不同** ⇒ `sensitivity.json` 里的秩相关与存活区域
       **不可复现**，报告里的敏感性结论无法被第三方重跑核对；
    2. **样本集与 `--seed` 无关**（原来只哈希了 `space`）⇒ `--seed` 这个旋钮对敏感性档完全失效。

    改用 `derive_seed`（`sim/core/streams.py`，全项目唯一的种子派生入口，避免同一个规则写两遍）
    后：① 跨进程/跨机器稳定；② 随 `--seed` 变化。

    回归守卫：`tests/sim/test_verify_sensitivity.py::test_lhs_sampling_is_process_stable`
    （在两个不同的 `PYTHONHASHSEED` 子进程里比对样本矩阵，并自带"旧实现必红"的负例）。
    """
    material = json.dumps(space, sort_keys=True)
    return random.Random(derive_seed(seed, material, "lhs"))


def run_sensitivity(params, scenarios: list[dict], results: list[dict], *, days: int, arrivals: int | None,
                    seed: int, lhs_samples: int, progress=print) -> dict:
    """全局 OAT + 分层拉丁超立方（秩相关）+ **每个场景本场景对照指标的 OAT** + 结论稳健性。

    「每个场景必须同时给出对哪些参数敏感，不许只给一条曲线」（本项目硬约束）的落地形式：
    * 全局 OAT（输出 = 走秤率 `M-04`）回答"整体上谁最要紧"；
    * **本场景 OAT**（输出 = 本场景结论用的那个指标，固定配置 = 本场景的对照臂）
      回答"这条结论对哪些参数敏感"——所有场景共用一张走秤率表会把这个问题答成同一句话；
    * LHS 秩相关回答"在参数空间里谁与输出同向"；稳健性回答"换了别的 [假设] 参数后序关系是否翻转"。
    """
    #: 子集运行（`--scenario S5,S6`）时**可能没有 S0** —— 第一版这里是 `next(...)`，
    #: 于是子集运行直接 `StopIteration`（连报告都出不来）。缺 S0 就退到第一个场景，
    #: 并在自己的输出里说明"敏感性档的固定配置取自哪个场景"。
    scenario = next((s for s in scenarios if s["id"] == "S0"), scenarios[0])
    base_overrides = dict(scenario["baseline_overrides"])
    profile = dict(base_overrides)
    if arrivals is not None:
        profile["daily_arrivals_per_market"] = arrivals

    def runner(overrides: dict, *, metric_id: str = "M-04", self_funded: bool = False) -> float:
        result = run_scenario(params, scenario_id="OAT", overrides=overrides, days=days, seed=seed,
                              out_dir=Path("data/sim/_oat"), self_funded=self_funded)
        return _contrast_value(result, metric_id)

    grid = {}
    for key in OAT_KEYS:
        low, high = _range_of(params, key)
        grid[key] = [low, (low + high) / 2.0, high]
    progress(f"  全局 OAT：{len(grid)} 个参数 × 3 点（{days} 营业日/次）")
    oat = oat_scan(runner, profile, grid)

    space = {key: _range_of(params, key) for key in LHS_KEYS}
    samples = latin_hypercube(space, lhs_samples, lhs_rng(space, seed))
    coverage = stratum_coverage(samples, space, lhs_samples)
    outputs, alive = [], []
    for index, sample in enumerate(samples):
        overrides = dict(profile)
        overrides.update(sample)
        result = run_scenario(params, scenario_id="LHS", overrides=overrides, days=days, seed=seed + index,
                              out_dir=Path("data/sim/_lhs"))
        outputs.append(_contrast_value(result, "M-04") or 0.0)
        alive.append(bool(survival_judgement(result["metrics"])["all_pass"]))
        if index and index % 16 == 0:
            progress(f"    · LHS {index}/{lhs_samples}")
    ranks = rank_correlations(samples, outputs, list(space))
    from ..verify.sensitivity import survival_region_shape

    region = survival_region_shape([{"alive": flag, "params": sample} for flag, sample in zip(alive, samples)])

    # 每个场景的**结论稳健性**：扰动"全局秩相关最强"的 3 个参数，看序关系是否翻转
    top_keys = [row["key"] for row in ranks if row["rho"] is not None][:3]
    by_scenario: dict[str, dict] = {}
    for scenario in scenarios:
        conclusion = SCENARIO_CONCLUSIONS.get(scenario["id"])
        entry = {"global_oat": oat["summary"], "rank_correlations": ranks, "stability": None,
                 "conclusion": conclusion, "own_oat": None}
        if conclusion:
            arm = scenario["arms"][SCENARIO_OAT_ARM.get(scenario["id"], conclusion["a"])]
            declared = _numeric_keys(params, scenario["varying_keys"])
            extra = list(SCENARIO_CONDITION_KEYS.get(scenario["id"], ()))
            own_keys = list(dict.fromkeys(declared + extra + ([k for k in top_keys if k not in declared] if not declared else [])))
            own_grid = {}
            fine = SCENARIO_OAT_POINTS.get(scenario["id"], {})
            for key in own_keys:
                if key in fine:
                    own_grid[key] = list(fine[key])
                else:
                    low, high = _range_of(params, key)
                    own_grid[key] = [low, (low + high) / 2.0, high]
            locked = merged_overrides(scenario, arm)
            own_flags = arm_flags(scenario, arm)
            progress(f"  {scenario['id']} 本场景 OAT：{len(own_grid)} 个参数 × 3 点"
                     f"（输出 {conclusion['metric']}，固定配置 = 臂 {arm['name']}"
                     f"{'（自费）' if own_flags['self_funded'] else ''}）")
            entry["own_oat"] = oat_scan(
                lambda overrides, _metric=conclusion["metric"], _flag=own_flags["self_funded"]:
                    runner(overrides, metric_id=_metric, self_funded=_flag),
                dict(locked), own_grid)
            entry["own_oat"]["metric"] = conclusion["metric"]
            entry["own_oat"]["arm"] = arm["name"]
            entry["own_oat"]["self_funded"] = own_flags["self_funded"]
            entry["own_oat"]["condition_keys"] = extra
            trials = []
            for key in top_keys or ["gross_margin_rate"]:
                low, high = _range_of(params, key)
                for point in (low, high):
                    arm_a = scenario["arms"][conclusion["a"]]
                    arm_b = scenario["arms"][conclusion["b"]]
                    values = []
                    for arm_item in (arm_a, arm_b):
                        overrides = merged_overrides(scenario, arm_item)
                        overrides[key] = point
                        if arrivals is not None:
                            overrides["daily_arrivals_per_market"] = arrivals
                        result = run_scenario(params, scenario_id="STAB", overrides=overrides, days=days,
                                              seed=seed + 7, out_dir=Path("data/sim/_stab"),
                                              self_funded=arm_flags(scenario, arm_item)["self_funded"])
                        values.append(_contrast_value(result, conclusion["metric"]) or 0.0)
                    trials.append((values[0], values[1]))
            entry["stability"] = ordering_stability(trials, conclusion["expected"])
            progress(f"  {scenario['id']} 结论稳健性：{entry['stability']['verdict']}"
                     f"（{len(trials)} 组，翻转 {len(entry['stability']['flips'])}）")
        by_scenario[scenario["id"]] = entry

    return {"global_oat": oat, "rank_correlations": ranks, "by_scenario": by_scenario, "top_keys": top_keys,
            #: 兼容读取（第一版的键名）：报告与旧调用方都还能拿到全局 OAT
            "oat": oat, "summary": oat["summary"],
            "lhs": {"samples": lhs_samples, "coverage_problems": coverage,
                    "rank_correlations": ranks, "survival_region": region,
                    "profile": {"days": days, "arrivals": arrivals},
                    "note": SENSITIVITY_PROFILE["note"]}}


def replication_facts(replications: int) -> dict:
    """重复次数 `R` 的**事实**（供报告措辞使用）。

    ## 为什么要把它单独提出来（`T-SIM-06` 收口时修的一个真缺陷）

    原实现把"R 的诚实提示"在**两处各写了一遍**（`R6` 第一层判定的括号里、报告第 5 节的不确定清单里），
    且两处的**触发阈值与措辞不一致**（都按 `R < 5` 判断，但正文一律写"每臂只有一次重复、
    p5/p95 退化为单点"）。于是 `R=3` 的实际运行会生成这样一句：

    > ⚠️ R=3 ⇒ 每臂只有一次重复，p5/p95 退化为单点

    —— 而产物里明明有 3 次重复、p5/p95 也明明是两个不同的数（实测 0.290005 ~ 0.485007）。
    **报告对自己产物的描述与产物不符**，正是本项目最不能接受的那类错误（"把假设写成事实"的孪生形态：
    把"证据弱"夸张成"没有证据"，会让读者据此错误地全盘丢弃区间）。

    收敛到一处后，两个调用点共用一个事实源，措辞与 `R` 必然一致。
    """
    return {
        "r": int(replications),
        "interval_is_a_single_point": int(replications) <= 1,
        "at_design_level": int(replications) >= 20,
    }


def replication_caveat(replications: int) -> str:
    """报告第 5 节用的**长**提示（与 `replication_caveat_short` 同源，不许各写一份）。"""
    facts = replication_facts(replications)
    if facts["interval_is_a_single_point"]:
        return ("**R=1 ⇒ 每臂只有一次重复，p5/p95 退化为单点，于是『区间不重叠』这条判据在 R=1 下"
                "几乎必然成立 —— 它是本报告里最弱的一类证据**，所有「显著优于 / 区间不重叠」的措辞都不可当结论；"
                "本报告的结论一律以**序关系 + 条件句扫描**为准。")
    if not facts["at_design_level"]:
        return (f"R={facts['r']} < 20 ⇒ p5/p95 由 {facts['r']} 个样本算出，区间估计有抽样误差，"
                "『区间不重叠』的强度**低于设计档**（但区间**不是**单点）；本报告的结论一律以"
                "**序关系 + 条件句扫描**为准。")
    return f"R={facts['r']} 达到设计档（≥20），区间估计可用。"


def replication_caveat_short(replications: int) -> str:
    """`R6` 第一层判定后面挂的**短**括号提示（同一事实源，只是更紧凑）。"""
    facts = replication_facts(replications)
    if facts["interval_is_a_single_point"]:
        return ("（⚠️ R=1 ⇒ 每臂只有一次重复，p5/p95 退化为单点，"
                "『区间不重叠』这一层在本报告里**不构成证据**；请以第二层的条件句扫描为准）")
    if not facts["at_design_level"]:
        return (f"（⚠️ R={facts['r']} < 设计档 20 ⇒ p5/p95 由 {facts['r']} 个样本算出，"
                "区间估计本身有抽样误差，『区间不重叠』这一层的强度**低于设计档**（但区间**不是**单点）；"
                "请以第二层的条件句扫描为准）")
    return f"（R={facts['r']} 达到设计档 ≥20）"


def _assemble(params, results: list[dict], sensitivity: dict) -> tuple[list[str], list[str], dict]:
    """把"疑问 / `R6` 三段证据 / 自检结果"整理成报告段落（**先自曝，再报喜**）。"""
    by_arm = {f"{row['scenario']}::{row['arm']}": row for row in results}
    s5 = [row for row in results if row["scenario"] == "S5"]
    r6_lines = ["### 三段证据（缺一不可）", "", "| 臂 | 走秤率(分子/分母) | 重复区间(p5~p95) | 价目表维护率 | 设备闲置(摊位-日) |",
                "| --- | --- | --- | --- | --- |"]
    for row in s5:
        m04 = row["metrics"]["M-04"]
        summary = row["summary"].get("M-04", {})
        r6_lines.append(
            f"| `{row['arm']}` | {m04['ratio']} ({m04['numerator']}/{m04['denominator']}) | "
            f"{summary.get('p5')} ~ {summary.get('p95')} | {row['metrics']['M-09']['ratio']} | "
            f"{row['metrics']['M-10']['ratio']} |")
    if len(s5) == 2:
        a, b = s5[0]["summary"].get("M-04", {}), s5[1]["summary"].get("M-04", {})
        overlap = bool(a) and bool(b) and min(a["p95"], b["p95"]) >= max(a["p5"], b["p5"])
        lower = (s5[1]["metrics"]["M-04"]["ratio"] or 0) < (s5[0]["metrics"]["M-04"]["ratio"] or 0)
        verdict = ("成立（自费组更低且区间不重叠）" if (lower and not overlap)
                   else "**不成立（区间重叠或方向相反）** —— 即模型里的『自费』在该权重下没有真实成本"
                   if not (lower and not overlap) else "成立")
        r_count = results[0]["replications"] if results else 0
        verdict += replication_caveat_short(r_count)
        r6_lines += ["", f"- **判定（第一层）**：{verdict}",
                     f"- 区间：出资 {a.get('p5')}~{a.get('p95')} vs 自费 {b.get('p5')}~{b.get('p95')}；"
                     f"重叠 = {overlap}；自费组更低 = {lower}"]
    s5_own = ((sensitivity.get("by_scenario") or {}).get("S5") or {}).get("own_oat") or {}
    own_rows = {row["key"]: row for row in (s5_own.get("summary") or [])}
    weight_row = own_rows.get("merchant_adoption_cost_weight")
    r6_lines += ["", "### 第二层：参与摩擦权重 `merchant_adoption_cost_weight`（**A-18 无出处**）"
                     " —— **在自费臂上扫描**", "",
                 "> 为什么必须在自费臂上扫：`adopt_decision` 的判据是 `cost_weight × upfront ≤ "
                 "月净收益 × 忍耐期`。市场方出资时 `upfront = 0` ⇒ 判据恒真，扫这个权重等于"
                 "**扫一个乘 0 的数**，只会得到一条假平线（第一版就是这么扫的，见本报告第 5 节）。", ""]
    if weight_row and not sensitivity.get("skipped"):
        funded_ratio = s5[0]["metrics"]["M-04"]["ratio"] if s5 else None
        output_cells = ["不可评估" if v is None else "%.4f" % v for v in weight_row["outputs"]]
        crossings = [(value, out) for value, out in zip(weight_row["values"], weight_row["outputs"])
                     if out is not None and funded_ratio is not None and out < funded_ratio]
        window = None
        if crossings:
            threshold = min(value for value, _out in crossings)
            below = [value for value in weight_row["values"] if value < threshold]
            window = (max(below) if below else None, threshold)
        r6_lines += [
            f"- 扫描点 {weight_row['values']} ⇒ 自费臂走秤率 {output_cells}，方向 `{weight_row['direction']}`",
            f"- 同配置下**出资臂**走秤率 = {funded_ratio}",
        ]
        if window:
            r6_lines.append(
                f"- ⇒ **翻转窗口 = ({window[0]}, {window[1]}]**（该区间内『自费 ⇒ 被弃用』由不成立翻成成立）。"
                f"`R6` 的结论因此只能写成**条件句**，而不是断言：① 权重 ≲ {window[0]} 时自费组与出资组"
                "**没有可观测差别** ⇒ 模型里的『自费』没有真实成本 ⇒ `结论 26` **在本模型里无法被检验**；"
                f"② 权重 > {window[1]} 时自费组被弃用（实测采用率直接掉到 0）⇒ `结论 26` 成立。"
                "⚠️ 该权重的出处是 A-18（**假设，无数据**）⇒ **不许**声称现实中的商户落在哪一侧；"
                "本报告只给出『要多强的参与摩擦才分得开』这一个**关于参数**的命题。")
        else:
            r6_lines.append(
                "- ⇒ 在扫描范围内**没有出现**低于出资臂的权重点 ⇒ 模型里的『自费』在该区间内没有真实成本，"
                "`结论 26` 无法被本模型检验（这是结论，不是缺陷掩盖）。")
        adopt_counts = [row for row in (weight_row.get("outputs") or [])]
        if any(value == 0.0 for value in adopt_counts):
            r6_lines.append(
                "- 机制说明（**必须一起读，否则会读成「自费只是稍微差一点」**）：走秤率掉到 0 是因为"
                "**采用是吸收态** —— 不采用 ⇒ 不发价目表 ⇒ 一笔走秤都没有。"
                "这个「全有或全无」的形态是本模型的简化，属已知边界。")
    else:
        r6_lines.append("- 本次未跑敏感性（`--no-study`）或本场景无 own-OAT 结果 ⇒ **第二层缺证据**，"
                        "不得据第一层下结论。")
    r6_lines += ["", "### 第三层：财务通道（`merchant_device_share_cents` vs 自费口径，**两条通道分开看**）", ""]
    if len(s5) == 2:
        m19 = s5[1]["metrics"].get("M-19", {})
        share = s5[1].get("flags", {}).get("self_funded")
        r6_lines += [
            f"- 自费臂的商户月净收入分布（`M-19`，取自自费臂）："
            f"p10={m19.get('detail', {}).get('p10')} · p50={m19.get('detail', {}).get('p50')} · "
            f"p90={m19.get('detail', {}).get('p90')}（单位：分/摊位-月）",
            "- 一次性支出（3750 元/台）与月净收入相差**两个量级以内**，故在权重 ≈ 1 时"
            "『一年忍耐期收益 ≫ 一次性支出』恒成立 ⇒ 意愿通道不触发；"
            "财务通道（`device_share_cents` 改为自费摊销）也**不足以**把商户推离 `comply`。",
            f"- 证据：自费臂的三份额 `M-06` = {s5[1]['metrics'].get('M-06', {}).get('ratio')}，"
            f"出资臂 = {s5[0]['metrics'].get('M-06', {}).get('ratio')}（若完全相同 ⇒ 两条通道都没起作用）。",
        ]
    else:
        r6_lines.append("- 缺 S5 两臂结果 ⇒ 无法比较。")
    r6_lines += ["", "> **结论口径（`R6` 三态，不许只有二态）**：",
                 "> * **成立**：自费组显著更差（第二层扫描出现低于出资臂的权重点，且该权重可被论证）；",
                 "> * **不成立**：自费组活得一样好 ⇒ 模型里的『自费』没有真实成本 ⇒ `结论 26` **无法被检验**"
                 "（此时必须如实说「这条反例没通过」，不许把它读成「设计更好」）；",
                 "> * **不稳健**：结论随无出处参数（成本权重 / 毛利率 / 单摊流水量级）翻转 ⇒ 只能作为"
                 "**关于参数的命题**呈现，不许写成「商户自费必然被弃用」。",
                 "> 本报告的实际落点见上两层的数字 —— **不是**预设的「自费必然被弃用」。", ""]
    sample = next((row for row in results if row["scenario"] == "S0"), results[-1] if results else {})
    checks = sample.get("checks") or {
        "arm": f"{sample.get('scenario', '?')}::{sample.get('arm', '?')}",
        "cross": ["（没有基线臂的复算自检结果）"],
        "declared": ["（没有基线臂的复算自检结果）"],
        "degeneracy": {"problems": ["（没有基线臂的复算自检结果）"], "checked": [], "degenerate": []},
    }
    profile = (sensitivity.get("lhs") or {}).get("profile") or {}
    sensitivity_note = (
        f"全局 OAT 与 LHS 在 **{profile.get('days')} 营业日**的缩减档上跑"
        f"（到达强度 {profile.get('arrivals')}），不是 360 日全量档 —— "
        "结论是序关系，量级不作为判据。" if profile else
        "**本次以 `--no-study` 运行：敏感性（OAT / LHS / 秩相关 / 稳健性）根本没有跑** —— "
        "报告里的敏感性一节是空的，那不是『跑了没发现敏感性』。"
    )
    doubts = [
        f"重复次数 R={results[0]['replications'] if results else 0}（设计 §5.1 要求 R=20）。"
        + replication_caveat(results[0]["replications"] if results else 0),
        "**本轮自查出并修掉的三个真缺陷（如实登记，因为它们各自都曾给出过「看起来正常」的结论）**："
        "① **存活判据③ 用错了指标**（拿 `M-13` 扫码率当 `M-04` 走秤率，且没有第 300–360 日窗口）"
        "⇒ 判据③ 在走秤率其实达标时假红；② **`S6` 的 `evade_feasibility` 根本没被模型读过**"
        "⇒ `S6-①堵死` 与 `S6-②不堵` 结果逐位相同，「堵死」这条对照实验什么也没测；"
        "③ **`R6` 的成本权重扫描跑在出资臂上**（`upfront=0` ⇒ 判据恒真）⇒ 扫出的是假平线。"
        "三者都已修，并各带回归守卫（`tests/sim/test_model_adapter.py` / `test_scenarios.py`）。",
        "`M-22`（model↔live 偏差）在 model 模式下**不可用**（需要 live 侧事件，属 `T-SIM-07/08`）；"
        "本报告没有把它当作 0 处理。",
        sensitivity_note,
        "监管强度（T5 季频）在本实现里按**月频**抽检：商户效用里的 `p_check` 是**名义**抽检率，"
        "监管的实际发现数来自抽样记录 —— 两者口径不同，报告同时给出、不混算。",
        "设计 §4.3 的 `regulator.update_intensity()`（季末动态调整强度）**未实现**：强度由场景固定，"
        "故本报告不能回答『动态监管是否会收敛』。",
        "信任半衰期 `M-16` 的解析值与实测值**单位已换算**，但实测轨迹按**月**汇总 ⇒ 拟合分辨率 1 个月，"
        "偏差里含离散化误差；基线配置下信任单调下降（`fitted_months` 为空）时该指标只报解析值。",
        "`M-11`（W_q）在 KPI=装机量的臂里为 `0/0`（维护预算为 0 ⇒ 一台都没修），"
        "该指标在这些臂上**退化**，不能用扰动法检验。",
        "**采用/退出都是「全有或全无」的**（`adopt_decision` 是布尔判据、退出是吸收态）："
        "自费臂一旦越过成本权重阈值，采用率直接 0/10、走秤率归零。现实里更可能是渐进流失 —— "
        "故本模型关于自费/退出的结论只能读作**方向**，不能读作**程度**；"
        "`R6` 因此必须以「翻转窗口 + 条件句」呈现，不能以「自费组差 X 个百分点」呈现。",
        "`bank_funding_cents_3y`（结论 19：200 余万元/三年期，**未交代覆盖几个市场**，A-17）"
        "在市场方现金流里占大头 ⇒ `M-17/M-18` 的绝对量级**不可信**，只作区域陈述。",
        "`price_sign_cents_per_stall`（物理价签单价）是把『低一个量级』落成的**假设**（110 元/摊），"
        "结论 1 只有做法没有成本。",
        "摊位费 `stall_fee_cents_per_month=200000`（2000 元/摊·月）是**假设**（无出处），"
        "它同时是市场方收入项与 S1-③ 的对照变量。",
    ]
    return doubts, r6_lines, checks
