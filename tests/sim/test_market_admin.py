"""`T-SIM-05` 市场方：**激励错配**、**决策迟滞**、以及"坏设备长期存在"的复现与对照。

三项判据都对应调研里的**已知现象**（不是我们设计的理想行为）：

* 绵阳 = **可用但不用**、武汉 = **黑屏不可用**（结论 6/4/5）→ 考核口径 = 装机量时维护占比为 0；
* 时间线 **2020 立项 → 2022 才被报道闲置**（结论 4/5/6）→ 必须有决策迟滞，否则模型几期就自我纠正；
* 故"坏设备长期存在"**没有迟滞就复现不出来** —— 本文件保留了无迟滞的对照组。

判据函数写成**纯函数**，合成负例可直接喂它 —— `T-036` 家族规矩：不验证灵敏度的验证是摆设。
"""

from __future__ import annotations

import pytest

from sim_support import PARAMS_PATH

from sim.agents.market_admin import (
    KPI_INSTALLED,
    KPI_USAGE,
    MarketAdminAgent,
    allocate_budget,
    marginal_kpi_return,
    market_admin_params,
    periods_until_cleared,
    queue_persistence,
    repair_capacity,
    run_maintenance_loop,
)
from sim.core.params import load_params

PERIODS = 40
SEED = 2026
#: 实测得到的"极低初始预算"（10000 分 < 单次维修 30000 分 ⇒ 产能 0，必须等预算调整）
LOW_BUDGET = 10_000.0
INITIAL_BROKEN = 4


@pytest.fixture(scope="module")
def params():
    return load_params(PARAMS_PATH)


# ---------------------------------------------------------------------------
# 判据函数（纯函数，便于合成负例）
# ---------------------------------------------------------------------------
def misalignment_violations(by_kpi: dict[str, float]) -> list[str]:
    """激励错配的**判定**：装机量口径下维护占比必须为 0，使用率口径下必须 > 0。"""
    problems: list[str] = []
    if by_kpi.get(KPI_INSTALLED, None) != 0.0:
        problems.append(f"装机量口径下维护占比应为 0，实际 {by_kpi.get(KPI_INSTALLED)}（激励错配未被复现）")
    if not by_kpi.get(KPI_USAGE, 0.0) > 0.0:
        problems.append(f"使用率口径下维护占比应 > 0，实际 {by_kpi.get(KPI_USAGE)}")
    return problems


def delay_displacement_violations(observed: dict[int, int | None]) -> list[str]:
    """迟滞的**判定**：`decision_delay = D` 时首次生效期必须恰为 `max(1, D)`。

    断言**位移量**而不是"更慢" —— "更慢"没法证伪。
    """
    problems: list[str] = []
    for delay, first_change in sorted(observed.items()):
        expected = max(1, delay)
        if first_change != expected:
            problems.append(f"迟滞 {delay} 的首次生效期应为 {expected}，实际 {first_change}")
    return problems


# ---------------------------------------------------------------------------
# ① 激励错配（同一市场状态，只有考核口径不同）
# ---------------------------------------------------------------------------
def test_marginal_returns_differ_only_by_kpi():
    """同一状态（装机已完成、使用率 0.6）下，两种口径的**维护边际产出**相反。"""
    installed_expansion, installed_maint = marginal_kpi_return(
        KPI_INSTALLED, installed_ratio=1.0, usage_rate=0.6
    )
    usage_expansion, usage_maint = marginal_kpi_return(KPI_USAGE, installed_ratio=1.0, usage_rate=0.6)
    assert installed_maint == 0.0, "装机量口径下维护不应有任何 KPI 产出（这正是错配根源）"
    assert usage_maint > 0.0, "使用率口径下维护必须有产出"
    assert installed_expansion == 0.0, "装机已完成 ⇒ 扩容也没有边际产出"
    assert usage_expansion == 0.0, "使用率 0.6 远低于 0.9 的扩容门槛"
    print(f"[T-SIM-05] 边际产出：装机量(扩容 {installed_expansion}, 维护 {installed_maint})；"
          f"使用率(扩容 {usage_expansion}, 维护 {usage_maint:.3f})")


def test_budget_allocation_reproduces_incentive_misalignment(params):
    """**核心判据**：同一预算、同一状态，考核口径不同 ⇒ 维护占比 `0` vs `> 0`。"""
    budget = float(params.value("market_budget_initial_cents"))
    shares: dict[str, float] = {}
    for kpi in (KPI_INSTALLED, KPI_USAGE):
        mp = market_admin_params(params, kpi=kpi)
        _expansion, maintenance = allocate_budget(
            budget, mp, installed_ratio=1.0, usage_rate=0.6
        )
        shares[kpi] = maintenance / budget
    assert misalignment_violations(shares) == []
    print(f"[T-SIM-05] 激励错配复现：装机量口径维护占比 {shares[KPI_INSTALLED]:.3f}、"
          f"使用率口径 {shares[KPI_USAGE]:.3f}（同一预算 {budget:,.0f} 分）")


def test_repair_capacity_goes_to_zero_without_maintenance_budget():
    """预算 → 0 ⇒ 工台**每天开班 0 台** ⇒ 报修队列永不消化（"设备闲置"的机制形态）。"""
    assert repair_capacity(0.0, 30_000.0) == 0
    assert repair_capacity(29_999.0, 30_000.0) == 0
    assert repair_capacity(30_000.0, 30_000.0) == 1
    print("[T-SIM-05] 产能换算：预算 < 单次维修成本 ⇒ 0 台/期（队列开始积压）")


# ---------------------------------------------------------------------------
# ② 决策迟滞：断言位移量
# ---------------------------------------------------------------------------
def test_decision_delay_shifts_effect_by_exactly_the_delay(params):
    """`decision_delay = D` 时首次生效期**恰好**是 `max(1, D)`（位移量，不是"更慢"）。"""
    observed: dict[int, int | None] = {}
    for delay in (0, 1, 3, 6):
        trace = run_maintenance_loop(
            params, kpi=KPI_USAGE, periods=PERIODS, seed=SEED, decision_delay=delay,
            initial_broken=INITIAL_BROKEN, initial_budget_cents=LOW_BUDGET,
        )
        observed[delay] = next((r["period"] for r in trace if r["applied_this_period"] is not None), None)
    assert delay_displacement_violations(observed) == []
    print(f"[T-SIM-05] 迟滞位移：{ {d: f'第 {p} 期' for d, p in sorted(observed.items())} }")


# ---------------------------------------------------------------------------
# ③ "坏设备长期存在"：复现 + 对照组（没有迟滞就复现不出来）
# ---------------------------------------------------------------------------
def test_broken_devices_persist_forever_under_installed_kpi(params):
    """装机量口径：报修队列**整段期限都不消化**、累计修好 0 台 —— 复现"坏设备长期存在"。"""
    trace = run_maintenance_loop(
        params, kpi=KPI_INSTALLED, periods=PERIODS, seed=SEED, decision_delay=6,
        initial_broken=INITIAL_BROKEN, initial_budget_cents=LOW_BUDGET,
    )
    summary = queue_persistence(trace, ignore_first=0)
    assert summary["waiting_periods"] == summary["periods"], "装机量口径下队列不该被消化过"
    assert periods_until_cleared(trace) is None, "队列不该清空"
    assert sum(r["repaired"] for r in trace) == 0, "装机量口径下不该修好任何一台"
    assert summary["working_end"] < int(params.value("device_count")), "可用设备应长期少于装机数"
    print(f"[T-SIM-05] 装机量口径复现：队列 {summary['waiting_periods']}/{summary['periods']} 期非空、"
          f"累计修好 0 台、末期可用 {summary['working_end']}/{int(params.value('device_count'))} 台")


def test_no_delay_controls_show_the_phenomenon_needs_the_delay(params):
    """**对照组**：使用率口径下，迟滞**直接**变成"设备停摆期数"（位移量 == 停摆期数）。

    这正是"**没有迟滞就复现不出坏设备长期存在**"的量化形式：`delay=0` 只停摆 1 期，
    `delay=6` 停摆 6 期；而装机量口径无论迟滞多少都是**永久**停摆（上一用例）。
    """
    persistence: dict[int, int | None] = {}
    for delay in (0, 3, 6):
        trace = run_maintenance_loop(
            params, kpi=KPI_USAGE, periods=PERIODS, seed=SEED, decision_delay=delay,
            initial_broken=INITIAL_BROKEN, initial_budget_cents=LOW_BUDGET,
        )
        persistence[delay] = periods_until_cleared(trace)
    assert persistence[0] == 1, f"零迟滞应仅停摆 1 期，实际 {persistence[0]}"
    for delay in (3, 6):
        assert persistence[delay] == delay, (
            f"迟滞 {delay} 的停摆期数应等于 {delay}，实际 {persistence[delay]} —— "
            "停摆期数没跟上迟滞，说明迟滞没有真正作用在维护上"
        )
    print(f"[T-SIM-05] 对照：停摆期数 = { {d: p for d, p in sorted(persistence.items())} }（与迟滞一一对应）")


def test_queue_persistence_is_recomputed_from_details(params):
    """"长期存在"的程度由**逐期明细复算**，不是让模型自己报一个结论。"""
    trace = run_maintenance_loop(
        params, kpi=KPI_INSTALLED, periods=PERIODS, seed=SEED, decision_delay=6,
        initial_broken=INITIAL_BROKEN, initial_budget_cents=LOW_BUDGET,
    )
    manual = sum(1 for r in trace if r["waiting"] > 0)
    assert manual == queue_persistence(trace, ignore_first=0)["waiting_periods"]
    assert max(r["waiting"] for r in trace) == INITIAL_BROKEN
    print(f"[T-SIM-05] 逐期复算一致：队列非空 {manual} 期、峰值 {INITIAL_BROKEN} 台")


# ---------------------------------------------------------------------------
# ④ 状态可快照 / 可复现
# ---------------------------------------------------------------------------
def test_snapshot_and_reproducibility(params):
    kwargs = dict(kpi=KPI_USAGE, periods=12, seed=99, decision_delay=3,
                  initial_broken=2, initial_budget_cents=LOW_BUDGET)
    first = run_maintenance_loop(params, **kwargs)
    second = run_maintenance_loop(params, **kwargs)
    assert first == second, "同 seed 两次运行结果不一致"

    agent = MarketAdminAgent(market_admin_params(params, kpi=KPI_USAGE, decision_delay=3))
    agent.step(0, installed_ratio=1.0, usage_rate=0.5)
    snapshot = agent.snapshot()
    for key in ("kpi", "budget_cents", "maintenance_share", "pending", "periods"):
        assert key in snapshot
    assert snapshot["pending"], "挂起的迟滞决策应出现在快照里"
    print(f"[T-SIM-05] 可复现 + 快照含挂起决策：{snapshot['pending']}")


# ---------------------------------------------------------------------------
# 灵敏度负例
# ---------------------------------------------------------------------------
def test_misalignment_checker_flags_a_fixed_share():
    """**灵敏度负例**：把装机量口径下的维护占比改成非 0（掩盖错配）→ 必红。"""
    good = {KPI_INSTALLED: 0.0, KPI_USAGE: 0.5}
    assert misalignment_violations(good) == []
    problems = misalignment_violations({KPI_INSTALLED: 0.05, KPI_USAGE: 0.5})
    assert problems and "激励错配未被复现" in problems[0], problems
    assert misalignment_violations({KPI_INSTALLED: 0.0, KPI_USAGE: 0.0}) != []
    print(f"[T-SIM-05] 错配判据负例判红：{problems[0]}")


def test_delay_checker_flags_wrong_displacement():
    """**灵敏度负例**：生效期位移量不对（例如迟滞没生效、或差一期）→ 必红。"""
    assert delay_displacement_violations({0: 1, 3: 3, 6: 6}) == []
    problems = delay_displacement_violations({6: 1})
    assert problems and "首次生效期应为 6" in problems[0], problems
    print(f"[T-SIM-05] 迟滞判据负例判红：{problems[0]}")
