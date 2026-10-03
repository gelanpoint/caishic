"""`T-SIM-08` 验收①②：`R1`~`R6` 的**三态**判定与「绝对阈值 / 序关系」的纪律。

对应 `docs/sim-design.md` §8 `T-SIM-08`：

| 判据 | 用例 | 形式 |
| --- | --- | --- |
| ① `R1`~`R6` 逐条三态，不许只有二态 | `test_verdicts_are_from_the_three_state_set` / `test_verdict_of_is_not_hardcoded` | 合成负例 |
| 「有出处才许用绝对阈值」是**机械**的 | `test_absolute_clause_is_unevaluable_when_a_dependency_is_assumed` | 合成负例 |
| 序关系判据随输入翻转 | `test_ordering_clause_flips_with_inputs` | 合成负例 |
| 与 `T-SIM-06` 的 `R6` 判定**同源** | `test_r6_verdict_matches_the_t_sim_06_helper` | 交叉核对 |

「不验证灵敏度的验证是摆设」：每条判定都喂一份构造出来的输入，断言结论**必须随之改变**；
判定若写死，这些负例必红。
"""

from __future__ import annotations

import pytest

from sim_support import PARAMS_PATH

from sim.core.params import load_params
from sim.verify import backtest
from sim.verify.r_criteria import ABSOLUTE, CRITERIA, CRITERIA_BY_ID, ORDERING, PROFILES, VERDICTS, Clause


def _fake_run(value, *, series=None, label="假臂"):
    """构造一个 `run_arm` 形状的最小替身（判定是纯函数，不需要真跑）。"""
    return {"label": label, "scenario": "S?", "arm": "假", "arm_index": 0, "overrides": {}, "self_funded": False,
            "profile": "L1", "rows": [{"rep": 0, "out_dir": ".",
                                       "metrics": {"M-04": {"ratio": value}, "M-01": {"ratio": value},
                                                   "M-03": {"ratio": value}, "M-10": {"ratio": value},
                                                   "M-15": {"ratio": value}},
                                       "series": {"M-15": series or [], "R6-upfront": [value]}}]}


@pytest.fixture(scope="module")
def params():
    return load_params(PARAMS_PATH)


# ---------------------------------------------------------------------------
# ① 三态
# ---------------------------------------------------------------------------
def test_verdicts_are_from_the_three_state_set():
    assert set(VERDICTS) == {"成立", "不成立", "不稳健"}, "三态必须同时存在（不许退化成二态）"
    assert len(VERDICTS) == 3


def test_verdict_of_is_not_hardcoded(params):
    """**合成负例**：同一批子句，只改一处状态 / 只改稳健性，判定必须随之改变。"""
    passing = [{"clause": "R1-a", "state": "通过", "reason": None}]
    failing = [{"clause": "R1-a", "state": "不通过", "reason": "实测不通过"}]
    blind = [{"clause": "R1-a", "state": "不可评估", "reason": "依赖无出处参数"}]
    clean_scan = {"flips": [], "flipped_keys": []}
    flipped = {"flips": [{"key": "gross_margin_rate"}], "flipped_keys": ["gross_margin_rate"]}

    assert backtest.verdict_of(passing, clean_scan)[0] == "成立"
    assert backtest.verdict_of(failing, clean_scan)[0] == "不成立"
    #: **不可评估不等于通过** —— 这是 `§6.9` 明写的纪律，也是最容易写错的一条
    assert backtest.verdict_of(blind, clean_scan)[0] == "不成立"
    assert backtest.verdict_of(passing, flipped)[0] == "不稳健"
    assert backtest.verdict_of(failing, flipped)[0] == "不成立", "已经实测不通过时，优先报『不成立』而不是掩盖成『不稳健』"
    for clauses, scan in ((passing, clean_scan), (failing, clean_scan), (blind, clean_scan), (passing, flipped)):
        assert backtest.verdict_of(clauses, scan)[0] in VERDICTS
    print("[T-SIM-08] 三态收敛规则随输入改变（负例：同一子句，四种输入四种结论）")


def test_every_criterion_declares_clauses_profile_and_source():
    assert [c.id for c in CRITERIA] == ["R1", "R2", "R3", "R4", "R5", "R6"]
    for crit in CRITERIA:
        assert crit.clauses, f"{crit.id} 没有任何子句"
        assert crit.basis and crit.question, f"{crit.id} 缺出处或问题"
        assert set(crit.arms) >= {"A", "B"}, f"{crit.id} 缺对照臂"
        for clause in crit.clauses:
            assert clause.kind in (ABSOLUTE, ORDERING)
            assert clause.profile or crit.profile, f"{crit.id}/{clause.id} 没有声明所用档位"
    print(f"[T-SIM-08] 6 条判据共 {sum(len(c.clauses) for c in CRITERIA)} 个子句，每条都带档位与出处")


# ---------------------------------------------------------------------------
# 「有出处才许用绝对阈值」——本项目最重要的规则，必须是机械的
# ---------------------------------------------------------------------------
def test_absolute_clause_is_unevaluable_when_a_dependency_is_assumed(params):
    clause = Clause("X-1", "t_first_exit ≤ 6 个月", ABSOLUTE, "M-02",
                    depends_on=("merchant_exit_reference_point", "gross_margin_rate"), threshold=6)
    verdict = backtest.judge_clause(params, clause, _fake_run(1.0))
    assert verdict["state"] == "不可评估", verdict
    assert "merchant_exit_reference_point" in verdict["reason"]
    assert verdict["unsourced"], "不可评估却没有点名是哪个参数没出处 ⇒ 报告读了也不知道该改什么"
    print(f"[T-SIM-08] 绝对阈值依赖无出处参数 ⇒ 不可评估：{verdict['unsourced']}")


def test_absolute_clause_is_judged_when_every_dependency_is_sourced(params):
    """**反向断言**：依赖全是有出处参数时，绝对阈值**照常生效**（否则纪律就变成了"一律不许判"）。"""
    clause = Clause("X-2", "商户自费金额 ≤ 0", ABSOLUTE, "R6-upfront",
                    depends_on=("merchant_self_funded_scale_cents",), threshold=0)
    assert backtest.judge_clause(params, clause, _fake_run(0.0))["state"] == "通过"
    #: **负例**：把取值改成正 ⇒ 判定必须翻成不通过（证明它不是写死的"通过"）
    assert backtest.judge_clause(params, clause, _fake_run(375000.0))["state"] == "不通过"
    print("[T-SIM-08] 依赖全是 sourced 时绝对阈值照常生效，且随取值翻转（负例必红）")


def test_shipped_criteria_never_hide_an_assumed_dependency_behind_an_absolute_threshold(params):
    """**纪律必须真的在生效**：每条 `absolute` 子句都必须至少点名一个无出处参数。

    若哪天有人把 `merchant_exit_reference_point` 标成 `sourced`，这条会红 ——
    那正是我们要的：出处升级必须是**显式且可复核**的动作，不是回测里悄悄放宽阈值。
    """
    checked = 0
    for crit in CRITERIA:
        for clause in crit.clauses:
            if clause.kind != ABSOLUTE:
                continue
            checked += 1
            unsourced = backtest.unsourced(params, clause)
            assert unsourced, (f"{crit.id}/{clause.id} 是绝对阈值，却声称全部依赖都有出处；"
                               f"若某个参数的出处真的变了，请同时改 `docs/sim-design.md` §9 并留痕")
    assert checked >= 5, f"absolute 子句太少（{checked}），纪律可能根本没被执行到"
    print(f"[T-SIM-08] {checked} 条绝对阈值子句全部被『无出处 ⇒ 不可评估』这条纪律覆盖")


def test_ordering_clause_flips_with_inputs(params):
    #: 方向约定：`direction` 说的是 **A 与 B 的关系**（A 是本判据的主臂）
    clause = Clause("X-3", "A 的走秤率低于 B", ORDERING, "M-04", direction="<")
    assert backtest.judge_clause(params, clause, _fake_run(0.4), _fake_run(0.6))["state"] == "通过"
    assert backtest.judge_clause(params, clause, _fake_run(0.6), _fake_run(0.4))["state"] == "不通过"
    assert backtest.judge_clause(params, clause, _fake_run(0.6), _fake_run(0.6))["state"] == "不通过"
    assert backtest.judge_clause(params, clause, _fake_run(None), _fake_run(0.4))["state"] == "不可评估"
    print("[T-SIM-08] 序关系判据随输入翻转，指标退化时判『不可评估』而不是『通过』（负例）")


def test_monotonic_clause_ignores_too_short_series(params):
    clause = Clause("X-4", "信任单调不增", ORDERING, "M-15", direction="non_increasing")
    assert backtest.judge_clause(params, clause, _fake_run(0.1, series=[0.5, 0.4, 0.2]))["state"] == "通过"
    assert backtest.judge_clause(params, clause, _fake_run(0.1, series=[0.5, 0.6, 0.2]))["state"] == "不通过"
    assert backtest.judge_clause(params, clause, _fake_run(0.1, series=[0.5]))["state"] == "不可评估"
    print("[T-SIM-08] 单调性判据：上升即不通过；序列不足 2 点判不可评估（测不到 ≠ 通过）")


# ---------------------------------------------------------------------------
# 与 `T-SIM-06` 已有的 `R6` 判定衔接（不许另搞一套）
# ---------------------------------------------------------------------------
def test_r6_verdict_matches_the_t_sim_06_helper():
    """`tests/sim/test_scenarios_and_r6.py::r6_verdict` 与本模块对同一组走秤率必须给出同一个三态值。

    这是"衔接"的机械形式：两个模块各写一套判定时，最容易出现的是**口径悄悄分叉**
    （一个把"指标退化"当不成立、另一个当不稳健），而两边都还"绿"。
    """
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    from test_scenarios_and_r6 import r6_verdict

    for funded, self_paid in ((0.6, 0.4), (0.6, 0.6), (0.4, 0.6)):
        params = load_params(PARAMS_PATH)
        clause = next(c for c in CRITERIA[-1].clauses if c.id == "R6-a")
        assert clause.direction == ">", "R6-a 的臂序是 A=出资 / B=自费 ⇒ 『自费更低』必须写成 A>B"
        state = backtest.judge_clause(params, clause, _fake_run(funded), _fake_run(self_paid))["state"]
        mine = "成立" if state == "通过" else "不成立"
        theirs = r6_verdict([funded, self_paid])
        assert mine == theirs, f"两组走秤率 {funded}/{self_paid} 下两边判定不一致：{mine} vs {theirs}"
    print("[T-SIM-08] R6 三态判定与 T-SIM-06 的 r6_verdict 在三组输入上逐条一致")


def test_r6_clause_requires_a_real_cost_in_the_self_funded_arm(params):
    """`R6-b`：自费臂必须真的有一次性支出。出资臂 `upfront=0`、自费臂 `>0` ⇒ 方向必须是 A<B。"""
    clause = next(c for c in CRITERIA[-1].clauses if c.id == "R6-b")
    assert clause.direction == "<", "方向写反会把『自费没花钱』判成通过"
    verdict = backtest.judge_clause(params, clause, _fake_run(0.0, label="出资"), _fake_run(375000.0, label="自费"))
    assert verdict["state"] == "通过"
    assert backtest.judge_clause(params, clause, _fake_run(0.0), _fake_run(0.0))["state"] == "不通过"
    print("[T-SIM-08] R6-b：自费臂一次性支出 > 出资臂（负例：两侧都是 0 时必红）")


# ---------------------------------------------------------------------------
# 2026-10-03 `R3-c` 规格修正的机械守卫（`docs/sim-design.md` §7.1「`R3-c` 判据修订留痕」）
# ---------------------------------------------------------------------------
#: `market_decision_delay_periods = 6` ⇒ 预算/结构决策 6 期后才生效 ⇒ **7 期是硬下限**（210 营业日）
R3_MIN_DAYS = 210


def test_r3_profile_is_long_enough_for_the_decision_delay():
    """**档位下限守卫**：把 `R3` 的档位改短到 <210 营业日（90 日 = 3 期那一档），必须当场红。

    上一轮踩过的坑就是它：90 日档里任何"按期变化"都测不到，而报告当时并没有为此判红。
    """
    crit = CRITERIA_BY_ID["R3"]
    profiles = {crit.profile} | {c.profile for c in crit.clauses if c.profile}
    for profile_id in profiles:
        days = PROFILES[profile_id]["days"]
        assert days >= R3_MIN_DAYS, (
            f"R3 用到的档位 `{profile_id}` 只有 {days} 营业日（< {R3_MIN_DAYS}）"
            f"⇒ `market_decision_delay_periods=6` 下测不到任何按期变化；"
            f"下限依据 docs/sim-design.md §7.1「`R3-c` 判据修订留痕」第 ⑥ 条"
        )
    assert PROFILES[crit.profile]["days"] >= R3_MIN_DAYS
    print(f"[T-SIM-08] R3 档位下限守卫：{sorted(profiles)} 均 ≥ {R3_MIN_DAYS} 营业日")


def test_r3_c_no_longer_judges_the_endogenous_budget_series():
    """`R3-c` 改判**结果量**：`B`（维护预算）不再是任何子句的判据，但序列仍留在产物里可复核。"""
    clauses = {c.id: c for c in CRITERIA_BY_ID["R3"].clauses}
    assert set(clauses) == {"R3-a", "R3-b", "R3-c", "R3-d"}
    assert clauses["R3-c"].measure == "M-10-按期", "R3-c 必须判 DeviceIdleRate 的按期序列"
    assert clauses["R3-c"].direction == "accelerating"
    assert clauses["R3-c"].measure_arm == "B", "加速上升判的是 0.3× 组（B 臂），不是足额组"
    for clause in clauses.values():
        assert clause.measure != "M-17-B", "维护预算不得再作为任何子句的被判量"
    assert "M-17-B" in CRITERIA_BY_ID["R3"].excluded_series, "被移出判据的序列必须留理由，否则修订不可复核"
    print("[T-SIM-08] R3-c 已改为只判结果量；维护预算序列保留为 excluded_evidence（可复核证据）")


@pytest.mark.parametrize(("series", "expected"), [
    ([0.10, 0.12, 0.18, 0.32, 0.56, 0.90], "通过"),      # 逐期增量 0.02→0.06→0.14→0.24→0.34：斜率递增
    ([0.70, 0.75, 0.80, 0.85, 0.90, 0.95], "不通过"),      # 线性上升：不是加速
    ([0.90, 0.88, 0.95, 0.99, 1.0, 1.0], "不通过"),        # 有回落
    ([0.95, 1.0, 1.0, 1.0, 1.0, 1.0], "不通过"),          # **无饱和上限时**按形状判 ⇒ 不通过
    ([0.95, 1.0], "不可评估"),                              # 点数不足 ⇒ 测不到 ≠ 通过
])
def test_accelerating_judge_is_sensitive_to_the_series_shape(series, expected):
    """**灵敏度负例**：「加速上升」的判定必须随序列形状改变。"""
    verdict = backtest.judge_accelerating({}, series)
    assert verdict["state"] == expected, verdict


@pytest.mark.parametrize(("series", "expected"), [
    ([0.947, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0], "不可评估"),   # 本轮 `R3-c` 的实测形态
    ([0.10, 0.12, 0.18, 0.32, 0.56, 0.90], "通过"),             # 不饱和 ⇒ 照常按形状判
    ([0.70, 0.75, 0.80, 0.85, 0.90, 0.95], "不通过"),           # 不饱和且线性 ⇒ 真不通过
    ([0.90, 1.0, 0.95, 0.60, 0.55, 0.50], "不通过"),           # 中途触顶又回落 ⇒ 不算饱和
])
def test_saturation_yields_unevaluable_not_failed(series, expected):
    """**饱和 = `不可评估`，不是「不通过」**（父代理 `2026-10-03` 第二层裁定）。

    最后一条是关键的**反向断言**：序列**中途**触顶又被拉回来 ⇒ 指标还能动 ⇒ 不算饱和、
    照常按形状判。明令**不得**写成"触顶即视为非加速"——那是把不可评估偷偷转成不通过。
    """
    assert backtest.judge_accelerating({}, series, ceiling=1.0)["state"] == expected


def test_saturation_evidence_records_ceiling_and_period_count():
    """饱和档必须在产物里写明**饱和证据**（序列 + 触顶期数 + 触顶起始期），否则不可复核。"""
    verdict = backtest.judge_accelerating({}, [0.947, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0], ceiling=1.0)
    observed = verdict["observed"]
    assert (observed["periods_at_ceiling"], observed["saturated_from_period"]) == (7, 2)
    assert observed["ceiling"] == 1.0
    assert "不可评估 ≠ 不通过" in verdict["reason"], verdict["reason"]
    assert backtest.saturation_periods([0.4, 0.4, 0.5, 0.711, 0.333], 1.0) == 0
    assert backtest.saturation_periods([0.9, 1.0, 1.0], 1.0) == 2
    print("[T-SIM-08] 饱和判『不可评估』并留下触顶期数证据；中途触顶回落不判饱和（负例）")


def test_r3_c_declares_its_saturation_ceiling():
    """`R3-c` 必须**显式声明**饱和上限（`ceiling=1.0`）：规则写在判据里，不是藏在判定器里。"""
    clause = next(c for c in CRITERIA_BY_ID["R3"].clauses if c.id == "R3-c")
    assert clause.ceiling == 1.0, "R3-c 没声明饱和上限 ⇒ 饱和时会被当成形状不通过"
    assert "不可评估 ≠ 不通过" in clause.note and "触顶即视为非加速" in clause.note
    print("[T-SIM-08] R3-c 显式声明 ceiling=1.0，判据文本里写明『不可评估 ≠ 不通过』")


def test_r3_c_clause_wires_the_accelerating_direction_end_to_end(params):
    """`R3-c` 的整条链路：子句声明 → `judge_clause` 走的是加速分支（不是掉进单调性分支）。"""
    clause = next(c for c in CRITERIA_BY_ID["R3"].clauses if c.id == "R3-c")

    def _arm(**series):
        run = _fake_run(0.9)
        run["rows"][0]["series"].update(series)
        return run

    #: ⚠️ `measure_arm="B"` ⇒ 序列挂在 **B 臂**上（0.3× 组）；挂在 A 臂上这条子句读不到值 ——
    #: 「判据文本说的组」与「臂表顺序」搞混正是 `R4-c` 第一版的真缺陷。
    verdict = backtest.judge_clause(params, clause, _arm(), _arm(**{"M-10-按期": [0.10, 0.12, 0.18, 0.32, 0.56, 0.90]}))
    assert verdict["state"] == "通过", verdict
    assert "slope_second" in verdict["observed"], "加速判定必须把两段斜率写进产物，否则不可复核"
    #: **负例**：换成实测的饱和形态 ⇒ 必须翻成 `不可评估`（证明它读的是 `M-10-按期` 而不是预算序列）
    ceiling = _arm(**{"M-10-按期": [0.947, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
                      "M-17-B": [1.5e6, 1.68e6, 1.875e6]})
    assert backtest.judge_clause(params, clause, _arm(), ceiling)["state"] == "不可评估"
    print("[T-SIM-08] R3-c 整链路：不饱和且加速判通过、实测饱和形态判不可评估（负例必红）")


def test_excluded_evidence_reports_the_rising_budget_series(params):
    """被移出判据的预算序列**必须仍能取到值**，否则"修订留痕"就只剩一句空话。"""
    run = _fake_run(0.9)
    run["rows"][0]["series"]["M-17-B"] = [1500000.0, 1500000.0, 1680000.0, 1875000.0]
    evidence = backtest.excluded_evidence(CRITERIA_BY_ID["R3"], {"B": run})
    assert evidence["M-17-B"]["monotone"] == "上升", evidence
    assert evidence["M-17-B"]["series"] == [1500000.0, 1500000.0, 1680000.0, 1875000.0]
    assert "不是" in evidence["M-17-B"]["why"] or "不再" in evidence["M-17-B"]["why"]
    #: **反向断言**：没有 `excluded_series` 的判据 ⇒ 本函数返回空（不是无条件塞一份占位）
    assert backtest.excluded_evidence(CRITERIA_BY_ID["R1"], {"B": run}) == {}
    print("[T-SIM-08] 被移出判据的预算序列仍落进产物并标为『上升』（负例：未声明的判据返回空）")