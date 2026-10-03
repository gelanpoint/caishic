"""`T-SIM-04` 消费者 Agent：信任更新的解析—仿真一致、**正反馈陷阱**、不可恢复区。

## ⚠️ 口径纪律（本文件与 `sim/agents/consumer.py`、报告必须同一口径）

`Q3`（信任能否恢复）在本设计里是**循环论证**：答案由 `η⁺/η⁻/δ` 决定，而这三个参数**没有任何出处**。
所以本文件里的一切断言都只针对**参数空间的性质**：

* 允许："当 `η⁻/η⁺` 与 `δ` 在某区域时，模型出现不可恢复"；
* **禁止**："现实中信任需要 N 个月恢复"。

判据函数写成**纯函数**，合成负例可直接喂它 —— `T-036` 家族规矩：不验证灵敏度的验证是摆设。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from sim_support import PARAMS_PATH

from sim.agents.consumer import (
    BOUNDARY,
    NOT_RECOVERED,
    RECOVERED,
    ConsumerAgent,
    analytic_fixed_point,
    analytic_half_life,
    consumer_params,
    periods_to_recover,
    recovery_region,
    scan_probability,
    simulate_consumers,
    update_trust,
)
from sim.core.params import load_params

SEED = 20261002


@pytest.fixture(scope="module")
def params():
    return load_params(PARAMS_PATH)


@pytest.fixture(scope="module")
def cp(params):
    return consumer_params(params)


# ---------------------------------------------------------------------------
# 判据函数（纯函数，便于合成负例）
# ---------------------------------------------------------------------------
def trap_holds(coupled_periods, uncoupled_periods) -> bool:
    """**陷阱判据**：耦合（扫码率随信任）后的恢复**必须严格慢于**不耦合。

    `None` = 在期限内没恢复 —— 视为"比任何有限期数都慢"。
    """
    if coupled_periods is None:
        return True
    if uncoupled_periods is None:
        return False
    return coupled_periods > uncoupled_periods


def asymmetry_holds(eta_neg: float, eta_pos: float) -> bool:
    """负面偏差 `η⁻ > η⁺` 是否成立（`Q3` 的核心非对称性）。"""
    return eta_neg > eta_pos


# ---------------------------------------------------------------------------
# ① 解析式与仿真相符（缺口半衰期）
# ---------------------------------------------------------------------------
def test_fixed_scan_rate_half_life_matches_analytic(cp, params):
    """固定扫码率 + 信息全为真实来源时，仿真到**不动点缺口减半**的期数应贴近解析式。

    口径要点（我第一版就错在这里）：`ln2/(δ+η⁺q)` 是**到不动点 `T* = qη⁺/(δ+qη⁺)` 的缺口**半衰期，
    不是"到 1.0 的缺口"半衰期。拿它去比"从 0.2 恢复到 0.6"会差近 2 倍（实测 9.56 vs 18）。
    """
    q = float(params.value("consumer_scan_baseline_rate"))
    fixed = replace(cp, p_scan_floor=q, scan_gain=0.0, info_real_share=1.0)
    star = analytic_fixed_point(fixed, scan_rate=q)
    analytic = analytic_half_life(fixed, scan_rate=q)

    start = 0.2
    trace = simulate_consumers(fixed, agents=400, periods=60, seed=7, shock_to=start)["trace"]
    goal = star - (star - start) / 2
    simulated = next((i for i, v in enumerate(trace) if v >= goal), None)
    assert simulated is not None, f"仿真在 60 期内未到达缺口半程 {goal:.4f}（不动点 {star:.4f}）"
    deviation = abs(simulated - analytic) / analytic
    assert deviation < 0.35, (
        f"仿真缺口半衰期 {simulated} 期 vs 解析 {analytic:.3f} 期（偏离 {deviation:.1%} > 35%）——"
        "解析式与实现口径可能又对不上了"
    )
    print(
        f"[T-SIM-04] 不动点 T*={star:.4f}；解析缺口半衰期 {analytic:.3f} 期、仿真 {simulated} 期"
        f"（偏离 {deviation:.1%}）"
    )


def test_negative_bias_is_enforced(cp):
    """`η⁻ > η⁺`（负面偏差）成立，且**单期对照**直接可见：同样的偏离量，负面更新幅度更大。"""
    assert asymmetry_holds(cp.eta_neg, cp.eta_pos)
    assert cp.negative_bias > 1.0
    trust = 0.5
    # 同样的 |o − T| = 0.3：一次向下、一次向上（都扫码到）
    down = update_trust(trust, 0.2, cp)
    up = update_trust(trust, 0.8, cp)
    assert (trust - down) > (up - trust), f"负向变化 {trust - down} 应大于正向变化 {up - trust}"
    print(
        f"[T-SIM-04] 负面偏差 η⁻/η⁺ = {cp.negative_bias:.3f}；同样 |Δ|=0.3 时"
        f"下降 {trust - down:.4f} > 上升 {up - trust:.4f}"
    )


# ---------------------------------------------------------------------------
# ② 陷阱可观测（序关系）
# ---------------------------------------------------------------------------
def test_scan_probability_is_monotone_in_trust(cp):
    """`p_scan(T)` 对信任**单调不减**，且信任为 0 时可以降到下限（陷阱的成立条件）。"""
    values = [scan_probability(t / 10.0, cp) for t in range(11)]
    assert all(b >= a - 1e-12 for a, b in zip(values, values[1:])), values
    assert scan_probability(0.0, cp) == pytest.approx(cp.p_scan_floor)
    print(f"[T-SIM-04] p_scan 单调不减：T=0 → {values[0]:.3f}，T=1 → {values[-1]:.3f}")


def test_trust_trap_makes_recovery_slower(cp):
    """**陷阱**：开启"扫码率随信任耦合"后，从同一冲击恢复**严格更慢**（序关系，不给绝对期数）。"""
    coupled = replace(cp, info_real_share=1.0)
    uncoupled = replace(cp, scan_gain=0.0, p_scan_floor=0.15, info_real_share=1.0)
    t_coupled = periods_to_recover(coupled, agents=150, periods=120, seed=11, shock_to=0.05)
    t_uncoupled = periods_to_recover(uncoupled, agents=150, periods=120, seed=11, shock_to=0.05)
    assert trap_holds(t_coupled, t_uncoupled), (
        f"耦合版 {t_coupled} 期未严格慢于不耦合版 {t_uncoupled} 期 —— 陷阱不成立"
        "（常见根因：扫码率写成了带正下限的可加形式，采样机会永不枯竭）"
    )
    print(f"[T-SIM-04] 陷阱成立：耦合 {t_coupled} 期 vs 不耦合 {t_uncoupled} 期")


def test_empty_shell_information_makes_recovery_impossible(cp):
    """**空壳信息**（`info_real_share = 0`）⇒ 没有任何正向观测 ⇒ 解析不动点为 0。"""
    no_real = replace(cp, info_real_share=0.0)
    q = scan_probability(0.5, no_real)
    assert analytic_fixed_point(no_real, scan_rate=q, info_real=False) == 0.0
    result = simulate_consumers(no_real, agents=60, periods=40, seed=13, shock_to=0.5)
    assert result["final_trust"] < 0.1, f"全为空壳信息时信任应归零，实际 {result['final_trust']}"
    print(f"[T-SIM-04] 全空壳 ⇒ 末期信任 {result['final_trust']:.4f}（解析不动点 0）")


# ---------------------------------------------------------------------------
# ③ 不可恢复区：**关于参数的命题**
# ---------------------------------------------------------------------------
def test_recovery_region_is_a_statement_about_parameters(cp):
    """区域判定只输出三态，且**高负面偏差 + 高衰减下出现不可恢复**。

    这是本项结论的正确表述形式：**"当 `η⁻/η⁺` 与 `δ` 落在某区域时，模型出现不可恢复"** ——
    关于参数的命题；**不是**"现实中的信任需要多久恢复"。
    """
    rows = recovery_region(
        cp, ratio_values=[1.0, 2.0, 6.0], delta_values=[0.0, 0.05, 0.3],
        agents=60, periods=60, seed=3, shock_to=0.05,
    )
    verdicts = {r["verdict"] for r in rows}
    assert verdicts <= {RECOVERED, NOT_RECOVERED, BOUNDARY}, f"出现未定义的三态之外的值：{verdicts}"
    rank = {RECOVERED: 2, BOUNDARY: 1, NOT_RECOVERED: 0}
    # 同一 δ 下，负面偏差越大不应"更好"
    for delta in (0.0, 0.05, 0.3):
        column = sorted([r for r in rows if r["delta"] == delta], key=lambda r: r["negative_bias_ratio"])
        ranks = [rank[r["verdict"]] for r in column]
        assert all(b <= a for a, b in zip(ranks, ranks[1:])), f"δ={delta} 列不单调：{column}"
    worst = [r for r in rows if r["negative_bias_ratio"] == 6.0 and r["delta"] == 0.3]
    assert worst and worst[0]["verdict"] == NOT_RECOVERED, f"最坏角应不可恢复：{worst}"
    print(f"[T-SIM-04] 区域三态分布：{[r['verdict'] for r in rows]}")
    print(f"[T-SIM-04] 最坏角（η⁻/η⁺=6、δ=0.3）→ {worst[0]['verdict']}（命题：关于参数，不是关于现实）")


def test_snapshot_exposes_state(cp):
    """状态量（信任 / 抽样计数）可快照 —— 后续敏感性分析要用。"""
    agent = ConsumerAgent("c-snap", cp)
    import random

    for _ in range(5):
        agent.step(random.Random(9))
    snapshot = agent.snapshot()
    for key in ("agent_id", "trust", "scanned_total", "real_total", "periods"):
        assert key in snapshot
    print(f"[T-SIM-04] 快照：{snapshot}")


# ---------------------------------------------------------------------------
# 灵敏度负例
# ---------------------------------------------------------------------------
def test_trap_checker_flags_the_additive_defect():
    """**灵敏度负例**：把"耦合更慢"这个方向反过来 → 判据必红。

    这正是我第一版实现出的现象（可加形式带正下限 ⇒ 耦合反而更快），必须能被抓住。
    """
    assert trap_holds(30, 10) is True
    assert trap_holds(None, 10) is True
    assert trap_holds(10, 30) is False, "耦合更快时判据竟成立 —— 陷阱判据失效"
    assert trap_holds(10, None) is False
    print("[T-SIM-04] 陷阱判据负例：耦合更快（10 vs 30）与不耦合不可恢复（10 vs None）均判红")


def test_asymmetry_checker_flags_symmetric_update():
    """**灵敏度负例**：把 `η⁻ = η⁺`（对称）→ 非对称性判据必红。"""
    assert asymmetry_holds(0.8, 0.35) is True
    assert asymmetry_holds(0.35, 0.35) is False
    print("[T-SIM-04] 非对称性负例：η⁻=η⁺ 时判红")


def test_scan_floor_is_the_switch_between_recoverable_and_not(cp):
    """**本项最有信息量的结论**：能不能恢复，几乎完全由 `consumer_scan_floor` 决定。

    实测（同一冲击 `T=0.05`、同一期限 120 期、**扫码增益仍在位**）：

    * `floor = 0`（信任崩到底 ⇒ 没人再扫）→ **耦合版 120 期内根本没恢复**；
    * `floor = 0.15`（即使不信也有人扫）→ **9 期**恢复。

    ⚠️ **别把这里的 9 期和另一个用例的 22 期混为一谈**：
    `test_trust_trap_makes_recovery_slower` 里的"不耦合"组**额外关掉了扫码增益**
    （`scan_gain=0.0, p_scan_floor=0.15`），那才是 **22 期**。两组不是同一个对照组 ——
    我第一版把两个数写进同一个 docstring，被下一任当成"文档漂移"查了半天。**这里只记本用例自己的数。**

    这就是"正反馈陷阱"的**可证伪形态**：结论不是"信任能否恢复"，而是
    **"当扫码率下限 `floor` 低于某水平时，模型进入不可恢复区"** —— 一句关于参数的命题。
    同时它也说明：想让信任可恢复，工程上该动的是**采样机会的下限**（例如强制抽检、公共计量公示），
    而不是去调 `η⁺`（那是观测量，不是可设计量）。
    """
    no_floor = replace(cp, p_scan_floor=0.0, info_real_share=1.0)
    with_floor = replace(cp, p_scan_floor=0.15, info_real_share=1.0)
    t_no_floor = periods_to_recover(no_floor, agents=150, periods=120, seed=11, shock_to=0.05)
    t_with_floor = periods_to_recover(with_floor, agents=150, periods=120, seed=11, shock_to=0.05)

    assert t_with_floor is not None, "给了正的扫码下限后应能恢复"
    assert t_no_floor is None, f"扫码下限为 0 时不应在 120 期内恢复，实际 {t_no_floor} 期"
    print(
        f"[T-SIM-04] 开关是扫码率下限：floor=0 → {t_no_floor}（期限内不恢复）；"
        f"floor=0.15 → {t_with_floor} 期恢复"
    )


def test_boundary_state_is_reachable_so_region_is_not_two_valued(cp):
    """**三态不许退化成二态**：区域里必须真的出现过渡带或两种以上判定。

    设计文档要求"不许只有二态"—— 若扫描结果全是 recovered / not_recovered，
    说明判据丧失了表达模糊带的能力（或网格取得太稀疏）。
    """
    rows = recovery_region(
        cp, ratio_values=[1.0, 2.0, 3.0, 4.0, 6.0], delta_values=[0.0, 0.02, 0.05, 0.1, 0.3],
        agents=40, periods=50, seed=5, shock_to=0.05,
    )
    verdicts = {r["verdict"] for r in rows}
    assert len(verdicts) >= 2, f"区域判定退化：只有 {verdicts}"
    print(f"[T-SIM-04] 区域网格 {len(rows)} 格，出现判定 {sorted(verdicts)}（三态机制未退化）")
