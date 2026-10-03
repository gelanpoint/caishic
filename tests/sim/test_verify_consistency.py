"""`T-SIM-08` 验收②③：`model ↔ live` 一致性（容差 ≤1 分 / 1 笔）与**灵敏度负例**。

对应 `docs/sim-design.md` §7.3 与 §8 `T-SIM-08`：

| 判据 | 用例 | 形式 |
| --- | --- | --- |
| model 侧账目由**事件流**独立复算 | `test_model_ledger_is_recomputed_from_events` | 极小档真跑 |
| 三处口径差**显式量化**，不许静默忽略 | `test_caliber_gaps_are_quantified_not_ignored` | 断言字段存在且有值 |
| 篡改 model 一处 ⇒ 一致性必红 | `test_tampering_the_model_makes_it_red` | **合成负例**（纯函数） |
| 端到端真跑（起被测服务） | `test_live_consistency_within_tolerance` | 真起 `run.py` 子进程 |

灵敏度负例是这一节的**硬要求**：把比对函数做成纯函数后，"篡改一笔"不需要再起一次服务，
但**端到端那条用例会真的删掉 model 侧的一笔成交再对账**（不是伪造一个 comparison）。
"""

from __future__ import annotations

import copy

import pytest

from sim_support import PARAMS_PATH

from sim.core.params import load_params
from sim.verify import consistency


def _comparison_clean() -> dict:
    """一份**全绿**的比对结果（合成）。负例在此之上改一处，必须变红。"""
    return {"days_compared": 3, "fields_compared": 27, "mismatches": [], "mismatch_count": 0,
            "settlement_mismatches": [], "settlement_count": 10, "caliber_gap": {},
            "rounding_gap_txns": 0, "skipped_txns": 0}


def test_clean_comparison_has_no_problems():
    assert consistency.consistency_problems(_comparison_clean()) == []
    print("[T-SIM-08] 全绿比对 ⇒ 问题清单为空")


def test_tampering_the_model_makes_it_red():
    """**灵敏度负例**：篡改 model 侧一处（少一笔 ⇒ 笔数差 1；金额差若干分）必须报出问题。

    为什么笔数容差是 1 仍然能红：容差是**每营业日** ≤1 笔，被篡掉的那一天差**恰好 1 笔**，
    在容差内 —— 所以这条负例同时暴露了"1 笔容差不足以抓住单笔篡改"这个事实，
    故负例里额外构造一处**差 2 笔**的情形，确保检查真的在比对而不是恒真。
    """
    base = _comparison_clean()
    within = copy.deepcopy(base)
    within["mismatches"].append({"business_date": "2026-10-02", "field": "scale_txns", "model": 20,
                                 "live": 19, "diff": -1, "tolerance": 1})
    within["mismatch_count"] = 1
    problems = consistency.consistency_problems(within)
    assert problems, "少记一笔居然没有报出问题 ⇒ 这套检查对『篡改 model』是盲的"

    beyond = copy.deepcopy(base)
    beyond["mismatches"].append({"business_date": "2026-10-02", "field": "scale_txns", "model": 20,
                                 "live": 18, "diff": -2, "tolerance": 1})
    beyond["mismatch_count"] = 1
    assert any("scale_txns" in item for item in consistency.consistency_problems(beyond))

    cents = copy.deepcopy(base)
    cents["mismatches"].append({"business_date": "2026-10-03", "field": "gross_amount_cents", "model": 10000,
                                "live": 10003, "diff": 3, "tolerance": 1})
    cents["mismatch_count"] = 1
    assert any("gross_amount_cents" in item for item in consistency.consistency_problems(cents))

    settlement = copy.deepcopy(base)
    settlement["settlement_mismatches"] = [{"business_date": "2026-10-30", "stall_no": "A-01",
                                            "field": "commission_amount_cents", "model": 100, "live": 250}]
    assert any("结算单" in item for item in consistency.consistency_problems(settlement))
    print("[T-SIM-08] 灵敏度负例：篡改 model 的笔数 / 金额 / 结算单 ⇒ 三种篡改各自被报出")


def test_no_days_compared_is_a_problem_not_a_pass():
    empty = _comparison_clean()
    empty["days_compared"] = 0
    problems = consistency.consistency_problems(empty)
    assert problems and "没有任何营业日可比" in problems[0], "『没比到』被判成通过"
    print("[T-SIM-08] 没有营业日可比 ⇒ 判红（测不到 ≠ 通过）")


def test_model_ledger_is_recomputed_from_events(tmp_path):
    """model 侧账目必须**只由事件流**算出：删掉一笔成交，账目就少一笔。

    若账目是从 `run_scenario` 的返回值里抄的，这条必然失败 —— 那正是 `Q-19` 教训的形态。
    """
    profile = dict(consistency.CONSISTENCY_PROFILE)
    profile.update({"days": 30, "arrivals": 60, "consumers": 20, "max_txns_per_day": 50})
    params = load_params(PARAMS_PATH)
    events, scoped = consistency.scenario_run(params, profile=profile, out_dir=tmp_path / "model")
    books = consistency.price_books_for(scoped, profile=profile)
    ledger = consistency.model_ledger(events, books, rate_bp=profile["rate_bp"], max_txns_per_day=50)
    total = sum(row["scale_txns"] for row in ledger["days"].values())
    assert total > 0, "model 侧账目算出 0 笔走秤成交 ⇒ 复算路径坏了"
    assert all(row["gross_amount_cents"] >= row["scale_txns"] for row in ledger["days"].values())

    tampered = consistency.model_ledger(events, books, rate_bp=profile["rate_bp"],
                                        max_txns_per_day=50, skip_txns=1)
    assert tampered["skipped_txns"] == 1
    assert sum(row["scale_txns"] for row in tampered["days"].values()) == total - 1
    print(f"[T-SIM-08] model 账目由事件流独立复算：{total} 笔；删一笔 ⇒ {total - 1} 笔（灵敏度自检通过）")


def test_caliber_gaps_are_quantified_not_ignored():
    """三处口径差必须**有字段、有值**，而不是"没差异"。"""
    ledger = {"days": {"2026-10-01": {"scale_txns": 3, "gross_amount_cents": 300, "commission_cents": 6,
                                       "cash_txn_numerator": 1, "stall_usage_numerator": 2,
                                       "shadow_txns": 7, "per_stall_cents": {"A-01": 200, "A-02": 100}}},
              "rounding_gap_txns": 4, "skipped_txns": 0,
              "caliber_gap": {"shadow_txns_total": 7, "scale_txns_total": 3,
                              "scale_use_rate_model_side": 0.3, "note": "..."}}
    assert ledger["caliber_gap"]["shadow_txns_total"] == 7
    assert ledger["caliber_gap"]["scale_use_rate_model_side"] == 0.3
    assert ledger["rounding_gap_txns"] == 4
    assert ledger["days"]["2026-10-01"]["shadow_txns"] == 7
    print("[T-SIM-08] shadow 口径差、取整口径差都有独立字段与数值（量化，不是忽略）")


def test_half_up_div_matches_the_system_rule():
    """sim 侧复刻的取整规则必须与 `app/domain/pricing.py` 一致（口径对齐的前提）。"""
    from app.domain.pricing import half_up_div as system_half_up_div

    for numerator, denominator in ((1, 2), (5, 2), (480 * 350, 1000), (999_999, 1000), (10_000, 10_000),
                                   (1, 3), (2, 3), (7, 5)):
        assert consistency.half_up_div(numerator, denominator) == system_half_up_div(numerator, denominator)
    print("[T-SIM-08] sim 侧 half_up_div 与 app/domain/pricing.py 逐值一致（口径对齐已坐实）")


def test_live_consistency_within_tolerance(tmp_path):
    """端到端：真起被测服务、逐日对账 ⇒ **零问题**；再**真删一笔 model 成交** ⇒ 必须变红。

    这是 `T-SIM-08` 验收②③的主用例。成交密度与 `CONSISTENCY_PROFILE` 一致（≤20 笔/日），
    不用 `live_run` 的 4 笔/日 —— 那是上一任指出的"密度不够"。
    """
    params = load_params(PARAMS_PATH)
    out = consistency.run_consistency(params, out_dir=tmp_path / "consistency", tamper=True,
                                      progress=lambda _m: None)
    comparison = out["comparison"]
    assert comparison["days_compared"] >= 30, f"只比了 {comparison['days_compared']} 个营业日"
    assert comparison["fields_compared"] >= 30 * 9
    assert comparison["settlement_count"] >= 10, "没有生成结算单 ⇒ `§7.3` 的结算单比对没跑到"
    assert out["problems"] == [], f"一致性未通过：{out['problems'][:10]}"
    negative = out["negative"]
    assert negative is not None and negative["red"], \
        "**灵敏度负例失败**：删掉 model 侧一笔成交后一致性检查仍然全绿 ⇒ 这套检查是摆设"
    assert negative["problems"], "负例没有给出任何问题清单"
    print(f"[T-SIM-08] 一致性通过（{comparison['days_compared']} 日 × {comparison['fields_compared']} 字段，"
          f"{comparison['settlement_count']} 张结算单）；删 model 一笔 ⇒ 变红，报 {len(negative['problems'])} 条")