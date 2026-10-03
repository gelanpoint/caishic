"""敏感性**编排**（`T-SIM-06` 建的、`T-SIM-08` 按语义搬出）：全局 OAT + 分层 LHS 秩相关
+ 每个场景本场景对照指标的 OAT + 结论稳健性。

## 为什么它从 `sim/bridge/study.py` 搬出来（`quality-gates.md` §1.2 的 400 行门禁）

`study.py` 原本同时负责两件事：**场景驱动器**（跑哪些臂、产哪些报告）与**敏感性编排**
（扫哪些参数、用什么网格）。两者语义正交，且后者本质上是**验证**活动 ——
它住在 `sim/verify/` 才与其余验证模块（`sensitivity.py` 的统计原语、`backtest.py` 的判据）
放在一起。搬动是**纯移动**：函数体逐字保留，注释里的来历一并带走。

`study.py` 保留同名转出（`from .sensitivity_runner import ...`），既有调用方一行不改 ——
这正是「拆文件而不改行为」该有的样子。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from ..bridge.model_adapter import arm_flags, merged_overrides, run_scenario
from ..core.streams import derive_seed
from ..observe.metrics import survival_judgement
from .sensitivity import (SENSITIVITY_PROFILE, latin_hypercube, oat_scan, ordering_stability,
                          rank_correlations, stratum_coverage, survival_region_shape)


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
def _contrast_value(result: dict, metric_id: str) -> float | None:
    metric = result["metrics"].get(metric_id, {})
    if metric.get("ratio_is_ratio") is False:
        value = metric.get("value", metric.get("numerator"))
    else:
        value = metric.get("ratio")
    return _scalar(value)

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
