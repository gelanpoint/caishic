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
from ..observe import verify_report as _report
from ..observe.metrics import compute_all, survival_judgement
from ..observe.metrics_check import cross_check_problems, mutation_sensitivity_problems, verify_against_declared
from ..observe.metric_util import pct
from ..verify.sensitivity import SENSITIVITY_PROFILE
from ..verify.sensitivity_runner import (  # `T-SIM-08` 按语义搬出；此处转出，既有调用方一行不改
    LHS_KEYS,
    OAT_KEYS,
    SCENARIO_CONDITION_KEYS,
    SCENARIO_CONCLUSIONS,
    SCENARIO_OAT_ARM,
    SCENARIO_OAT_POINTS,
    _contrast_value,
    _numeric_keys,
    _range_of,
    _scalar,
    lhs_rng,
    run_sensitivity,
)
from .model_adapter import arm_flags, load_scenario, merged_overrides, read_events, run_scenario, scenario_files, scenario_problems





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




def replication_facts(replications: int) -> dict:
    """重复次数 R 的**事实**（唯一事实源）。

    实现已迁到 `sim/observe/verify_report.py`（`T-SIM-08` 随「R6 三段证据」一并按语义迁出，
    见该文件 `replication_facts` 的 docstring：这段提示曾经被写两遍、触发阈值还不一致，
    导致 R=3 时报告说「p5/p95 退化为单点」而产物里明明是两个不同的数）。本处只做转出，
    **不保留第二份实现** —— 同一个事实写两遍就是下次漂移的种子。
    """
    return _report.replication_facts(replications)


def replication_caveat(replications: int) -> str:
    """报告第 5 节用的**长**提示（同源，不许各写一份）。"""
    return _report.replication_caveat(replications)


def replication_caveat_short(replications: int) -> str:
    """`R6` 第一层判定后面挂的**短**括号提示（同一事实源，只是更紧凑）。"""
    return _report.replication_caveat_short(replications)


def _assemble(params, results: list[dict], sensitivity: dict) -> tuple[list[str], list[str], dict]:
    """把"疑问 / `R6` 三段证据 / 自检结果"整理成报告段落（**先自曝，再报喜**）。"""
    by_arm = {f"{row['scenario']}::{row['arm']}": row for row in results}
    s5 = [row for row in results if row["scenario"] == "S5"]
    #: 「R6 三段证据」与「R 的诚实提示」的**装配**已按语义迁到 `sim/observe/verify_report.py`
    #: （`T-SIM-08`）。这里只取结果，不重写措辞 —— 同一个事实写两遍就是下次漂移的种子，
    #: 而 `R6` 的措辞本身就是结论的一部分。
    r6_lines = _report.r6_three_part_lines(results, sensitivity)
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
