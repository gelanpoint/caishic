"""§6 的 **22 个指标**：**只读事件流**的纯函数（`T-SIM-06`；`docs/sim-design.md` §6）。

指标的**目录**（分子/分母口径、来源事件、适用条件）在 `metric_specs.py`；本模块只负责**复算**。

## 三条不可让步的口径

1. **指标只认 `events.jsonl`**，不读任何运行时状态。理由：`Q-19` 的教训 —— 若指标与产出方共用
   一份内存状态，"复算检查"就只是把同一个值抄两遍，**写死与真查输出相同，检查根本发现不了**。
   本模块所有函数的签名都是 `(events) -> dict`，运行产物与复算结果是**两条独立路径**。
2. **每个指标都写清分子与分母**（`AC-005`「第三方可逐一核对」的精神）。比值型给
   `numerator / denominator / ratio / bp`；非比值型（`M-02`/`M-16`/`M-18`）显式声明 `ratio_is_ratio=False`
   并给出它到底是什么量，**不硬凑一个分母**。
3. **在什么条件下这个数才算数**：每条指标的 `condition` 记录它的适用条件 ——
   有真实数据的参数才允许说绝对阈值，无数据的（MTBF / 罚款 / 扫码基线 / 信任权重 / 退出阈值）
   **只允许序关系与区间不重叠**。报告必须把这一列原样带上。

`M-20`/`M-21` 在 model 模式下用**同精神的仿真侧口径**（仿真账本自洽 / 仿真侧留痕齐全），
`M-22`（model↔live 偏差）**只在 live 模式可用** —— 三者都不许把"没有数据"说成"通过"。
"""

from __future__ import annotations

import math

from .metric_specs import SPECS, SPEC_BY_ID
from .metric_util import effective_params, of, pct, device_spells, ratio_row, txns, unavailable

# ---------------------------------------------------------------------------
# 22 个指标
# ---------------------------------------------------------------------------
def m01(events, spec=SPEC_BY_ID["M-01"]) -> dict:
    series, num, den = [], 0, 0
    for event in of(events, "month_closed"):
        series.append({"period": event["period"], "numerator": event["exits"],
                       "denominator": event["active_start"],
                       "bp": round(event["exits"] / event["active_start"] * 10000, 4) if event["active_start"] else None})
        num += event["exits"]
        den += event["active_start"]
    return ratio_row(num, den, spec, detail={"by_month": series})


def m02(events, spec=SPEC_BY_ID["M-02"]) -> dict:
    first = next((e for e in of(events, "month_closed") if e["exits"] > 0), None)
    if first is None:
        #: 从未退出时 `value` 显式置 `None`（**不是 0**：第 0 月没有退出 ≠ 首次退出发生在第 0 月）。
        #: 第一版这里连 `value` 键都没有，于是报告只能回退去印分子（0），读起来像"第 0 期就垮了"。
        out = ratio_row(0, 0, spec, detail={"t_first_exit": None, "note": "期内从未出现退出（这是结果，不是缺失）"})
        out["value"] = None
        return out
    out = ratio_row(first["exits"], first["active_start"], spec, detail={"t_first_exit": first["period"]})
    out["value"] = first["period"]
    return out


def m03(events, spec=SPEC_BY_ID["M-03"]) -> dict:
    months = of(events, "month_closed")
    finished = of(events, "run_finished")
    if not months:
        return ratio_row(0, 0, spec)
    active_end = finished[-1]["active_end"] if finished else months[-1]["active_end"]
    return ratio_row(active_end, months[0]["active_start"], spec,
                  detail={"active_end": active_end, "initial_active": months[0]["active_start"]})


#: 存活判据③ 的窗口（设计 §6.9：「第 300–360 日平均 ScaleUseRate ≥ 60%」）
SURVIVAL_WINDOW_START = 300


def survival_window(events, *, start_index: int = SURVIVAL_WINDOW_START) -> dict:
    """**存活判据③ 的窗口口径**：第 `start_index` 日起（含）的平均走秤率。

    为什么必须单独成一个函数、且由**营业日序号**切窗：存活判据写的是"第 300–360 日"，
    而营业日与日历日期不是一回事（`DAYS_PER_MONTH=30` 的仿真月与自然月不对齐），
    用日期做算术切出来的窗口会默默错位。营业日序号取自 `day_arrivals_total`（每日一条），
    它同时保证"这一天真的跑过"，不会把没跑的日子算进分母。
    """
    days = sorted({e["business_date"] for e in of(events, "day_arrivals_total")})
    window_days = days[start_index:]
    per_day: dict[str, list[int]] = {}
    for txn in txns(events):
        bucket = per_day.setdefault(txn["business_date"], [0, 0])
        bucket[1] += 1
        if txn["channel"] == "scale":
            bucket[0] += 1
    num = sum(per_day.get(day, [0, 0])[0] for day in window_days)
    den = sum(per_day.get(day, [0, 0])[1] for day in window_days)
    return {"numerator": num, "denominator": den,
            "ratio": (num / den) if den else None,
            "window": f"第 {start_index}–{max(start_index, len(days) - 1)} 营业日",
            "window_days": len(window_days),
            "run_days": len(days),
            "evaluable": bool(window_days) and den > 0}


def m04(events, spec=SPEC_BY_ID["M-04"]) -> dict:
    num, den = 0, 0
    series: dict[str, list[int]] = {}
    for txn in txns(events):
        bucket = series.setdefault(txn["business_date"], [0, 0])
        bucket[1] += 1
        if txn["channel"] == "scale":
            bucket[0] += 1
    num = sum(v[0] for v in series.values())
    den = sum(v[1] for v in series.values())
    return ratio_row(num, den, spec, detail={
        "by_day": {d: {"numerator": v[0], "denominator": v[1]} for d, v in sorted(series.items())},
        #: 存活判据③ 用的是**窗口均值**，不是全程均值 —— 两个口径都在这里给出，避免报告里把前者当后者
        "window_300_360": survival_window(events),
    })


def m05(events, spec=SPEC_BY_ID["M-05"]) -> dict:
    series: dict[str, list[int]] = {}
    for txn in txns(events):
        bucket = series.setdefault(txn["business_date"], [0, 0])
        bucket[1] += 1
        if txn["channel"] == "shadow":
            bucket[0] += 1
    return ratio_row(sum(v[0] for v in series.values()), sum(v[1] for v in series.values()), spec,
                  detail={"by_day": {d: {"numerator": v[0], "denominator": v[1]} for d, v in sorted(series.items())}})


def m06(events, spec=SPEC_BY_ID["M-06"]) -> dict:
    counts: dict[str, int] = {}
    total = 0
    for event in of(events, "merchant_period"):
        counts[event["action"]] = counts.get(event["action"], 0) + 1
        total += 1
    detail = {action: {"numerator": counts.get(action, 0), "denominator": total,
                       "bp": round(counts.get(action, 0) / total * 10000, 4) if total else None}
              for action in ("comply", "evade", "exit")}
    return ratio_row(counts.get("comply", 0), total, spec, detail=detail)


def m07(events, spec=SPEC_BY_ID["M-07"]) -> dict:
    used: dict[str, set] = {}
    for txn in txns(events):
        if txn["channel"] == "scale":
            used.setdefault(txn["business_date"], set()).add(txn["stall_no"])
    active: dict[str, int] = {}
    for event in of(events, "price_list"):
        active[event["business_date"]] = active.get(event["business_date"], 0) + 1
    num = sum(len(v) for v in used.values())
    den = sum(active.values())
    detail = {day: {"numerator": len(used.get(day, ())), "denominator": active.get(day, 0)} for day in sorted(active)}
    return ratio_row(num, den, spec, detail={"by_day": detail})


def m08(events, spec=SPEC_BY_ID["M-08"]) -> dict:
    cash = sum(1 for t in txns(events) if t["channel"] == "scale" and t.get("method") == "cash")
    scale = sum(1 for t in txns(events) if t["channel"] == "scale" and t.get("method"))
    return ratio_row(cash, scale, spec)


def m09(events, spec=SPEC_BY_ID["M-09"]) -> dict:
    rows = of(events, "price_list")
    complete = sum(1 for e in rows if e.get("complete"))
    return ratio_row(complete, len(rows), spec,
                  detail={"adopted_stalls": complete, "active_stalls": len(rows)})


def m10(events, spec=SPEC_BY_ID["M-10"]) -> dict:
    used: dict[str, set] = {}
    for txn in txns(events):
        if txn["channel"] == "scale":
            used.setdefault(txn["business_date"], set()).add(txn["stall_no"])
    active_days = of(events, "price_list")
    idle_stall_days = sum(1 for e in active_days if e["stall_no"] not in used.get(e["business_date"], set()))
    tau = float(effective_params(events).get("device_idle_tau_repair_days", 7) or 7)
    device_days = of(events, "device_day")
    spells = device_spells(events)
    over_tau = 0
    for device_id, lengths in spells["completed"].items():
        over_tau += sum(1 for length in lengths if length > tau)
    over_tau += sum(1 for length in spells["open"].values() if length > tau)
    return ratio_row(
        idle_stall_days, len(active_days), spec,
        detail={
            "口径①摊位-日": {"numerator": idle_stall_days, "denominator": len(active_days),
                             "bp": round(idle_stall_days / len(active_days) * 10000, 4) if active_days else None},
            "口径②设备-日(停摆>τ)": {"numerator": over_tau, "denominator": len(device_days), "tau_repair_days": tau,
                                     "bp": round(over_tau / len(device_days) * 10000, 4) if device_days else None,
                                     "note": "τ_repair 无出处（A-13）⇒ 该口径的绝对值不可作为判据"},
        },
    )


from . import metrics_trust_cash as longrun  # noqa: E402  (放在此处：两边互相引用会成环)

#: 指标 id → 复算函数（**顺序与 SPECS 一致**，便于"22 个都跑到"变成可断言的属性）
FUNCTIONS = {
    "M-01": m01, "M-02": m02, "M-03": m03, "M-04": m04, "M-05": m05, "M-06": m06, "M-07": m07,
    "M-08": m08, "M-09": m09, "M-10": m10, "M-11": longrun.m11, "M-12": longrun.m12, "M-13": longrun.m13, "M-14": longrun.m14,
    "M-15": longrun.m15, "M-16": longrun.trust_half_life_metric, "M-17": longrun.m17, "M-18": longrun.m18, "M-19": longrun.m19,
    "M-20": longrun.m20, "M-21": longrun.m21, "M-22": longrun.m22,
}


def compute_all(events) -> dict:
    """复算全部 22 个指标（**唯一入口**；产出方与复算方走的是同一条只读路径）。"""
    out: dict[str, dict] = {}
    for spec in SPECS:
        function = FUNCTIONS[spec.id]
        try:
            out[spec.id] = function(events)
        except Exception as exc:  # pragma: no cover - 复算失败必须显式暴露，不许静默
            out[spec.id] = unavailable(spec, f"复算抛出 {type(exc).__name__}: {exc}")
    return out


def survival_judgement(metrics: dict) -> dict:
    """`§6.9` 的存活判据：**四条分别给**（哪一条不满足），不许只给一个布尔值。

    四个阈值全部是假设 —— 故本函数返回的每一条都带 `assumption=True`，报告必须原样带上。

    ⚠️ **判据③ 的口径是 `M-04`（走秤率）的「第 300–360 日」窗口**，不是 `M-13`（扫码率）——
    两者数值相近但语义不同（前者是"系统没被绕过"，后者是"顾客还信"），弄混会让判据③
    在"信任低但走秤率高"的臂上假红、在相反情形下假绿。实现第一版就把它写成了 `M-13` 的
    口径①（`test_survival_criterion_uses_scale_use_rate` 是这条的回归守卫）。
    """
    from .metric_specs import SURVIVAL_THRESHOLDS

    cum_cash = float(metrics.get("M-18", {}).get("value") or 0.0)
    active_ratio = metrics.get("M-03", {}).get("ratio")
    window = (metrics.get("M-04", {}).get("detail") or {}).get("window_300_360") or {}
    scale_use = window.get("ratio")
    evaluable = bool(window.get("evaluable"))
    trust = metrics.get("M-15", {}).get("ratio")
    criteria = {
        "CumCash(360) >= 0": {"value": cum_cash, "pass": cum_cash >= 0, "assumption": True},
        "active_ratio >= 0.70": {"value": active_ratio, "pass": (active_ratio or 0) >= 0.70, "assumption": True},
        "scale_use_rate_300_360 >= 0.60": {
            "value": scale_use, "pass": bool(evaluable and (scale_use or 0) >= 0.60), "assumption": True,
            "window": window.get("window"), "window_days": window.get("window_days"),
            "numerator": window.get("numerator"), "denominator": window.get("denominator"),
            "note": None if evaluable else (
                f"运行不足 {SURVIVAL_WINDOW_START} 个营业日（本段 {window.get('run_days')} 日）"
                "⇒ 该条**不可评估**，按不通过处理（不许把「没跑到」说成「通过」）"),
        },
        "TrustIndex(360) >= 0.5": {"value": trust, "pass": (trust or 0) >= 0.5, "assumption": True},
    }
    return {"criteria": criteria, "thresholds": SURVIVAL_THRESHOLDS,
            "all_pass": all(item["pass"] for item in criteria.values()),
            "disclaimer": "四个阈值（0 / 70% / 60% / 0.5）全部是**假设**，无一有调研出处；"
                          "故这只是『在本设计定义的存活语义下的参数区域』，不是现实承诺。不许调阈值凑结果。"}
