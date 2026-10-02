"""`T-SIM-06` 验收①②：**§6 的 22 个指标逐个必须能由事件流复算**。

`Q-19` 的教训写在这里：**退化状态下"写死"与"真查"输出相同，检查根本发现不了**
（当年 `cash_txn` 1/1、`price_list` 10/10 两条指标就是这么"通过"的）。
故本文件分四层，每层各自防一种假绿，且**每层都带合成负例**：

1. **22 个都在、都写了分子/分母**（非比值指标显式声明，不硬凑百分比）；
2. **独立复算**：用**不复用 `sim.observe` 任何代码**的朴素 jsonl 遍历重算关键指标
   （`M-04/05/08/09/12`），与模块输出逐位对账 —— 这才叫"第三方可逐一核对"；
3. **非退化（抗写死）**：把某指标的来源事件整族删掉 ⇒ 值必须变；把某指标写死成常量 ⇒ 必红；
4. **口径守卫**（本轮自查出的两个真缺陷的回归）：存活判据③ 必须用 `M-04` 的
   第 300–360 日窗口（第一版用了 `M-13` 扫码率且没有窗口）；`M-19` 是**分布**
   （均值金额），不许被印成百分比；`M-20` 的佣金核对**必须真的执行过**
   （第一版在短档上静默跳过、仍报 100%）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sim_support import tiny_run

EXPECTED_IDS = [f"M-{i:02d}" for i in range(1, 23)]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("tsim06-metrics")
    result, events = tiny_run(out_dir=out / "S0")
    return {"metrics": result["metrics"], "events": events, "out": out}


# ---------------------------------------------------------------------------
# 1. 22 个都在，都有分子/分母
# ---------------------------------------------------------------------------
def test_all_22_metrics_present_with_numerator_and_denominator(run):
    metrics = run["metrics"]
    assert sorted(metrics) == EXPECTED_IDS, f"§6 只定义 22 个指标：{sorted(metrics)}"
    missing_labels = [key for key, row in metrics.items()
                      if not row.get("numerator_label") or not row.get("denominator_label")]
    assert missing_labels == [], f"这些指标没写清分子/分母口径：{missing_labels}"
    unavailable = [key for key, row in metrics.items() if not row.get("available")]
    assert unavailable == ["M-22"], f"model 模式只允许 M-22 不可用（live 才有）：{unavailable}"
    assert "live" in metrics["M-22"]["reason"]
    for key, row in metrics.items():
        if row.get("available") and row.get("ratio_is_ratio") is not False:
            ratio, num, den = row["ratio"], row["numerator"], row["denominator"]
            if den:
                assert ratio == pytest.approx(num / den), f"{key} 的比值与分子/分母不一致"
        if row.get("ratio_is_ratio") is False:
            assert "value" in row, f"非比值指标 {key} 必须显式给出它到底是什么量"
    print(f"[T-SIM-06] 22 个指标齐备；不可用 = {unavailable}（如实标注，不当 0）")


def test_metrics_are_recomputable_from_the_event_log(run):
    """产物值与事件流复算必须一致（`verify_against_declared` 的能力边界见其 docstring）。"""
    from sim.observe.metrics_check import verify_against_declared

    problems = verify_against_declared(run["metrics"], run["events"])
    assert problems == [], "\n  ".join(problems)
    print("[T-SIM-06] 22 个指标的产物值 = 事件流复算值")


# ---------------------------------------------------------------------------
# 2. 独立复算（**不复用 sim.observe 任何代码**）
# ---------------------------------------------------------------------------
def naive_counts(events_path: Path) -> dict:
    """朴素 jsonl 遍历：只认 `kind`/字段，**不 import `sim.observe`**。第三方照着这段就能核对。"""
    scale = shadow = cash_scale = short = 0
    stall_days: set = set()
    price_complete = price_total = 0
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event["kind"] == "txn":
            if event["channel"] == "scale":
                scale += 1
                stall_days.add((event["business_date"], event["stall_no"]))
                if event.get("method") == "cash":
                    cash_scale += 1
            else:
                shadow += 1
            if event.get("short_weight"):
                short += 1
        elif event["kind"] == "price_list":
            price_total += 1
            price_complete += 1 if event.get("complete") else 0
    return {"scale": scale, "shadow": shadow, "cash_scale": cash_scale, "short": short,
            "stall_days": len(stall_days), "price_complete": price_complete, "price_total": price_total,
            "total_txns": scale + shadow}


def test_key_metrics_match_an_independent_recount(run):
    naive = naive_counts(Path(run["out"]) / "S0" / "events.jsonl")
    metrics = run["metrics"]
    assert metrics["M-04"]["numerator"] == naive["scale"]
    assert metrics["M-04"]["denominator"] == naive["total_txns"]
    assert metrics["M-05"]["numerator"] == naive["shadow"]
    assert metrics["M-05"]["denominator"] == naive["total_txns"]
    assert metrics["M-08"]["numerator"] == naive["cash_scale"]
    assert metrics["M-08"]["denominator"] == naive["scale"]
    assert metrics["M-09"]["numerator"] == naive["price_complete"]
    assert metrics["M-09"]["denominator"] == naive["price_total"]
    assert metrics["M-12"]["numerator"] == naive["short"]
    assert metrics["M-12"]["denominator"] == naive["total_txns"]
    assert metrics["M-07"]["numerator"] == naive["stall_days"]
    print(f"[T-SIM-06] 独立复算一致：走秤 {naive['scale']}/{naive['total_txns']}、"
          f"私下 {naive['shadow']}、短秤 {naive['short']}、摊位-日 {naive['stall_days']}")


# ---------------------------------------------------------------------------
# 3. 非退化（抗写死）
# ---------------------------------------------------------------------------
def test_non_degeneracy_check_passes_on_real_data(run):
    from sim.observe.metrics_check import mutation_sensitivity_problems

    report = mutation_sensitivity_problems(run["events"])
    assert report["problems"] == [], "\n  ".join(report["problems"])
    assert len(report["checked"]) >= 15, f"用扰动法检验过的指标太少：{report['checked']}"
    print(f"[T-SIM-06] 扰动法检验 {len(report['checked'])} 个指标全部对事实敏感；"
          f"当前数据下退化（不能用扰动法检验）= {[d['id'] for d in report['degenerate']]}")


def test_non_degeneracy_check_is_sensitive_to_a_written_constant(run, monkeypatch):
    """**合成负例**：把 `M-04` 写死成常量 ⇒ 同一条检查必须判红（`Q-19` 的教训）。"""
    from sim.observe import metrics as metrics_mod
    from sim.observe.metrics_check import mutation_sensitivity_problems

    frozen = dict(metrics_mod.FUNCTIONS["M-04"](run["events"]))
    monkeypatch.setitem(metrics_mod.FUNCTIONS, "M-04", lambda events, _row=frozen: dict(_row))
    report = mutation_sensitivity_problems(run["events"])
    assert any("M-04" in problem for problem in report["problems"]), \
        f"把 M-04 写死成常量后没有判红：{report['problems']}"
    print("[T-SIM-06] 负例：把 M-04 写死成常量 ⇒ 非退化自检判红")


# ---------------------------------------------------------------------------
# 4. 口径守卫（三个真缺陷的回归）
# ---------------------------------------------------------------------------
def _synthetic_metrics(*, window_ratio, scan_ratio=0.05, months=12, active_ratio=1.0,
                       cum_cash=1.0, trust=0.9):
    """合成一份"够用"的 metrics（只放存活判据读的那几个字段），用来单独检验判据口径。"""
    from sim.observe.metric_specs import SPEC_BY_ID
    from sim.observe.metric_util import ratio_row

    return {
        "M-03": ratio_row(10, 10, SPEC_BY_ID["M-03"]),
        "M-04": ratio_row(60, 100, SPEC_BY_ID["M-04"], detail={
            "window_300_360": {"numerator": 90, "denominator": 100, "ratio": window_ratio,
                               "window": "第 300–360 营业日", "window_days": 61, "run_days": 360,
                               "evaluable": True}}),
        "M-13": ratio_row(5, 100, SPEC_BY_ID["M-13"],
                          detail={"口径①读到有效信息 / 走秤": {"numerator": 5, "denominator": 100,
                                                              "bp": scan_ratio * 10000}}),
        "M-15": ratio_row(trust * 100, 100, SPEC_BY_ID["M-15"]),
        "M-18": dict(ratio_row(cum_cash, months, SPEC_BY_ID["M-18"]), value=cum_cash),
    }


def test_survival_criterion_uses_the_scale_use_window_not_the_scan_rate():
    """存活判据③ = **`M-04` 的第 300–360 日窗口**，不是 `M-13` 扫码率（第一版的口径错）。"""
    from sim.observe.metrics import survival_judgement
    from sim.observe.metric_specs import SURVIVAL_THRESHOLDS

    good = survival_judgement(_synthetic_metrics(window_ratio=0.9, scan_ratio=0.05))
    key = "scale_use_rate_300_360 >= 0.60"
    assert key in good["criteria"], f"判据③ 的键名必须与阈值表一致：{list(good['criteria'])}"
    assert key in SURVIVAL_THRESHOLDS
    assert good["criteria"][key]["pass"] is True, \
        "走秤率窗口 90% 而扫码率 5% 时必须**通过** —— 用 M-13 当判据会在此假红（第一版的真实形态）"
    assert good["criteria"][key]["numerator"] == 90 and good["criteria"][key]["denominator"] == 100

    #: **合成负例**：窗口走秤率低于阈值 ⇒ 必须判红
    bad = survival_judgement(_synthetic_metrics(window_ratio=0.4))
    assert bad["criteria"][key]["pass"] is False, "窗口走秤率 40% 竟然通过 —— 判据对窗口值不敏感"
    #: **合成负例**：运行不足 300 日（窗口不可评估）⇒ 判红且必须给出理由，不许当"通过"
    short = _synthetic_metrics(window_ratio=None)
    short["M-04"]["detail"]["window_300_360"] = {"numerator": 0, "denominator": 0, "ratio": None,
                                                 "window_days": 0, "run_days": 60, "evaluable": False}
    verdict = survival_judgement(short)["criteria"][key]
    assert verdict["pass"] is False and "不可评估" in (verdict["note"] or ""), \
        "窗口未跑到时必须如实说『不可评估』，不许静默当成通过或当成 0"
    print("[T-SIM-06] 存活判据③ 口径守卫：窗口 90% 通过 / 40% 判红 / 未跑到 300 日判「不可评估」")


def test_m19_is_a_distribution_not_a_percentage(run):
    m19 = run["metrics"]["M-19"]
    assert m19["ratio_is_ratio"] is False, "M-19 的商是**均值金额（分）**，不是比例 —— 不许印成百分比"
    assert m19["value"] == pytest.approx(m19["numerator"] / m19["denominator"])
    for key in ("p10", "p50", "p90"):
        assert m19["detail"][key] is not None, f"M-19 必须报 {key}（设计 §6：报分布，不只报均值）"
    print(f"[T-SIM-06] M-19 = 分布：p10/p50/p90 = "
          f"{m19['detail']['p10']}/{m19['detail']['p50']}/{m19['detail']['p90']}（分/摊位-月）")


def test_m20_commission_check_actually_executed(run):
    """`M-20` 的第二半（佣金两条路径对账）**必须真的跑过**，不许因映射错位被静默跳过。"""
    m20 = run["metrics"]["M-20"]
    detail = m20["detail"]
    assert detail["commission_checked_days"] > 0, "佣金核对一天都没执行 —— 短档上静默跳过是缺陷"
    assert detail["commission_checked_days"] == m20["denominator"], \
        "每个有走秤交易的营业日都应完成佣金核对（缺当期账目必须判红，不许跳过）"

    #: **合成负例**：删掉月度账目 ⇒ 必须判红（缺账 = 对账无法成立），而不是"没问题"
    broken = [e for e in run["events"] if e.get("kind") != "market_cash_month"]
    from sim.observe.metrics_trust_cash import m20 as m20_fn

    after = m20_fn(broken)
    assert after["detail"]["violation_count"] > 0 and after["ratio"] == 0.0, \
        "删掉 market_cash_month 后 M-20 仍绿 —— 佣金那一半对账没在真跑"
    print(f"[T-SIM-06] M-20：{detail['commission_checked_days']}/{m20['denominator']} 天完成佣金核对；"
          "删账目 ⇒ 判红")
