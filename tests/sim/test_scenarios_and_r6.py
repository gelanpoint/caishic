"""`T-SIM-06` 验收⑤⑥：**7 个场景（`S0` + `S1`~`S6`）**与"对照实验只差一件事"。

本文件盯三件事，每件都带**合成负例**：

1. **场景结构**：每个场景 `scenario_problems()` 为空；引用的参数键都在 `params.json` 里；
   每一臂**恰好**只给出本场景声明的对照变量（多一个键 = 偷改别的参数）；
2. **`R6` 反例存在且被执行**：`S5` 的两臂必须有 `self_funded` 一真一假，
   且这条反例的分母（走秤率）真的被算出来 —— 一个只会说"我们的设计好"的仿真没有价值；
3. **对照变量必须真的被模型读到**（本轮自查出的真缺陷换来的守卫）：
   `S6` 的 `evade_feasibility` 一度**只在 OAT 键表里出现、没有任何模型代码读它**，
   于是 `S6-①堵死` 与 `S6-②不堵` 结果逐位相同 —— "对照实验"什么也没测。
   现在每个场景的每个 `varying_keys` 都必须在 `sim/**` 里被 `value("<key>")` 读过。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sim_support import PARAMS_PATH, SIM_DIR, REPO_ROOT, scenario_by_id, sim_python_files, tiny_run

EXPECTED_SCENARIOS = ["S0", "S1", "S2", "S3", "S4", "S5", "S6"]

#: 场景**开关**（`flags`）的落地位置：不是参数覆盖，故单独登记（少一处即判红）
KNOWN_FLAG_SITES = {"self_funded": ("sim/bridge/model_adapter.py", "sim/bridge/month_loop.py")}


def all_scenarios() -> list[dict]:
    from sim.bridge.scenario import load_scenario, scenario_files

    return [load_scenario(path) for path in scenario_files()]


def unwired_keys(scenario: dict, source_text: str) -> list[str]:
    """本场景声明的对照变量里，**没有任何 sim 源码读它**的那些（纯函数，可喂负例）。"""
    unwired = []
    for key in scenario.get("varying_keys") or []:
        if f'value("{key}")' not in source_text:
            unwired.append(key)
    for flag in scenario.get("varying_flags") or []:
        sites = KNOWN_FLAG_SITES.get(flag)
        if not sites:
            unwired.append(flag)
            continue
        if not any(flag in (REPO_ROOT / site).read_text(encoding="utf-8") for site in sites):
            unwired.append(flag)
    return unwired


@pytest.fixture(scope="module")
def sim_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sim_python_files())


# ---------------------------------------------------------------------------
# 1. 场景结构与参数出处
# ---------------------------------------------------------------------------
def test_seven_scenarios_exist_and_are_structurally_valid():
    from sim.bridge.scenario import scenario_problems

    scenarios = all_scenarios()
    assert [s["id"] for s in scenarios] == EXPECTED_SCENARIOS, "设计 §5 要求 S0 + S1~S6 共 7 个"
    for scenario in scenarios:
        problems = scenario_problems(scenario)
        assert problems == [], f"{scenario['id']} 结构不合规：{problems}"
    arms = sum(len(s["arms"]) for s in scenarios)
    assert arms >= 7, f"场景臂太少（{arms}）—— 对照实验至少每场景一臂"
    print(f"[T-SIM-06] 7 个场景共 {arms} 臂，结构校验全绿")


def test_scenario_parameters_exist_in_the_params_file():
    from sim.bridge.scenario import scenario_param_problems
    from sim.core.params import load_params

    params = load_params(PARAMS_PATH)
    bad = []
    for scenario in all_scenarios():
        bad += scenario_param_problems(scenario, params)
    assert bad == [], "\n  ".join(bad)
    print(f"[T-SIM-06] 场景引用的参数键全部存在于 {Path(PARAMS_PATH).name}（{len(params.parameters())} 个参数）")


def test_scenario_problems_flags_a_smuggled_extra_key():
    """**合成负例**：某一臂偷偷多改一个参数 / 两臂完全重复 ⇒ 必须判红。"""
    from sim.bridge.scenario import scenario_problems

    scenario = json.loads(json.dumps(scenario_by_id("S1")))
    assert scenario_problems(scenario) == []
    scenario["arms"][0]["overrides"]["gross_margin_rate"] = 0.9
    assert scenario_problems(scenario), "臂里多塞一个未声明的键，结构校验竟然通过"
    doubled = json.loads(json.dumps(scenario_by_id("S1")))
    doubled["arms"][1]["overrides"] = dict(doubled["arms"][0]["overrides"])
    assert scenario_problems(doubled), "两臂取值完全相同（假对照）竟然通过"
    print("[T-SIM-06] 负例：臂里多塞键 / 重复臂 ⇒ 结构校验判红")


# ---------------------------------------------------------------------------
# 2. `R6` 反例：存在、被执行、结果如实报
# ---------------------------------------------------------------------------
def test_r6_counterexample_scenario_is_present_and_declared():
    from sim.bridge.scenario import arm_flags

    scenario = scenario_by_id("S5")
    flags = [arm_flags(scenario, arm)["self_funded"] for arm in scenario["arms"]]
    assert flags == [False, True], f"S5 必须同时有『市场方出资』与『商户自费』两臂：{flags}"
    names = " ".join(arm["name"] for arm in scenario["arms"])
    assert "自费" in names, f"S5 的自费臂必须在名字里写清（便于报告引用）：{names}"
    print(f"[T-SIM-06] R6 反例存在：{names}")


def test_r6_counterexample_runs_and_reports_its_actual_result(tmp_path):
    """**执行**这条反例，并把"到底有没有被弃用"作为一个**可失败**的断言来问。

    注意断言的形态：这里**不假设** R6 成立。它断言的是"两条臂都跑出了结果、且差值是可计算的"——
    若自费组与出资组一样好，本用例同样通过，但 `r6_verdict` 会给出 `不成立`；
    **把"设计好"当成预设才是要防的错误**。
    """
    from sim.bridge.model_adapter import read_events, run_scenario
    from sim.bridge.scenario import arm_flags, merged_overrides
    from sim.core.params import load_params
    from sim.observe.metrics import compute_all

    scenario = scenario_by_id("S5")
    params = load_params(PARAMS_PATH)
    ratios = {}
    for index, arm in enumerate(scenario["arms"]):
        overrides = merged_overrides(scenario, arm)
        overrides["daily_arrivals_per_market"] = 60
        overrides["consumer_agent_count"] = 20
        out = tmp_path / f"arm{index}"
        run_scenario(params, scenario_id=f"S5::{arm['name']}", overrides=overrides, days=30,
                     seed=20261002, out_dir=out, self_funded=arm_flags(scenario, arm)["self_funded"])
        metrics = compute_all(read_events(out / "events.jsonl"))
        ratios[arm["name"]] = metrics["M-04"]["ratio"]
    verdict = r6_verdict(list(ratios.values()))
    assert all(value is not None for value in ratios.values()), f"两臂都必须算出走秤率：{ratios}"
    assert verdict in ("成立", "不成立", "不稳健"), verdict
    upfront = [e for e in read_events(tmp_path / "arm1" / "events.jsonl") if e.get("kind") == "stall_adopted"]
    assert any((row.get("upfront_cents") or 0) > 0 for row in upfront), \
        "自费臂没有任何正的一次性支出 ⇒ 『自费』在该臂上没有成本，R6 无法被检验"
    print(f"[T-SIM-06] R6 实际结果：{ratios} ⇒ 判定 = {verdict}（如实报告，不预设结论）")


def r6_verdict(ratios: list[float]) -> str:
    """`R6` 的三态判定（`成立` / `不成立` / `不稳健`）。**纯函数**，可喂合成负例。

    单次重复下拿不到区间 ⇒ 只能给"方向"层面的判定，故把"差值为 0 或方向相反"记为**不成立**
    （= 模型里的自费没有真实成本 ⇒ `结论 26` 无法被检验），这正是设计 §7.1 `R6` 行要求的形态。
    """
    if len(ratios) != 2 or any(value is None for value in ratios):
        return "不稳健"
    funded, self_paid = ratios
    if self_paid < funded:
        return "成立"
    return "不成立"


def test_r6_verdict_is_not_hardcoded():
    """**合成负例**：把两臂喂成相同/相反方向，判定必须随之翻转（否则"判定"是写死的）。"""
    assert r6_verdict([0.6, 0.4]) == "成立"
    assert r6_verdict([0.6, 0.6]) == "不成立"
    assert r6_verdict([0.4, 0.6]) == "不成立"
    assert r6_verdict([0.6, None]) == "不稳健"
    print("[T-SIM-06] 负例：R6 判定随输入翻转，不是写死的")


# ---------------------------------------------------------------------------
# 3. 对照变量必须真的被模型读到
# ---------------------------------------------------------------------------
def test_every_declared_varying_key_is_actually_read_by_the_model(sim_source):
    """每个场景声明的对照变量都必须在 `sim/**` 里被读过 —— 否则那条对照是**空的**。

    这条守卫的来历：`S6` 的 `evade_feasibility` 第一版只在 `study.py` 的 OAT 键表里出现过，
    `day_loop.py` 里没有任何代码读它 ⇒ `S6-①堵死` 与 `S6-②不堵` 的走秤率**逐位相同**，
    而报告会照常打印两行不同的数字，看起来像"堵死没用"。
    """
    offenders = {scenario["id"]: unwired_keys(scenario, sim_source) for scenario in all_scenarios()}
    offenders = {key: value for key, value in offenders.items() if value}
    assert offenders == {}, f"这些场景声明的对照变量没有任何模型代码读它：{offenders}"
    print("[T-SIM-06] 7 个场景声明的对照变量逐个都能在 sim/** 里找到读取点")


def test_unwired_key_guard_is_sensitive(sim_source):
    """**合成负例**：给一个"声明了但没人读"的键与开关 ⇒ 守卫必须判红。"""
    fake = {"id": "S9", "varying_keys": ["no_such_wired_key"], "varying_flags": ["no_such_flag"]}
    assert set(unwired_keys(fake, sim_source)) == {"no_such_wired_key", "no_such_flag"}
    real = scenario_by_id("S6")
    assert unwired_keys(real, sim_source) == []
    print("[T-SIM-06] 负例：声明了但没人读的键/开关 ⇒ 守卫判红")


def test_evade_feasibility_is_read_in_the_day_loop():
    """`S6` 的堵死开关必须在**日频成交**里生效（不是只在参数表里躺着）。"""
    source = (SIM_DIR / "bridge" / "day_loop.py").read_text(encoding="utf-8")
    assert "evade_feasible" in source and "forced_on_scale" in source, \
        "day_loop 没有实现『想私下也私不成』这条机制"
    assert re.search(r"evade_feasibility", (SIM_DIR / "bridge" / "model_adapter.py").read_text(encoding="utf-8"))
    print("[T-SIM-06] evade_feasibility 在 day_loop 里真的生效，且 forced_on_scale 落进事件流")


# ---------------------------------------------------------------------------
# 4. 报告对**自己产物**的描述必须与产物一致（收口 Agent 修的真缺陷的回归）
# ---------------------------------------------------------------------------
def test_replication_caveat_matches_the_actual_R():
    """`R=3` 时**不许**说"每臂只有一次重复、p5/p95 退化为单点" —— 那是 `R=1` 的事实。

    ## 这条用例的来历（一个实测到的真缺陷）

    `study.py` 原先把"R 的诚实提示"在**两处各写了一遍**，触发条件都按 `R < 5`，但正文一律写
    "每臂只有一次重复、p5/p95 退化为单点"。于是 `R=3` 的实际运行会生成：

    > ⚠️ R=3 ⇒ 每臂只有一次重复，p5/p95 退化为单点

    而产物里明明有 3 次重复、p5/p95 也明明是两个不同的数（`S5` 实测 0.290005 ~ 0.485007）。
    **报告对自己产物的描述与产物不符**，会让读者据此把"证据弱"误读成"没有证据"而全盘丢弃区间。
    现收敛到 `replication_facts()` 一个事实源；本用例即该缺陷的回归守卫。
    """
    from sim.bridge.study import replication_caveat, replication_caveat_short, replication_facts

    assert replication_facts(1)["interval_is_a_single_point"] is True
    assert replication_facts(3)["interval_is_a_single_point"] is False, \
        "R=3 的 p5/p95 不是单点 —— 事实源必须说 False"
    assert replication_facts(3)["at_design_level"] is False
    assert replication_facts(20)["at_design_level"] is True

    at_one = replication_caveat(1) + replication_caveat_short(1)
    at_three = replication_caveat(3) + replication_caveat_short(3)
    at_twenty = replication_caveat(20) + replication_caveat_short(20)

    assert "退化为单点" in at_one and "R=1" in at_one
    #: **负例**：旧实现在 R=3 时也输出这句，本断言就是它必红的地方
    assert "退化为单点" not in at_three, \
        f"R=3 时仍在说『p5/p95 退化为单点』—— 报告与产物不符：{at_three}"
    assert "R=3" in at_three and "低于设计档" in at_three
    assert "达到设计档" in at_twenty
    print("[T-SIM-06] R 的诚实提示与实际 R 一致：R=1 说『退化为单点』、"
          "R=3 说『低于设计档但区间不是单点』、R=20 说『达到设计档』（旧实现 R=3 时必红）")

