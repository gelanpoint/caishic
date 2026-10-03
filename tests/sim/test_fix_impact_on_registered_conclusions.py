"""`T-SIM-12`：**`V-02`/`V-03`/`V-04` 重跑**（父代理 2026-10-03 裁定：缺陷污染的正是商户决策的 `Q`，三条必须重跑）。

## 这份文件替代的是什么

上一任的交班语是：「修复只落在 `close_month`/`peer_mean_q`，Agent 层扫描不走这两处 —— **但这是推断不是实测**」。
父代理不接受推断（`docs/sim-验证结论实况.md` §V-02/V-03/V-04 待复核条）。
本文件把"重跑"做成**可执行断言**，并把"为什么不受影响"从推断换成**运行时证据**。

## 三层证据，缺一层都不算数

1. **数字层**：三条登记数字在修复后逐位复现（`test_v02/v03/v04_numbers_*`）——
   这是父代理要的"重跑"，且**不许只报"没变"**，故锁的是具体数值。
2. **执行层**：给两处接缝装**调用探针**，重跑三条的**计算过程**里探针一次都不响
   （`test_the_agent_level_sweeps_never_execute_the_fixed_seams`）——
   这是把"推断"换成"实测"的那一步。
3. **探针非空转层**：探针在**集成运行**里必须会响（`test_the_call_probe_is_not_vacuous`）。
   没有这一层，第 2 层的"没响"就只是因为探针坏了。

## ⚠️ 一条**必须说清**的边界（父代理点名的 `V-03` 疑虑）

父代理担心：「信任经由『消费者是否被欺骗』更新，而欺骗率取决于商户走秤/私下决策比例 ——
商户决策正是被污染的那条通道」。**这个担心是对的，但传导发生在集成层，不在 `V-03` 自身。**

- `V-03` 登记数字来自 `simulate_consumers()`：**纯消费者**，输入只有 `ConsumerParams`，
  它**不读任何商户状态**，故缺陷不在它的计算路径上（本文件第 2 层实测）；
- 但**集成运行里**的信任观测（`M-13`/`M-14`/`M-15`）确实会被缺陷改变
  （`test_the_defect_reaches_integrated_consumer_trust` 实测）。

⇒ **`V-03` 的登记结论（关于 `η⁻/η⁺/δ/floor` 的参数命题）成立且未被污染**；
但**任何引用集成档信任观测的表述，必须带"修复后"三个字**。这条边界不写清，两种误读都会发生。
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

import sim_support  # noqa: F401
from sim_support import PARAMS_PATH

from sim.agents.consumer import (
    analytic_fixed_point, analytic_half_life, consumer_params,
    periods_to_recover, simulate_consumers, update_trust,
)
from sim.agents.market_admin import (
    KPI_INSTALLED, KPI_USAGE, allocate_budget, market_admin_params,
    periods_until_cleared, queue_persistence, run_maintenance_loop,
)
from sim.agents.merchant import COMPLY, EVADE, sweep_commission_rate, sweep_detect_rate
from sim.bridge import month_loop
from sim.core.params import load_params

#: `V-02` 的档位：Agent 层扫描，40 商户 × 24 期，seed 20261002，单摊 14 万元/期、1000 走秤笔。
V02_AGENTS, V02_PERIODS, V02_SEED = 40, 24, 20261002
V02_VOLUME_CENTS, V02_SCALE_TXNS = 14_000_000.0, 1000
#: `V-04` 的档位：40 期，seed 2026，决策迟滞 0/1/3/6，初始 4 台坏、初始预算 10,000 分。
V04_PERIODS, V04_SEED, V04_LOW_BUDGET, V04_INITIAL_BROKEN = 40, 2026, 10_000.0, 4


# ---------------------------------------------------------------------------
# 调用探针：把"这两处没被调用"从**推断**变成**运行时测量**
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def call_probe():
    """把两处接缝包上计数器；退出上下文时恢复原对象。**上下文管理器**，故不漏恢复。"""
    calls = {"coexisting_peers": 0, "realized_volume_for_learning": 0}
    original = (month_loop.coexisting_peers, month_loop.realized_volume_for_learning)

    def wrap(name, func):
        def counted(*args, **kwargs):
            calls[name] += 1
            return func(*args, **kwargs)
        return counted

    month_loop.coexisting_peers = wrap("coexisting_peers", original[0])
    month_loop.realized_volume_for_learning = wrap("realized_volume_for_learning", original[1])
    try:
        yield calls
    finally:
        month_loop.coexisting_peers, month_loop.realized_volume_for_learning = original


@contextlib.contextmanager
def fix_switched_off():
    """把 `T-SIM-11` 的修复**关掉**（复原 2026-10-04 之前的写法）。与守卫文件里那份**同源**。"""
    original = (month_loop.coexisting_peers, month_loop.realized_volume_for_learning)
    month_loop.coexisting_peers = lambda world: [s.merchant for s in world.stalls.values()]
    month_loop.realized_volume_for_learning = lambda stall, realized: max(
        1.0, float(realized.get("volume_cents") or 0.0)
    )
    try:
        yield
    finally:
        month_loop.coexisting_peers, month_loop.realized_volume_for_learning = original


# ---------------------------------------------------------------------------
# 三条结论的实测摘要（**带档位的数字**，不是"没变"）
# ---------------------------------------------------------------------------
def v02_digest(params) -> dict:
    """`V-02` 的两条扫描（抽佣 / 检测率）各自的三路径占比。**档位见模块常量**。"""
    kwargs = dict(agents=V02_AGENTS, periods=V02_PERIODS, seed=V02_SEED,
                  volume_cents=V02_VOLUME_CENTS, scale_txn_count=V02_SCALE_TXNS)
    commission = [
        [r["final_share_ratio"].get(COMPLY, 0.0), r["final_share_ratio"].get(EVADE, 0.0)]
        for r in sweep_commission_rate(params, [0, 100, 200, 400, 600, 1000], **kwargs)
    ]
    detect = [
        [r["final_share_ratio"].get(EVADE, 0.0), r["final_share_ratio"].get(COMPLY, 0.0)]
        for r in sweep_detect_rate(params, [0.0, 0.05, 0.2, 0.5, 1.0], **kwargs)
    ]
    return {"抽佣 0→1000bp": commission, "p_detect 0→1.0": detect}


def v03_digest(params) -> dict:
    """`V-03`：不动点 / 半衰期 / 负面偏差 / 陷阱 / 下限开关。**档位见函数内注释与各用例打印**。"""
    cp = consumer_params(params)
    scan = float(params.value("consumer_scan_baseline_rate"))
    fixed = replace(cp, p_scan_floor=scan, scan_gain=0.0, info_real_share=1.0)
    star = analytic_fixed_point(fixed, scan_rate=scan)
    goal = star - (star - 0.2) / 2
    trace = simulate_consumers(fixed, agents=400, periods=60, seed=7, shock_to=0.2)["trace"]
    simulated = next((i for i, v in enumerate(trace) if v >= goal), None)
    trust = 0.5
    coupled = replace(cp, info_real_share=1.0)
    uncoupled = replace(cp, scan_gain=0.0, p_scan_floor=0.15, info_real_share=1.0)
    no_floor = replace(cp, p_scan_floor=0.0, info_real_share=1.0)
    with_floor = replace(cp, p_scan_floor=0.15, info_real_share=1.0)
    return {
        "不动点 T*": round(star, 4),
        "解析半衰期": round(analytic_half_life(fixed, scan_rate=scan), 3),
        "仿真半衰期": simulated,
        "η⁻/η⁺": round(cp.negative_bias, 3),
        "同样|Δ|=0.3 的下降": round(trust - update_trust(trust, 0.2, cp), 4),
        "同样|Δ|=0.3 的上升": round(update_trust(trust, 0.8, cp) - trust, 4),
        "耦合恢复期数(None=未恢复)": periods_to_recover(coupled, agents=150, periods=120, seed=11, shock_to=0.05),
        "不耦合恢复期数": periods_to_recover(uncoupled, agents=150, periods=120, seed=11, shock_to=0.05),
        "floor=0 恢复期数": periods_to_recover(no_floor, agents=150, periods=120, seed=11, shock_to=0.05),
        "floor=0.15 恢复期数": periods_to_recover(with_floor, agents=150, periods=120, seed=11, shock_to=0.05),
    }


def v04_digest(params) -> dict:
    """`V-04`：错配占比 / 队列不消化 / 迟滞位移 / 无迟滞对照。**档位见模块常量**。"""
    budget = float(params.value("market_budget_initial_cents"))

    def loop(kpi, delay):
        return run_maintenance_loop(params, kpi=kpi, periods=V04_PERIODS, seed=V04_SEED,
                                    decision_delay=delay, initial_broken=V04_INITIAL_BROKEN,
                                    initial_budget_cents=V04_LOW_BUDGET)

    shares = {}
    for kpi in (KPI_INSTALLED, KPI_USAGE):
        _expansion, maintenance = allocate_budget(
            budget, market_admin_params(params, kpi=kpi), installed_ratio=1.0, usage_rate=0.6)
        shares[kpi] = maintenance / budget
    installed = loop(KPI_INSTALLED, 6)
    persistence = queue_persistence(installed, ignore_first=0)
    return {
        "维护占比 装机量": shares[KPI_INSTALLED],
        "维护占比 使用率": shares[KPI_USAGE],
        "装机量口径 队列非空/总期数": [persistence["waiting_periods"], persistence["periods"]],
        "装机量口径 累计修好": sum(r["repaired"] for r in installed),
        "装机量口径 末期可用/装机": [persistence["working_end"], int(params.value("device_count"))],
        "迟滞→首次生效期": {d: next((r["period"] for r in loop(KPI_USAGE, d)
                                     if r["applied_this_period"] is not None), None)
                             for d in (0, 1, 3, 6)},
        "使用率口径 停摆期数": {d: periods_until_cleared(loop(KPI_USAGE, d)) for d in (0, 3, 6)},
    }


@pytest.fixture(scope="module")
def params():
    return load_params(PARAMS_PATH)


# ---------------------------------------------------------------------------
# ① 数字层：三条登记数字在修复后**逐位**复现（父代理要的"重跑"）
# ---------------------------------------------------------------------------
def test_v02_numbers_after_the_fix(params):
    digest = v02_digest(params)
    assert digest["抽佣 0→1000bp"][0] == [0.225, 0.775], digest["抽佣 0→1000bp"][0]
    assert digest["抽佣 0→1000bp"][-1] == [0.15, 0.85], digest["抽佣 0→1000bp"][-1]
    assert [row[0] for row in digest["抽佣 0→1000bp"]] == [0.225, 0.225, 0.2, 0.175, 0.175, 0.15]
    assert digest["p_detect 0→1.0"][0][0] == 0.8 and digest["p_detect 0→1.0"][-1][0] == 0.1
    print(f"[T-SIM-12] V-02（40 商户×24 期·seed 20261002）：抽佣 0→1000bp 合规 "
          f"{digest['抽佣 0→1000bp'][0][0]}→{digest['抽佣 0→1000bp'][-1][0]}（挪 7.5pp）；"
          f"p_detect 0→1.0 转暗 {[row[0] for row in digest['p_detect 0→1.0']]}")


def test_v03_numbers_after_the_fix(params):
    digest = v03_digest(params)
    assert digest["不动点 T*"] == 0.7241
    assert digest["解析半衰期"] == 9.561 and digest["仿真半衰期"] == 8
    assert round(abs(8 - 9.561) / 9.561 * 100, 1) == 16.3, "偏离幅度变了"
    assert digest["η⁻/η⁺"] == 2.286
    assert digest["同样|Δ|=0.3 的下降"] > digest["同样|Δ|=0.3 的上升"] == 0.095
    assert digest["耦合恢复期数(None=未恢复)"] is None and digest["不耦合恢复期数"] == 22
    assert digest["floor=0 恢复期数"] is None
    print(f"[T-SIM-12] V-03：T*={digest['不动点 T*']}、解析 {digest['解析半衰期']} 期 vs 仿真 "
          f"{digest['仿真半衰期']} 期、η⁻/η⁺={digest['η⁻/η⁺']}、耦合 {digest['耦合恢复期数(None=未恢复)']}"
          f" vs 不耦合 {digest['不耦合恢复期数']}、floor=0 → {digest['floor=0 恢复期数']}")


def test_v04_numbers_after_the_fix(params):
    digest = v04_digest(params)
    assert digest["维护占比 装机量"] == 0.0 and digest["维护占比 使用率"] == 1.0
    assert digest["装机量口径 队列非空/总期数"] == [40, 40]
    assert digest["装机量口径 累计修好"] == 0
    assert digest["装机量口径 末期可用/装机"] == [6, 10]
    assert digest["迟滞→首次生效期"] == {0: 1, 1: 1, 3: 3, 6: 6}, "迟滞位移必须恰为 max(1, D)"
    assert digest["使用率口径 停摆期数"] == {0: 1, 3: 3, 6: 6}
    print(f"[T-SIM-12] V-04（40 期·seed 2026）：维护占比 {digest['维护占比 装机量']:.3f} vs "
          f"{digest['维护占比 使用率']:.3f}、队列 {digest['装机量口径 队列非空/总期数'][0]}/40 非空、"
          f"修好 {digest['装机量口径 累计修好']} 台、末期可用 {digest['装机量口径 末期可用/装机']}")


# ---------------------------------------------------------------------------
# ② 执行层：三条的计算过程**一次都没碰**修复代码（把推断换成实测）
# ---------------------------------------------------------------------------
def test_the_agent_level_sweeps_never_execute_the_fixed_seams(params):
    with call_probe() as calls:
        v02_digest(params)
        v03_digest(params)
        v04_digest(params)
    assert calls == {"coexisting_peers": 0, "realized_volume_for_learning": 0}, (
        f"重算 V-02/V-03/V-04 时竟然碰到了修复代码：{calls} —— "
        "『Agent 层扫描不走这两处』这个说法必须按新事实重新评估")
    print("[T-SIM-12] 重算 V-02/V-03/V-04 全过程：两处接缝调用次数 0 / 0（实测，非推断）")


def test_the_call_probe_is_not_vacuous(tmp_path):
    """**探针的非空转守卫**：同样的探针装在**集成运行**上，必须会响。

    没有这一条，② 里的"0 次调用"可能只是因为探针坏了 —— 而这正是"验证要能失败"的意思。
    """
    from sim.bridge.model_adapter import read_events, run_scenario
    from sim.bridge.scenario import arm_flags, load_scenario, merged_overrides

    scenario = load_scenario(Path(PARAMS_PATH).parents[1] / "scenarios" / "S3_maintenance.json")
    arm = scenario["arms"][0]
    overrides = merged_overrides(scenario, arm)
    overrides["daily_arrivals_per_market"] = 40
    overrides["consumer_agent_count"] = 20
    with call_probe() as calls:
        run_scenario(load_params(PARAMS_PATH), scenario_id="S3::probe", overrides=overrides,
                     days=120, seed=20261002, out_dir=tmp_path,
                     self_funded=arm_flags(scenario, arm)["self_funded"])
        read_events(tmp_path / "events.jsonl")
    assert calls["realized_volume_for_learning"] > 0, (
        f"集成运行里接缝一次都没被调用（{calls}）⇒ 探针装错了地方，② 的『0 次』没有信息量")
    assert calls["coexisting_peers"] > 0, f"coexisting_peers 未被调用（{calls}）"
    print(f"[T-SIM-12] 探针非空转：同一探针在集成运行（120 日·40 到达）里响了 "
          f"{calls['coexisting_peers']}/{calls['realized_volume_for_learning']} 次")


# ---------------------------------------------------------------------------
# ③ 反向灵敏度负例：**把修复关掉**，这三条的数字必须**一个都不变**
# ---------------------------------------------------------------------------
def test_switching_the_fix_off_does_not_move_any_of_the_three(params):
    """**灵敏度负例（反向）**：`V-02`/`V-03`/`V-04` 的数字与两处接缝的实现**无关**。

    为什么必须是反向的：结论是"**不受影响**"。若只断言"修复后 = 登记值"，那条断言在
    "结论其实受影响、但恰好这一次种子相同"时也会绿。**把修复关掉数字仍必须一致**，
    才排除了这种偶然 —— 这是本条不可替代的地方。
    """
    baseline = {"V-02": v02_digest(params), "V-03": v03_digest(params), "V-04": v04_digest(params)}
    with fix_switched_off():
        with call_probe() as calls:
            off = {"V-02": v02_digest(params), "V-03": v03_digest(params), "V-04": v04_digest(params)}
    for name in baseline:
        assert off[name] == baseline[name], (
            f"{name} 的数字在『修复关掉』后变了 ⇒ 『不受影响』的说法是错的：\n"
            f"  修复在位 {json.dumps(baseline[name], ensure_ascii=False)}\n"
            f"  修复关掉 {json.dumps(off[name], ensure_ascii=False)}")
    assert calls == {"coexisting_peers": 0, "realized_volume_for_learning": 0}, (
        f"修复关掉后探针仍然 0 次 ⇒ 这个负例根本没复原原行为 {calls}")
    print("[T-SIM-12] 反向负例：修复关掉后 V-02/V-03/V-04 数字逐位不变（不是『碰巧同种子』）")


# ---------------------------------------------------------------------------
# ④ 边界守卫：**集成层的信任观测确实被缺陷改变** —— 这条不许被"顺手优化"掉
# ---------------------------------------------------------------------------
def _s3_binding_profile_events(tmp_path, mtbf_days=180):
    """`S-约束` 压力档（240 营业日 / 8 期 / 300 到达 / 40 消费者）但 MTBF 改 180，`S3` **足额臂**。

    选这一档的理由（不是随便挑的）：它是 `docs/sim-results.md` §8.5 记录"修复前后 `M-04` 不同"的那一档 ——
    **缺陷在这里真的会改变商户动作**，所以它是"缺陷 ⇒ 欺骗率 ⇒ 消费者信任"这条链的**唯一样本载体**。
    """
    from sim.bridge.model_adapter import read_events, run_scenario
    from sim.bridge.scenario import arm_flags, load_scenario, merged_overrides
    from sim.verify.r_criteria import BINDING_PROFILE

    scenario = load_scenario(Path(PARAMS_PATH).parents[1] / "scenarios" / "S3_maintenance.json")
    arm = scenario["arms"][0]
    overrides = merged_overrides(scenario, arm)
    overrides["daily_arrivals_per_market"] = BINDING_PROFILE["arrivals"]
    overrides["consumer_agent_count"] = BINDING_PROFILE["consumers"]
    overrides.update(BINDING_PROFILE.get("param_overrides") or {})
    overrides["device_mtbf_days"] = mtbf_days
    target = tmp_path / f"mtbf{mtbf_days}"
    run_scenario(load_params(PARAMS_PATH), scenario_id="S3::probe", overrides=overrides,
                 days=BINDING_PROFILE["days"], seed=20261002, out_dir=target,
                 self_funded=arm_flags(scenario, arm)["self_funded"])
    return read_events(target / "events.jsonl")


def test_the_defect_reaches_integrated_consumer_trust(tmp_path):
    """**父代理的担心是对的**：缺陷确实经"商户动作 ⇒ 欺骗率"传到**集成层的信任观测**。

    判的是"修复关掉后 `M-15`/`M-13` 必须变" —— 它**不是**在保护缺陷，而是在**保护边界这条界线**：
    `V-03` 的登记结论是 `simulate_consumers()` 上的**参数命题**，不受污染；
    而集成档的信任数字**受污染**，引用时必须写"修复后"。这条用例一红，就说明界线被动了。

    **灵敏度负例就在同一函数里**：修���后的序列被钉死（`[0.422441, …, 0.281045]`），
    只钉"两者不等"的话，等号成立（缺陷不存在了）也会绿。
    """
    from sim.observe.metrics import compute_all

    fixed = compute_all(_s3_binding_profile_events(tmp_path / "fixed"))["M-15"]["detail"]["by_period"]
    with fix_switched_off():
        broken = compute_all(_s3_binding_profile_events(tmp_path / "broken"))["M-15"]["detail"]["by_period"]
    assert [row["mean"] for row in fixed] != [row["mean"] for row in broken], (
        "这一档里缺陷不再改变信任存量 ⇒ 父代理那条『集成层也受污染』的边界描述失效，"
        "必须重新实测并更新本文件与文档，**不许直接改断言蒙混过去**")
    assert [round(row["mean"], 6) for row in fixed] == [
        0.422441, 0.338922, 0.322525, 0.308326, 0.306069, 0.296016, 0.290554, 0.281045], fixed
    assert [round(row["mean"], 6) for row in broken] == [
        0.422441, 0.338922, 0.322525, 0.308326, 0.265442, 0.260133, 0.254931, 0.249832], broken
    print(f"[T-SIM-12] 集成层信任（M-15 逐期均值）：修复后末值 {fixed[-1]['mean']:.6f} vs "
          f"修复前 {broken[-1]['mean']:.6f} ⇒ 『V-03 参数命题不受污染』与『集成档信任必须带修复后』两条同时成立")


def test_v03_registered_conclusion_survives_because_it_is_agent_level(params):
    """把 ④ 与 `V-03` 的关系钉死：`V-03` 的数字在两种接缝实现下**一致**，与 ④ 并不矛盾。

    ④ 说"集成层受污染"，这里说"`V-03` 本身不受污染" —— 两句话必须同时为真。
    若有人把 `V-03` 的数字改成从集成档读，这条立刻与 ④ 打架。
    """
    baseline = v03_digest(params)
    assert baseline["耦合恢复期数(None=未恢复)"] is None and baseline["不耦合恢复期数"] == 22
    with fix_switched_off():
        assert v03_digest(params) == baseline
    print("[T-SIM-12] V-03（`simulate_consumers` 的参数命题）与集成层信任是**两件事**：前者与接缝实现无关")
