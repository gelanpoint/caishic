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

from ..observe.metrics import survival_judgement
from .r_criteria import ABSOLUTE, CRITERIA, VERDICTS, Clause, Criterion, criterion, profile

#: `converging_high` 的"高位"阈值。⚠️ 这**不是**本模块新拍的数：`sim/observe/report.py` 的存活判据
#: ④ 用的是同一个 0.5（`§6.9` 的 TrustIndex 阈值），此处沿用以免同一件事有两个阈值。
TRUST_HIGH = 0.5


# ---------------------------------------------------------------------------
# 取数层的同名转出（2026-10-03 按语义拆出；搬动理由见 arm_runner.py 的 docstring）
# ---------------------------------------------------------------------------
from .arm_runner import INTERVAL, arm_overrides, interval, measure, run_arm, scenario_by_id  # noqa: E402,F401  （原样转出：既有调用方与既有用例零改动）

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


#: 判定饱和用的容差（序列按 6 位小数取整，比 1e-9 小好几个量级 ⇒ 不会误判）
CEILING_TOLERANCE = 1e-9


def saturation_periods(series: list[float], ceiling: float) -> int:
    """序列**末尾连续**等于上限的点数（0 = 没饱和）。

    为什么只看末尾：中途触顶又被拉下来不算"被夹住"，那说明指标还能动；只有**贴着上限
    收尾**才说明观测空间已经没有了。
    """
    count = 0
    for value in reversed(series):
        if isinstance(value, (int, float)) and abs(float(value) - ceiling) <= CEILING_TOLERANCE:
            count += 1
        else:
            break
    return count


def judge_accelerating(base: dict, series: list[float], ceiling: float | None = None) -> dict:
    """「逐期**加速上升**」的判定（`R3-c`，2026-10-03 规格修正后）。**纯函数** ⇒ 负例可直接喂。

    **饱和优先于一切形状判定**（父代理 `2026-10-03` 第二层裁定）：序列末尾连续贴住上限
    ⇒ 斜率是**饱和的算术后果**、不是关于机制的事实 ⇒ 判 **`不可评估`**、**不是不通过**。
    明令不得把"触顶"写成"非加速"—— 那是用判据措辞掩盖测量失效。产物里写明**触顶期数**。

    不饱和时的两条要求，都要满足才算通过：

    1. **单调不降** —— 不加速的下降或持平都不算「加速上升」；
    2. **后半程斜率 > 前半程斜率** —— 线性上升**不算**加速（这是「加速」二字的全部含义）。

    序列切两半：前半 = 前 `ceil(n/2)` 点、后半 = 余下点，各段用**端点斜率**
    （`(末-首)/(点数-1)`）。少于 4 个点判 `不可评估`：两点只能判单调性，
    三点切两半有一段只有 1 个点、斜率退化成单点增量 —— **测不到 ≠ 通过**。
    比较用**严格大于**（无容差）：序列值域在 `[0,1]`，浮点噪声量级 ~1e-16，
    加容差只会把「线性上升」误判成加速。
    """
    base = dict(base)
    points = [float(v) for v in series if isinstance(v, (int, float))]
    at_ceiling = saturation_periods(points, ceiling) if ceiling is not None else 0
    if at_ceiling:
        first_period = len(points) - at_ceiling + 1
        base.update({"state": "不可评估",
                     "observed": {"series": points, "ceiling": ceiling, "periods_at_ceiling": at_ceiling,
                                  "saturated_from_period": first_period},
                     "reason": f"序列自**第 {first_period} 期**起连续 {at_ceiling} 期贴在上限 {ceiling}"
                               f" ⇒ 形状判据（斜率）在该档**没有观测空间**；后半程斜率 0 是**饱和的算术后果**，"
                               f"不是关于机制的事实 ⇒ 判**不可评估**，**不可评估 ≠ 不通过**"
                               f"（本项目明令不得写成『触顶即视为非加速』）"})
        return base
    if len(points) < 4:
        base.update({"state": "不可评估", "observed": points,
                     "reason": f"序列只有 {len(points)} 个点（需 ≥4 才谈得上『加速』）⇒ 不可评估，"
                               "不算通过（载体要求：`market_decision_delay_periods=6` ⇒ ≥7 期 = 210 营业日）"})
        return base
    mid = (len(points) + 1) // 2
    first, second = points[:mid], points[mid:]
    slope_first = (first[-1] - first[0]) / (len(first) - 1)
    slope_second = (second[-1] - second[0]) / (len(second) - 1)
    drops = [i for i in range(1, len(points)) if points[i] < points[i - 1]]
    ok = not drops and slope_second > slope_first
    shape = (f"前半 {len(first)} 点斜率 {slope_first:+.6f}/期 · 后半 {len(second)} 点斜率 {slope_second:+.6f}/期")
    if drops:
        reason = f"序列非单调不降（第 {drops} 处回落）：{points}"
    elif slope_second <= slope_first:
        reason = (f"单调不降但**没有加速**（{shape}）：线性上升不构成『加速上升』——"
                  f"『观测不到加速 → 死亡螺旋假设被推翻』正是这条判据要检验的反面")
    else:
        reason = None
    base.update({"state": "通过" if ok else "不通过",
                 "observed": {"series": series, "slope_first": slope_first, "slope_second": slope_second},
                 "reason": reason})
    return base


def judge_clause(params, clause: Clause, a: dict, b: dict | None = None, *, arms_note: str = "") -> dict:
    """判定一条子句。**纯函数**。返回的状态 ∈ {通过, 不通过, 不可评估}。"""
    left = measure(a, clause.measure)
    base = {"clause": clause.id, "text": clause.text, "kind": clause.kind, "measure": clause.measure,
            "profile": clause.profile or a["profile"], "threshold": clause.threshold,
            "measure_arm": clause.measure_arm, "ceiling": clause.ceiling,
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
    if clause.direction in ("decreasing", "non_increasing", "converging_high", "accelerating"):
        #: 序列类子句测的是 `measure_arm` 指定的那一臂（判据文本说"哪一组"，不是臂表的 A/B 顺序）
        target = b if (clause.measure_arm == "B" and b is not None) else a
        series = [float(v) for v in (measure(target, clause.measure)[1] or []) if isinstance(v, (int, float))]
        if clause.direction == "accelerating":
            return judge_accelerating(base, series, clause.ceiling)
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
def excluded_evidence(spec, runs: dict) -> dict:
    """**不再是判据、但仍落进产物**的序列（`§7.1`「`R3-c` 判据修订留痕」第 ⑤ 条）。

    为什么留着：规格修订的理由（B 是内生决策变量、只会往上调）**必须能从产物直接复核**，
    而不是只在提交说明里留一句结论。取 `B` 臂（若判据有 B 臂）—— 足额组的预算是平的，
    看不出「只升不降」。
    """
    if not getattr(spec, "excluded_series", None):
        return {}
    arm_name = "B" if "B" in runs else "A"
    run = runs.get(arm_name)
    if run is None:
        return {}
    out = {}
    for key, why in spec.excluded_series.items():
        values = [v for v in (measure(run, key)[1] or []) if isinstance(v, (int, float))]
        out[key] = {"arm": arm_name, "label": run["label"], "series": values,
                    "monotone": "上升" if all(b >= a for a, b in zip(values, values[1:])) else
                                ("下降" if all(b <= a for a, b in zip(values, values[1:])) else "非单调"),
                    "why": why}
    return out


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
            "excluded_evidence": excluded_evidence(spec, runs),
            "criterion_issue": getattr(spec, "criterion_issue", "") or "",
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