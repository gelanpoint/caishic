"""**世界构造**：把 `params.json` 的取值搭成一个可运行的世界（`T-SIM-08` 从
`model_adapter.py` 按语义搬出；`quality-gates.md` §1.2 的 400 行门禁，`Q-16` 裁定按语义拆分、
不放宽阈值、不删注释凑行数）。

搬动理由：`model_adapter.py` 原本同时装了三样正交的东西 ——
**纯函数判据**（`adopt_decision` 等）、**世界构造**（本模块）、**运行编排**（`run_scenario`）。
本模块只做第二样，取值一律来自 `params`，不写任何数值常量。

搬动是**纯移动**：函数体逐字保留，注释里的来历一并带走；`model_adapter.py` 保留同名转出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..agents.consumer import ConsumerParams, consumer_params
from ..agents.market_admin import MarketAdminAgent, market_admin_params
from ..agents.merchant import MerchantAgent, merchant_params
from ..agents.regulator import RegulatorAgent, regulator_params
from ..core.clock import DAYS_PER_MONTH, Block, SimClock
from ..core.params import Params
from ..core.streams import StreamSet
from ..env.devices import DeviceFleet
from ..env.market import build_market

#: 计价单位（品类计量单位 500g；与 `sim/env/market.py` 的 `CATEGORIES` 同源）
UNIT_GRAMS = 500


@dataclass
class StallState:
    """一个摊位的运行时状态（**全部事实都会落进事件流**，故这里不持有任何"指标"）。"""

    stall_no: str
    category_id: str
    device_id: str
    merchant: MerchantAgent
    adopted: bool = False
    active: bool = True
    horizon_months: float = 12.0
    short_propensity: bool = False
    price_book: dict[str, int] = field(default_factory=dict)
    prev_volume_cents: float = 0.0
    prev_scale_txns: int = 0
    exits_period: int | None = None
    current_action: str | None = None
    #: 本期"私下交易到底做不做得到"（`S6` 的堵死开关 `evade_feasibility`）。
    #: `True` = 照旧（想私下就私下）；`False` = 本期被出口查验/扫码覆盖堵死 ⇒ 想私下也私不成。
    evade_feasible: bool = True
    upfront_cents: float = 0.0
    self_funded_share_cents: float = 0.0


def _stall_prices(market, params, streams: StreamSet) -> dict[str, dict[str, int]]:
    """每个摊位自己的一份价目表（含±10% 的摊位级差价），**故价格能区分摊位**。

    为什么不能用品类级的基准价：同品类摊位若价格相同，消费者选摊的价格项就是常数 ——
    `M-13`/`S4`（价格公示的拉动）与"比价"这条机制会静默失效。
    """
    out: dict[str, dict[str, int]] = {}
    for stall in market.stalls:
        rng = streams.stream(f"price-{stall.stall_no}", "price")
        out[stall.stall_no] = {
            product.product_id: max(1, int(round(product.base_price_cents * rng.uniform(0.9, 1.1))))
            for product in market.products_of(stall.category_id)
        }
    return out


def _avg_amount_cents(market, price_books: dict[str, dict[str, int]], avg_grams: float) -> float:
    """单笔金额的量级估计（用于月初"上期流水"的初值，**不是硬编码的魔法数**）。"""
    total, count = 0.0, 0
    for prices in price_books.values():
        for unit_price in prices.values():
            total += unit_price * avg_grams / UNIT_GRAMS
            count += 1
    return total / max(1, count)


# ---------------------------------------------------------------------------
# 世界状态（**只放"事实"**；指标一律由事件流复算，故这里没有 metrics 字段）
# ---------------------------------------------------------------------------
@dataclass
class World:
    params: Params
    streams: StreamSet
    clock: SimClock
    market: object
    fleet: DeviceFleet
    cp: ConsumerParams
    merchant_base: object
    admin_params: object
    regulator: RegulatorAgent
    admin: MarketAdminAgent
    stalls: dict[str, StallState] = field(default_factory=dict)
    consumer_ids: list[str] = field(default_factory=list)
    consumer_rng: object = None
    initial_volume_cents: float = 0.0
    trust: dict = field(default_factory=dict)
    visited: set = field(default_factory=set)
    actions: dict = field(default_factory=dict)
    device_stall: dict = field(default_factory=dict)
    category_mean_price: dict = field(default_factory=dict)
    capacity_per_day: int = 0
    daily_arrivals: float = 0.0
    avg_basket_grams: float = 2000.0
    choice_temperature: float = 0.2
    price_sensitivity: float = 1.0
    cash_payment_share: float = 0.7
    short_on_scale_probability: float = 0.5
    short_ratio: float = 0.2
    refund_probability: float = 0.0
    price_adjust_probability: float = 0.0
    adoption_cost_weight: float = 1.0
    #: `S6`：私下交易**可行性**（0 = 完全堵死，1 = 不堵）。设计 §5 的 `feas(evade)`。
    #: ⚠️ 第一版**根本没有读它**（只在 OAT 键表里出现过）⇒ `S6-①堵死` 与 `S6-②不堵` 的结果
    #: 逐位相同，"堵死"这条对照实验什么也没测（`test_scenario_varying_keys_are_actually_read` 是守卫）。
    evade_feasibility: float = 1.0
    commission_rate_bp: float = 0.0
    commission_charged_to_merchant: bool = True
    self_funded: bool = False
    self_funded_upfront_cents: float = 0.0
    self_funded_share_cents: float = 0.0
    refund_rng: object = None
    adjust_rng: object = None
    #: 月内累加器：它们只用于**落月度事实事件**，不是指标来源（指标从 `txn` 明细复算）
    month_by_stall: dict = field(default_factory=dict)
    month_on_scale: int = 0
    month_amount: int = 0
    month_commission_cents: float = 0.0
    month_buyer_fee_cents: float = 0.0
    month_scanned: int = 0
    month_short: int = 0
    month_repairs: int = 0


def build_world(params: Params, *, days: int, seed: int, start: str) -> World:
    """按参数搭好世界（市场 / 机队 / 消费者 / 摊位）。**取值一律来自 `params`**。"""
    from datetime import date

    clock = SimClock(
        start=date.fromisoformat(start),
        days=days,
        blocks=tuple(Block(name, start_at, intensity) for name, start_at, intensity in params.blocks()),
    )
    streams = StreamSet(seed)
    market = build_market(params, streams)
    fleet = DeviceFleet(
        total=int(params.value("device_count")),
        mtbf_days=float(params.value("device_mtbf_days")),
        repair_mean_days=float(params.value("repair_mean_days")),
    )
    mp = merchant_params(params)
    admin_params = market_admin_params(params)
    world = World(
        params=params,
        streams=streams,
        clock=clock,
        market=market,
        fleet=fleet,
        cp=consumer_params(params),
        merchant_base=mp,
        admin_params=admin_params,
        regulator=RegulatorAgent(regulator_params(params)),
        admin=MarketAdminAgent(admin_params),
        daily_arrivals=float(params.value("daily_arrivals_per_market")),
        avg_basket_grams=float(params.value("avg_basket_grams")),
        choice_temperature=float(params.value("consumer_stall_choice_temperature")),
        price_sensitivity=float(params.value("consumer_price_sensitivity")),
        cash_payment_share=float(params.value("cash_payment_share")),
        short_on_scale_probability=float(params.value("short_weight_on_scale_probability")),
        short_ratio=float(params.value("short_weight_ratio")),
        refund_probability=float(params.value("refund_probability")),
        price_adjust_probability=float(params.value("price_adjust_probability")),
        adoption_cost_weight=float(params.value("merchant_adoption_cost_weight")),
        evade_feasibility=float(params.value("evade_feasibility")),
        commission_rate_bp=float(params.value("commission_rate_bp")),
        commission_charged_to_merchant=bool(params.value("commission_charged_to_merchant")),
    )
    price_books = _stall_prices(market, params, streams)
    device_ids = sorted(fleet.states)
    for index, stall in enumerate(market.stalls):
        device_id = device_ids[index % len(device_ids)]
        stall_rng = streams.stream(stall.stall_no, "adapt")
        low, high = params.value("merchant_adoption_horizon_months_range")
        world.stalls[stall.stall_no] = StallState(
            stall_no=stall.stall_no,
            category_id=stall.category_id,
            device_id=device_id,
            merchant=MerchantAgent(stall.stall_no, mp),
            horizon_months=stall_rng.uniform(float(low), float(high)),
            short_propensity=stall_rng.random() < float(params.value("short_weight_propensity_share")),
            price_book=price_books[stall.stall_no],
        )
        world.device_stall[device_id] = stall.stall_no
    consumer_count = int(params.value("consumer_agent_count"))
    world.consumer_ids = [f"c-{i + 1:04d}" for i in range(consumer_count)]
    world.consumer_rng = streams.stream("consumers", "choice")
    for consumer_id in world.consumer_ids:
        for stall_no in world.stalls:
            world.trust[(consumer_id, stall_no)] = 0.5
    for stall in market.stalls:
        prices = [p.base_price_cents for p in market.products_of(stall.category_id)]
        world.category_mean_price[stall.category_id] = sum(prices) / max(1, len(prices))
    world.initial_volume_cents = (
        world.daily_arrivals / max(1, len(world.stalls)) * DAYS_PER_MONTH * _avg_amount_cents(market, price_books, world.avg_basket_grams)
    )
    return world


# ---------------------------------------------------------------------------

