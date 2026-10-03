"""model 适配器：**进程内"记账世界"**（`T-SIM-06`；`docs/sim-design.md` §2.2/§3/§4）。

## 语义边界（`T-SIM-08` 按语义拆分后的分工，`Q-16` 裁定"按语义拆分、不放宽阈值"）

| 模块 | 负责 |
| --- | --- |
| `world_setup.py` | **建世界**：市场 / 机队 / 消费者 / 摊位状态（`StallState` / `World` / `build_world`） |
| 本模块 `model_adapter.py` | **纯函数判据**（`adopt_decision` 等）+ **一次运行的编排**（`run_scenario` / `read_events`） |
| `day_loop.py` | **日频**事实生产（到达 → 选摊 → 一笔成交 → 信任更新 → 设备推进） |
| `month_loop.py` | **月频**结构决策（商户选动作 / 市场方分预算 / 监管抽检 / 现金流 / 期初开账） |

`StallState` / `World` / `build_world` 在本模块**同名转出**（既有调用方与
`day_loop` / `month_loop` 的相对导入一行不改）—— 这正是「拆文件而不改行为」该有的样子。

## 它替换掉了什么

`T-SIM-01` 的骨架运行只证明"分流 + 落盘 + 可复现"，它的 agent 叫 `skeleton-*`、
事件类型叫 `skeleton_step`，并**自报** `layer="skeleton" / skeleton_only=true`。
本模块把 `--mode=model` 的**集成运行**接上：**真跑四个 Agent**（商户 / 消费者 / 市场方 / 监管），
`layer="model-adapter"`，指标一律由事件流复算（`sim/observe/metrics.py`），**不另存一份状态**。

> 骨架运行**没有被删掉**（`cli.py` 的缺省路径仍是它）：`T-SIM-01` 的验收①（同 seed 逐字节一致）
> 与"新增 agent 不平移他人随机数"两条判据直接断言 `skeleton_step` 的存在，
> 那是已验收的判据，**不为新功能而改**。集成运行由 `--scenario=S0..S6`（或 `--integrated`）触发。

## 一次运行里四类 Agent 各干什么（频率分层见设计 §4.1）

| 频率 | 谁 | 干什么 |
| --- | --- | --- |
| 日 | 环境 | 客流（时段块泊松）→ 消费者到达 → 选摊（logit）→ 一笔成交（走秤 / 私下） |
| 日 | 设备 | 故障 → 报修队列 → 工台（**产能由维护预算决定**，`T-SIM-05`） |
| 月 | 商户 | 上期实现流水 → 效用四项 → softmax 选 `comply`/`evade` → Q/EWMA 更新 → 破线则退出（吸收态） |
| 月 | 市场方 | 考核口径 → 边际 KPI 产出 → 预算分配（带决策迟滞）→ 工台产能 |
| 月 | 监管 | 抽检（不放回）→ 发现 → 罚金（只给序关系，不给绝对阈值） |
| 月 | 消费者 | 逐笔信任更新（扫码见真信息 / 空壳 / 未扫码衰减）→ 月末汇总出分布 |

## 口径纪律（本模块的硬约束，不是注释装饰）

1. **`commission_rate_bp` 的 2% 不是实测费率**（只出现在结论 21 的标题对照口径）——
   任何引用它的结论都必须带这句；
2. **无数据的参数（MTBF / 罚款 / 扫码基线 / 信任权重 / 退出阈值）只允许序关系与区间不重叠**，
   绝不在无数据处给绝对阈值；
3. **`R6` 反例（商户自费 ⇒ 必须被弃用）保留并执行**：`S5_device_funding.json` 的
   `self_funded` 臂就是它。它的结论由 `adopt_decision`（意愿通道，设计 §4.3 的
   `merchant.wants_price_list` + A-18 的 w0..w5）与 `merchant_device_share_cents`（财务通道）
   共同决定 —— **两条通道谁在起作用必须能分开看**，否则"自费被弃用"就只是一个被写死的结论。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.clock import DAYS_PER_MONTH
from ..core.events import EventLog
from ..core.params import Params, with_overrides
from .world_setup import StallState, World, build_world  # `T-SIM-08` 按语义搬出；此处转出
from .scenario import (  # noqa: F401  (场景 API 仍从本模块统一导出)
    SCENARIOS_DIR,
    ScenarioError,
    arm_flags,
    load_scenario,
    merged_overrides,
    scenario_files,
    scenario_param_problems,
    scenario_problems,
)

LAYER = "model-adapter"

#: 场景目录（`docs/sim-design.md` §2.2）
SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"

#: 计价单位（品类计量单位 500g；与 `sim/env/market.py` 的 `CATEGORIES` 同源）
UNIT_GRAMS = 500


# ---------------------------------------------------------------------------
# 纯函数：参与判据（`R6` 的机制就落在这三个函数上，故它们可被单独喂负例）
# ---------------------------------------------------------------------------
def adopt_decision(*, upfront_cents: float, monthly_net_benefit_cents: float, cost_weight: float, horizon_months: float) -> bool:
    """**纯函数**：一次性设备支出是否值得承受 ⇒ 商户是否"愿意装、愿意用"（设计 §4.3 的意愿通道）。

    判据（**关于参数的条件句，不是事实陈述**）：

    ```
    承受得起  ⟺  cost_weight × upfront_cents  ≤  monthly_net_benefit_cents × horizon_months
                      ↑ 被感知的一次性支出            ↑ 这笔钱在忍耐期内能挣回的净收益
    ```

    * `cost_weight`（`merchant_adoption_cost_weight`，A-18 的 w0..w5）**没有出处**：=1 表示按字面金额，
      取值越大表示越"舍不得掏这笔钱"（损失厌恶）。**`R6` 成立与否几乎全由它决定** —— 故 `R6` 的
      结论只能是"当该权重超过 k 时自费组显著更差"，而不是"自费就该被弃用"。
    * `horizon_months` 逐摊异质（每个摊位抽一次，A-18）⇒ "谁先不用"是可观测的**分布**，不是一个全群一致的阈值。
    * 市场方出资 ⇒ `upfront_cents = 0` ⇒ 恒采用（这正是结论 14「免费 + 统一检定维修 → 愿意用」的形态）。
    """
    if upfront_cents <= 0:
        return True
    if monthly_net_benefit_cents <= 0:
        return False
    return cost_weight * upfront_cents <= monthly_net_benefit_cents * horizon_months


def short_weight_share_of(*, propensity_share: float, on_scale_probability: float, evade_share: float) -> float:
    """**纯函数**：任意一笔成交"短秤"的概率（仅用于报告里的口径解释与负例）。

    短秤来自两路：**转暗**（私下交易，本来就绕开系统）与**走秤但被"作弊倾向"污染**。
    若只有前者，`M-12`（八两秤发生率）会退化成 `M-05`（转暗份额）的同义词 —— 那正是
    "看起来合理但其实什么都没测"的形态（`Q-19` 教训的同类）。故两路都必须存在。
    """
    return evade_share + (1.0 - evade_share) * propensity_share * on_scale_probability


def stall_choice_utility(*, relative_price: float, trust: float, price_sensitivity: float) -> float:
    """**纯函数**：消费者对一个摊位的效用（价格相对分位 + 对它的信任）。

    `relative_price` 是该摊位均价相对同品类均价的**相对偏离**（负数 = 更便宜）。
    """
    return trust - price_sensitivity * relative_price


# ---------------------------------------------------------------------------
# 一次集成运行
# ---------------------------------------------------------------------------
def run_scenario(
    params: Params,
    *,
    scenario_id: str,
    overrides: dict | None = None,
    days: int = 360,
    seed: int = 20261002,
    start: str = "2026-10-01",
    out_dir: Path | str | None = None,
    self_funded: bool = False,
    events_name: str = "events.jsonl",
) -> dict:
    """跑一个场景（一个臂）：落 `events.jsonl`，指标**由事件流复算**后返回。"""
    from .day_loop import advance_devices, emit_price_lists, walk_day
    from .month_loop import close_month, decide_month_start, open_period

    overrides = dict(overrides or {})
    scoped = with_overrides(params, overrides) if overrides else params
    world = build_world(scoped, days=days, seed=seed, start=start)
    world.self_funded = bool(self_funded)
    world.self_funded_upfront_cents = float(scoped.value("merchant_self_funded_scale_cents")) if self_funded else 0.0
    world.self_funded_share_cents = (
        float(scoped.value("merchant_self_funded_scale_cents")) / float(scoped.value("device_amortization_months"))
        + float(scoped.value("merchant_cloud_software_cents_per_year")) / 12.0
        if self_funded
        else float(scoped.value("merchant_device_share_cents"))
    )
    world.refund_rng = world.streams.stream("market", "repair")
    world.adjust_rng = world.streams.stream("market", "adapt")
    target = Path(out_dir) if out_dir else Path("data") / "sim" / f"{scenario_id}-{seed}"
    target.mkdir(parents=True, exist_ok=True)
    for stall in world.stalls.values():
        stall.current_action = None

    usage_rate = float(scoped.value("scale_use_baseline_rate"))
    with EventLog(target / events_name) as log:
        log.emit(
            "run_started",
            mode="model",
            layer=LAYER,
            scenario=scenario_id,
            seed=seed,
            days=days,
            start=start,
            stalls=len(world.stalls),
            products=len(world.market.products),
            devices=world.fleet.total,
            consumers=len(world.consumer_ids),
            blocks=[b.name for b in world.clock.blocks],
            overrides={k: v for k, v in sorted(overrides.items())},
            self_funded=bool(self_funded),
            initial_volume_cents=round(world.initial_volume_cents, 6),
            #: **让事件流自解释**：第三方拿着 events.jsonl 就能复算全部指标，不必再要参数文件；
            #: 出处类型一并落盘，故"这个数是有出处还是假设"在报告里不会丢。
            effective_params={key: scoped.value(key) for key in sorted(scoped.parameters())},
            param_provenance={key: scoped.kind(key) for key in sorted(scoped.parameters())},
        )
        month_totals = open_period(world, day_index=0, day_count=days)
        for day_index, business_date, month_end, quarter_end in world.clock.iter_days():
            period = day_index // DAYS_PER_MONTH
            if day_index % DAYS_PER_MONTH == 0:
                month_totals = open_period(world, day_index=day_index, day_count=days,
                                           month_totals=month_totals, usage_rate=usage_rate,
                                           decide=decide_month_start, log=log, business_date=business_date,
                                           period=period)
            log.emit("day_opened", day_index=day_index, business_date=business_date, month_index=period)
            price_today = emit_price_lists(world, log, business_date)
            advance_devices(world, log, business_date)
            day_totals = walk_day(world, log, business_date, day_index)
            commission_today = day_totals["amount"] * world.commission_rate_bp / 10000.0 if world.commission_charged_to_merchant else 0.0
            log.emit(
                "model_daily",
                business_date=business_date,
                scale_txns=day_totals["on_scale"],
                gross_amount_cents=day_totals["amount"],
                commission_cents=round(commission_today, 6),
                settlement_cents=round(day_totals["amount"] - commission_today, 6),
                usage_numerator=day_totals["stalls_used"],
                usage_denominator=price_today["active"],
                cash_numerator=day_totals["cash"],
                cash_denominator=day_totals["on_scale"],
                price_numerator=price_today["complete"],
                price_denominator=price_today["active"],
            )
            log.emit("day_closed", day_index=day_index, business_date=business_date, month_end=month_end, quarter_end=quarter_end)
            if month_end:
                usage_rate = close_month(world, log, business_date, period, month_totals)
        log.emit(
            "run_finished",
            days=days,
            active_end=sum(1 for s in world.stalls.values() if s.active),
            exits_total=sum(1 for s in world.stalls.values() if s.exits_period is not None),
        )

    from ..observe.metrics import compute_all

    events = read_events(target / events_name)
    metrics = compute_all(events)
    return {"scenario": scenario_id, "overrides": overrides, "self_funded": self_funded, "out_dir": str(target), "metrics": metrics}


def read_events(path: Path | str) -> list[dict]:
    """读事件明细（第三方复算脚本与指标模块共用同一个入口）。"""
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
