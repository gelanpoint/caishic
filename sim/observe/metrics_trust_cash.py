"""§6 的**信任、现金流与一致性类指标**（`M-15`~`M-22`；`T-SIM-06`）。

与 `metrics.py` 的分界是语义的：那边是**交易与设备**类（`M-01`~`M-14`，逐笔/逐日可数），
这边是**状态量与账本级**（信任存量、半衰期、市场方现金流、商户收入分布、对账、留痕、model↔live 偏差）。
拆成两个文件的原因是 `quality-gates.md` §1.2 的"单文件 ≤ 400 行"，不是设计上的分层。
"""

from __future__ import annotations

import math

from ..agents.consumer import ConsumerParams, analytic_fixed_point, analytic_half_life
from ..core.clock import DAYS_PER_MONTH
from .metric_specs import SPEC_BY_ID
from .metric_util import device_spells, effective_params, of, pct, ratio_row, txns, unavailable


def m11(events, spec=SPEC_BY_ID["M-11"]) -> dict:
    spells = device_spells(events)
    lengths = [length for group in spells["completed"].values() for length in group]
    return ratio_row(sum(lengths), len(lengths), spec,
                  detail={"未完成修复段": sorted(spells["open"].values(), reverse=True)[:10],
                          "note": "W_q 含维修时长本身（设计 §6 的 M-11 定义：修复完成 − 报修）"})


def m12(events, spec=SPEC_BY_ID["M-12"]) -> dict:
    short = sum(1 for t in txns(events) if t.get("short_weight"))
    total = len(txns(events))
    by_channel: dict[str, dict[str, int]] = {"scale": {"numerator": 0}, "shadow": {"numerator": 0}}
    for txn in txns(events):
        if txn.get("short_weight"):
            by_channel[txn["channel"]]["numerator"] += 1
    for txn in txns(events):
        by_channel[txn["channel"]]["denominator"] = by_channel[txn["channel"]].get("denominator", 0) + 1
    return ratio_row(short, total, spec, detail={"by_channel": by_channel})


def m13(events, spec=SPEC_BY_ID["M-13"]) -> dict:
    scale = [t for t in txns(events) if t["channel"] == "scale"]
    scanned = [t for t in txns(events) if t.get("scanned")]
    read_real = [t for t in scanned if t.get("info_real")]
    return ratio_row(len(read_real), len(scale), spec,
                  detail={"口径①读到有效信息 / 走秤": {"numerator": len(read_real), "denominator": len(scale)},
                          "口径②扫码（不论结果）/ 全部实际交易": {"numerator": len(scanned), "denominator": len(txns(events))}})


def m14(events, spec=SPEC_BY_ID["M-14"]) -> dict:
    scanned = [t for t in txns(events) if t.get("scanned")]
    hollow = [t for t in scanned if t.get("info_real") is False]
    return ratio_row(len(hollow), len(scanned), spec)


def m15(events, spec=SPEC_BY_ID["M-15"]) -> dict:
    rows = of(events, "consumer_period")
    if not rows:
        return ratio_row(0, 0, spec)
    periods = sorted({r["period"] for r in rows})
    last = periods[-1]
    tail = [r for r in rows if r["period"] == last]
    numerator = sum(r["trust_sum"] for r in tail)
    denominator = sum(r["stalls"] for r in tail)
    means = [r["trust_mean"] for r in tail]
    series = []
    for period in periods:
        group = [r["trust_mean"] for r in rows if r["period"] == period]
        series.append({"period": period, "mean": round(sum(group) / max(1, len(group)), 6),
                       "p10": pct(group, 10), "p50": pct(group, 50), "p90": pct(group, 90)})
    return ratio_row(numerator, denominator, spec,
                  detail={"period": last, "p10": pct(means, 10), "p50": pct(means, 50), "p90": pct(means, 90),
                          "by_period": series})


def trust_half_life_metric(events, spec=SPEC_BY_ID["M-16"]) -> dict:
    """`M-16`：解析半衰期 vs **由仿真实测轨迹拟合**的半衰期，两个口径并列 + 偏差。

    ⚠️ 单位必须换清楚才不会得出"实现错了"的假结论：
    解析式的时间单位是**一次观测机会**（本仿真里 = 一笔走秤交易），而实测轨迹按**月**汇总，
    故必须除以"每个（消费者, 摊位）对每月拿到几次观测机会"，两者才可比。
    """
    from_params = effective_params(events)
    if not from_params:
        return unavailable(spec, "缺少 run_started.effective_params，无法复算 M-16")
    cp = ConsumerParams(
        eta_pos=float(from_params["trust_update_eta_pos"]),
        eta_neg=float(from_params["trust_update_eta_neg"]),
        delta=float(from_params["trust_decay_delta"]),
        p_scan_floor=float(from_params["consumer_scan_floor"]),
        scan_gain=float(from_params["consumer_scan_trust_gain"]),
        info_real_share=float(from_params["info_real_share"]),
        recovery_target=float(from_params["trust_recovery_target"]),
    )
    scale = [t for t in txns(events) if t["channel"] == "scale"]
    scan_rate = (sum(1 for t in scale if t.get("scanned")) / len(scale)) if scale else 0.0
    analytic_periods = analytic_half_life(cp, scan_rate=scan_rate)
    fixed_point = analytic_fixed_point(cp, scan_rate=scan_rate)
    rows = of(events, "consumer_period")
    periods = sorted({r["period"] for r in rows})
    trace = []
    for period in periods:
        group = [r["trust_mean"] for r in rows if r["period"] == period]
        trace.append(sum(group) / max(1, len(group)))
    pairs = max(1, len({r["consumer_id"] for r in rows}) * (rows[0]["stalls"] if rows else 1))
    visits_per_pair_per_month = len(scale) / pairs / max(1, len(periods))
    analytic_months = analytic_periods / visits_per_pair_per_month if visits_per_pair_per_month else None
    fitted_months = _fit_half_life_months(trace, fixed_point)
    deviation = None
    if analytic_months and fitted_months:
        deviation = round(abs(fitted_months - analytic_months) / analytic_months, 6)
    return {
        "id": spec.id, "name": spec.name, "available": True, "ratio_is_ratio": False,
        "numerator": round(analytic_periods, 6) if analytic_periods != math.inf else None,
        "denominator": round(visits_per_pair_per_month, 9),
        #: 非比值指标的"它到底是什么量"：**换算后的解析半衰期（月）**。单位不写清就会
        #: 把"一次观测机会"读成"一个月"（两者在本仿真里差 1~2 个数量级）。
        "value": None if analytic_months is None else round(analytic_months, 6),
        "numerator_label": "解析半衰期（单位：一次观测机会）",
        "denominator_label": "每（消费者,摊位）对每月的观测机会数",
        "ratio": None, "bp": None, "condition": spec.condition, "sources": list(spec.sources),
        "detail": {
            "scan_rate": round(scan_rate, 6),
            "fixed_point": round(fixed_point, 6),
            "analytic_months": None if analytic_months is None else round(analytic_months, 6),
            "fitted_months": fitted_months,
            "deviation": deviation,
            "trust_trace_by_month": [round(v, 6) for v in trace],
            "note": "两个口径的时间单位已换算；拟合分辨率 = 1 个月，故偏差里含离散化误差",
        },
    }


def _fit_half_life_months(trace: list[float], fixed_point: float) -> float | None:
    """从**实测**月均信任轨迹里拟合"到不动点的缺口减半"所需月数（分辨率 1 个月）。"""
    if len(trace) < 3:
        return None
    gaps = [abs(value - fixed_point) for value in trace]
    start = max(range(len(gaps)), key=lambda i: gaps[i])
    if gaps[start] <= 0:
        return None
    for index in range(start + 1, len(gaps)):
        if gaps[index] <= gaps[start] / 2.0:
            return float(index - start)
    return None


def m17(events, spec=SPEC_BY_ID["M-17"]) -> dict:
    rows = of(events, "market_cash_month")
    income = sum(r["income_cents"] for r in rows)
    expense = sum(r["expense_cents"] for r in rows)
    return ratio_row(income, expense, spec,
                  detail={"net_cents": round(income - expense, 6),
                          "by_month": [{"period": r["period"], "income": r["income_cents"],
                                        "expense": r["expense_cents"], "net": r["net_cents"]} for r in rows],
                          "note": "「分子减分母」= 净现金流（设计 §6 的 M-17 写法）"})


def m18(events, spec=SPEC_BY_ID["M-18"]) -> dict:
    rows = of(events, "market_cash_month")
    total = sum(r["net_cents"] for r in rows)
    cumulative, series = 0.0, []
    for row in rows:
        cumulative += row["net_cents"]
        series.append({"period": row["period"], "cumulative_net_cents": round(cumulative, 6)})
    out = ratio_row(total, len(rows), spec, detail={"cumulative_by_month": series, "cumulative_net_cents": round(total, 6)})
    out["value"] = round(total, 6)
    return out


def m19(events, spec=SPEC_BY_ID["M-19"]) -> dict:
    """商户月净收入的**分布**（`§6` M-19：报 p10/p50/p90，不只报均值）。

    `ratio_is_ratio=False`：分子/分母的商是**均值金额（分）**，不是比例 ——
    把它印成百分比会让"月净收入 34.67 万 / 摊"读成"3467400%"，是纯粹的口径错误
    （`test_m19_is_a_distribution_not_a_percentage` 是这条的回归守卫）。
    """
    values = [e["net_income_cents"] for e in of(events, "merchant_month")]
    if not values:
        return ratio_row(0, 0, spec)
    out = ratio_row(sum(values), len(values), spec,
                    detail={"p10": pct(values, 10), "p50": pct(values, 50), "p90": pct(values, 90),
                            "min": round(min(values), 6), "max": round(max(values), 6),
                            "unit": "分/摊位-月（商 = 均值，不是比例）"})
    out["value"] = round(sum(values) / len(values), 6)
    return out


def m20(events, spec=SPEC_BY_ID["M-20"]) -> dict:
    """对账等式成立率（**model 口径 = 仿真账本自洽率**，与 live 的系统口径分开写）。

    逐营业日核两件事：
    ① **金额守恒**：当日走秤金额 = 当日按支付方式（cash/qr）记账的金额之和；
    ② **佣金一致**：当月由 `txn` 明细复算的费率乘积 = 当月 `market_cash_month` 记的佣金+买方费。
    ② 是**两条路径**的对账（明细复算 vs 产出方累加器）—— 只查①的话它对"写死"是盲的。

    ⚠️ **"营业日 → 期"的映射只能有一处**（本函数第一版有两处：`by_month` 用序号推导、
    查 `market_cash_month` 却用月/日算术，且查不到就当"没问题"跳过 —— 实测 60 日档下
    ② 一次都没真正执行、M-20 仍报 100%，是典型的"检查不覆盖"）。现在两处共用
    `period_of`，且**当期账目缺失一律判红**（缺账不是通过）。
    """
    params = effective_params(events)
    rate = float(params.get("commission_rate_bp", 0) or 0) / 10000.0
    #: **唯一**的"营业日 → 期"映射：第 i 个营业日属于第 i//30 期（与产出方 `run_scenario` 同口径）。
    #: 不用"月/日算术"猜 —— 第一版就是那样把 10-01 算成第 9 期、且短档下静默跳过核对。
    business_days = sorted({e["business_date"] for e in of(events, "day_arrivals_total")})
    period_of = {day: index // DAYS_PER_MONTH for index, day in enumerate(business_days)}
    by_day: dict[str, dict[str, float]] = {}
    by_month: dict[int, float] = {}
    for txn in txns(events):
        if txn["channel"] != "scale":
            continue
        day = by_day.setdefault(txn["business_date"], {"scale": 0.0, "methods": 0.0})
        day["scale"] += txn["amount_cents"]
        if txn.get("method"):
            day["methods"] += txn["amount_cents"]
        month = period_of.get(txn["business_date"])
        if month is not None:
            by_month[month] = by_month.get(month, 0.0) + txn["amount_cents"] * rate
    declared = {row["period"]: row for row in of(events, "market_cash_month")}
    ok_days, problems, checked = 0, [], 0
    for day, bucket in sorted(by_day.items()):
        month = period_of.get(day)
        declared_row = declared.get(month) if month is not None else None
        if declared_row is None:
            #: 缺当期账目 = 对账**无法成立**（不许当成"没问题"跳过 —— 那正是第一版的缺陷）
            problems.append({"business_date": day, "period": month, "reason": "缺当期 market_cash_month 账目"})
            continue
        declared_fee = declared_row["income_detail"].get("commission", 0.0) + declared_row["income_detail"].get("buyer_fee", 0.0)
        commission_ok = abs(declared_fee - by_month.get(month, 0.0)) < 1.0
        checked += 1
        if abs(bucket["scale"] - bucket["methods"]) < 1e-9 and commission_ok:
            ok_days += 1
        else:
            problems.append({"business_date": day, "period": month,
                             "amount_conservation": bucket["scale"] == bucket["methods"],
                             "commission_consistent": commission_ok,
                             "scale_cents": bucket["scale"], "methods_cents": bucket["methods"],
                             "declared_fee_cents": declared_fee, "recomputed_fee_cents": by_month.get(month, 0.0)})
    out = ratio_row(ok_days, len(by_day), spec,
                    detail={"violations": problems[:10], "violation_count": len(problems),
                            "commission_checked_days": checked,
                            "note": "live 模式的 M-20 取系统 /admin/reconciliation.balanced（口径不同，报告分开写）"})
    return out


def m21(events, spec=SPEC_BY_ID["M-21"]) -> dict:
    """留痕完整性：**应产生**的留痕（改价/退货）与**已产生**的 `audit_event` 逐类核对。

    补传与幂等命中两类属 live（`T-SIM-07`），在 model 模式下**显式标为不可用**（不得当 0 或 1）。
    """
    expected = {"price_change": len(of(events, "price_change")), "refund": len(of(events, "refund"))}
    recorded = {"price_change": 0, "refund": 0}
    for event in of(events, "audit_event"):
        if event.get("event_class") in recorded:
            recorded[event["event_class"]] += 1
    return ratio_row(sum(recorded.values()), sum(expected.values()), spec,
                  detail={"by_class": {k: {"expected": expected[k], "recorded": recorded[k]} for k in expected},
                          "live_only_classes": ["offline_backfill", "idempotency_hit"],
                          "live_only_note": "补传/幂等命中的留痕只能在 live 模式核对（T-SIM-07），故不并入本率"})


#: `M-22` 逐日比对的字段（live 与 model 两侧都要有）
LIVE_FIELDS = ("scale_txns", "gross_amount_cents", "commission_cents", "settlement_cents",
               "usage_numerator", "usage_denominator", "cash_numerator", "cash_denominator",
               "price_numerator", "price_denominator")


def m22(events, spec=SPEC_BY_ID["M-22"], tolerance_cents: float = 1.0) -> dict:
    """`model↔live` 偏差（**仅 live 模式可算**）：逐日比笔数/金额/佣金/结算/三指标分子分母。"""
    live = {row["business_date"]: row for row in of(events, "live_daily")}
    model = {row["business_date"]: row for row in of(events, "model_daily")}
    if not live or not model:
        return unavailable(spec, "model 模式下没有 live 侧事件（live 适配器属 T-SIM-07/08）")
    worst, mismatches, checked = 0.0, [], 0
    for day in sorted(set(live) & set(model)):
        for field in LIVE_FIELDS:
            live_value = float(live[day].get(field, 0) or 0)
            model_value = float(model[day].get(field, 0) or 0)
            checked += 1
            denominator = abs(model_value) if model_value else 1.0
            deviation = abs(live_value - model_value) / denominator
            worst = max(worst, deviation)
            if abs(live_value - model_value) > max(tolerance_cents, 1e-9) * max(1.0, abs(model_value) / 100.0):
                mismatches.append({"business_date": day, "field": field,
                                   "live": live_value, "model": model_value})
    return ratio_row(worst, 1.0, spec,
                  detail={"checked_fields": checked, "mismatches": mismatches[:10],
                          "mismatch_count": len(mismatches), "tolerance": "笔数与整数分要求完全相等"})
