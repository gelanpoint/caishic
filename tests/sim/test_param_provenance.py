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

import pytest

from sim_support import PARAMS_PATH

from sim.core.params import ParamsError, load_params, provenance_problems


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
