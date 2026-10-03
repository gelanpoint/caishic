"""档 2 · **匹配矩**（pattern-oriented modeling, POM；`T-SIM-08`；`§7.5`）。

## 这是什么，不是什么

把 `R1`~`R6` 当作 **6 个定性矩**（"2% 组必须比 0% 组差"、"自费组必须比出资组差"、
"空壳组的信任不得恢复"…），在 `[假设]` 参数空间上采样，找**同时满足全部 6 个矩**的区域。

⚠️ **点名它是领域方法**：pattern-oriented modeling 是 ABM 领域的标准做法，
**不假称它来自本项目调研**。本项目自己的调研（《报告》结论 1~26）只提供矩的**定性来源**，
不提供矩的**数值门限**。

## 必须一起读的三条诚实声明

1. **矩只有 6 个，待定参数有 20 项** ⇒「过度拟合 6 个定性矩」是**真实风险**
   （`§7.5` 原话）。本模块因此**不**报"找到一个完美参数点"，只报**区域占比** +
   **结论对哪些参数最敏感**。
2. **矩 `M5`（存活）内部含 4 个假设阈值**（0 / 70% / 60% / 0.5，`§6.9` 明说无一有调研出处）。
   本模块**照用**，但把它标成 `assumed`；矩命中与否因此是**关于参数**的命题，不是现实承诺。
3. **本次是网格 + 拒绝采样，不是全域穷举**。未采到的区域一律报"未评估"，
   **不许**写成"该区域不存在"。
"""

from __future__ import annotations

import random
from pathlib import Path

from .sensitivity import latin_hypercube, rank_correlations, stratum_coverage

#: 参与匹配矩的参数空间。**刻意只列决定矩的参数**，不列全部 20 项假设参数 ——
#: 把与矩无关的参数也拉进来只会稀释采样（同样点数下每个维度更稀），不会让结论更可信。
MOMENT_SPACE = {
    "gross_margin_rate": (0.15, 0.35),
    "merchant_exit_reference_point": (0.0, 4_000_000.0),
    "merchant_adoption_cost_weight": (1.0, 400.0),
    "device_mtbf_days": (10.0, 1095.0),
    "trust_update_eta_neg": (0.4, 1.0),
    "consumer_scan_floor": (0.0, 0.5),
}

#: 矩的声明。`kind` 只用于报告展示：`ordering` = 序关系；`assumed` = 内含假设阈值。
MOMENTS = (
    {"id": "M1", "from": "R1", "kind": "ordering", "statement": "抽 200bp 组的走秤率低于不抽佣组",
     "source": "结论 9（2‰ 已被督查并全额退还 ⇒ 2% 必然更严重，单调性）+ 结论 10（商户搬离）"},
    {"id": "M2", "from": "R2", "kind": "ordering", "statement": "免维护 + 不抽佣组严格优于 R1 组（走秤率更高）",
     "source": "结论 14（衡阳/杭州：免费 + 统一检定维修 ⇒ 愿意用）+ D-02"},
    {"id": "M3", "from": "R3", "kind": "ordering", "statement": "维护预算 0.3× 组的修复等待高于足额组",
     "source": "结论 6（维修不及时 → 落灰）"},
    {"id": "M4", "from": "R4", "kind": "ordering", "statement": "空壳扫码页组的信任低于真实来源组",
     "source": "结论 16（扫过空码之后再也不信）"},
    {"id": "M5", "from": "R5", "kind": "assumed", "statement": "温州口径存活 12 个月（`§6.9` 四条全过）",
     "source": "结论 12（2001 年批复、运行 20 余年）；⚠️ 四个阈值全是假设"},
    {"id": "M6", "from": "R6", "kind": "ordering", "statement": "商户自费组的走秤率低于市场方出资组",
     "source": "结论 26（商户自费 3750~4600 元/台 ⇒ 基本等于注定被弃用）"},
)

#: 匹配用的运行档。**比 `L1` 短**：匹配矩问的是"区域在哪"，不是"12 个月的精确时序"；
#: 360 日 × 每点 13 臂 × 数十个点在本机跑不完 —— 跑不完就少跑几个点并**如实写明**，
#: 不许拿"单点跑通"冒充"区域已找到"。
CALIBRATION_PROFILE = {
    "id": "K",
    "label": "K · 90 营业日 / 200 到达·日 / 40 消费者（设备压力档：MTBF=10 天、修复均值=6 天）",
    "days": 90,
    "arrivals": 200,
    "consumers": 40,
    "param_overrides": {"device_mtbf_days": 10, "repair_mean_days": 6},
    "why": "90 日 = 3 个完整仿真月（跨月的判据才有载体）；MTBF=10 天让 `M3` 有载体"
           "（基线档 540 天下 60~90 日期望故障 <2 次，V-01 实测 0 次 ⇒ 机制不可观测）。"
           "这两个取值**超出 `params.json` 登记区间**，且已明确标注为压力档。",
}


def _ratio(metrics: dict, metric_id: str):
    row = metrics.get(metric_id) or {}
    return row.get("ratio")


def _run(params, scenario_id, arm_index, extra, *, seed, out_root, progress=print) -> dict:
    """跑一个臂（档位规模固定在 `CALIBRATION_PROFILE`），返回该臂的判定所需量。"""
    from .backtest import run_arm
    from .r_criteria import L1_PROFILE

    prof = dict(L1_PROFILE)
    prof.update({k: v for k, v in CALIBRATION_PROFILE.items() if k in ("days", "arrivals", "consumers",
                                                                     "param_overrides")})
    prof["id"] = CALIBRATION_PROFILE["id"]
    run = run_arm(params, scenario_id=scenario_id, arm_index=arm_index, label=f"{scenario_id}#{arm_index}",
                  prof=prof, extra=extra, seed=seed, out_root=Path(out_root), progress=lambda _m: None)
    metrics = run["rows"][0]["metrics"]
    from ..observe.metrics import survival_judgement

    return {"M-04": _ratio(metrics, "M-04"), "M-01": _ratio(metrics, "M-01"), "M-03": _ratio(metrics, "M-03"),
            "M-15": _ratio(metrics, "M-15"), "M-11": _ratio(metrics, "M-11"), "M-10": _ratio(metrics, "M-10"),
            "survival": survival_judgement(metrics)["all_pass"],
            "trust_series": (metrics["M-15"].get("detail") or {}).get("by_period", [])}


def evaluate_point(params, sample: dict, *, seed: int, out_root: Path | str, progress=print) -> dict:
    """跑一个参数点，判定 6 个矩。返回 `{矩 id: bool, 臂的读数}`。"""
    extra = dict(sample)
    out_root = Path(out_root)
    s1_a = _run(params, "S1", 0, extra, seed=seed, out_root=out_root / "S1-0")   # 抽 200bp
    s1_b = _run(params, "S1", 1, extra, seed=seed, out_root=out_root / "S1-1")   # 不抽佣
    s3_a = _run(params, "S3", 0, extra, seed=seed, out_root=out_root / "S3-0")   # 预算足额
    s3_b = _run(params, "S3", 4, extra, seed=seed, out_root=out_root / "S3-4")   # 预算 0.3×
    s4_a = _run(params, "S4", 0, extra, seed=seed, out_root=out_root / "S4-0")   # 空壳
    s4_b = _run(params, "S4", 2, extra, seed=seed, out_root=out_root / "S4-2")   # 真实来源
    s5_a = _run(params, "S5", 0, extra, seed=seed, out_root=out_root / "S5-0")   # 出资
    s5_b = _run(params, "S5", 1, extra, seed=seed, out_root=out_root / "S5-1")   # 自费
    s1_c = _run(params, "S1", 2, extra, seed=seed, out_root=out_root / "S1-2")   # 温州式
    moments = {
        "M1": _lt(s1_a["M-04"], s1_b["M-04"]),
        "M2": _gt(s1_b["M-04"], s1_a["M-04"]),
        "M3": _gt(s3_b["M-11"], s3_a["M-11"]),
        "M4": _lt(s4_a["M-15"], s4_b["M-15"]),
        "M5": bool(s1_c["survival"]),
        "M6": _lt(s5_b["M-04"], s5_a["M-04"]),
    }
    progress(f"    · 点 {sample} ⇒ {sum(moments.values())}/6 个矩命中")
    return {"moments": moments, "hits": sum(moments.values()),
            "arms": {"S1-①抽200bp": s1_a, "S1-②不抽佣": s1_b, "S3-足额": s3_a, "S3-0.3×": s3_b,
                     "S4-①空壳": s4_a, "S4-③真实来源": s4_b, "S5-出资": s5_a, "S5-自费": s5_b,
                     "S1-③温州式": s1_c}}


def _lt(left, right) -> bool:
    return left is not None and right is not None and left < right


def _gt(left, right) -> bool:
    return left is not None and right is not None and left > right


def sample_space(samples: int, seed: int):
    """LHS 抽样（**不引入 numpy**；抽样器与 `sensitivity.py` 同一个，避免同一规则写两遍）。"""
    rng = random.Random(seed)
    return latin_hypercube(dict(MOMENT_SPACE), samples, rng)


def run_calibration(params, *, out_dir: Path | str = "data/sim/calibration", samples: int = 32,
                    seed: int = 20261002, out_root: Path | str = "data/sim/calibration/_points",
                    progress=print) -> dict:
    """在参数空间采样 → 逐点判 6 个矩 → 报**区域占比**与**最敏感参数**（不报"完美参数点"）。"""
    from ..observe.verify_report import render_calibration, write_calibration

    points = sample_space(samples, seed)
    coverage = stratum_coverage(points, dict(MOMENT_SPACE), samples)
    rows = []
    for index, sample in enumerate(points):
        row = evaluate_point(params, sample, seed=seed, out_root=Path(out_root) / f"p{index:03d}", progress=progress)
        row["params"] = sample
        row["accepted"] = row["hits"] == len(MOMENTS)
        rows.append(row)
    accepted = [row for row in rows if row["accepted"]]
    outputs = [float(row["hits"]) for row in rows]
    ranks = rank_correlations(points, outputs, list(MOMENT_SPACE))
    per_moment = {moment["id"]: sum(1 for row in rows if row["moments"][moment["id"]])
                  for moment in MOMENTS}
    payload = {
        "schema_version": 1, "params_path": params.source, "profile": CALIBRATION_PROFILE,
        "space": {k: list(v) for k, v in MOMENT_SPACE.items()},
        "samples": len(rows), "accepted": len(accepted),
        "accepted_ratio": round(len(accepted) / len(rows), 6) if rows else None,
        "stratum_coverage_problems": coverage,
        "per_moment_hits": per_moment,
        "mean_hits": round(sum(outputs) / len(outputs), 4) if outputs else None,
        "rank_correlations": ranks,
        "moments": [dict(moment) for moment in MOMENTS],
        "accepted_points": [row["params"] for row in accepted],
        "rows": rows,
        "method": "pattern-oriented modeling（ABM 领域方法，**不假称来自本项目调研**）："
                  "把 R1~R6 当作 6 个定性矩，在 [假设] 参数空间做分层 LHS 采样 + 拒绝采样",
        "caveats": [
            "矩只有 6 个、待定参数 20 项 ⇒『过度拟合 6 个定性矩』是真实风险（§7.5 原话）；"
            "因此本结果只报**区域占比**与**最敏感参数**，不报『完美参数点』。",
            "矩 M5 内含 `§6.9` 的四个假设阈值（0 / 70% / 60% / 0.5），无一有调研出处；"
            "命中与否是**关于参数**的命题，不是现实承诺。",
            "这是**网格 + 拒绝采样**，不是全域穷举；未采到的区域一律是『未评估』，不是『不存在』。",
            "运行档为 90 营业日的**压缩档**（不是 360 日全量档）；MTBF=10 天 / 修复均值=6 天"
            "**超出 `params.json` 登记区间**，是压力档。",
        ],
    }
    paths = write_calibration(out_dir, payload, render_calibration(payload))
    return {"accepted": len(accepted), "samples": len(rows), "payload": payload, "paths": paths}