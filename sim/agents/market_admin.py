"""市场方 Agent：**考核口径决定维护预算**（`T-SIM-05`；`docs/sim-design.md` §3.4）。

## 要复现的已知现象

调研结论里反复出现的一组现象是"**设备装了但坏着、长期不修**"：
绵阳 = **可用但不用**（商户用回自己的秤，结论 6）、武汉 = **黑屏不可用**（结论 4/5）。
本模块要复现的不是"设备会坏"（那是 `sim/env/devices.py` 的事），而是**为什么坏着不修**。

机制（本模块的核心，必须能被单独观测）：

```
维护的边际 KPI 产出 = ∂KPI/∂维护
  考核口径 = 装机量 → ∂KPI/∂维护 = 0（修不修都不影响"装了多少台"）⇒ 维护预算 → 0
  考核口径 = 使用率 → ∂KPI/∂维护 > 0（设备停摆会直接拉低使用率）⇒ 维护预算 > 0
```

于是"**同一市场、同一批设备、只是考核口径不同**"会得到**相反**的维护行为 —— 这就是**激励错配**。

## 决策迟滞是必需的，不是修饰

调研里的时间线是"**2020 立项 → 2022 才被报道闲置**"。若市场方当期就按观测值调整预算，
模型会在几期内就纠正过来，**复现不出"坏设备长期存在"**。故预算调整一律经
`decision_delay` 期才生效（`T-DECIDE` → `T+delay` 生效），且**必须留无迟滞的对照组**
（`tests/sim/test_market_admin.py` 里有这条对照）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 考核口径
KPI_INSTALLED = "installed"  # 装机量：装了就算达标
KPI_USAGE = "usage"  # 使用率：设备不可用会拉低它

KPI_MODES = (KPI_INSTALLED, KPI_USAGE)


@dataclass(frozen=True)
class MarketAdminParams:
    kpi: str
    budget_initial_cents: float
    budget_min_cents: float
    kappa: float
    growth_cap: float
    decision_delay: int
    maintenance_floor_share: float

    def __post_init__(self) -> None:
        if self.kpi not in KPI_MODES:
            raise ValueError(f"未登记的考核口径：{self.kpi!r}（允许 {list(KPI_MODES)}）")
        if self.budget_initial_cents < 0:
            raise ValueError(f"初始预算不能为负：{self.budget_initial_cents}")
        if self.budget_min_cents < 0:
            raise ValueError(f"保底预算不能为负：{self.budget_min_cents}")
        if self.decision_delay < 0:
            raise ValueError(f"决策迟滞不能为负（期）：{self.decision_delay}")
        if not 0.0 <= self.maintenance_floor_share <= 1.0:
            raise ValueError(f"维护保底占比必须在 [0,1]：{self.maintenance_floor_share}")
        if self.growth_cap <= 0:
            raise ValueError(f"单期调整上限必须为正：{self.growth_cap}")


def market_admin_params(params, **overrides) -> MarketAdminParams:
    base = {
        "kpi": str(params.value("market_kpi_metric")),
        "budget_initial_cents": float(params.value("market_budget_initial_cents")),
        "budget_min_cents": float(params.value("market_budget_min_cents")),
        "kappa": float(params.value("market_budget_adjust_kappa")),
        "growth_cap": float(params.value("market_budget_growth_cap")),
        "decision_delay": int(params.value("market_decision_delay_periods")),
        "maintenance_floor_share": float(params.value("market_maintenance_floor_share")),
    }
    unknown = set(overrides) - set(base)
    if unknown:
        raise KeyError(f"未知的市场方参数覆盖项：{sorted(unknown)}")
    base.update(overrides)
    return MarketAdminParams(**base)


def marginal_kpi_return(kpi: str, *, installed_ratio: float, usage_rate: float) -> tuple[float, float]:
    """返回 `(扩容的边际 KPI 产出, 维护的边际 KPI 产出)`。**纯函数** —— 激励错配的全部分歧在这里。

    * `installed`：扩容有产出（还能多装），**维护恒为 0**（修不修都不改变"装了多少台"）；
    * `usage`：两者都有产出 —— 维护的产出就是"多一台可用设备 ⇒ 使用率上升"。
    """
    if kpi == KPI_INSTALLED:
        expansion = max(0.0, 1.0 - installed_ratio)  # 还没装够才有产出
        maintenance = 0.0  # ← 激励错配的根源：维护不产出这个 KPI
        return expansion, maintenance
    if kpi == KPI_USAGE:
        # 扩容只在使用率已经很高（容量不足）时才有边际产出；维护的产出与"停摆比例"同向
        expansion = max(0.0, usage_rate - 0.9) / 0.1
        maintenance = max(0.0, 1.0 - usage_rate)
        return expansion, maintenance
    raise ValueError(f"未登记的考核口径：{kpi!r}")


def allocate_budget(
    budget_cents: float, mp: MarketAdminParams, *, installed_ratio: float, usage_rate: float
) -> tuple[float, float]:
    """把预算分成 `(扩容, 维护)` 两份：按边际 KPI 产出**比例**分配，再套用维护保底占比。

    分配规则刻意简单（**按边际产出线性分摊**）：复杂的分配规则会让"到底是哪一条机制在起作用"
    变得不可辨认，而本项的全部价值就在于**这条机制能被单独看到**。
    """
    if budget_cents < 0:
        raise ValueError(f"预算不能为负：{budget_cents}")
    expansion_return, maintenance_return = marginal_kpi_return(
        mp.kpi, installed_ratio=installed_ratio, usage_rate=usage_rate
    )
    total_return = expansion_return + maintenance_return
    if total_return <= 0.0:
        # 两个方向都没有 KPI 产出 ⇒ 只留保底（保底 = 0 时维护为 0，设备就此开始积压）
        floor = budget_cents * mp.maintenance_floor_share
        return budget_cents - floor, floor
    maintenance = budget_cents * maintenance_return / total_return
    floor = budget_cents * mp.maintenance_floor_share
    maintenance = max(maintenance, floor)
    return budget_cents - maintenance, maintenance


@dataclass
class MarketAdminAgent:
    """市场方。状态量（预算、待生效决策、历史观测）**可快照**。"""

    mp: MarketAdminParams
    budget_cents: float = 0.0
    maintenance_share: float = 0.0
    maintenance_budget_cents: float = 0.0
    pending: list[tuple[int, float]] = field(default_factory=list)
    applied_periods: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.budget_cents == 0.0:
            self.budget_cents = self.mp.budget_initial_cents

    def _kpi_value(self, *, installed_ratio: float, usage_rate: float) -> float:
        """当期观测到的考核值。`installed` 口径**只看装了多少**，与设备是否可用无关。"""
        return installed_ratio if self.mp.kpi == KPI_INSTALLED else usage_rate

    def step(self, period: int, *, installed_ratio: float, usage_rate: float) -> dict:
        """走一期：① 结算到期的迟滞决策 → ② 按当期观测算出**下一期**预算（挂进队列）。"""
        applied: float | None = None
        due = [item for item in self.pending if item[0] <= period]
        self.pending = [item for item in self.pending if item[0] > period]
        if due:
            # 同一期到期的多条决策按登记顺序依次生效（顺序固定 ⇒ 可复现）
            for _due_period, value in sorted(due):
                applied = value
            self.budget_cents = applied

        expansion, maintenance = allocate_budget(
            self.budget_cents, self.mp, installed_ratio=installed_ratio, usage_rate=usage_rate
        )
        self.maintenance_share = maintenance / self.budget_cents if self.budget_cents > 0 else 0.0
        self.maintenance_budget_cents = maintenance

        kpi_value = self._kpi_value(installed_ratio=installed_ratio, usage_rate=usage_rate)
        adjustment = max(-self.mp.growth_cap, min(self.mp.growth_cap, self.mp.kappa * (1.0 - kpi_value)))
        # **必须取 max(保底, 乘法增长)**：纯乘法更新在预算为 0（或极低）时**永远涨不回来**
        # （`0 × (1+adj) = 0`），模型会锁死在一个"注定不修"的状态里 —— 那是建模伪影，不是机制。
        # 现实里运维经费有财政保底，故用 `budget_min_cents` 兜住。
        next_budget = max(self.mp.budget_min_cents, self.budget_cents * (1.0 + adjustment))
        # **生效期 = period + max(1, decision_delay)**，那条 `max(1, …)` 是刻意的、不是笔误：
        # 决策发生在**观测之后**，所以哪怕零迟滞，也最快只能在**下一期**生效 ——
        # 没有任何机制能在同一期里先看到结果再据此改变已发生的支出。
        # 我第一版写的是 `period + decision_delay`，于是 `delay=0` 实测"首次生效期 = 1"，
        # 与 `decision_delay` 的字面含义对不上（鬼影 off-by-one）。现在把它**显式化**：
        # 位移量恒为 `max(1, decision_delay)`，测试直接断言这个位移量。
        effective_period = period + max(1, self.mp.decision_delay)
        self.pending.append((effective_period, next_budget))

        record = {
            "period": period,
            "kpi": self.mp.kpi,
            "kpi_value": round(kpi_value, 6),
            "budget_cents": round(self.budget_cents, 6),
            "expansion_cents": round(expansion, 6),
            "maintenance_cents": round(maintenance, 6),
            "maintenance_share": round(self.maintenance_share, 6),
            "applied_this_period": None if applied is None else round(applied, 6),
            "pending_count": len(self.pending),
        }
        self.applied_periods.append(record)
        return record

    def snapshot(self) -> dict:
        return {
            "kpi": self.mp.kpi,
            "budget_cents": round(self.budget_cents, 6),
            "maintenance_share": round(self.maintenance_share, 6),
            "pending": [[p, round(v, 6)] for p, v in self.pending],
            "periods": len(self.applied_periods),
        }


def repair_capacity(maintenance_budget_cents: float, repair_cost_cents: float) -> int:
    """维护预算 → **每天能开工几台**。预算为 0 ⇒ 0 台 ⇒ 报修队列永不消化（"设备闲置"）。"""
    if repair_cost_cents <= 0:
        raise ValueError(f"单次维修成本必须为正：{repair_cost_cents}")
    if maintenance_budget_cents <= 0:
        return 0
    return int(maintenance_budget_cents // repair_cost_cents)


def run_maintenance_loop(
    params,
    *,
    kpi: str,
    periods: int,
    seed: int,
    decision_delay: int | None = None,
    initial_broken: int = 0,
    mtbf_days: float | None = None,
    initial_budget_cents: float | None = None,
) -> list[dict]:
    """**闭环**：市场方预算 → 工台产能 → 设备可用率 → 使用率 → 回到市场方的观测。

    这条闭环就是本项要展示的东西：**KPI 口径一变，同一批设备的命运完全相反**。

    `initial_broken`（**关键实验设计**）：起始就把 N 台设备置为"待修"。
    为什么必须这样起手，而不是等随机故障自己出现：MTBF = 540 天、10 台设备、60 期的期望故障数
    只有约 1.1 次 —— 我第一版就是这么跑的，结果四个组合**全都是"从未有设备坏过"**，
    机制一次都没被触发（`报修非空 = 0/55`），于是"复现激励错配"根本没验到。
    现实里"坏设备长期存在"本来就是**多年累积**的结果（绵阳/武汉都是项目跑了几年之后被报道的），
    故从"已经有 N 台待修"起手才是有意义的初值，而不是取巧。

    `mtbf_days`：可选覆盖，用于在短周期内提高故障率（敏感性实验用）。
    """
    from ..core.streams import StreamSet
    from ..env.devices import DeviceFleet, REPAIRING, WAITING, WORKING

    overrides = {"kpi": kpi}
    if decision_delay is not None:
        overrides["decision_delay"] = decision_delay
    if initial_budget_cents is not None:
        overrides["budget_initial_cents"] = initial_budget_cents
    mp = market_admin_params(params, **overrides)
    fleet = DeviceFleet(
        total=int(params.value("device_count")),
        mtbf_days=float(mtbf_days if mtbf_days is not None else params.value("device_mtbf_days")),
        repair_mean_days=float(params.value("repair_mean_days")),
    )
    if not 0 <= initial_broken <= len(fleet.states):
        raise ValueError(f"初始待修台数非法：{initial_broken}（机队 {len(fleet.states)} 台）")
    for device_id in sorted(fleet.states)[:initial_broken]:
        fleet.states[device_id] = WAITING

    admin = MarketAdminAgent(mp)
    repair_cost = float(params.value("repair_cost_cents"))
    streams = StreamSet(seed)

    trace: list[dict] = []
    for period in range(periods):
        counts = fleet.counts()
        installed_ratio = 1.0  # 装机目标一开始就完成（这才是"激励错配"暴露出来的前提）
        usage_rate = counts[WORKING] / max(1, len(fleet.states))
        record = admin.step(period, installed_ratio=installed_ratio, usage_rate=usage_rate)
        capacity = repair_capacity(admin.maintenance_budget_cents, repair_cost)
        fleet_state = fleet.step_day(
            streams.stream("market", "breakdown"),
            streams.stream("market", "repair"),
            max_admissions_per_day=capacity,
        )
        trace.append(
            {
                **record,
                "repair_capacity": capacity,
                "working": fleet_state["working"],
                "waiting": fleet_state["waiting"],
                "repairing": fleet_state["repairing"],
                "admitted": fleet_state["admitted"],
                "repaired": fleet_state["repaired"],
            }
        )
    return trace


def periods_until_cleared(trace: list[dict], *, target_waiting: int = 0) -> int | None:
    """报修队列**首次**降到 `target_waiting` 的期号；`None` = 期限内从未降到。

    用于把"迟滞"变成可断言的**位移量**，而不是"更慢"这种含糊说法。
    """
    for record in trace:
        if record["waiting"] <= target_waiting:
            return record["period"]
    return None


def queue_persistence(trace: list[dict], *, ignore_first: int = 5) -> dict:
    """由明细复算"坏设备长期存在"的程度（**不是让代码自己说没问题**）。

    `waiting_periods`：报修队列非空的期数；`max_waiting`：队列峰值；
    `working_end`：末期可用设备数。`ignore_first` 跳过起始暂态。
    """
    tail = trace[ignore_first:]
    return {
        "waiting_periods": sum(1 for r in tail if r["waiting"] > 0),
        "max_waiting": max((r["waiting"] for r in tail), default=0),
        "working_end": trace[-1]["working"] if trace else 0,
        "maintenance_share_end": trace[-1]["maintenance_share"] if trace else 0.0,
        "periods": len(tail),
    }
