"""`T-SIM-05` 监管 Agent：抽检 → 发现 → 罚金（只给序关系）。

## 为什么只允许序关系

抽检率**没有本情形的公开数据**：结论 4 的"10%"是**验收**抽检（区级验收后市里按 10% 抽检**验收**），
与"日常会不会被查到短秤"不是同一个量；结论 2 的 2 万元罚的是**市场开办者**、事由是**明码标价**。
故本文件**不设任何绝对阈值**，只断言"抽检率↑ ⇒ 发现数不降"这类**序关系**。

判据函数写成**纯函数**，合成负例可直接喂它 —— `T-036` 家族规矩：不验证灵敏度的验证是摆设。
"""

from __future__ import annotations

import pytest

from sim_support import PARAMS_PATH

from sim.agents.regulator import RegulatorAgent, regulator_params, sweep_inspection_rate
from sim.core.params import load_params

PERIODS = 30
SEED = 4242
STALLS = 40
OFFENDERS = 12


@pytest.fixture(scope="module")
def params():
    return load_params(PARAMS_PATH)


def monotonicity_violations(series, key, direction: str) -> list[str]:
    """检查 `series` 里 `key` 是否按 `direction` 单调（**只比序关系**）。"""
    if direction not in ("non_decreasing", "non_increasing"):
        raise ValueError(f"未支持的方向：{direction!r}")
    problems: list[str] = []
    for prev, curr in zip(series, series[1:]):
        a, b = prev[key], curr[key]
        violated = (b < a - 1e-9) if direction == "non_decreasing" else (b > a + 1e-9)
        if violated:
            problems.append(f"{key} 在 {prev['label']} → {curr['label']} 违反 {direction}：{a} → {b}")
    return problems


def test_inspection_rate_raises_findings(params):
    """抽检率↑ ⇒ 累计发现数**不降**（序关系；不给绝对阈值）。"""
    rows = sweep_inspection_rate(
        params, [0.0, 0.05, 0.1, 0.25, 0.5, 1.0],
        periods=PERIODS, seed=SEED, stalls_in_scope=STALLS, offenders=OFFENDERS,
    )
    series = [{"label": f"{r['inspection_rate']:.2f}", "findings_total": r["findings_total"]} for r in rows]
    assert monotonicity_violations(series, "findings_total", "non_decreasing") == []
    print("[T-SIM-05] 抽检率扫描（累计发现）：" + " ".join(f"{s['label']}→{s['findings_total']}" for s in series))


def test_full_coverage_finds_all_offenders(params):
    """抽检率 = 100% 时，当期发现数**必须等于**违规摊位数（这是可精确断言的一条）。"""
    rp = regulator_params(params, inspection_rate=1.0)
    agent = RegulatorAgent(rp)
    import random

    record = agent.inspect_period(random.Random(1), stalls_in_scope=STALLS, offenders=OFFENDERS, period=0)
    assert record["inspected"] == STALLS
    assert record["findings"] == OFFENDERS
    assert record["fine_short_cents"] == pytest.approx(OFFENDERS * rp.fine_short_cents)
    assert record["fine_operator_cents"] == pytest.approx(rp.fine_market_operator_cents)
    print(f"[T-SIM-05] 全量抽检：发现 {record['findings']}/{OFFENDERS}、"
          f"商户罚金 {record['fine_short_cents']:,.0f} 分、开办者罚金 {record['fine_operator_cents']:,.0f} 分")


def test_zero_offenders_yields_zero_findings_and_no_operator_fine(params):
    """无违规 ⇒ 0 发现、**不罚开办者**（罚开办者的条件是"场内确有违规"，不是"抽检过"）。"""
    agent = RegulatorAgent(regulator_params(params, inspection_rate=1.0))
    import random

    record = agent.inspect_period(random.Random(2), stalls_in_scope=STALLS, offenders=0, period=0)
    assert record["findings"] == 0
    assert record["fine_operator_cents"] == 0.0
    print("[T-SIM-05] 无违规 ⇒ 0 发现、开办者罚金 0（罚开办者的条件被正确约束）")


def test_sampling_is_without_replacement(params):
    """抽检是**不放回**的：同一期不会把同一个摊位抽两次（否则发现数会被算高）。"""
    agent = RegulatorAgent(regulator_params(params, inspection_rate=0.5))
    import random

    record = agent.inspect_period(random.Random(3), stalls_in_scope=100, offenders=50, period=0)
    assert record["inspected"] == 50, f"50% × 100 摊应为 50 个不同摊位，实际 {record['inspected']}"
    total_inspected = sum(
        r["inspected"]
        for r in (
            agent.inspect_period(random.Random(3 + i), stalls_in_scope=100, offenders=50, period=i)
            for i in range(5)
        )
    )
    assert total_inspected == 250
    print(f"[T-SIM-05] 不放回抽样：每期恰抽 50 个不同摊位（{PERIODS} 期口径一致）")


def test_snapshot_and_reproducibility(params):
    """状态可快照、同 seed 可复现。"""
    def run():
        agent = RegulatorAgent(regulator_params(params, inspection_rate=0.2))
        import random

        rng = random.Random(77)
        for period in range(10):
            agent.inspect_period(rng, stalls_in_scope=STALLS, offenders=OFFENDERS, period=period)
        return agent.snapshot()

    assert run() == run(), "同 seed 两次运行结果不一致"
    snapshot = run()
    for key in ("inspection_rate", "inspected_total", "findings_total", "fines_short_cents", "periods"):
        assert key in snapshot
    print(f"[T-SIM-05] 监管可复现 + 快照：{snapshot}")


# ---------------------------------------------------------------------------
# 灵敏度负例
# ---------------------------------------------------------------------------
def test_monotonicity_checker_flags_reversed_series():
    """**灵敏度负例**：把发现数序列反过来 → 必红。"""
    good = [{"label": "a", "findings_total": 1}, {"label": "b", "findings_total": 5}]
    assert monotonicity_violations(good, "findings_total", "non_decreasing") == []
    problems = monotonicity_violations(list(reversed(good)), "findings_total", "non_decreasing")
    assert problems, "反向序列未被判红"
    print(f"[T-SIM-05] 序关系负例判红：{problems[0]}")


def test_illegal_inputs_fail_loudly(params):
    """非法输入必须**明确报错**（违规数超过摊位总数、抽检率越界），不许静默截断。"""
    agent = RegulatorAgent(regulator_params(params, inspection_rate=0.5))
    import random

    with pytest.raises(ValueError):
        agent.inspect_period(random.Random(4), stalls_in_scope=10, offenders=11, period=0)
    with pytest.raises(ValueError):
        regulator_params(params, inspection_rate=1.5)
    print("[T-SIM-05] 非法输入（违规数 > 摊位总数、抽检率 1.5）均明确报错")
