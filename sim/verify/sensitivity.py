"""敏感性分析（`T-SIM-06`；`docs/sim-design.md` §5.1/§7.2）——**只用标准库**。

## 三件事，各自防一种假结论

1. **OAT（一次一参数）**：把某参数逐点取 `{p10, p50, p90}`，其余固定在基线。
   它回答"**这条结论对哪些参数敏感**"（`§7.2` 第 3 条：不许只给一条曲线）。
2. **分层拉丁超立方（LHS）**：`N` 组样本，每维每层**恰一个**样本 —— 覆盖度可机械校验
   （`stratum_coverage`），否则"我抽了 512 组"可能只是一堆挤在中间的样本。
3. **自写 Spearman 秩相关**（约 30 行，不引 `numpy`/`scipy`）：报告"哪个参数与输出的秩相关最强"。
   必须处理**并列秩**（取平均秩），否则大量取值为常数的参数会算出假相关。

## 稳健性判据（写死，`§7.2` 第 2 条）

若某条结论在任一 `[假设]` 参数的合理区间内**翻转**，该结论标记为
**「不稳健，不得作为论证依据」**，并在报告里与稳健结论**分区展示**。
本模块只提供机制（`ordering_stability`），判定与展示在 `report.py`。
"""

from __future__ import annotations

import math
import random

#: 敏感性配置档（**必须在报告里写明**：结论是对着这个档说的，不是对着 360 日全量说的）
SENSITIVITY_PROFILE = {
    "days": 60,
    "note": "敏感性档：60 营业日（结论是**序关系**，不是绝对量级；全量档 360 日在报告里另给）",
}


# ---------------------------------------------------------------------------
# 自写 Spearman（含并列秩）
# ---------------------------------------------------------------------------
def average_ranks(values: list[float]) -> list[float]:
    """平均秩（并列取均值）。**纯函数**：并列处理错会让秩相关系统性偏高。"""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        rank = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = rank
        position = end + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman 秩相关；任一侧完全并列（无秩变化）时返回 `None`（**不是 0，也不是 1**）。

    返回 `None` 而不是 0 是刻意的：**"没有秩变化"与"没有相关性"是两件事**，
    把它们混起来会得出"该参数不敏感"的假结论 —— 实际是"这组样本没扫到它的变化"。
    """
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    rx, ry = average_ranks(xs), average_ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    numerator = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0.0 or dy == 0.0:
        return None
    return numerator / (dx * dy)


def rank_correlations(samples: list[dict], outputs: list[float], keys: list[str]) -> list[dict]:
    """逐参数给出秩相关（|rho| 降序；`None` 表示该参数在这组样本里没有秩变化）。"""
    rows = []
    for key in keys:
        values = [float(sample[key]) for sample in samples]
        rho = spearman(values, outputs)
        rows.append({"key": key, "rho": None if rho is None else round(rho, 6),
                     "samples": len(samples), "note": None if rho is not None else "该参数在本组样本里无秩变化（推广≠不敏感）"})
    rows.sort(key=lambda row: (row["rho"] is not None, abs(row["rho"] or 0.0)), reverse=True)
    return rows


# ---------------------------------------------------------------------------
# 分层拉丁超立方 + 覆盖度检验
# ---------------------------------------------------------------------------
def latin_hypercube(space: dict[str, tuple[float, float]], samples: int, rng: random.Random) -> list[dict]:
    """分层拉丁超立方：每维把区间等分 `samples` 层，每层内随机取一点，再**独立打乱**各维。"""
    if samples < 1:
        raise ValueError(f"样本数必须 ≥1：{samples}")
    columns: dict[str, list[float]] = {}
    for key, (low, high) in space.items():
        if high < low:
            raise ValueError(f"参数 {key} 的区间上下界反了：{low} > {high}")
        column = []
        for index in range(samples):
            left = index / samples
            right = (index + 1) / samples
            column.append(low + (high - low) * rng.uniform(left, right))
        rng.shuffle(column)  # 各维独立打乱 ⇒ 组合不相关，但仍保持每维分层
        columns[key] = column
    return [{key: columns[key][index] for key in space} for index in range(samples)]


def stratum_coverage(samples: list[dict], space: dict[str, tuple[float, float]], strata: int) -> list[str]:
    """**覆盖度检验（分层不退化）**：每维每层恰一个样本；否则报出是哪一维哪一层。"""
    problems: list[str] = []
    for key, (low, high) in space.items():
        if high <= low:
            problems.append(f"参数 {key} 的区间退化（{low} == {high}），无法分层")
            continue
        counts = [0] * strata
        for sample in samples:
            index = int((float(sample[key]) - low) / (high - low) * strata)
            index = min(strata - 1, max(0, index))
            counts[index] += 1
        bad = [index for index, count in enumerate(counts) if count != 1]
        if bad:
            problems.append(f"参数 {key} 分层退化：层 {bad} 的样本数 ≠ 1（{counts}）")
    return problems


def coverage_of(samples: list[dict], space, strata: int | None = None) -> list[str]:
    return stratum_coverage(samples, space, strata or len(samples))


# ---------------------------------------------------------------------------
# OAT（一次一参数）+ 稳健性
# ---------------------------------------------------------------------------
def oat_points(low: float, high: float, center: float | None = None) -> list[float]:
    """`{p10, p50, p90}` 的落地形式：`[下界, 中点, 上界]`（中点缺省取区间中点）。"""
    middle = center if center is not None else (low + high) / 2.0
    return [low, middle, high]


def oat_scan(runner, base_overrides: dict, grid: dict[str, list[float]]) -> dict:
    """跑 OAT：`grid` = `{参数: [取值...]}`，每个取值单独跑一次（其余固定在 `base_overrides`）。

    `runner(overrides) -> float` 由调用方提供（例如"跑一次场景并返回走秤率"）——
    本模块**不碰仿真内部**，故 OAT 对场景/指标的选择是解耦的、可单测的。
    """
    rows = []
    for key, values in grid.items():
        for value in values:
            overrides = dict(base_overrides)
            overrides[key] = value
            rows.append({"key": key, "value": value, "output": runner(overrides)})
    return {"rows": rows, "summary": oat_summary(rows)}


def oat_summary(rows: list[dict]) -> list[dict]:
    """把 OAT 明细压成"每个参数：输出区间 + 方向 + 是否单调"（**序关系**，不给绝对阈值）。

    ⚠️ 扫描点算不出值时（`None`）**不许当场崩，也不许当 0**：它多半意味着"该指标在这个档上退化"
    （例：不足 30 营业日时没有任何 `month_closed` ⇒ `M-15` 的分母为 0）。这种情况记为
    `unavailable` 并如实报出来 —— 当 0 会把"没测到"读成"测到了 0"。
    """
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["key"], []).append(row)
    summary = []
    for key, group in grouped.items():
        ordered = sorted(group, key=lambda row: row["value"])
        outputs = [row["output"] for row in ordered]
        if any(value is None for value in outputs):
            summary.append({
                "key": key, "values": [row["value"] for row in ordered], "outputs": outputs,
                "span": None, "direction": "unavailable（有扫描点算不出值：该指标在这个档上退化）",
                "monotone": False, "unavailable_points": sum(1 for value in outputs if value is None),
            })
            continue
        span = max(outputs) - min(outputs)
        if span == 0.0:
            direction, monotone = "flat（这组扫描点上完全不改变输出）", False
        else:
            increasing = all(b >= a for a, b in zip(outputs, outputs[1:]))
            decreasing = all(b <= a for a, b in zip(outputs, outputs[1:]))
            direction = "increasing" if increasing else "decreasing" if decreasing else "non_monotone"
            monotone = bool(increasing or decreasing)
        summary.append(
            {
                "key": key,
                "values": [row["value"] for row in ordered],
                "outputs": outputs,
                "span": span,
                "direction": direction,
                "monotone": monotone,
            }
        )
    summary.sort(key=lambda row: abs(row["span"] or 0.0), reverse=True)
    return summary


def ordering_stability(pair_outputs: list[tuple[float, float]], expected: str) -> dict:
    """**结论稳健性**：给定若干对照实验的 `(组A, 组B)` 输出与期望序关系，逐组判是否翻转。

    `expected` ∈ `{"A<B", "A>B"}`。返回 `{verdict, flips}` —— 只要有一组翻转，
    整条结论就是 **not_robust**（`§7.2`：不稳健的结论不得作为论证依据）。
    """
    if expected not in ("A<B", "A>B"):
        raise ValueError(f"未登记的期望序关系：{expected!r}")
    flips = []
    for left, right in pair_outputs:
        holds = (left < right) if expected == "A<B" else (left > right)
        if not holds:
            flips.append({"A": left, "B": right})
    return {"verdict": "robust" if not flips else "not_robust", "expected": expected,
            "trials": len(pair_outputs), "flips": flips}


def survival_region_shape(rows: list[dict]) -> dict:
    """**存活区域形状**（`§7.2` 第 3 条）：给出存活样本占比 + 最敏感参数，不只说"存在能活的组合"。

    `rows` = `[{"alive": bool, "params": {...}}]`。存活占比极低时**必须如实报告**"对参数高度敏感"。
    """
    if not rows:
        return {"samples": 0, "alive_ratio": None, "note": "无样本"}
    alive = [row for row in rows if row["alive"]]
    keys = sorted(rows[0]["params"])
    sensitivities = []
    if 0 < len(alive) < len(rows):
        binary = [1.0 if row["alive"] else 0.0 for row in rows]
        for key in keys:
            values = [float(row["params"][key]) for row in rows]
            rho = spearman(values, binary)
            sensitivities.append({"key": key, "rho": None if rho is None else round(rho, 6)})
        sensitivities.sort(key=lambda row: abs(row["rho"] or 0.0), reverse=True)
    return {
        "samples": len(rows),
        "alive": len(alive),
        "alive_ratio": round(len(alive) / len(rows), 6),
        "most_sensitive": sensitivities[:5],
        "note": "存活判据的四个阈值全是假设（§6.9）⇒ 这里给的是**参数区域**，不是现实承诺；"
                "占比极低时必须如实说『该设计对参数高度敏感』",
    }
