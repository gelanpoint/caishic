"""月频结构决策（`T-SIM-06`；商户 / 市场方 / 监管；频率分层见设计 §4.1）。

拆出本模块的原因同 `day_loop.py`：`quality-gates.md` §1.2 的"单文件 ≤ 400 行"（`Q-16`：按语义拆分，
不放宽阈值）。本模块只做**月频**的事：商户选动作与更新 Q/EWMA、市场方分配预算（带迟滞）、
监管抽检、市场方现金流、消费者信任的月度汇总。**它不计算任何指标** —— 指标只认事件流。
"""

from __future__ import annotations

from ..agents.consumer import update_trust
from ..agents.market_admin import repair_capacity
from ..agents.merchant import COMPLY, merchant_terms
from ..core.clock import DAYS_PER_MONTH


# ---------------------------------------------------------------------------
def _merchant_params_for_period(world, stall):
    """本期的商户参数：**抽检率换成监管的当期名义抽检率**（S2 的机制链在此接上）。

    `p_check` 的口径必须说清：商户效用里的 `Ψ = p_check·(F_short + Loss_rep)` 用的是**名义**
    抽检率（场景变量），而监管的**实现**发现数由抽样记录给出 —— 两者口径不同，**报告同时给出、不混算**。
    """
    from ..agents.merchant import merchant_params

    overrides = {"p_check": world.regulator.rp.inspection_rate}
    if world.self_funded:
        overrides["device_share_cents"] = world.self_funded_share_cents
    return merchant_params(world.params, **overrides)


def coexisting_peers(world) -> list:
    """同期**仍在营**的商户（同伴网络 `Q̄_peer` 的成员）。**纯函数**，故负例可直接喂构造出来的 `world`。

    ⚠️ 已退出的商户**不在**这个网络里：它已经离场，它的经验不该继续对留下的人施加同伴压力。
    这不是洁癖 —— 把离场商户算进来会出事，见 [`peer_mean_q`](month_loop.py) 的算术说明。
    """
    return [s.merchant for s in world.stalls.values() if s.active]


def realized_volume_for_learning(stall, realized: dict) -> float | None:
    """本期**用于学习的**实现流水；返回 `None` = 没有实现流水 ⇒ **不可学习**。**纯函数**。

    `T-SIM-11` 修复：原文对退出/停业摊位用 `volume = max(1.0, 实现流水)` 兜底除零，
    于是"没有流水"被当成"流水 1 分"，`normalized = U / 1.0 = −29999.75`
    （退出后只剩设备分摊 30000 与 `p_check·(F+L)=102500`）——
    **比真实的每元效用（~0.25）大 10⁵ 倍**的垃圾值，既污染它自己的 `Q`，也污染同伴均值。

    语义上正确的规则是：**一期没有任何成交 ⇒ 没有实现值可学**（这是"没有数据"，不是"经营失败"）。
    顺带堵掉一个假出口：没有生意的月份不能被判"赚不够活下去"。
    """
    if not stall.active:
        return None
    volume = float(realized.get("volume_cents") or 0.0)
    return volume if volume > 0 else None


def peer_mean_q(agents) -> float:
    """上一期**同期仍在营**的商户群 Q 均值（同伴影响项 `ρ` 的输入）。

    ⚠️ **调用方必须只传 `coexisting_peers(world)`**。理由不是洁癖，是算术：
    已退出的商户没有流水可学，它的 `Q` 是残值；而"没有流水"那一期若被兜底喂进
    `observe()`，它的 `Q` 就变成 **−30000 量级**（见 `realized_volume_for_learning`）——
    比真实的每元效用（~0.25）大 **10⁵ 倍**。把它算进同伴均值，
    `ρ(1−φ)·Q̄_peer ≈ 0.08 × (−7700) ≈ −616` 就被加进**每一个在营商户**的 `Q`，
    直接压过 0.25 量级的真实效用差，`softmax` 于是被垃圾值支配。

    **实测（`T-SIM-11`）**：这一条污染单独造出
    `M-04 = [0.594, 0.479, 0.305, 0.183, 0.727, 0, 0, 0]` 的"大涨后归零"假象
    （`R3` 压力档 MTBF=180 / 240 日 / `S3` 足额臂），修掉之后同一臂变成
    `[0.594, 0.479, 0.305, 0.183, 0.187, 0.180, 0.152, 0.145]` —— **不再大涨、不再归零**。
    """
    live = [a for a in agents if a.q]
    if not live:
        return 0.0
    return sum(sum(a.q.values()) / len(a.q) for a in live) / len(live)


def market_cash_month(world, *, active_stalls: int, period: int) -> dict:
    """市场方**当月现金流**（M-17 的分子/分母）：收入侧 / 费用侧逐项落进事件，可被逐项核对。

    每一项都注明它来自哪个参数；`subsidy` 的**摊销口径是假设**（补贴是资本性的一次性收入，
    这里按设备摊销期摊到每月），故 M-17/M-18 的结论必须与假设分区展示。
    """
    params = world.params
    commissions = world.month_commission_cents + world.month_buyer_fee_cents
    channel_fee = commissions * float(params.value("channel_fee_rate_bp")) / 10000.0
    device_capex = float(world.fleet.total) * float(params.value("scale_unit_price_cents")) + float(
        params.value("device_screen_count")
    ) * float(params.value("device_screen_unit_price_cents"))
    screen_capex = float(params.value("device_screen_count")) * float(params.value("device_screen_unit_price_cents"))
    sign_capex = float(params.value("price_sign_cents_per_stall")) * active_stalls
    amort_months = float(params.value("device_amortization_months"))
    income = {
        "commission": world.month_commission_cents,
        "buyer_fee": world.month_buyer_fee_cents,
        "stall_fee": float(params.value("stall_fee_cents_per_month")) * active_stalls,
        "bank": float(params.value("bank_funding_cents_3y")) / 36.0,
        "subsidy": (device_capex + sign_capex) * float(params.value("subsidy_ratio_bp")) / 10000.0 / amort_months,
    }
    expense = {
        "device_amortization": device_capex / amort_months,
        "maintenance": world.month_repairs * float(params.value("repair_cost_cents")),
        "verification": float(params.value("market_verification_cents_per_month")),
        "network": float(params.value("network_refit_cents_per_market")) / float(params.value("network_amortization_months")),
        "labor": float(params.value("market_labor_cents_per_month")),
        "channel_fee": channel_fee,
    }
    return {
        "period": period,
        "income_cents": round(sum(income.values()), 6),
        "expense_cents": round(sum(expense.values()), 6),
        "net_cents": round(sum(income.values()) - sum(expense.values()), 6),
        "income_detail": {k: round(v, 6) for k, v in sorted(income.items())},
        "expense_detail": {k: round(v, 6) for k, v in sorted(expense.items())},
        "active_stalls": active_stalls,
    }


def open_period(world, *, day_index: int, day_count: int, month_totals: dict | None = None,
                usage_rate: float | None = None, decide=None, log=None, business_date: str | None = None,
                period: int | None = None) -> dict:
    """期初的**结构决策**（`T-SIM-08` 从 `run_scenario` 搬进来，`Q-16` 的"按语义拆分"）。

    ## 为什么这两行值得单独成函数

    它们决定的是"**月频**结构"，却长在日循环的编排里，于是读 `run_scenario` 的人会把
    `month_totals["active_start"]` 的更新时点当成日循环的一部分 —— 而它其实是**期初**语义：
    在 `decide_month_start` **之前**统计一次"本月初在营摊位数"，所以本期刚退出的摊位仍计入分母。
    这个口径差异直接决定 `M-01`（商户月度流失率）的分子分母，放错地方就会被后人"顺手修正"成
    另一种口径而不自知。

    `decide=None` 时只建/返回期初账目（第一次调用）；给了 `decide` 才真正执行月频决策。
    """
    if month_totals is None:
        return {"active_start": sum(1 for s in world.stalls.values() if s.active)}
    month_totals["active_start"] = sum(1 for s in world.stalls.values() if s.active)
    if decide is not None:
        decide(world, log, business_date, period, usage_rate)
    return month_totals


def decide_month_start(world, log, business_date: str, period: int, usage_rate: float) -> None:
    """月初：退出生效 → 市场方预算 → 逐摊"要不要用/怎么用"。"""
    from .model_adapter import adopt_decision  # 延迟导入：model_adapter 在运行时才需要本模块
    for stall in world.stalls.values():
        if stall.active and stall.merchant.exited and stall.exits_period is not None:
            stall.active = False
            log.emit(
                "stall_exit",
                business_date=business_date,
                period=period,
                stall_no=stall.stall_no,
                decided_period=stall.exits_period,
            )
    record = world.admin.step(period, installed_ratio=1.0, usage_rate=usage_rate)
    world.capacity_per_day = repair_capacity(world.admin.maintenance_budget_cents, float(world.params.value("repair_cost_cents")))
    log.emit("market_admin_period", business_date=business_date, repair_capacity=world.capacity_per_day, **record)

    for stall in world.stalls.values():
        stall.upfront_cents = float(world.self_funded_upfront_cents) if world.self_funded else 0.0
        stall.self_funded_share_cents = float(world.self_funded_share_cents)
        mp = _merchant_params_for_period(world, stall)
        volume = stall.prev_volume_cents or world.initial_volume_cents
        scale_txns = stall.prev_scale_txns or max(1, int(round(world.daily_arrivals / len(world.stalls) * DAYS_PER_MONTH * 0.5)))
        terms = merchant_terms(COMPLY, mp, volume_cents=volume, scale_txn_count=scale_txns)
        if not stall.adopted and stall.active:
            stall.adopted = adopt_decision(
                upfront_cents=stall.upfront_cents,
                monthly_net_benefit_cents=terms.utility,
                cost_weight=world.adoption_cost_weight,
                horizon_months=stall.horizon_months,
            )
            if stall.adopted:
                log.emit(
                    "stall_adopted",
                    business_date=business_date,
                    period=period,
                    stall_no=stall.stall_no,
                    upfront_cents=stall.upfront_cents,
                    horizon_months=round(stall.horizon_months, 6),
                    cost_weight=world.adoption_cost_weight,
                    monthly_net_benefit_cents=round(terms.utility, 6),
                )
        if not stall.active:
            continue
        #: **本次私下交易是否做得到**（`S6` 的 `feas(evade)`）。抽一次、本期有效：
        #: 出口查验与扫码覆盖是"这段时间有没有人管"的环境事实，不是每笔独立掷骰子。
        #: 抽签用该摊位自己的 `cheat` 流（同一用途 = 该摊位的作弊/私下决策），故**不影响别的摊位**。
        cheat_rng = world.streams.stream(stall.stall_no, "cheat")
        stall.evade_feasible = cheat_rng.random() < world.evade_feasibility
        action = stall.merchant.choose(cheat_rng)
        world.actions[stall.stall_no] = action
        stall.current_action = action
        log.emit(
            "merchant_period",
            business_date=business_date,
            period=period,
            stall_no=stall.stall_no,
            action=action,
            adopted=stall.adopted,
            upfront_cents=stall.upfront_cents,
            adopted_channel="scale" if (stall.adopted and action == COMPLY) else "shadow",
            expected_volume_cents=round(volume, 6),
            expected_scale_txns=scale_txns,
            expected_terms=terms.as_dict(),
        )


def close_month(world, log, business_date: str, period: int, month_totals: dict) -> float:
    """月末：商户实现值 → Q/EWMA；监管抽检；市场方现金流；消费者信任分布。返回本月观测到的使用率。"""
    from ..agents.merchant import merchant_terms as terms_fn

    observed = world.month_on_scale + sum(
        world.month_by_stall.get(stall_no, {}).get("shadow_txns", 0) for stall_no in world.stalls
    )
    usage_rate = world.month_on_scale / observed if observed else float(world.params.value("scale_use_baseline_rate"))
    #: **同伴均值只取"同期仍在营"的商户** —— 已退出的商户不在这个 10 摊网络里（理由见 `peer_mean_q`）。
    peer_q = peer_mean_q(coexisting_peers(world))
    active_start = month_totals["active_start"]
    for stall in world.stalls.values():
        if stall.current_action is None:
            continue
        realized = world.month_by_stall.get(stall.stall_no, {"volume_cents": 0, "scale_txns": 0, "short_txns": 0})
        #: **没有实现的流水，就没有可学习的实现值**（`T-SIM-11` 修复；规则见 `realized_volume_for_learning`）。
        volume = realized_volume_for_learning(stall, realized)
        if volume is None:
            continue
        mp = _merchant_params_for_period(world, stall)
        realized_terms = terms_fn(stall.current_action, mp, volume_cents=volume, scale_txn_count=realized["scale_txns"])
        stall.merchant.observe(stall.current_action, realized_terms, volume, peer_q)
        if stall.merchant.exited and stall.exits_period is None:
            stall.exits_period = period
        stall.prev_volume_cents = float(realized["volume_cents"])
        stall.prev_scale_txns = int(realized["scale_txns"])
        log.emit("merchant_month", business_date=business_date, period=period, stall_no=stall.stall_no,
                 action=stall.current_action, realized_volume_cents=round(float(realized["volume_cents"]), 6),
                 realized_scale_txns=int(realized["scale_txns"]), realized_short_txns=int(realized["short_txns"]),
                 net_income_cents=round(realized_terms.utility, 6),
                 ewma_utility=round(stall.merchant.ewma_utility, 6), exited=stall.merchant.exited)

    offenders = sum(
        1
        for stall in world.stalls.values()
        if stall.active and world.month_by_stall.get(stall.stall_no, {}).get("short_txns", 0) > 0
    )
    active_stalls = sum(1 for stall in world.stalls.values() if stall.active)
    inspection = world.regulator.inspect_period(
        world.streams.stream("regulator", "adapt"),
        stalls_in_scope=active_stalls,
        offenders=min(offenders, active_stalls),
        period=period,
    )
    log.emit("regulator_period", business_date=business_date, **inspection)

    cash = market_cash_month(world, active_stalls=active_stalls, period=period)
    log.emit("market_cash_month", business_date=business_date, **cash)

    for consumer_id in world.consumer_ids:
        total = 0.0
        for stall_no in world.stalls:
            key = (consumer_id, stall_no)
            if key not in world.visited:
                world.trust[key] = update_trust(world.trust[key], None, world.cp)
            total += world.trust[key]
        log.emit(
            "consumer_period",
            business_date=business_date,
            period=period,
            consumer_id=consumer_id,
            trust_sum=round(total, 9),
            trust_mean=round(total / max(1, len(world.stalls)), 9),
            stalls=len(world.stalls),
        )
    log.emit(
        "month_closed",
        business_date=business_date,
        period=period,
        active_start=active_start,
        active_end=active_stalls,
        exits=sum(1 for s in world.stalls.values() if s.exits_period == period),
        short_offenders=offenders,
    )
    world.visited.clear()
    world.month_by_stall = {}
    world.month_on_scale = 0
    world.month_amount = 0
    world.month_commission_cents = 0.0
    world.month_buyer_fee_cents = 0.0
    world.month_scanned = 0
    world.month_short = 0
    world.month_repairs = 0
    return usage_rate
