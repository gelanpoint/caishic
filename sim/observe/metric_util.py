"""指标复算的**共用小工具**（纯函数）。

拆出来的唯一原因是 `quality-gates.md` §1.2 的"单文件 ≤ 400 行"（`Q-16`：按语义拆分、不放宽阈值）。
本模块**不定义任何指标**：指标在 `metrics.py`（交易/设备类）与 `metrics_trust_cash.py`（信任/现金流/一致性类）。
"""

from __future__ import annotations

import math

from .metric_specs import MetricSpec


# ---------------------------------------------------------------------------
# 通用小工具（全部纯函数）
# ---------------------------------------------------------------------------
def of(events, kind: str) -> list[dict]:
    return [e for e in events if e.get("kind") == kind]


def effective_params(events) -> dict:
    for event in events:
        if event.get("kind") == "run_started":
            return dict(event.get("effective_params") or {})
    return {}


def ratio_row(numerator: float, denominator: float, spec: MetricSpec, *, detail: dict | None = None) -> dict:
    out = {
        "id": spec.id,
        "name": spec.name,
        "numerator": numerator,
        "denominator": denominator,
        "numerator_label": spec.numerator,
        "denominator_label": spec.denominator,
        "ratio": (numerator / denominator) if denominator else None,
        "bp": round(numerator / denominator * 10000, 4) if denominator else None,
        "ratio_is_ratio": spec.ratio,
        "condition": spec.condition,
        "sources": list(spec.sources),
        "available": True,
    }
    if detail:
        out["detail"] = detail
    return out


def unavailable(spec: MetricSpec, reason: str) -> dict:
    """不可用也要**说清它的分子/分母本该是什么** —— "没有数据"不等于"没有口径"。

    （第一版这里没有 `numerator_label`/`denominator_label`，于是报告里 `M-22` 一行成了
    "— / —"，读者无法判断它缺的是哪一半数据。）
    """
    return {
        "id": spec.id, "name": spec.name, "available": False, "reason": reason,
        "ratio_is_ratio": spec.ratio, "condition": spec.condition, "sources": list(spec.sources),
        "numerator": None, "denominator": None, "ratio": None, "bp": None, "value": None,
        "numerator_label": spec.numerator, "denominator_label": spec.denominator,
    }


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    position = (len(ordered) - 1) * p / 100.0
    low = int(math.floor(position))
    high = min(len(ordered) - 1, low + 1)
    weight = position - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 6)


def per_day(counter: dict) -> dict:
    return {day: counter[day] for day in sorted(counter)}


def txns(events) -> list[dict]:
    return of(events, "txn")


def device_spells(events) -> dict:
    """把 `device_day` 明细折成"停摆段"：`{device_id: [段长...]}`（**M-10/M-11 的共同来源**）。

    「段」= 该设备连续处于非 `working` 状态的天数；`completed` 是已恢复的段、`open` 是期末仍未恢复的段。
    这样 M-10 的"停摆超过 τ 天"与 M-11 的"修复完成 − 报修"都**只由事件明细复算**，不靠产出方自报。
    """
    spells: dict[str, list[int]] = {}
    current: dict[str, int] = {}
    for event in of(events, "device_day"):
        device_id = event["device_id"]
        if event["state"] == "working":
            if device_id in current:
                spells.setdefault(device_id, []).append(current.pop(device_id))
        else:
            current[device_id] = current.get(device_id, 0) + 1
    return {"completed": spells, "open": dict(current)}
