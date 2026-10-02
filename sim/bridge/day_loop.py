"""`T-SIM-06` 的**日内机制**：营业日内的客流、选摊、成交、信任、设备推进。

拆出本模块的**唯一原因**是 `quality-gates.md` §1.2 的"单文件 ≤ 400 行"（`Q-16` 的裁定是
"按语义拆分、不放宽阈值"）。语义边界是清楚的：

* `model_adapter.py` = 场景装载 + **月频**结构决策（商户/市场方/监管）+ 运行编排；
* 本模块 = **日频**事实生产（到达 → 选摊 → 一笔成交 → 信任更新 → 设备推进）。

两者共享同一个 `World` 状态对象（定义在 `model_adapter.py`），事实一律经 `EventLog` 落盘 ——
本模块**不计算任何指标**，指标只认事件流（`sim/observe/metrics.py`）。
"""

from __future__ import annotations

import math
import random

from ..agents.consumer import scan_probability, update_trust
from ..agents.merchant import COMPLY
from ..env.demand import day_arrivals
from ..env.devices import WORKING
from ..env.market import CATEGORIES

UNIT_GRAMS = 500


def _exp(value: float) -> float:
    """有界指数：logit 权重不能溢出成 inf（否则"选最好的摊位"会退化成"选第一个"）。"""
    return math.exp(min(60.0, max(-60.0, value)))



def current_action(world, stall_no: str) -> str | None:
    """该摊位**本期**的商户动作（月初决定，整期有效）；退出即无动作。"""
    return world.actions.get(stall_no)


def emit_price_lists(world, log, business_date: str) -> dict:
    """日初：每个**在营且已采用**的摊位设置当日价目表（`REQ-003` 的复制上日 + 逐条调整）。

    不采用的摊位发 `price_list` 且 `complete=False` —— **意愿通道必须留下事实**，否则
    `M-09`（价目表维护率）的分母会变成"只统计愿意用的摊位"，把结论算漂亮。
    """
    active = complete = 0
    for stall in world.stalls.values():
        if not stall.active:
            continue
        active += 1
        is_complete = stall.adopted
        complete += 1 if is_complete else 0
        log.emit(
            "price_list",
            business_date=business_date,
            stall_no=stall.stall_no,
            adopted=stall.adopted,
            complete=is_complete,
            items=len(stall.price_book) if is_complete else 0,
            source="copied_previous_day" if is_complete else "none",
        )
        if is_complete and world.adjust_rng.random() < world.price_adjust_probability:
            product_id = sorted(stall.price_book)[world.adjust_rng.randrange(len(stall.price_book))]
            old = stall.price_book[product_id]
            new = max(1, int(round(old * world.adjust_rng.uniform(0.95, 1.05))))
            stall.price_book[product_id] = new
            log.emit(
                "price_change",
                business_date=business_date,
                stall_no=stall.stall_no,
                product_id=product_id,
                old_price_cents=old,
                new_price_cents=new,
            )
            log.emit(
                "audit_event",
                business_date=business_date,
                event_class="price_change",
                stall_no=stall.stall_no,
                ref=f"{stall.stall_no}:{product_id}",
            )
    return {"active": active, "complete": complete}


def _pick(rng: random.Random, choices: list):
    return choices[rng.randrange(len(choices))]


def _one_transaction(world, log, business_date: str, block: str, choice_rng: random.Random) -> dict:
    """一笔成交：选品类 → 选摊（logit）→ 走秤 / 私下 → 可能的短秤 → 扫码与信任更新。

    每一步的口径都在事件里留下可见字段，故"为什么这一笔是私下"是可追的（不是黑箱）。
    """
    consumer_id = _pick(world.consumer_rng, world.consumer_ids)
    category_id = _pick(choice_rng, [c[0] for c in CATEGORIES])
    candidates = [s for s in world.stalls.values() if s.active and s.category_id == category_id]
    if not candidates:  # 该品类无在营摊位 → 退到全部在营摊位（否则到达会静默少一笔）
        candidates = [s for s in world.stalls.values() if s.active]
    if not candidates:
        # **未成交**也必须留事实：否则「到达 = 走秤 + 私下」的守恒无法由明细复算（本实现第一版就漏了它，
        # 两条路径对账时直接抓出「未成交 0 vs 17992」）。它**不算一笔成交**，故不进 `txn` 事件。
        log.emit("lost_sale", business_date=business_date, block=block, consumer_id=consumer_id,
                 category_id=category_id, reason="no_active_stall")
        return {"channel": "lost", "amount_cents": 0, "scanned": False, "short_weight": False, "stall_no": None}

    category_mean = world.category_mean_price[category_id]
    scored = []
    for stall in candidates:
        price_mean = sum(stall.price_book.values()) / max(1, len(stall.price_book))
        relative = (price_mean - category_mean) / category_mean
        scored.append((stall, world.trust[(consumer_id, stall.stall_no)], relative))
    temperature = max(world.choice_temperature, 1e-9)
    weights = [
        (stall, _exp((-world.price_sensitivity * relative + trust) / temperature))
        for stall, trust, relative in scored
    ]
    total = sum(weight for _stall, weight in weights)
    threshold = choice_rng.random() * total
    picked = weights[-1][0]
    for stall, weight in weights:
        threshold -= weight
        if threshold <= 0:
            picked = stall
            break

    device_working = world.fleet.states[picked.device_id] == WORKING
    action = world.actions.get(picked.stall_no)
    on_scale = bool(picked.adopted and action == COMPLY and device_working)
    #: `S6`：商户选了 `evade`，但本期**私下交易不可行**（出口查验 / 扫码覆盖 / ρ_anchor≈1）⇒
    #: 想私下也私不成。此时：装了秤且设备正常的 ⇒ 只能走秤；没装秤的 ⇒ 这笔**做不成**（留 `lost_sale`）。
    #: 这条机制正是"堵死私下交易"的落地形式；`evade_feasibility=1.0`（基线）时恒不触发，行为不变。
    forced_on_scale = False
    if (not on_scale) and action is not None and action != COMPLY and not picked.evade_feasible:
        if picked.adopted and device_working:
            on_scale, forced_on_scale = True, True
    channel = "scale" if on_scale else "shadow"
    if (not on_scale) and action is not None and action != COMPLY and not picked.evade_feasible and not picked.adopted:
        #: 没装秤又被堵死 ⇒ 这笔成交做不成（**不是**私下成交，不许把它算成 M-05 的分子）
        log.emit("lost_sale", business_date=business_date, block=block, consumer_id=consumer_id,
                 category_id=category_id, stall_no=picked.stall_no, reason="evade_blocked_no_scale")
        return {"channel": "lost", "amount_cents": 0, "scanned": False, "short_weight": False, "stall_no": None}

    product_ids = sorted(picked.price_book)
    product_id = product_ids[choice_rng.randrange(len(product_ids))]
    grams = choice_rng.randint(int(world.avg_basket_grams * 0.5), int(world.avg_basket_grams * 1.5))
    unit_price = picked.price_book[product_id]
    amount = max(1, int(round(unit_price * grams / UNIT_GRAMS)))

    short_weight = False
    if picked.short_propensity:
        if channel == "shadow":
            short_weight = True  # 私下交易：系统不可见，短秤没有系统侧成本
        else:
            short_weight = choice_rng.random() < world.short_on_scale_probability

    method = None
    scanned = False
    info_real = None
    trust_before = world.trust[(consumer_id, picked.stall_no)]
    trust_after = trust_before
    bucket = world.month_by_stall.setdefault(
        picked.stall_no, {"volume_cents": 0, "scale_txns": 0, "shadow_txns": 0, "short_txns": 0}
    )
    bucket["volume_cents"] += amount
    if channel == "scale":
        method = "cash" if choice_rng.random() < world.cash_payment_share else "qr"
        trust_rng = world.streams.stream(consumer_id, "trust")
        p_scan = scan_probability(trust_before, world.cp)
        scanned = trust_rng.random() < p_scan
        outcome = None
        if scanned:
            info_real = trust_rng.random() < world.cp.info_real_share
            outcome = 1.0 if info_real else 0.0
        trust_after = update_trust(trust_before, outcome, world.cp)
        world.trust[(consumer_id, picked.stall_no)] = trust_after
        world.visited.add((consumer_id, picked.stall_no))
        world.month_on_scale += 1
        world.month_amount += amount
        bucket["scale_txns"] += 1
        fee = amount * world.commission_rate_bp / 10000.0
        if world.commission_charged_to_merchant:
            world.month_commission_cents += fee
        else:
            world.month_buyer_fee_cents += fee
        if scanned:
            world.month_scanned += 1
    else:
        bucket["shadow_txns"] += 1
    if short_weight:
        world.month_short += 1
        bucket["short_txns"] += 1

    log.emit(
        "txn",
        business_date=business_date,
        block=block,
        consumer_id=consumer_id,
        stall_no=picked.stall_no,
        category_id=category_id,
        product_id=product_id,
        channel=channel,
        method=method,
        retail_action=action,
        #: `S6` 的两个可见事实：本期私下交易**能不能做**、这笔走秤是不是**被堵回来的**
        evade_feasible=bool(picked.evade_feasible),
        forced_on_scale=bool(forced_on_scale),
        grams=grams,
        amount_cents=amount,
        short_weight=short_weight,
        short_ratio=world.short_ratio if short_weight else 0.0,
        scanned=scanned,
        info_real=info_real,
        trust_before=round(trust_before, 9),
        trust_after=round(trust_after, 9),
        device_working=device_working,
        adopted=picked.adopted,
    )
    if channel == "scale" and world.refund_rng.random() < world.refund_probability:
        log.emit(
            "refund",
            business_date=business_date,
            stall_no=picked.stall_no,
            amount_cents=amount,
            reason="sim_refund",
        )
        log.emit(
            "audit_event",
            business_date=business_date,
            event_class="refund",
            stall_no=picked.stall_no,
            ref=f"{business_date}:{picked.stall_no}:{amount}",
        )
    return {"channel": channel, "amount_cents": amount, "scanned": scanned, "short_weight": short_weight,
            "stall_no": picked.stall_no, "method": method}


def walk_day(world, log, business_date: str, day_index: int) -> dict:
    """推进一个营业日：各时段块到达 → 逐笔成交 → 逐块与整日汇总事件。"""
    totals = {"count": 0, "on_scale": 0, "off_scale": 0, "lost": 0, "scanned": 0, "short": 0,
              "amount": 0, "cash": 0, "qr": 0}
    stalls_used: set = set()
    choice_rng = world.streams.stream("market", "choice")
    for arrivals in day_arrivals(world.clock.blocks, world.daily_arrivals, world.streams.stream("market", "arrival")):
        block_totals = {"on_scale": 0, "off_scale": 0, "lost": 0, "scanned": 0, "short": 0, "amount": 0}
        for _ in range(arrivals.count):
            record = _one_transaction(world, log, business_date, arrivals.block, choice_rng)
            if record["channel"] == "scale":
                block_totals["on_scale"] += 1
                stalls_used.add(record["stall_no"])
                totals[record["method"]] = totals.get(record["method"], 0) + 1
            elif record["channel"] == "shadow":
                block_totals["off_scale"] += 1
            else:
                block_totals["lost"] += 1
            block_totals["scanned"] += 1 if record["scanned"] else 0
            block_totals["short"] += 1 if record["short_weight"] else 0
            block_totals["amount"] += record["amount_cents"]
        log.emit(
            "block_arrivals",
            business_date=business_date,
            block=arrivals.block,
            start=arrivals.start,
            intensity=arrivals.intensity,
            lam=round(arrivals.lam, 12),
            count=arrivals.count,
            on_scale=block_totals["on_scale"],
            off_scale=block_totals["off_scale"],
        )
        log.emit("block_summary", business_date=business_date, block=arrivals.block, **block_totals)
        for key in ("on_scale", "off_scale", "lost", "scanned", "short", "amount"):
            totals[key] += block_totals[key]
        totals["count"] += arrivals.count
    totals["stalls_used"] = len(stalls_used)
    log.emit("day_arrivals_total", business_date=business_date, count=totals["count"], day_index=day_index)
    return totals


def advance_devices(world, log, business_date: str) -> dict:
    """设备推进一天并逐台落 `device_day`（**M-10 的设备口径与 M-11 的 W_q 都从它复算**）。"""
    before = world.fleet.repairs_done_total
    state = world.fleet.step_day(
        world.streams.stream("market", "breakdown"),
        world.streams.stream("market", "repair"),
        max_admissions_per_day=world.capacity_per_day,
    )
    world.month_repairs += world.fleet.repairs_done_total - before
    state["repairs_this_month"] = world.month_repairs
    for device_id in sorted(world.fleet.states):
        log.emit(
            "device_day",
            business_date=business_date,
            device_id=device_id,
            stall_no=world.device_stall[device_id],
            state=world.fleet.states[device_id],
        )
    log.emit("device_state", business_date=business_date, total=len(world.fleet.states), **state)
    return state
