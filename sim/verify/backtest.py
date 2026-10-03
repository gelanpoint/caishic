"""`R1`~`R6` 回测与**三态**判定（`T-SIM-08`；`docs/sim-design.md` §7.1）。

## 三态，不是二态（验收判据①）

每条 `R` 只允许落在 **`成立` / `不成立` / `不稳健`** 三个值上，收敛规则写死在 `verdict_of`
里，**不是**"看情况说"：

1. 任何子句 **不通过** ⇒ `不成立`（含"该档没有载体"而被判为不可评估的情形 ——
   不可评估**不等于通过**，`§6.9` 明写"不许把『没跑到』说成『通过』"）；
2. 子句全通过、但**稳健性扫描**在某个无出处参数的区间端点上**翻转**了序关系 ⇒ `不稳健`
   （`§7.2` 第 2 条：不稳健的结论不得作为论证依据）；
3. 否则 ⇒ `成立`。

## 「绝对阈值 vs 序关系」的纪律是**机械的**（本项目最重要的规则）

`judge_clause` 对每条 `absolute` 子句逐个查它依赖的参数在 `params.json` 里的
`provenance.kind`：只要有一个是 `assumed`，该子句判 **`不可评估`**，并点名是哪一个参数。
**不允许**顺手把阈值放宽，也不允许把它算成通过。实测值照给（供读者自己判断），但不当作结论。

## 档位是数据的一部分

机制有显形的时间尺度与规模（`docs/sim-验证结论实况.md` V-01：基线档 60 天 `60/60` 天
设备零故障 ⇒ 那条对照实验什么也没测）。故档位写在 [`r_criteria.py`](r_criteria.py) 里
并**随报告一起打印**，"用哪个档说出的结论"不许藏在注释里。

## 内存纪律

`run_arm` 算完指标就**释放事件明细**并只保留派生序列（`study.py` 踩过的 `MemoryError`
在这里不重演：24 臂 × 每份 36 万事件的整份列表同时驻留会崩）。
"""

from __future__ import annotations

from pathlib import Path

from ..bridge.model_adapter import read_events, run_scenario
from ..bridge.scenario import arm_flags, load_scenario, merged_overrides, scenario_files
from ..observe.metrics import compute_all, survival_judgement
from ..observe.metric_util import pct
from .r_criteria import ABSOLUTE, CRITERIA, VERDICTS, Clause, Criterion, criterion, profile

#: 重复区间用 p5/p95（与 `study.py` 的 `_summary` 同口径，不另立一套）
INTERVAL = (5, 95)

#: `converging_high` 的"高位"阈值。⚠️ 这**不是**本模块新拍的数：`sim/observe/report.py` 的存活判据
#: ④ 用的是同一个 0.5（`§6.9` 的 TrustIndex 阈值），此处沿用以免同一件事有两个阈值。
TRUST_HIGH = 0.5


# ---------------------------------------------------------------------------
# 档位 → 运行参数 → 跑一个臂
# ---------------------------------------------------------------------------
def scenario_by_id(scenario_id: str) -> dict:
    for path in scenario_files():
        scenario = load_scenario(path)
        if scenario["id"] == scenario_id:
            return scenario
    raise KeyError(f"没有场景 {scenario_id!r}")


def arm_overrides(scenario: dict, arm: dict, prof: dict, extra: dict | None = None) -> dict:
    """某一臂在某一档下的**最终参数覆盖**（场景基线 + 该臂对照变量 + 档位规模 + 档位压力值）。"""
    out = merged_overrides(scenario, arm)
    out["daily_arrivals_per_market"] = prof["arrivals"]
    out["consumer_agent_count"] = prof["consumers"]
    out.update(prof.get("param_overrides") or {})
    out.update(extra or {})
    return out


def _scale_series(events: list[dict]) -> list[float]:
    """逐期走秤率序列（`M-04` 的按期切分）——「单调下降」这类判据必须有序列才判得了。

    切窗用**营业日序号**而不是月/日算术（与 `sim/observe/metrics.py::survival_window` 同纪律）：
    仿真月（`DAYS_PER_MONTH=30`）与自然月不对齐，用日期做算术会默默错位。
    """
    days = sorted({e["business_date"] for e in events if e.get("kind") == "day_arrivals_total"})
    period_of = {day: index // 30 for index, day in enumerate(days)}
    buckets: dict[int, list[int]] = {}
    for event in events:
        if event.get("kind") != "txn":
            continue
        bucket = buckets.setdefault(period_of.get(event["business_date"], -1), [0, 0])
        bucket[1] += 1
        if event.get("channel") == "scale":
            bucket[0] += 1
    return [round(num / den, 6) for _period, (num, den) in sorted(buckets.items()) if den]


def _series_of(events: list[dict], metrics: dict) -> dict:
    """从事件流/指标里抽出判据要用的**序列**（算完即丢事件明细）。"""
    maintenance = [float(e.get("maintenance_cents") or 0.0) for e in events if e.get("kind") == "market_admin_period"]
    upfront = max((float(e.get("upfront_cents") or 0.0) for e in events if e.get("kind") == "stall_adopted"),
                  default=None)
    idle_detail = (metrics.get("M-10") or {}).get("detail") or {}
    return {
        "M-01": [row["bp"] / 10000.0 for row in ((metrics.get("M-01") or {}).get("detail") or {}).get("by_month", [])
                 if row.get("denominator")],
        "M-04": _scale_series(events),
        "M-15": [row["mean"] for row in ((metrics.get("M-15") or {}).get("detail") or {}).get("by_period", [])],
        "M-17-B": maintenance,
        "R6-upfront": [upfront] if upfront is not None else [],
        "M-10-设备口径": [idle_detail.get("口径②设备-日(停摆>τ)", {})],
    }


def run_arm(params, *, scenario_id: str, arm_index: int, label: str, prof: dict,
            extra: dict | None = None, seed: int = 20261002, out_root: Path | str = "data/sim/backtest",
            self_funded: bool | None = None, progress=print) -> dict:
    """跑完一个臂的 `replications` 次重复，返回指标 + 派生序列（**不保留事件明细**）。"""
    scenario = scenario_by_id(scenario_id)
    arm = scenario["arms"][arm_index]
    overrides = arm_overrides(scenario, arm, prof, extra)
    flag = arm_flags(scenario, arm)["self_funded"] if self_funded is None else bool(self_funded)
    out_root = Path(out_root)
    rows = []
    for rep in range(prof["replications"]):
        target = out_root / f"{scenario_id}-{arm_index}-{rep:02d}"
        result = run_scenario(params, scenario_id=f"{scenario_id}::{arm['name']}", overrides=overrides,
                              days=prof["days"], seed=seed + rep * 101, out_dir=target, self_funded=flag)
        events = read_events(target / "events.jsonl")
        metrics = compute_all(events)
        rows.append({"rep": rep, "metrics": metrics, "series": _series_of(events, metrics), "out_dir": str(target)})
        del events
        progress(f"    · {label} rep{rep} 走秤率 {rows[-1]['metrics']['M-04']['ratio']}")
    return {"label": label, "scenario": scenario_id, "arm": arm["name"], "arm_index": arm_index,
            "overrides": overrides, "self_funded": flag, "profile": prof["id"], "rows": rows}


# ---------------------------------------------------------------------------
# 取值：measure → 标量 / 序列
# ---------------------------------------------------------------------------
def measure(run: dict, name: str):
    metrics = run["rows"][0]["metrics"]
    series = run["rows"][0].get("series") or {}
    if name in series:
        values = [v for v in series[name] if isinstance(v, (int, float))]
        return (values[0] if name in ("R6-upfront", "M-10-设备口径") else
                (sum(values) / len(values) if values else None)), series[name]
    if name.startswith("M-"):
        row = metrics.get(name) or {}
        return (row.get("value") if row.get("ratio_is_ratio") is False else row.get("ratio")), []
    if name.startswith("survival:"):
        rows = list(survival_judgement(metrics)["criteria"].values())
        return rows[int(name.split(":")[1]) - 1].get("value"), []
    raise KeyError(f"未登记的 measure：{name!r}")


def interval(run: dict, name: str) -> tuple[float | None, float | None]:
    """该 measure 在各次重复上的 p5/p95（**不许只报单次结果**，`§5.1`）。"""
    values = []
    for row in run["rows"]:
        value = measure({"rows": [row]}, name)[0]
        if isinstance(value, (int, float)):
            values.append(float(value))
    return (pct(values, INTERVAL[0]), pct(values, INTERVAL[1])) if values else (None, None)


# ---------------------------------------------------------------------------
# 判定：纯函数（合成负例可直接喂）
# ---------------------------------------------------------------------------
def unsourced(params, clause: Clause) -> list[str]:
    """该子句依赖的、无出处的参数（`absolute` 子句只要有一个就不许用绝对阈值）。"""
    out = []
    for key in clause.depends_on:
        try:
            kind = params.kind(key)
        except KeyError:
            out.append(f"{key}(未登记)")
            continue
        if kind != "sourced":
            out.append(f"{key}({kind})")
    return out


def judge_clause(params, clause: Clause, a: dict, b: dict | None = None, *, arms_note: str = "") -> dict:
    """判定一条子句。**纯函数**。返回的状态 ∈ {通过, 不通过, 不可评估}。"""
    left = measure(a, clause.measure)
    base = {"clause": clause.id, "text": clause.text, "kind": clause.kind, "measure": clause.measure,
            "profile": clause.profile or a["profile"], "threshold": clause.threshold,
            "measure_arm": clause.measure_arm,
            "depends_on": list(clause.depends_on), "unsourced": unsourced(params, clause), "note": clause.note}
    if clause.kind == ABSOLUTE and base["unsourced"]:
        base.update({
            "state": "不可评估", "observed": left[0],
            "reason": f"绝对阈值依赖在无出处参数上（前 3 个：{base['unsourced'][:3]}）⇒ 按 §7.1"
                      f"「无数据的参数只允许序关系/单调性/区间不重叠」，本子句**不得**用绝对月份或"
                      f"绝对比率判定（实测值照给，但不当作结论；完整名单见 `unsourced` 字段）",
        })
        return base
    if clause.kind == ABSOLUTE:
        value = left[0]
        ok = value is not None and value <= clause.threshold
        base.update({"state": "通过" if ok else ("不可评估" if value is None else "不通过"), "observed": value,
                     "reason": None if value is not None else "该指标在此档退化（分母为 0）⇒ 不可评估，不算通过"})
        return base
    if clause.direction == "overlap":
        lo_a, hi_a = interval(a, clause.measure)
        lo_b, hi_b = interval(b, clause.measure)
        overlap = None if None in (lo_a, hi_a, lo_b, hi_b) else min(hi_a, hi_b) >= max(lo_a, lo_b)
        base.update({"observed": {"A": [lo_a, hi_a], "B": [lo_b, hi_b], "overlap": overlap},
                     "state": "不可评估" if overlap is None else ("通过" if overlap else "不通过"),
                     "reason": None if overlap is not None else "区间不可得（重复次数不足或指标退化）"})
        return base
    if clause.direction in ("decreasing", "non_increasing", "converging_high"):
        #: 序列类子句测的是 `measure_arm` 指定的那一臂（判据文本说"哪一组"，不是臂表的 A/B 顺序）
        target = b if (clause.measure_arm == "B" and b is not None) else a
        series = [float(v) for v in (measure(target, clause.measure)[1] or []) if isinstance(v, (int, float))]
        if len(series) < 2:
            base.update({"state": "不可评估", "observed": series,
                         "reason": "序列不足 2 个点，无法判定单调性（不许把『测不到』说成『通过』）"})
            return base
        rises = [i for i in range(1, len(series)) if series[i] > series[i - 1]]
        ok = not rises
        base.update({"state": "通过" if ok else "不通过", "observed": series[:6] + ["…"] + series[-4:] if len(series) > 10 else series,
                     "reason": None if ok else f"序列出现 {len(rises)} 次上升：{series[:6]}…{series[-4:]}"})
        if clause.direction == "converging_high":
            base["state"] = "通过" if (ok and series[-1] >= TRUST_HIGH) else "不通过"
            base["reason"] = None if base["state"] == "通过" else (
                f"末期均值 {series[-1]:.4f}（{'单调性通过' if ok else '单调性不通过'}），高位阈值 {TRUST_HIGH}"
                " —— 与 §6.9 存活判据④ 同一个阈值，不是本模块新拍的数")
        return base
    right = measure(b, clause.measure)[0] if b is not None else None
    lo_a, hi_a = interval(a, clause.measure)
    lo_b, hi_b = interval(b, clause.measure)
    if left[0] is None or right is None:
        base.update({"state": "不可评估", "observed": {"A": left[0], "B": right},
                     "reason": "至少一侧指标退化（分母为 0）⇒ 不可评估"})
        return base
    holds = left[0] < right if clause.direction == "<" else left[0] > right
    lo_overlap = None if None in (lo_a, hi_a, lo_b, hi_b) else min(hi_a, hi_b) >= max(lo_a, lo_b)
    if left[0] == right:
        reason = f"两侧**完全相同**（A=B={left[0]}）⇒ 这条对照在本档**测不出差异**（不是方向相反）"
    else:
        reason = f"方向相反（A={left[0]} 不满足 `{clause.direction}` B={right}）"
    base.update({
        "state": "通过" if holds else "不通过",
        "observed": {"A": left[0], "B": right, "A_p5_p95": [lo_a, hi_a], "B_p5_p95": [lo_b, hi_b],
                     "intervals_overlap": lo_overlap, "arms": arms_note},
        "reason": None if holds else reason,
    })
    return base


def verdict_of(clauses: list[dict], robustness: dict) -> tuple[str, str]:
    """子句状态 + 稳健性 → `R` 级三态结论。**收敛规则只此一处**，别处不得另判一次。"""
    failed = [c["clause"] for c in clauses if c["state"] == "不通过"]
    unevaluable = [c for c in clauses if c["state"] == "不可评估"]
    if failed:
        return "不成立", f"子句 {failed} 在中心档实测不通过"
    if unevaluable:
        detail = "；".join((c.get("reason") or "")[:70] for c in clauses if c["state"] == "不可评估")
        return "不成立", (f"子句 {[c['clause'] for c in clauses if c['state'] == '不可评估']} **不可评估**"
                         f"（{detail}）—— 不可评估不等于通过，故不能判『成立』")
    if robustness.get("flips"):
        return "不稳健", (f"中心档子句全通过，但序关系在无出处参数的区间端点上翻转 {len(robustness['flips'])} 次"
                          f"（{robustness['flipped_keys']}）⇒ 按 §7.2 不得作为论证依据")
    verdict, reason = "成立", "中心档子句全通过，且无出处参数的区间端点上序关系未翻转"
    assert verdict in VERDICTS, verdict
    return verdict, reason


def relation(value_a, value_b) -> str:
    """两个标量的序关系（`A<B` / `A>B` / `A=B`）。**纯函数**，可喂合成负例。"""
    if value_a is None or value_b is None:
        return "unknown"
    if value_a == value_b:
        return "A=B"
    return "A<B" if value_a < value_b else "A>B"


def robustness_scan(params, crit, prof, base_runs: dict, *, measure_name: str = "M-04",
                    seed: int = 20261002, out_root: Path | str = "data/sim/backtest/_scan",
                    progress=print) -> dict:
    """把**决定这条结论**的无出处参数逐个推到区间端点，重跑 A/B 两臂，看**序关系是否翻转**。

    扫的是**区间端点**而不是点估计：`§7.2` 第 2 条问的是"在合理区间内会不会翻转"。
    翻转的定义 = 该端点下的序关系与中心档不同（含退化成 `A=B`，那同样意味着这条对照在该区域
    **测不出差异**）。端点取不到值记 `skipped`（**不当作稳健**）。
    """
    base_a, base_b = measure(base_runs["A"], measure_name)[0], measure(base_runs["B"], measure_name)[0]
    base_rel = relation(base_a, base_b)
    trials, flips, skipped = [], [], []
    for key in crit.scan_keys:
        entry = params.parameters().get(key)
        if entry is None or "range" not in entry:
            skipped.append(f"{key}(未登记或无区间)")
            continue
        #: **只扫标量参数**。区间型参数（如 `merchant_adoption_horizon_months_range`）不能被推成
        #: 一个标量 —— 第一版就这么试过，`build_world` 直接吃到 `int` 然后 `TypeError`。
        #: 这类参数改标 `skipped`，**不当作稳健**。
        if isinstance(entry.get("value"), bool) or not isinstance(entry.get("value"), (int, float)):
            skipped.append(f"{key}(区间型参数，不能推成标量)")
            continue
        low, high = entry["range"]
        for point in (low, high):
            values = {}
            for name in ("A", "B"):
                run = base_runs[name]
                values[name] = run_arm(params, scenario_id=run["scenario"], arm_index=run["arm_index"],
                                       label=f"{run['label']}@{key}={point}", prof=prof, extra={key: point},
                                       seed=seed, out_root=Path(out_root) / f"{key}-{point}",
                                       self_funded=run["self_funded"], progress=lambda _m: None)
            va, vb = measure(values["A"], measure_name)[0], measure(values["B"], measure_name)[0]
            if va is None or vb is None:
                skipped.append(f"{key}={point}(指标退化)")
                continue
            rel = relation(va, vb)
            row = {"key": key, "value": point, "A": va, "B": vb, "relation": rel,
                   "base_relation": base_rel, "flipped": rel != base_rel}
            trials.append(row)
            if row["flipped"]:
                flips.append(row)
            progress(f"    · 稳健性 {key}={point}: A={va} B={vb} ⇒ {rel}"
                     f"（中心档 {base_rel}）{'【翻转】' if row['flipped'] else ''}")
    return {"measure": measure_name, "base": {"A": base_a, "B": base_b, "relation": base_rel},
            "trials": trials, "flips": flips, "flipped_keys": sorted({t["key"] for t in flips}),
            "skipped": skipped,
            "note": "『翻转』= 该无出处参数取到区间端点时 A、B 的序关系与中心档不同（含退化成 A=B，"
                    "那意味着这条对照在该区域**测不出差异**）"}


# ---------------------------------------------------------------------------
# 编排：跑完 R1~R6，落 JSON + Markdown
# ---------------------------------------------------------------------------
def judge_criterion(params, crit, *, seed: int = 20261002, out_root: Path | str = "data/sim/backtest",
                    with_scan: bool = True, progress=print) -> dict:
    """跑一条 `R` 判据的全部臂 → 逐子句判定 → 稳健性扫描 → 三态结论。"""
    spec = crit if isinstance(crit, Criterion) else criterion(crit)
    prof = profile(spec.profile)
    runs, clauses = {}, []
    for name, arm in spec.arms.items():
        runs[name] = run_arm(params, scenario_id=arm["scenario"], arm_index=arm["arm_index"],
                             label=f"{spec.id}{name}·{arm['label']}", prof=prof, extra=arm.get("extra"),
                             seed=seed, out_root=Path(out_root) / spec.id, progress=progress)
    for clause in spec.clauses:
        clause_prof = profile(clause.profile) if clause.profile else prof
        #: 子句可以在与 `R` 不同的档位下判定（例如 R3 全部子句都走「预算约束压力档」）；
        #: 若声明了档位而中央档位的臂不适用，就在这里**重跑**该档位的两臂。
        if clause.profile and clause.profile != spec.profile:
            for name, arm in spec.arms.items():
                if name == "C":
                    continue
                runs[f"{name}@{clause.profile}"] = run_arm(
                    params, scenario_id=arm["scenario"], arm_index=arm["arm_index"],
                    label=f"{spec.id}{name}·{arm['label']}@{clause.profile}", prof=clause_prof,
                    extra=arm.get("extra"), seed=seed, out_root=Path(out_root) / f"{spec.id}-{clause.profile}",
                    progress=progress)
        suffix = f"@{clause.profile}" if clause.profile and clause.profile != spec.profile else ""
        note = " / ".join(f"{n}={runs[n + suffix]['label']}" for n in spec.arms
                          if n != "C" and n + suffix in runs)
        clauses.append(judge_clause(params, clause, runs["A" + suffix],
                                    runs.get("B" + suffix) if "B" in spec.arms else None, arms_note=note))
    scan = {"trials": [], "flips": [], "flipped_keys": [], "skipped": [],
            "note": "本次未跑稳健性扫描（`with_scan=False`）—— 那不是『没发现翻转』"}
    if with_scan:
        scan = robustness_scan(params, spec, prof, runs, progress=progress)
    verdict, reason = verdict_of(clauses, scan)
    return {"id": spec.id, "title": spec.title, "basis": spec.basis, "question": spec.question,
            "verdict": verdict, "reason": reason, "profile": prof, "clauses": clauses, "robustness": scan,
            "arms": {name: {"label": run["label"], "scenario": run["scenario"], "arm": run["arm"],
                            "overrides": run["overrides"], "self_funded": run["self_funded"],
                            "M-04": run["rows"][0]["metrics"]["M-04"]["ratio"],
                            "M-01": run["rows"][0]["metrics"]["M-01"]["ratio"],
                            "M-03": run["rows"][0]["metrics"]["M-03"]["ratio"],
                            "M-10": run["rows"][0]["metrics"]["M-10"]["ratio"],
                            "M-15": run["rows"][0]["metrics"]["M-15"]["ratio"],
                            "survival": survival_judgement(run["rows"][0]["metrics"])["criteria"],
                            "interval_M04": interval(run, "M-04")}
                    for name, run in runs.items() if "@" not in name}}


def run_backtest(params, *, out_dir: Path | str = "data/sim/backtest", only: list[str] | None = None,
                 with_scan: bool = True, progress=print) -> dict:
    """跑完 `R1`~`R6`，落 `backtest.json`（机器可读）与 `backtest.md`（人读）。"""
    from ..observe.verify_report import render_backtest, write_backtest

    chosen = [c for c in CRITERIA if not only or c.id in set(only)]
    results = []
    for crit in chosen:
        progress(f"  [{crit.id}] {crit.title}")
        results.append(judge_criterion(params, crit, out_root=Path(out_dir) / "_runs",
                                       with_scan=with_scan, progress=progress))
    payload = {"schema_version": 1, "params_path": params.source,
               "profile_note": "每个 `R` 的所用档位随结论一起给出（机制有显形时间尺度，档位不对就看不到机制）",
               "results": results}
    paths = write_backtest(out_dir, payload, render_backtest(payload))
    return {"results": results, "paths": paths, "verdicts": {r["id"]: r["verdict"] for r in results}}