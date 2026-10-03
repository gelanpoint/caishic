"""`T-SIM-01` 隔离闸门之二：**参数出处必填**（`docs/sim-design.md` §2.5）。

「参数必须标来源」是一句承诺，承诺会漂移；把出处写进参数文件并由测试强制，它才变成约束。
规则（唯一权威是 `sim/core/params.py` 的 `provenance_problems`，本文件不另写一套）：

- `sourced` → 必须有非空 `ref`，且只允许 `结论 N` / `D-xx`；
- **`Q-xx` 不算 sourced** —— 那是项目自己登记的待确认假设，把它当"有出处"就是把假设写成事实；
- `assumed` → 必须有非空 `calibration`（校准思路）。

本文件自带**合成负例**：直接喂合成条目给纯函数校验器，逐个断言必须判红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sim_support import PARAMS_PATH

from sim.core.params import Params, ParamsError, load_params, provenance_problems


def test_shipped_params_file_is_provenance_clean():
    """仓库里真正那份参数文件必须全部合规（sourced 有出处 / assumed 有校准思路）。"""
    params = load_params(PARAMS_PATH)
    sourced, assumed = params.sourced_ids(), params.assumed_ids()
    assert sourced, "一个 sourced 参数都没有 —— 说明出处标注被清空了"
    assert assumed, "一个 assumed 参数都没有 —— 说明假设被伪装成了事实"
    print(f"[T-SIM-01] 参数文件合规：sourced={len(sourced)} 项、assumed={len(assumed)} 项")
    print(f"[T-SIM-01] sourced：{sourced}")
    print(f"[T-SIM-01] assumed：{assumed}")


def test_param_ids_are_unique_and_blocks_are_ordered():
    """参数 id 不得重复、时段块必须按起始时刻递增（顺序错了客流时间轴就错）。"""
    raw = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
    ids = list(raw["parameters"].keys())
    assert len(ids) == len(set(ids)), "参数 id 有重复（JSON 里重复键会被静默吞掉）"
    starts = [block["start"] for block in raw["blocks"]]
    assert starts == sorted(starts), f"时段块未按起始时刻升序：{starts}"
    print(f"[T-SIM-01] {len(ids)} 个参数 id 唯一；{len(starts)} 个时段块按时序排列 {starts}")


def test_every_param_entry_carries_a_value_or_range():
    """每个参数必须有 `value` 或 `range`（否则仿真无从取值，却可能蒙混过出处检查）。"""
    raw = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
    for pid, entry in raw["parameters"].items():
        assert "value" in entry or "range" in entry, f"参数 {pid} 既无 value 也无 range"


def test_blocks_carry_provenance_and_timing():
    """时段块（强度/时刻）同样要标出处，且必须给出 `start` 与 `intensity`。"""
    raw = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
    for index, block in enumerate(raw["blocks"]):
        assert "start" in block and "intensity" in block, f"时段块 {block.get('name')} 缺 start/intensity"
        problems = provenance_problems(f"blocks[{index}]", block, "合成")
        assert problems == [], f"时段块 {block.get('name')} 出处不合规：{problems}"
    print(f"[T-SIM-01] {len(raw['blocks'])} 个时段块出处全部合规（主峰/次峰来自 Q-9，标为 assumed）")


# ---------------------------------------------------------------------------
# 灵敏度负例：合成条目逐个必须判红
# ---------------------------------------------------------------------------
def test_sourced_without_ref_is_red():
    entry = {"value": 1, "provenance": {"kind": "sourced"}}
    problems = provenance_problems("合成参数", entry, "合成文件")
    assert problems and "为空" in problems[0], problems
    print(f"[T-SIM-01] sourced 缺 ref → 判红：{problems[0]}")


def test_q_ref_cannot_be_claimed_as_sourced():
    """**最关键的一条**：`Q-xx` 是项目自认的待确认假设，不许当出处。"""
    entry = {"value": 1, "provenance": {"kind": "sourced", "ref": "Q-9"}}
    problems = provenance_problems("合成参数", entry, "合成文件")
    assert problems and "必须改标 assumed" in problems[0], problems
    print(f"[T-SIM-01] Q-9 冒充 sourced → 判红：{problems[0]}")


def test_assumed_without_calibration_is_red():
    entry = {"value": 1, "provenance": {"kind": "assumed", "ref": "Q-9"}}
    problems = provenance_problems("合成参数", entry, "合成文件")
    assert problems and "校准思路" in problems[0], problems
    print(f"[T-SIM-01] assumed 缺校准思路 → 判红：{problems[0]}")


def test_missing_provenance_and_bad_kind_are_red():
    assert provenance_problems("p", {"value": 1}, "合成") != []
    bad_kind = provenance_problems("p", {"value": 1, "provenance": {"kind": "事实"}}, "合成")
    assert bad_kind and "只允许" in bad_kind[0]
    print(f"[T-SIM-01] 缺 provenance / 非法 kind → 均判红：{bad_kind[0]}")


def test_missing_or_broken_params_file_fails_loud(tmp_path):
    """读不到 / 不是 JSON → 明确报错，**不静默用默认值**（那会让"出处没标"变成看不见的事）。"""
    with pytest.raises(ParamsError):
        load_params(tmp_path / "不存在.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{不是 JSON", encoding="utf-8")
    with pytest.raises(ParamsError):
        load_params(broken)
    print("[T-SIM-01] 参数文件缺失 / 非法 JSON → 均明确报错")


def test_clean_synthetic_entry_stays_green():
    """反向断言：合规的 sourced / assumed 条目都不得误报。"""
    assert provenance_problems("a", {"value": 1, "provenance": {"kind": "sourced", "ref": "结论 23"}}, "x") == []
    assert provenance_problems("b", {"range": [1, 2], "provenance": {"kind": "sourced", "ref": "D-02"}}, "x") == []
    assert provenance_problems("c", {"value": 1, "provenance": {"kind": "assumed", "calibration": "待实地采集"}}, "x") == []
    print("[T-SIM-01] 合规条目（结论 N / D-xx / assumed+calibration）零误报")


# ---------------------------------------------------------------------------
# `T-SIM-08` 验收④：回测判据里引用的每个参数，都必须过本文件这一套出处校验
# ---------------------------------------------------------------------------
def test_every_key_used_by_the_backtest_criteria_is_a_registered_parameter():
    """`R1`~`R6` 的 `depends_on` / `scan_keys` / 臂覆盖里出现的每个参数名都必须在参数文件里。

    **本文件是出处的唯一权威**（`params.py::provenance_problems` + `Params.kind`），
    回测侧不另写一套"看起来像出处"的检查 —— 那样两套规则迟早分叉。
    """
    from sim.verify.r_criteria import CRITERIA, PROFILES

    params = load_params(PARAMS_PATH)
    known = set(params.parameters())
    referenced: set[str] = set()
    for crit in CRITERIA:
        referenced |= set(crit.scan_keys)
        for clause in crit.clauses:
            referenced |= set(clause.depends_on)
        for arm in crit.arms.values():
            referenced |= set(arm.get("extra") or {})
    for prof in PROFILES.values():
        referenced |= set(prof.get("param_overrides") or {})
    unknown = sorted(referenced - known)
    assert not unknown, f"回测引用了参数文件里没有的键：{unknown}"
    print(f"[T-SIM-08] 回测引用的 {len(referenced)} 个参数键全部在 {Path(PARAMS_PATH).name} 里登记")


def test_backtest_provenance_reading_matches_the_params_file():
    """回测判定用的 `Params.kind` 与本文件的校验结论必须一致（不允许第二套"更宽松"的判定）。

    **灵敏度负例**：把某条 `assumed` 伪造成 `sourced`，回测侧必须立刻不再把它算进
    "无出处"名单 —— 这证明它读的是参数文件，而不是自己抄了一份分类表。
    """
    from sim.verify.backtest import unsourced
    from sim.verify.r_criteria import ABSOLUTE, CRITERIA, Clause

    params = load_params(PARAMS_PATH)
    clause = Clause("X", "合成", ABSOLUTE, "M-02", depends_on=("merchant_exit_reference_point",), threshold=6)
    assert unsourced(params, clause) == ["merchant_exit_reference_point(assumed)"]

    raw = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
    entry = raw["parameters"]["merchant_exit_reference_point"]
    entry["provenance"] = {"kind": "sourced", "ref": "结论 8"}
    assert provenance_problems("merchant_exit_reference_point", entry, "合成") == [], \
        "合成的 sourced 自身必须先合规（否则下面的负例测的不是同一件事）"
    forged = Params(raw, "合成文件")
    assert unsourced(forged, clause) == [], "参数文件标成 sourced 之后，回测侧仍把它算成无出处 ⇒ 两套规则"
    print("[T-SIM-08] 回测的出处判定读的是参数文件（负例：伪造成 sourced 后名单立刻清空）")
