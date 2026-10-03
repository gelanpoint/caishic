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

from sim_support import PARAMS_PATH, REPO_ROOT

from sim.core.params import (
    Params, ParamsError, Q_REF_RE, conclusion_blocks, conclusion_numbers, load_params,
    provenance_problems, q_ref_problems, ref_conclusion_numbers, ref_target_problems,
)


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


# ---------------------------------------------------------------------------
# `T-SIM-12`：`ref` 指向的**结论号**必须真实存在 + `Q-xx` 必须归 `assumed`
#
# 上一任如实登记：「全部转引自 `sim-design.md` §9 与 `params.json.provenance.ref`，
# **没有逐条打开调研报告核对原文与编号**」—— 这是当时最大的未复核面。本节把它变成机械检查。
# ---------------------------------------------------------------------------
#: 《调研报告》原文（`ref` 的唯一权威来源；`params.json` 的 ref 指的就是它的结论号）
REPORT_PATH = REPO_ROOT / "docs" / "调研报告-现实情况.md"


def _report_conclusions() -> set[int]:
    return conclusion_numbers(REPORT_PATH.read_text(encoding="utf-8"))


def test_report_conclusion_set_is_1_to_26_contiguous():
    """**前提守卫**：报告的结论号集合必须是 1~26 连续。

    没有这一条，"ref 指向的 N 都存在" 可能在集合为空/只剩一个号时**假绿** ——
    检查自身空转比检查失效更坏。
    """
    numbers = _report_conclusions()
    assert numbers == set(range(1, 27)), f"报告结论号集合不是 1~26 连续：{sorted(numbers)}"
    print(f"[T-SIM-12] 《调研报告》结论号集合 = {min(numbers)}~{max(numbers)}（连续，共 {len(numbers)} 条）")


def test_every_conclusion_ref_points_to_a_real_conclusion():
    """**本节的主闸门**：每条 `ref` 里的每个 `结论 N`，`N` 必须真实存在于报告里。"""
    known = _report_conclusions()
    raw = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
    entries = [(pid, e) for pid, e in raw["parameters"].items()]
    entries += [(f"blocks[{i}]", b) for i, b in enumerate(raw.get("blocks") or [])]
    problems: list[str] = []
    referenced: dict[int, list[str]] = {}
    for entry_id, entry in entries:
        ref = (entry.get("provenance") or {}).get("ref")
        problems += ref_target_problems(entry_id, ref, known)
        for number in ref_conclusion_numbers(ref):
            referenced.setdefault(number, []).append(entry_id)
    assert problems == [], f"有 ref 指向不存在的结论号：{problems}"
    assert referenced, "一条结论号都没被引用 —— 闸门空转了"
    uncovered = sorted(known - set(referenced))
    print(f"[T-SIM-12] {sum(len(v) for v in referenced.values())} 处结论引用全部命中；"
          f"引用到的结论号 {sorted(referenced)}")
    print(f"[T-SIM-12] 本项目**未**引用的结论号：{uncovered or '（无）'}")


def test_ref_pointing_at_a_nonexistent_conclusion_is_red():
    """**灵敏度负例**：把某条 `ref` 伪造成 `结论 99`（报告只到 26）⇒ 必须判红。

    同时负例第二问：把集合换成空的 ⇒ 也必须判红（否则"集合为空"会让所有 `ref` 通过）。
    """
    assert ref_target_problems("合成参数", "结论 99", {1, 26}), "指向不存在的结论号却没判红"
    assert ref_target_problems("合成参数", "结论 26", {1, 26}) == []
    assert ref_target_problems("合成参数", "结论 4 / 5 / 6（时间线）", {4, 5, 6}) == []
    assert ref_target_problems("合成参数", "结论 4 / 99", {4}), "多号 ref 里混一个不存在的也必须判红"
    assert ref_target_problems("合成参数", "结论 1", set()), "集合为空时必须判红（否则闸门空转）"
    assert ref_target_problems("合成参数", None, set()) == [], "没有 ref 就不是这条闸门的事"
    print("[T-SIM-12] 负例：`结论 99` 与空集合均判红；`结论 4 / 5 / 6` 零误报")


def test_conclusion_number_extraction_ignores_cross_references():
    """**负例的前提守卫**：正文里的交叉引用（`结合结论 13、14`）**不得**混进结论号集合。

    若混进去，"结论号集合"会虚高，于是"指向了不存在的结论"这类错误检不出来。
    """
    numbers = conclusion_numbers("**结论 7：能查溯源的机器被扔在角落。**\n这里结合结论 13、14 一并看。\n")
    assert numbers == {7}, f"把正文交叉引用也算成结论了：{sorted(numbers)}"
    print("[T-SIM-12] 结论号只认行首粗体标题；正文交叉引用不进集合")


def test_every_q_ref_entry_is_marked_assumed():
    """父代理裁定：**`Q-xx` 一律 `assumed`**（那是项目自认的待确认假设，不是查证事实）。"""
    raw = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
    entries = [(pid, e) for pid, e in raw["parameters"].items()]
    entries += [(f"blocks[{i}]", b) for i, b in enumerate(raw.get("blocks") or [])]
    problems: list[str] = []
    q_holders: list[str] = []
    for entry_id, entry in entries:
        prov = entry.get("provenance") or {}
        ref = prov.get("ref")
        if isinstance(ref, str) and Q_REF_RE.search(ref):
            q_holders.append(f"{entry_id}({Q_REF_RE.search(ref).group(0)})")
        problems += q_ref_problems(entry_id, prov.get("kind"), ref)
    assert q_holders, "一条 `Q-xx` 引用都没有 ⇒ 本用例在本文件上没有载体"
    assert problems == [], f"`Q-xx` 没有归 assumed：{problems}"
    print(f"[T-SIM-12] `Q-xx` 引用 {len(q_holders)} 处，全部 `assumed`：{q_holders}")


def test_q_ref_marked_sourced_is_red():
    """**灵敏度负例**：`Q-9` 被标成 `sourced`（或任何非 `assumed`）⇒ 必须判红。"""
    for kind in ("sourced", "事实"):
        problems = q_ref_problems("合成参数", kind, "Q-9")
        assert problems and "必须归 `assumed`" in problems[0], problems
    assert q_ref_problems("合成参数", "assumed", "Q-9") == []
    assert q_ref_problems("合成参数", "sourced", "结论 4") == [], "普通结论号不该被这条闸门误伤"
    print("[T-SIM-12] 负例：`Q-9` 标成 sourced/事实 均判红；`结论 4` 零误报")


# ---------------------------------------------------------------------------
# `T-SIM-12`：**人工对照表的可执行版本** —— `sourced` 参数的取值必须等于报告原文写的那个金额
#
# 上一任登记的「最大未复核面」里，编号存在性只是第一层；**取值是否就是报告里那个数**
# 机械检查做不了（需要人判断"这个参数对应结论里的哪个量"），但**人判断完之后可以把结论固化成数据**。
# 下表就是那份人工对照表：每行 `参数键 -> (ref 结论号, 报告原文片段, 折算后的值/区间)`。
# 用例做两件机械的事：① 报告原文片段**必须真的出现在**那条结论里（防止表抄错 ref）；
# ② 参数的 `value`/`range` 必须等于折算值。
#
# ⚠️ 本轮在这张表上查出并修掉了**一处 10 倍量级错**：`network_refit_cents_per_market`
# 原值 12,050,000 分（= 12.05 万元），而结论 25 原文与本条目自己的 `quote` 都写 120.5 万元
# = 120,500,000 分。`provenance_problems` 只查形态、查不出这个 —— **这正是这张表存在的理由**。
# ---------------------------------------------------------------------------
#: `参数键 -> (结论号, 报告原文里必须存在的片段, 折算后的 value, 折算后的 range 或 None)`
SOURCED_AMOUNT_TABLE: dict[str, tuple[int, str, object, object]] = {
    "admin_fine_market_operator_cents": (2, "市场开办者被罚 2 万元", 2_000_000, None),
    "bank_funding_cents_3y": (19, "三年期 200 余万元", 200_000_000, None),
    "channel_fee_rate_bp": (21, "费率是 0.38%–0.6%", 38, [38, 60]),
    "channel_fee_rate_bp_range": (21, "费率是 0.38%–0.6%", None, [38, 60]),
    "device_screen_unit_price_cents": (24, "摊位屏 1100 元/台", 110_000, None),
    "merchant_cloud_software_cents_per_year": (26, "云软件 5000 元/年", 500_000, None),
    "merchant_self_funded_scale_cents": (26, "智慧电子秤 3750 元/台", 375_000, None),
    "network_refit_cents_per_market": (25, "合计占整个 366.9 万元标的的 33%", 120_500_000, None),
    "scale_unit_price_cents": (23, "普通溯源秤 **1350 元/台**", 135_000, [135_000, 256_600]),
    "subsidy_ratio_bp": (3, "智慧菜场按审计实际投入的 50% 补贴", 5_000, None),
    "verification_sampling_bp": (4, "按照 10% 的比例抽检验收", 1_000, None),
    "wenzhou_buyer_rate_bp_range": (12, "收取交易额的 **0.5%–6%**", None, [50, 600]),
}


#: `sourced` 里**取值不是报告原文里那个数**的参数（所以不在上面那张表里）。
#: 每一个都必须在这里写明"为什么不是"，否则 `test_every_sourced_amount_param_is_in_the_table` 判红。
DERIVED_NOT_IN_TABLE = {
    # `结论 13` 只给出**定性**表述（"不能再搞八两秤了"），报告里**没有**任何短秤幅度数字。
    # 取值 0.2 是把俗语「八两秤」读作 8 两 / 1 斤 = 0.8 得到的推算 —— `quote` 里已写明"分布与比例无出处"。
    # 它仍是 `sourced`：**行为事实**（这类行为真实存在且被官方点名）有出处，**幅度**没有。
    "short_weight_ratio",
}


def sourced_amount_problems(params, blocks: dict[int, str], table=None) -> list[str]:
    """**人工对照表的判定函数**（纯函数，故合成负例可以直接喂伪造的 `Params`）。

    两关都过才算过：① 报告原文里**真的**有那句话（表没把 ref 抄错）；
    ② 参数取值真的等于它（没抄错也没算错）。
    """
    table = SOURCED_AMOUNT_TABLE if table is None else table
    problems: list[str] = []
    for key, (number, fragment, value, rng) in table.items():
        if fragment not in blocks.get(number, ""):
            problems.append(f"{key}: 结论 {number} 原文里找不到 {fragment!r}（对照表抄错了 ref？）")
        entry = params.parameters().get(key)
        if entry is None:
            problems.append(f"{key}: 参数文件里没有这一项")
            continue
        if value is not None and entry.get("value") != value:
            problems.append(f"{key}: 取值 {entry.get('value')} ≠ 报告折算值 {value}")
        if rng is not None and list(entry.get("range") or []) != list(rng):
            problems.append(f"{key}: 区间 {entry.get('range')} ≠ 报告折算区间 {rng}")
    return problems


def test_sourced_amounts_equal_the_figure_written_in_the_report():
    """**人工对照表的机械断言**：参数取值 == 报告原文写的那个金额。"""
    blocks = conclusion_blocks(REPORT_PATH.read_text(encoding="utf-8"))
    problems = sourced_amount_problems(load_params(PARAMS_PATH), blocks)
    assert problems == [], "sourced 参数的取值与报告原文对不上：\n  " + "\n  ".join(problems)
    print(f"[T-SIM-12] {len(SOURCED_AMOUNT_TABLE)} 个 sourced 参数的取值 == 报告原文金额（逐条核对通过）")


def test_every_sourced_amount_param_is_in_the_table():
    """**防漏守卫**：`sourced` 参数**每一个**都必须在这张对照表里有行（或在下面的例外名单里）。

    漏一行就等于放弃一个参数的复核，而漏了不会有人发现 —— 故让它显式爆炸。
    """
    params = load_params(PARAMS_PATH)
    missing = sorted(set(params.sourced_ids()) - set(SOURCED_AMOUNT_TABLE) - set(DERIVED_NOT_IN_TABLE))
    assert missing == [], (
        f"这些 sourced 参数既不在金额对照表、也没登记进 DERIVED_NOT_IN_TABLE（取值不是报告里的原数）：{missing}"
    )
    print(f"[T-SIM-12] {len(params.sourced_ids())} 个 sourced 参数全部有交代；"
          f"例外 {sorted(DERIVED_NOT_IN_TABLE)}（取值来自报告原文之外的推算，已在 quote 里写明）")


def test_amount_table_goes_red_when_a_value_contradicts_the_report():
    """**灵敏度负例**：把取值改回本轮修掉的那个 **10 倍错** ⇒ 判定函数必须报出问题。

    负例必须**真跑判定函数**才算数：只断言"伪造值 ≠ 期望值"证明不了这张表会红。
    用**内存里的 `Params`** 改（不碰磁盘），证明它读的是参数而不是自己抄了一份常量。
    """
    blocks = conclusion_blocks(REPORT_PATH.read_text(encoding="utf-8"))
    raw = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
    key = "network_refit_cents_per_market"
    _number, _fragment, value, _rng = SOURCED_AMOUNT_TABLE[key]
    assert sourced_amount_problems(Params(raw, "合成文件"), blocks) == [], "负例前提：伪造前必须先合规"
    raw["parameters"][key]["value"] = 12_050_000  # 本轮修掉的 10 倍量级错（= 12.05 万元）
    problems = sourced_amount_problems(Params(raw, "合成文件"), blocks)
    assert any(key in problem for problem in problems), f"退回 10 倍错值却没判红：{problems}"
    assert f"{value}" in problems[0], f"报错信息应当点出正确值 {value}：{problems[0]}"
    print(f"[T-SIM-12] 负例：`network_refit` 退回 12,050,000 分（10 倍错）⇒ 判红（{problems[0]}）")


def test_amount_table_goes_red_when_a_ref_points_at_the_wrong_conclusion():
    """**灵敏度负例（第二问）**：把对照表某行的结论号抄错 ⇒ 必须判红（防止"表抄错 ref"静默通过）。"""
    blocks = conclusion_blocks(REPORT_PATH.read_text(encoding="utf-8"))
    wrong = {**SOURCED_AMOUNT_TABLE,
             "subsidy_ratio_bp": (12, SOURCED_AMOUNT_TABLE["subsidy_ratio_bp"][1], 5_000, None)}
    problems = sourced_amount_problems(load_params(PARAMS_PATH), blocks, table=wrong)
    assert problems and "抄错了 ref" in problems[0], f"把结论 3 的原文错记成结论 12 却没判红：{problems}"
    print(f"[T-SIM-12] 负例：把 `subsidy_ratio_bp` 的出处错记为结论 12 ⇒ 判红（{problems[0]}）")


def test_conclusion_block_slices_only_its_own_paragraph():
    """**负例的前提守卫**：`conclusion_blocks(13)` 不得串进结论 26 的正文（否则对照表会抄到邻居的数）。"""
    blocks = conclusion_blocks(REPORT_PATH.read_text(encoding="utf-8"))
    assert set(blocks) == set(range(1, 27)), f"结论块不完整：{sorted(blocks)}"
    assert "八两秤" in blocks[13], "结论 13 的正文没切到"
    assert "云软件 5000 元/年" not in blocks[13], "结论 13 的块里串进了结论 26 的原文 ⇒ 切分坏了"
    assert "云软件 5000 元/年" in blocks[26], "结论 26 的正文没切到"
    assert "普通溯源秤" not in blocks[26], "结论 26 的块里串进了结论 23 的原文 ⇒ 切分坏了"
    print("[T-SIM-12] 结论块按标题切分：13/26 各自只含自己的原文（未串到邻居）")


def test_derived_not_in_table_entries_declare_their_derivation():
    """**例外名单的守卫**：不在金额表里的 `sourced` 参数，`quote` 必须写明"怎么来的"。"""
    params = load_params(PARAMS_PATH)
    for key in sorted(DERIVED_NOT_IN_TABLE):
        quote = str((params.parameters()[key]["provenance"] or {}).get("quote") or "")
        assert "无出处" in quote or "推算" in quote, (
            f"{key} 不在金额对照表里，`quote` 却没说清取值怎么来的：{quote!r}"
        )
        assert "结论" in str(params.parameters()[key]["provenance"]["ref"]), f"{key} 的 ref 必须仍指向报告结论"
    print(f"[T-SIM-12] 例外名单 {sorted(DERIVED_NOT_IN_TABLE)}：quote 均写明推算来源与『无出处』")
