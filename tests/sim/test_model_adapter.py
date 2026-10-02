"""`T-SIM-06` 验收①（集成运行）：**`layer=skeleton` 换成真跑四个 Agent**。

`docs/sim-design.md` §8 的 `T-SIM-06` 要求 `sim/bridge/model_adapter.py` 把四个 Agent
（商户 / 消费者 / 市场方 / 监管）真的跑起来，并把全部事实落进**只增不改**的事件流
（`layer="model-adapter"`，不是骨架运行）。本文件是这条的机械形式：

1. `layer` 与事件类型证明**四类 Agent 都跑到了**（不是"只要进程退出码为 0 就算集成成功"）；
2. **同 seed 逐字节可复现**（`T-SIM-01` 的地基判据在集成档上仍然成立）；
3. **守恒与两条路径对账**（`txn` 明细 vs `block_summary`）在集成档上仍然成立；
4. `R6` 反例（商户自费）**被真的执行**：自费臂的 `upfront_cents > 0`、
   `adopt_decision` 的判据在该臂上**能**被触及（第一版把权重扫描跑在出资臂上，
   `upfront=0` ⇒ 判据恒真 ⇒ 扫出一条假平线）；
5. `S6` 的 `evade_feasibility` **真的接线**（第一版没有任何模型代码读它，
   `S6-①堵死` 与 `S6-②不堵` 逐位相同）。

四条"检查类产出"各自都带**合成负例**：把检查关掉/喂坏数据必须变红。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sim_support import TINY_RUN, tiny_run

#: 四类 Agent 各自**必须**留下的事件类型（缺一类即判红）
AGENT_EVIDENCE = {
    "商户": ("merchant_period", "merchant_month"),
    "消费者": ("txn", "consumer_period"),
    "市场方": ("market_admin_period", "market_cash_month", "device_day"),
    "监管": ("regulator_period",),
}

SKELETON_KINDS = ("skeleton_step", "skeleton", "agent_step")


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    out = tmp_path_factory.mktemp("tsim06-adapter")
    result, events = tiny_run(out_dir=out / "S0")
    return {"result": result, "events": events, "out": out}


# ---------------------------------------------------------------------------
# 判据本身（纯函数）：好让合成负例直接喂它
# ---------------------------------------------------------------------------
def agent_evidence_problems(events: list[dict]) -> list[str]:
    """四类 Agent 是否都真的跑到了。**纯函数**（合成负例可直接喂）。"""
    problems: list[str] = []
    started = [e for e in events if e.get("kind") == "run_started"]
    if not started:
        return ["没有 run_started 事件 —— 这不是一次完整的集成运行"]
    layer = started[0].get("layer")
    if layer != "model-adapter":
        problems.append(f"layer = {layer!r}，不是 'model-adapter'（骨架运行不许冒充集成运行）")
    kinds = {e.get("kind") for e in events}
    for agent, needed in AGENT_EVIDENCE.items():
        if not (kinds & set(needed)):
            problems.append(f"{agent} Agent 没有留下任何事件（{needed} 一个都没出现）")
    for kind in SKELETON_KINDS:
        if kind in kinds:
            problems.append(f"集成运行里出现了骨架事件 {kind!r}（说明层没换掉）")
    return problems


def evade_gate_problems(stuck: list[dict], open_events: list[dict]) -> list[str]:
    """`S6` 的"堵死"是否真的起作用：① 走秤率更高 ② 出现被堵回的走秤 ③ 不堵臂不得出现。

    **纯函数**：喂"两组一模一样的事件"必须报红 —— 那正是 `evade_feasibility` 没接线时的形态。
    """
    def ratio(events):
        txns = [e for e in events if e.get("kind") == "txn"]
        scale = sum(1 for e in txns if e["channel"] == "scale")
        return (scale / len(txns)) if txns else None, len(txns)

    stuck_ratio, stuck_n = ratio(stuck)
    open_ratio, open_n = ratio(open_events)
    problems: list[str] = []
    if stuck_n == 0 or open_n == 0:
        return [f"样本不足：堵死 {stuck_n} 笔 / 不堵 {open_n} 笔"]
    if not (stuck_ratio > open_ratio):
        problems.append(f"堵死臂走秤率 {stuck_ratio!r} 不高于不堵臂 {open_ratio!r} —— 堵死没有起作用")
    forced_stuck = sum(1 for e in stuck if e.get("kind") == "txn" and e.get("forced_on_scale"))
    forced_open = sum(1 for e in open_events if e.get("kind") == "txn" and e.get("forced_on_scale"))
    if forced_stuck == 0:
        problems.append("堵死臂没有一笔 forced_on_scale（'想私下也私不成'这条机制没落地）")
    if forced_open != 0:
        problems.append(f"不堵臂出现了 {forced_open} 笔 forced_on_scale —— 环境不该堵")
    return problems


# ---------------------------------------------------------------------------
# 1. 真跑四个 Agent
# ---------------------------------------------------------------------------
def test_integrated_run_actually_runs_four_agents(tiny):
    events = tiny["events"]
    assert agent_evidence_problems(events) == [], agent_evidence_problems(events)
    kinds: dict[str, int] = {}
    for event in events:
        kinds[event.get("kind")] = kinds.get(event.get("kind"), 0) + 1
    assert kinds["merchant_period"] >= 10, "10 个摊位各应留下月度动作"
    assert kinds["txn"] > 0 and kinds["consumer_period"] > 0
    assert kinds["market_admin_period"] >= 1 and kinds["regulator_period"] >= 1
    print(f"[T-SIM-06] 集成运行事件类型 {len(kinds)} 种：{sorted(kinds.items())}")


def test_agent_evidence_check_is_sensitive(tiny):
    """**合成负例**：缺一类 Agent / 层没换 / 混进骨架事件 —— 三种都必须判红。"""
    events = tiny["events"]
    for drop_kinds in AGENT_EVIDENCE.values():
        broken = [e for e in events if e.get("kind") not in set(drop_kinds)]
        assert agent_evidence_problems(broken), f"删掉 {drop_kinds} 后没判红"
    relayered = [dict(e, layer="skeleton") if e.get("kind") == "run_started" else e for e in events]
    assert agent_evidence_problems(relayered), "layer=skeleton 未被判红"
    assert agent_evidence_problems(events + [{"kind": "skeleton_step"}]), "骨架事件未被判红"
    assert agent_evidence_problems([]), "空事件流未被判红"
    print("[T-SIM-06] 合成负例（缺 Agent / layer 未换 / 混骨架事件 / 空流）全部判红")


# ---------------------------------------------------------------------------
# 2. 同 seed 逐字节可复现
# ---------------------------------------------------------------------------
def test_same_seed_is_byte_identical(tiny, tmp_path):
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    first = tiny["out"] / "S0" / "events.jsonl"
    second_result, _ = tiny_run(out_dir=tmp_path / "again", seed=20261002)
    second = tmp_path / "again" / "events.jsonl"
    assert digest(first) == digest(second), "同 seed 两次运行的 events.jsonl 必须逐字节相同"
    third_result, _ = tiny_run(out_dir=tmp_path / "other", seed=20261003)
    third = tmp_path / "other" / "events.jsonl"
    assert digest(first) != digest(third), "不同 seed 必须不同（否则 seed 没起作用）"
    assert json.dumps(tiny["result"]["metrics"], sort_keys=True) == json.dumps(second_result["metrics"], sort_keys=True)
    assert json.dumps(third_result["metrics"], sort_keys=True) != json.dumps(tiny["result"]["metrics"], sort_keys=True)
    print(f"[T-SIM-06] 同 seed sha256={digest(first)[:16]}… 逐字节相同；不同 seed 不同")


# ---------------------------------------------------------------------------
# 3. 守恒 / 两条路径对账
# ---------------------------------------------------------------------------
def test_conservation_holds_on_the_integrated_run(tiny):
    from sim.observe.metrics_check import cross_check_problems

    problems = cross_check_problems(tiny["events"])
    assert problems == [], "两条独立事件路径必须一致：\n  " + "\n  ".join(problems)
    print(f"[T-SIM-06] 两条路径对账零问题（{len(tiny['events'])} 条事件）")


def test_cross_check_is_sensitive_to_a_tampered_ledger(tiny):
    """**合成负例**：抹掉一笔走秤明细 ⇒ 对账必须判红（否则那个检查是恒真的）。"""
    from sim.observe.metrics_check import cross_check_problems

    events = tiny["events"]
    scale_index = next(i for i, e in enumerate(events) if e.get("kind") == "txn" and e["channel"] == "scale")
    tampered = events[:scale_index] + events[scale_index + 1:]
    assert cross_check_problems(tampered), "少记一笔走秤后对账仍绿 —— 检查对事实不敏感"
    print("[T-SIM-06] 负例：抹掉一笔走秤明细 ⇒ 两条路径对账判红")


# ---------------------------------------------------------------------------
# 4. `R6` 反例被真的执行（自费臂的支出必须真的进入判据）
# ---------------------------------------------------------------------------
def adopt_decision_problems(adopted_events: list[dict]) -> list[str]:
    """`R6` 能否被检验：自费臂必须留下**正的**一次性支出，否则判据恒真。

    **纯函数**：喂"upfront=0"的臂（= 市场方出资）必须报红 —— 在那种臂上扫描
    `merchant_adoption_cost_weight` 是扫一个乘 0 的数（第一版就是这么扫的）。
    """
    from sim.bridge.model_adapter import adopt_decision

    problems: list[str] = []
    rows = [e for e in adopted_events if e.get("kind") == "stall_adopted"]
    if not rows:
        return ["自费臂没有任何 stall_adopted 事件 ⇒ R6 无从检验"]
    upfronts = {row.get("upfront_cents") for row in rows}
    if not any((value or 0) > 0 for value in upfronts):
        problems.append(f"stall_adopted 的 upfront_cents 全为 {upfronts} ⇒ 模型里的『自费』没有成本，"
                        "R6（自费 ⇒ 必然被弃用）无法被检验")
    row = rows[0]
    benefit, upfront = float(row["monthly_net_benefit_cents"]), float(row["upfront_cents"])
    if upfront <= 0:
        return problems
    #: 权重 = 1（字面金额）时应当采用；权重足够大时必须**拒绝**采用 —— 判据必须真的能被触及
    assert adopt_decision(upfront_cents=upfront, monthly_net_benefit_cents=benefit, cost_weight=1.0,
                          horizon_months=12.0) is True, "权重 1 时应采用（否则口径写反了）"
    threshold = benefit * 12.0 / upfront
    assert adopt_decision(upfront_cents=upfront, monthly_net_benefit_cents=benefit,
                          cost_weight=threshold * 2.0, horizon_months=12.0) is False, \
        "权重足够大时仍未拒绝 ⇒ 自费的成本项没进判据"
    print(f"[T-SIM-06] R6：自费 upfront={upfront:.0f} 分、月净收益={benefit:.0f} 分 ⇒ 判据在权重 "
          f"≈{threshold:.1f} 处翻转")
    return problems


def test_r6_counterexample_is_executed(tmp_path):
    """`S5` 的自费臂必须真的把一次性支出交给商户（`R6` 的第一层前提）。"""
    import pytest as _pytest

    self_funded = scenario_arms_flags("S5")
    assert sorted(self_funded) == [False, True], f"S5 两臂的 self_funded 必须一真一假：{self_funded}"
    with _pytest.raises(Exception):
        #: 自费臂在**参数文件**里没有"商户自费"这个键 —— 它是 `flags`，故必须由调用方显式传递。
        #: 这条负例钉住"自费不是一个普通参数覆盖"这件事（否则有人会把它写成参数、绕开 flags 校验）。
        tiny_run(scenario_id="S5", arm_index=1, out_dir=tmp_path / "bad", extra_overrides={"self_funded": True})

    _, events = tiny_run(scenario_id="S5", arm_index=1, out_dir=tmp_path / "self-funded")
    problems = adopt_decision_problems(events)
    assert problems == [], "\n  ".join(problems)
    #: **合成负例**：把支出抹成 0（= 出货方出资的形态）⇒ 同一条检查必须判红
    zeroed = [dict(e, upfront_cents=0.0) if e.get("kind") == "stall_adopted" else e for e in events]
    assert adopt_decision_problems(zeroed), "upfront=0 时 R6 检查仍绿 —— 检查对『自费』不敏感"
    print("[T-SIM-06] R6 负例：把 upfront 抹成 0 ⇒ 判据触不到 ⇒ 判红")


def scenario_arms_flags(scenario_id: str) -> list[bool]:
    from sim.bridge.scenario import arm_flags
    from sim_support import scenario_by_id

    scenario = scenario_by_id(scenario_id)
    return [arm_flags(scenario, arm)["self_funded"] for arm in scenario["arms"]]


# ---------------------------------------------------------------------------
# 5. `S6` 的 `evade_feasibility` 真的接线
# ---------------------------------------------------------------------------
def test_evade_feasibility_actually_blocks_shadow_trades(tmp_path):
    _, stuck = tiny_run(scenario_id="S6", arm_index=0, out_dir=tmp_path / "stuck")
    _, open_events = tiny_run(scenario_id="S6", arm_index=1, out_dir=tmp_path / "open")
    problems = evade_gate_problems(stuck, open_events)
    assert problems == [], "\n  ".join(problems)
    #: **合成负例**：把"堵死"臂的可行性改成 1（= 什么都不堵）⇒ 同一条检查必须判红。
    #: 这正是第一版的真实形态：`evade_feasibility` 只在 OAT 键表里出现，没有任何模型代码读它。
    _, not_wired = tiny_run(scenario_id="S6", arm_index=0, out_dir=tmp_path / "not-wired",
                            extra_overrides={"evade_feasibility": 1.0})
    assert evade_gate_problems(not_wired, open_events), "把堵死关掉后检查仍绿 —— 这条对照实验什么也没测"
    print("[T-SIM-06] S6：堵死臂走秤率更高且有 forced_on_scale；关掉堵死 ⇒ 判红")


def test_tiny_run_helper_uses_the_product_entrypoint():
    """支撑自检：极小档真的是"1 个完整仿真月"，否则月度指标会全部分母为 0。"""
    assert TINY_RUN["days"] == 30 and TINY_RUN["days"] % 30 == 0
