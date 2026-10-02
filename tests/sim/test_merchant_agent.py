"""`T-SIM-03` 商户 Agent：机制链的**单项可观测性** + 序关系判据（带灵敏度负例）。

判据分两类（口径见 `sim/agents/merchant.py`）：

* **恒等式类**（佣金只按走秤流水计、`ΔΠ = V·(1−g)·d`）→ 允许精确断言；
* **行为类**（三路径占比怎么随参数变）→ **只允许序关系**，禁止绝对月份/比率。

判据函数写成**纯函数**，合成负例可直接喂它 —— `T-036` 家族规矩：不验证灵敏度的验证是摆设。
"""

from __future__ import annotations

import pytest

from sim_support import PARAMS_PATH

from sim.agents.merchant import (
    COMPLY,
    EVADE,
    EXIT,
    MerchantAgent,
    merchant_params,
    merchant_terms,
    simulate_population,
    summarize,
    sweep_commission_and_margin,
    sweep_commission_rate,
    sweep_detect_rate,
)
from sim.core.params import load_params

VOLUME_CENTS = 14_000_000.0  # 14 万元/期（结论 18 的量级锚，标为推算）
SCALE_TXNS = 1000


@pytest.fixture(scope="module")
def params():
    return load_params(PARAMS_PATH)


@pytest.fixture(scope="module")
def mp(params):
    return merchant_params(params)


# ---------------------------------------------------------------------------
# 判据函数（纯函数，便于合成负例）
# ---------------------------------------------------------------------------
def monotonicity_violations(series, key, direction: str) -> list[str]:
    """检查 `series` 里 `key` 是否按 `direction`（`"non_decreasing"` / `"non_increasing"`）单调。

    **只比序关系**：无出处的参数下给绝对阈值等于把假设包装成事实。
    """
    if direction not in ("non_decreasing", "non_increasing"):
        raise ValueError(f"未支持的方向：{direction!r}")
    problems: list[str] = []
    for prev, curr in zip(series, series[1:]):
        a, b = prev[key], curr[key]
        violated = (b < a - 1e-9) if direction == "non_decreasing" else (b > a + 1e-9)
        if violated:
            problems.append(f"{key} 在 {prev.get('label', '?')} → {curr.get('label', '?')} 违反 {direction}：{a} → {b}")
    return problems


def commission_channel_violations(terms_by_action: dict[str, dict[str, float]], *, rate_bp: float, volume_cents: float) -> list[str]:
    """检查"佣金**只**按走秤流水计"这条通道：合规含佣金、转暗不含。

    期望值由 `rate_bp` 与规模**算出来**，不是抄一个数 —— 否则参数一改检查就失效。
    """
    expected_commission = volume_cents * rate_bp / 10000.0
    problems: list[str] = []
    comply, evade = terms_by_action[COMPLY], terms_by_action[EVADE]
    # 合规成本 = 佣金 + 设备分摊 + 单笔运营费；转暗成本 = 仅设备分摊
    comply_extra = comply["cost"] - evade["cost"]
    if comply_extra < expected_commission - 1e-6:
        problems.append(f"合规比转暗多出的成本 {comply_extra} 小于应收佣金 {expected_commission}")
    if evade["penalty"] == 0.0 or comply["penalty"] != 0.0:
        problems.append("罚金通道不对：合规应为 0、转暗应 > 0")
    if evade["trust_loss"] == 0.0 or comply["trust_loss"] != 0.0:
        problems.append("声誉通道不对：合规应为 0、转暗应 > 0")
    return problems


# ---------------------------------------------------------------------------
# 恒等式类
# ---------------------------------------------------------------------------
def test_short_weight_gain_identity_is_exact(mp):
    """`ΔΠ = V·(1−g)·d` **精确**成立（这是机制链的算术内核，不允许有偏差）。"""
    comply = merchant_terms(COMPLY, mp, volume_cents=VOLUME_CENTS, scale_txn_count=SCALE_TXNS)
    evade = merchant_terms(EVADE, mp, volume_cents=VOLUME_CENTS, scale_txn_count=SCALE_TXNS)
    expected = VOLUME_CENTS * (1.0 - mp.gross_margin_rate) * mp.short_weight_ratio
    assert evade.revenue - comply.revenue == pytest.approx(expected, abs=1e-6)
    print(f"[T-SIM-03] 恒等式 ΔΠ = V·(1−g)·d = {expected:,.0f} 分（精确成立）")


def test_commission_is_charged_only_on_on_scale_flow(mp):
    """**核心耦合**：佣金只按走秤流水计 ⇒ 抽佣同时让合规变贵、让转暗相对更划算。"""
    comply = merchant_terms(COMPLY, mp, volume_cents=VOLUME_CENTS, scale_txn_count=SCALE_TXNS)
    evade = merchant_terms(EVADE, mp, volume_cents=VOLUME_CENTS, scale_txn_count=SCALE_TXNS)
    terms = {COMPLY: comply.as_dict(), EVADE: evade.as_dict()}
    assert commission_channel_violations(terms, rate_bp=mp.commission_rate_bp, volume_cents=VOLUME_CENTS) == []
    # 佣金规模 = V·rate
    commission = VOLUME_CENTS * mp.commission_rate_bp / 10000.0
    print(
        f"[T-SIM-03] 合规成本含佣金 {commission:,.0f} 分、转暗成本仅设备分摊 "
        f"{mp.device_share_cents:,.0f} 分（差额 = 抽佣造成的相对劣势）"
    )


def test_charging_the_buyer_removes_the_merchant_side_commission(params):
    """`charge_to_merchant=false`（温州式向买方收）⇒ 商户侧佣金项为 0。"""
    buyer_pays = merchant_params(params, commission_charged_to_merchant=False)
    comply = merchant_terms(COMPLY, buyer_pays, volume_cents=VOLUME_CENTS, scale_txn_count=SCALE_TXNS)
    baseline = merchant_terms(COMPLY, merchant_params(params), volume_cents=VOLUME_CENTS, scale_txn_count=SCALE_TXNS)
    assert comply.cost < baseline.cost, "改向买方收之后商户成本应下降"
    assert comply.cost == pytest.approx(buyer_pays.device_share_cents + buyer_pays.op_fee_per_txn_cents * SCALE_TXNS)
    print(f"[T-SIM-03] 温州式（向买方收）⇒ 商户合规成本 {comply.cost:,.0f} 分（不含佣金）")


def test_exit_action_is_absorbing_and_snapshot_has_state(mp):
    """退出是吸收态；状态量（Q / EWMA / 破线计数）必须可快照。"""
    import random

    agent = MerchantAgent("m-test", mp)
    agent.exited = True
    assert agent.choose(random.Random(1)) == EXIT
    snapshot = agent.snapshot()
    for key in ("agent_id", "q", "ewma_utility", "breach_streak", "exited", "last_action", "periods_observed"):
        assert key in snapshot, f"快照缺少 {key}"
    print(f"[T-SIM-03] 退出为吸收态；快照字段齐全 {sorted(snapshot)}")


def test_unit_mismatch_defect_is_guarded(params):
    """**回归守卫**：Q 用"每元效用"、退出判据用"绝对分/期"，两者**不能混用**。

    我第一版把两处都写成"每元"，于是拿 `0.2` 去比 `1_000_000`，**所有商户第 2 期就全部退出**
    （三路径退化成一条），而且没有任何报错。本用例把"量纲正确"钉住：
    基线参数下（毛利率 25%）**不应出现任何退出**，且 Q 值必须是"每元"量级（|Q| < 1）。
    """
    steps = simulate_population(
        merchant_params(params), agents=20, periods=12, seed=5,
        volume_cents=VOLUME_CENTS, scale_txn_count=SCALE_TXNS,
    )
    summary = summarize(steps)
    exit_share = summary["final_share_ratio"].get(EXIT, 0.0)
    assert exit_share == 0.0, f"基线（毛利率 25%）不应出现退出，实际 {exit_share} —— 量纲可能又混了"
    agent = MerchantAgent("m-q", merchant_params(params))
    import random

    for _ in range(10):
        agent.step(random.Random(3), volume_cents=VOLUME_CENTS, scale_txn_count=SCALE_TXNS)
    assert all(abs(v) < 1.0 for v in agent.q.values()), f"Q 值不在『每元』量级：{agent.q}"
    print(f"[T-SIM-03] 量纲守卫通过：Q={ {k: round(v, 4) for k, v in agent.q.items()} }（每元），退出占比 0")


# ---------------------------------------------------------------------------
# 行为类：只给序关系
# ---------------------------------------------------------------------------
def test_commission_rate_pushes_merchants_off_book(params):
    """抽佣率↑ ⇒ **合规占比不升、转暗占比不降**（`Q1` 的机制，只给序关系）。"""
    rows = sweep_commission_rate(
        params, [0, 100, 200, 400, 600, 1000], agents=40, periods=24, seed=20261002,
        volume_cents=VOLUME_CENTS, scale_txn_count=SCALE_TXNS,
    )
    series = [
        {"label": f"{r['commission_rate_bp']:.0f}bp", "comply": r["final_share_ratio"].get(COMPLY, 0.0),
         "evade": r["final_share_ratio"].get(EVADE, 0.0)}
        for r in rows
    ]
    assert monotonicity_violations(series, "comply", "non_increasing") == []
    assert monotonicity_violations(series, "evade", "non_decreasing") == []
    print("[T-SIM-03] 抽佣率扫描：" + " ".join(f"{s['label']}→合规{s['comply']:.3f}" for s in series))
    print(
        f"[T-SIM-03] 机制幅度：合规占比从 {series[0]['comply']:.3f} 降到 {series[-1]['comply']:.3f}"
        "（**弱**：在基线假设下短秤增益远大于佣金，故抽佣主要不是靠费用把人逼走，而是靠相对价格）"
    )


def test_detect_rate_suppresses_evasion(params):
    """短秤检测概率↑ ⇒ **转暗占比不升**（`Q2` 的机制：堵死八两秤）。"""
    rows = sweep_detect_rate(
        params, [0.0, 0.05, 0.2, 0.5, 1.0], agents=40, periods=24, seed=20261002,
        volume_cents=VOLUME_CENTS, scale_txn_count=SCALE_TXNS,
    )
    series = [
        {"label": f"p={r['p_detect']:.2f}", "evade": r["final_share_ratio"].get(EVADE, 0.0),
         "comply": r["final_share_ratio"].get(COMPLY, 0.0)}
        for r in rows
    ]
    assert monotonicity_violations(series, "evade", "non_increasing") == []
    print("[T-SIM-03] 检测率扫描：" + " ".join(f"{s['label']}→转暗{s['evade']:.3f}" for s in series))


def test_exit_region_is_the_low_margin_corner(params):
    """**`Q1` 的可证伪回答**：退出只出现在**低毛利率角**，高毛利下任何费率都不出现退出。

    这是**区域陈述**（无数据的参数只给区域/序关系），不是"多久流失"的点预测。
    实测：毛利率 ≥ 0.20 时退出占比恒为 0；毛利率 0.05 时任何费率都已有退出。
    """
    rows = sweep_commission_and_margin(
        params, [0, 200, 600, 1000], [0.05, 0.10, 0.15, 0.20, 0.25],
        agents=40, periods=24, seed=20261002, volume_cents=VOLUME_CENTS, scale_txn_count=SCALE_TXNS,
    )
    high_margin = [r for r in rows if r["gross_margin_rate"] >= 0.20]
    low_margin = [r for r in rows if r["gross_margin_rate"] == 0.05]
    assert all(r["exit_share"] == 0.0 for r in high_margin), "高毛利下不应出现退出"
    assert all(r["exit_share"] > 0.0 for r in low_margin), "低毛利下应出现退出"
    print("[T-SIM-03] 退出区域：毛利率 ≥0.20 全部 0；毛利率 0.05 全部 >0（区域陈述，非点预测）")
    # 反直觉但可解释：低毛利下抽佣**降低**退出占比（把商户推向私下交易这条活路）
    low_by_rate = sorted(low_margin, key=lambda r: r["commission_rate_bp"])
    print(
        "[T-SIM-03] 低毛利角内的方向："
        + " ".join(f"{r['commission_rate_bp']:.0f}bp→退出{r['exit_share']:.3f}" for r in low_by_rate)
        + "（抽佣把商户推向转暗，反而**减少**退出 —— 这正是 Q1/Q2 的耦合）"
    )


def test_same_seed_is_reproducible(params):
    """同 seed ⇒ 三路径家数与均值逐项一致（复现是后续所有对照实验的地基）。"""
    kwargs = dict(agents=20, periods=12, seed=424242, volume_cents=VOLUME_CENTS, scale_txn_count=SCALE_TXNS)
    first = summarize(simulate_population(merchant_params(params), **kwargs))
    second = summarize(simulate_population(merchant_params(params), **kwargs))
    assert first == second
    print(f"[T-SIM-03] 同 seed 复现：三路径家数 {first['final_shares']}")


# ---------------------------------------------------------------------------
# 灵敏度负例
# ---------------------------------------------------------------------------
def test_monotonicity_checker_flags_reversed_series():
    """**灵敏度负例**：把合规占比序列反过来 → 必红。"""
    good = [{"label": "a", "comply": 0.3}, {"label": "b", "comply": 0.2}, {"label": "c", "comply": 0.1}]
    assert monotonicity_violations(good, "comply", "non_increasing") == []
    problems = monotonicity_violations(list(reversed(good)), "comply", "non_increasing")
    assert problems, "反向序列未被判红"
    print(f"[T-SIM-03] 序关系负例判红：{problems[0]}")


def test_commission_channel_checker_flags_wrong_channel():
    """**灵敏度负例**：把佣金算到**转暗**头上（即"按全部流水抽佣"）→ 通道判据必红。"""
    good = {
        COMPLY: {"cost": 315_000.0, "penalty": 0.0, "trust_loss": 0.0},
        EVADE: {"cost": 30_000.0, "penalty": 102_500.0, "trust_loss": 210_000.0},
    }
    assert commission_channel_violations(good, rate_bp=200, volume_cents=VOLUME_CENTS) == []
    broken = {COMPLY: dict(good[COMPLY]), EVADE: {"cost": 310_000.0, "penalty": 0.0, "trust_loss": 0.0}}
    problems = commission_channel_violations(broken, rate_bp=200, volume_cents=VOLUME_CENTS)
    assert len(problems) >= 2, f"佣金通道被改坏却没被判红：{problems}"
    print(f"[T-SIM-03] 佣金通道负例判红（{len(problems)} 条）：{problems[0]}")
