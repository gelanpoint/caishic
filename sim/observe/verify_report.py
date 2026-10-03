"""**验证类报告的装配**：`T-SIM-08` 的 `R1`~`R6` 回测报告（Markdown + 稳定 JSON），
以及从 `sim/bridge/study.py` 迁来的「`R6` 三段证据装配」与「`R` 的诚实提示」。

## 为什么报告装配住在 `observe/` 而不是 `verify/`

`verify/` 负责**判定**（纯函数 + 运行编排），`observe/` 负责**观测与呈现**（`§6` 的 22 指标、
复算自检、报告渲染）。判定与呈现混在一个文件里会有两个后果：

1. 同一段「R6 三段证据」的措辞在 `study.py` 与 `backtest.py` 各写一遍 ⇒ 下次漂移的种子
   （`study.py` 的 `replication_caveat` 已经因为"两处各写一遍、触发阈值不一致"踩过一次，
   见该文件 `replication_facts` 的 docstring）；
2. 报告措辞会反过来影响判定逻辑（改一个词就改了一个判据的含义）。

故本模块只做**呈现**，不改任何判定；判定永远来自 `sim/verify/r_criteria.py` 的数据。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

#: 判定状态 → 报告里的标记（**三态必须一眼可辨**；`不稳健` 与 `不成立` 绝不合并成一个词）
STATE_MARK = {"通过": "✅通过", "不通过": "❌不通过", "不可评估": "⚠️不可评估"}
KIND_MARK = {"absolute": "绝对阈值", "ordering": "序关系"}


def write_backtest(out_dir: Path | str, payload: dict, text: str) -> dict:
    """稳定写出 `backtest.json` / `backtest.md`（键序固定、LF、不转义中文）。"""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
    json_path = target / "backtest.json"
    json_path.write_text(json_text, encoding="utf-8", newline="\n")
    md_path = target / "backtest.md"
    md_path.write_text(text, encoding="utf-8", newline="\n")
    return {"json": str(json_path), "markdown": str(md_path),
            "json_sha256": hashlib.sha256(json_text.encode("utf-8")).hexdigest()}


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _clause_row(clause: dict) -> str:
    kind = KIND_MARK.get(clause.get("kind"), str(clause.get("kind")))
    deps = ", ".join(f"`{k}`" for k in clause.get("depends_on") or []) or "—"
    unsourced = "、".join(clause.get("unsourced") or []) or "无"
    arm = clause.get("measure_arm") or "A"
    return (f"| `{clause['clause']}` | {clause['text']} | {kind} | `{clause['profile']}`"
            f"（测 {arm} 臂） | {deps} | {unsourced} | {STATE_MARK.get(clause['state'], clause['state'])} |")


def render_clause_section(result: dict) -> list[str]:
    lines = ["| 子句 | 判据 | 形式 | 档位 | 依赖参数 | 其中无出处 | 判定 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for clause in result["clauses"]:
        lines.append(_clause_row(clause))
    lines.append("")
    for clause in result["clauses"]:
        lines.append(f"- `{clause['clause']}` 实测 `{_fmt(clause.get('observed'))}`"
                     + (f"；{clause['reason']}" if clause.get("reason") else ""))
        if clause.get("note"):
            lines.append(f"  - ⚠️ {clause['note']}")
    return lines


def render_robustness_section(result: dict) -> list[str]:
    scan = result.get("robustness") or {}
    lines = [f"- 基准（中心档）序关系：`{scan.get('base', {}).get('relation')}`"
             f"（A={_fmt(scan.get('base', {}).get('A'))} / B={_fmt(scan.get('base', {}).get('B'))}）"]
    trials = scan.get("trials") or []
    if not trials:
        lines.append(f"- 本次没有扫描点。{scan.get('note', '')}")
    for trial in trials:
        lines.append(f"- `{trial['key']}={trial['value']}` ⇒ A={_fmt(trial['A'])} B={_fmt(trial['B'])}"
                     f" ⇒ `{trial['relation']}`" + ("　**【翻转】**" if trial["flipped"] else ""))
    if scan.get("skipped"):
        lines.append(f"- 未取到值的扫描点（**不当作稳健**）：{scan['skipped']}")
    return lines


def render_excluded_section(result: dict) -> list[str]:
    """**不再作为判据**的序列（`§7.1`「`R3-c` 判据修订留痕」第 ⑤ 条）。

    必须留在报告正文里：判据被修订的理由若只存在于提交说明，读者无法复核 ——
    「0.3× 组的维护预算逐期**单调上升**」这句话得由产物自己说。
    """
    evidence = result.get("excluded_evidence") or {}
    if not evidence:
        return []
    lines = ["### 已移出判据、但保留在产物里的序列（**判据被修订的可复核证据**）", ""]
    for key, item in evidence.items():
        series = item.get("series") or []
        head = ", ".join(_fmt(v) for v in series[:6]) + (" … " + ", ".join(_fmt(v) for v in series[-3:])
                                                    if len(series) > 6 else "")
        lines += [f"- `{key}`（{item.get('arm')} 臂：{item.get('label')}）实测序列：{head or '（空）'}"
                  f" ⇒ 逐期**{item.get('monotone')}**", f"  - 不再作为判据的理由：{item.get('why')}"]
    lines += ["", "> 完整留痕（改了什么 / 为什么 / 依据 / 档位下限 / 重跑命令）见 "
              "`docs/sim-design.md` §7.1「`R3-c` 判据修订留痕」。", ""]
    return lines


def render_backtest(payload: dict) -> str:
    results = payload.get("results") or []
    lines = ["# `R1`~`R6` 回测结论（`T-SIM-08`）", "",
             "> 判定收敛规则只写在 `sim/verify/backtest.py::verdict_of` 一处：**任一子句不通过 ⇒ 不成立**；"
             "**子句含不可评估 ⇒ 不成立**（不可评估不等于通过）；**中心档全通过但无出处参数端点翻转 ⇒ 不稳健**；"
             "否则**成立**。", "",
             f"参数文件：`{payload.get('params_path')}`", "",
             "## 汇总", "", "| 判据 | 三态 | 一句话理由 | 所用档位 |", "| --- | --- | --- | --- |"]
    for result in results:
        lines.append(f"| `{result['id']}` {result['title']} | **{result['verdict']}** | "
                     f"{result['reason']} | `{result['profile']['label']}` |")
    lines.append("")
    for result in results:
        lines += [f"## `{result['id']}` {result['title']}", "",
                  f"- **三态判定：{result['verdict']}** —— {result['reason']}",
                  f"- **问题**：{result['question']}",
                  f"- **出处**：{result['basis']}",
                  f"- **所用档位**：`{result['profile']['label']}`（R={result['profile']['replications']}）",
                  f"- **为什么是这个档位**：{result['profile']['why']}", ""]
        lines += ["### 臂", "", "| 臂 | 场景 | 走秤率 M-04 | 重复区间 p5~p95 | 在营比 M-03 | 信任 M-15 |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for name, arm in (result.get("arms") or {}).items():
            lo, hi = arm.get("interval_M04") or (None, None)
            lines.append(f"| `{name}` {arm['label']} | `{arm['scenario']}`/{arm['arm']} | {_fmt(arm['M-04'])} | "
                         f"{_fmt(lo)} ~ {_fmt(hi)} | {_fmt(arm['M-03'])} | {_fmt(arm['M-15'])} |")
        lines += ["", "### 子句逐条", ""]
        lines += render_clause_section(result)
        lines += render_excluded_section(result)
        lines += ["", "### 稳健性扫描（无出处参数推到区间端点）", ""]
        lines += render_robustness_section(result)
        lines.append("")
    lines += ["## 口径纪律（复述，因为它是本报告全部结论的前提）", "",
              "- 凡**有真实数据支撑量级**的参数（秤价、费率、补贴比例）→ 可用绝对阈值；",
              "- 凡**关键参数无数据**的（MTBF、维修价、罚款、扫码基线、信任权重、退出阈值）→ "
              "**只允许序关系 / 单调性 / 区间不重叠**，禁止绝对月份与绝对比率；",
              "- 违反者在本报告里一律判 **`不可评估`**，并点名是哪一个参数 —— 不放宽阈值，也不算通过。", ""]
    return "\n".join(lines) + "\n"


def write_consistency(out_dir: Path | str, payload: dict, text: str) -> dict:
    """稳定写出 `consistency.json` / `consistency.md`（与 `write_backtest` 同一约定）。"""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
    (target / "consistency.json").write_text(json_text, encoding="utf-8", newline="\n")
    (target / "consistency.md").write_text(text, encoding="utf-8", newline="\n")
    return {"json": str(target / "consistency.json"), "markdown": str(target / "consistency.md"),
            "json_sha256": hashlib.sha256(json_text.encode("utf-8")).hexdigest()}


def render_consistency(payload: dict) -> str:
    """`§7.3` 一致性报告。**三处口径差必须出现在正文里**，不许只落在 JSON。"""
    comparison = payload["comparison"]
    profile = payload["profile"]
    gap = comparison.get("caliber_gap", {})
    lines = ["# `model ↔ live` 一致性（`T-SIM-08`；`§7.3`）", "",
             f"- 档位：`{profile['label']}`", f"- 容差：**≤ {payload['tolerance']['cents']} 分 / "
             f"≤ {payload['tolerance']['txns']} 笔**",
             f"- 比对：{comparison['days_compared']} 个营业日 × {comparison['fields_compared']} 个字段，"
             f"另有 {comparison['settlement_count']} 张结算单",
             f"- 不一致：{comparison['mismatch_count']} 条字段 + "
             f"{len(comparison['settlement_mismatches'])} 条结算单", "",
             "## 口径差（显式处理，**不是静默忽略**）", "",
             "1. **私下交易（shadow）**：model 侧含、系统侧不可见（`Q-15` 的外部基准）。"
             f"本次 model 侧走秤 {gap.get('scale_txns_total')} 笔 / 私下 {gap.get('shadow_txns_total')} 笔 ⇒ "
             f"model 侧 `M-04` = {_fmt(gap.get('scale_use_rate_model_side'))}，"
             "而系统侧根本看不到分母。**处置**：一致性对账只在走秤通道上做；",
             "2. **取整与计量单位**：model 用「每 500g 单价 + 内置 `round`」、系统用「每公斤单价 + "
             f"`half_up_div`」。单价已按 ×{2} 换算，model 侧期望金额用系统那条取整规则重算；"
             f"两套规则差 1 分的笔数 = **{comparison.get('rounding_gap_txns')} 笔**（单列，不藏）。",
             "3. **佣金**：model 自己是「当日金额 × 费率」的浮点累加，系统是**逐摊位逐日**按实收 `half_up`；"
             "model 侧按系统口径独立复算后再比。", "",
             "## 逐项结果", "", "| 项 | 结果 |", "| --- | --- |",
             f"| 一致性判定 | {'**通过**' if not payload['problems'] else '**不通过**'} |"]
    for problem in payload["problems"][:20]:
        lines.append(f"| 问题 | {problem} |")
    negative = payload.get("sensitivity_negative")
    lines += ["", "## 灵敏度负例（`T-SIM-08` 验收③）", ""]
    if negative is None:
        lines.append("- 本次**未跑**负例（`--consistency` 缺省跑；只有显式关掉才会没有）—— "
                     "那不是『验证过了』。")
    else:
        lines += [f"- 做法：从 model 侧事件流里**删掉一笔走秤成交**，拿**同一份** live 报告重新对账；",
                  f"- 结果：{'**变红（通过）**' if negative.get('red') else '**仍全绿（不通过）**'}，"
                  f"报出 {len(negative.get('problems', []))} 条问题："]
        for problem in (negative.get("problems") or [])[:5]:
            lines.append(f"  - {problem}")
    return "\n".join(lines) + "\n"


def write_calibration(out_dir: Path | str, payload: dict, text: str) -> dict:
    """稳定写出 `calibration.json` / `calibration.md`（与前两个产物同一约定）。"""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
    (target / "calibration.json").write_text(json_text, encoding="utf-8", newline="\n")
    (target / "calibration.md").write_text(text, encoding="utf-8", newline="\n")
    return {"json": str(target / "calibration.json"), "markdown": str(target / "calibration.md"),
            "json_sha256": hashlib.sha256(json_text.encode("utf-8")).hexdigest()}


def render_calibration(payload: dict) -> str:
    """档 2 匹配矩报告。**四条诚实声明必须在正文里**（模块 docstring 的那几条）。"""
    lines = ["# 档 2 · 匹配矩（`T-SIM-08`；`§7.5`）", "",
             f"- 方法：{payload['method']}", f"- 运行档：`{payload['profile']['label']}`",
             f"- 采样：{payload['samples']} 点（LHS）；同时满足**全部 6 个矩**的点："
             f"**{payload['accepted']}**（占比 {payload['accepted_ratio']}）",
             f"- 六个矩的平均命中数：{payload['mean_hits']} / 6", "",
             "## 逐矩命中数", "", "| 矩 | 来源 | 形式 | 表述 | 命中点数 | 出处 |", "| --- | --- | --- | --- | --- | --- |"]
    for moment in payload["moments"]:
        lines.append(f"| `{moment['id']}` | `{moment['from']}` | "
                     f"{'序关系' if moment['kind'] == 'ordering' else '**内含假设阈值**'} | "
                     f"{moment['statement']} | {payload['per_moment_hits'][moment['id']]} | {moment['source']} |")
    lines += ["", "## 结论对哪些参数最敏感（Spearman 秩相关，输出 = 命中的矩数）", "",
              "| 参数 | rho | 样本 | 说明 |", "| --- | --- | --- | --- |"]
    for row in payload["rank_correlations"]:
        lines.append(f"| `{row['key']}` | {row['rho']} | {row['samples']} | {row['note'] or ''} |")
    if payload["stratum_coverage_problems"]:
        lines += ["", "## ⚠️ 分层覆盖度问题（**不当作已覆盖**）", ""]
        lines += [f"- {item}" for item in payload["stratum_coverage_problems"]]
    lines += ["", "## 诚实声明（读结论前必读）", ""]
    lines += [f"{index}. {item}" for index, item in enumerate(payload["caveats"], 1)]
    if payload["accepted"]:
        lines += ["", "## 满足全部 6 个矩的参数点", "", "| 参数 | 取值 |", "| --- | --- |"]
        for point in payload["accepted_points"][:20]:
            cells = " | ".join(f"`{key}`={point[key]:g}" for key in sorted(point))
            lines.append(f"| — | {cells} |")
    else:
        lines += ["", "> **本次没有参数点同时满足全部 6 个矩。** 这是结论，不是缺陷掩盖 —— "
                  "它意味着：在本模型当前接线与本参数空间下，`R1`~`R6` 互相矛盾（详见逐矩命中数）。"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 从 `sim/bridge/study.py` 迁来的两部分（欠账清账：`study.py` 588 行 → ≤400 行）
# ---------------------------------------------------------------------------
def replication_facts(replications: int) -> dict:
    """重复次数 `R` 的**事实**（供报告措辞使用）。唯一事实源，调用点不许各写一份。

    ## 为什么要把它单独提出来（`T-SIM-06` 收口时修的一个真缺陷）

    原实现把"R 的诚实提示"在**两处各写了一遍**（`R6` 第一层判定的括号里、报告第 5 节的不确定清单里），
    且两处的**触发阈值与措辞不一致**。于是 `R=3` 的实际运行会生成"每臂只有一次重复、p5/p95 退化为单点"
    —— 而产物里明明有 3 次重复、p5/p95 也明明是两个不同的数（实测 0.290005 ~ 0.485007）。
    **报告对自己产物的描述与产物不符**，正是本项目最不能接受的那类错误。
    """
    return {
        "r": int(replications),
        "interval_is_a_single_point": int(replications) <= 1,
        "at_design_level": int(replications) >= 20,
    }


def replication_caveat(replications: int) -> str:
    """报告用的**长**提示（与 `replication_caveat_short` 同源，不许各写一份）。"""
    facts = replication_facts(replications)
    if facts["interval_is_a_single_point"]:
        return ("**R=1 ⇒ 每臂只有一次重复，p5/p95 退化为单点，于是『区间不重叠』这条判据在 R=1 下"
                "几乎必然成立 —— 它是本报告里最弱的一类证据**，所有「显著优于 / 区间不重叠」的措辞都不可当结论；"
                "本报告的结论一律以**序关系 + 条件句扫描**为准。")
    if not facts["at_design_level"]:
        return (f"R={facts['r']} < 20 ⇒ p5/p95 由 {facts['r']} 个样本算出，区间估计有抽样误差，"
                "『区间不重叠』的强度**低于设计档**（但区间**不是**单点）；本报告的结论一律以"
                "**序关系 + 条件句扫描**为准。")
    return f"R={facts['r']} 达到设计档（≥20），区间估计可用。"


def replication_caveat_short(replications: int) -> str:
    """`R6` 第一层判定后面挂的**短**括号提示（同一事实源，只是更紧凑）。"""
    facts = replication_facts(replications)
    if facts["interval_is_a_single_point"]:
        return ("（⚠️ R=1 ⇒ 每臂只有一次重复、p5/p95 退化为单点，"
                "『区间不重叠』这一层在本报告里**不构成证据**；请以第二层的条件句扫描为准）")
    if not facts["at_design_level"]:
        return (f"（⚠️ R={facts['r']} < 设计档 20 ⇒ p5/p95 由 {facts['r']} 个样本算出，"
                "区间估计本身有抽样误差，『区间不重叠』这一层的强度**低于设计档**（但区间**不是**单点）；"
                "请以第二层的条件句扫描为准）")
    return f"（R={facts['r']} 达到设计档 ≥20）"


# ---------------------------------------------------------------------------
# `R6` 三段证据装配（从 `study.py` 原样迁来，**不重写措辞**：改措辞 = 改结论的表达）
# ---------------------------------------------------------------------------
def r6_three_part_lines(results: list[dict], sensitivity: dict) -> list[str]:
    """`R6` 反例的**三段证据**（缺一不可）。纯呈现函数：输入 `study.py` 的臂结果，输出 Markdown 行。

    ## 为什么 `R6` 要给三段而不是一个布尔值（`docs/sim-design.md` §7.1 的 `R6` 行）

    反例检验的意义是**防止"怎么调都能活"**。若只报一个布尔值，读者无法分辨：

    * 自费组真的更差 ⇒ `结论 26` 可被检验；
    * 自费组与出资组一样好 ⇒ **模型里的"自费"没有真实成本**（这不是缺陷掩盖，是**结论**）。

    第二段（成本权重扫描）就是为回答"要多强的心理权重才分得开"而存在的 ——
    它让结论从断言变成**关于参数的命题**，而这正是 `A-18`（无出处）允许的唯一表述方式。
    """
    s5 = [row for row in results if row["scenario"] == "S5"]
    lines = ["### 三段证据（缺一不可）", "", "| 臂 | 走秤率(分子/分母) | 重复区间(p5~p95) | 价目表维护率 | 设备闲置(摊位-日) |",
             "| --- | --- | --- | --- | --- |"]
    for row in s5:
        m04 = row["metrics"]["M-04"]
        summary = row["summary"].get("M-04", {})
        lines.append(
            f"| `{row['arm']}` | {m04['ratio']} ({m04['numerator']}/{m04['denominator']}) | "
            f"{summary.get('p5')} ~ {summary.get('p95')} | {row['metrics']['M-09']['ratio']} | "
            f"{row['metrics']['M-10']['ratio']} |")
    lines += _r6_first_layer(s5, results)
    lines += ["", "### 第二层：参与摩擦权重 `merchant_adoption_cost_weight`（**A-18 无出处**）"
              " —— **在自费臂上扫描**", "",
              "> 为什么必须在自费臂上扫：`adopt_decision` 的判据是 `cost_weight × upfront ≤ "
              "月净收益 × 忍耐期`。市场方出资时 `upfront = 0` ⇒ 判据恒真，扫这个权重等于"
              "**扫一个乘 0 的数**，只会得到一条假平线（第一版就是这么扫的，见本报告第 5 节）。", ""]
    lines += _r6_second_layer(s5, sensitivity)
    lines += ["", "### 第三层：财务通道（`merchant_device_share_cents` vs 自费口径，**两条通道分开看**）", ""]
    lines += _r6_third_layer(s5)
    lines += ["", "> **结论口径（`R6` 三态，不许只有二态）**：",
              "> * **成立**：自费组显著更差（第二层扫描出现低于出资臂的权重点，且该权重可被论证）；",
              "> * **不成立**：自费组活得一样好 ⇒ 模型里的『自费』没有真实成本 ⇒ `结论 26` **无法被检验**"
              "（此时必须如实说「这条反例没通过」，不许把它读成「设计更好」）；",
              "> * **不稳健**：结论随无出处参数（成本权重 / 毛利率 / 单摊流水量级）翻转 ⇒ 只能作为"
              "**关于参数的命题**呈现，不许写成「商户自费必然被弃用」。",
              "> 本报告的实际落点见上两层的数字 —— **不是**预设的「自费必然被弃用」。", ""]
    return lines


def _r6_first_layer(s5: list[dict], results: list[dict]) -> list[str]:
    if len(s5) != 2:
        return ["", "- 缺 S5 两臂结果 ⇒ 第一层无法比较。"]
    a, b = s5[0]["summary"].get("M-04", {}), s5[1]["summary"].get("M-04", {})
    overlap = bool(a) and bool(b) and min(a["p95"], b["p95"]) >= max(a["p5"], b["p5"])
    lower = (s5[1]["metrics"]["M-04"]["ratio"] or 0) < (s5[0]["metrics"]["M-04"]["ratio"] or 0)
    verdict = ("成立（自费组更低且区间不重叠）" if (lower and not overlap)
               else "**不成立（区间重叠或方向相反）** —— 即模型里的『自费』在该权重下没有真实成本")
    verdict += replication_caveat_short(results[0]["replications"] if results else 0)
    return ["", f"- **判定（第一层）**：{verdict}",
            f"- 区间：出资 {a.get('p5')}~{a.get('p95')} vs 自费 {b.get('p5')}~{b.get('p95')}；"
            f"重叠 = {overlap}；自费组更低 = {lower}"]


def _r6_second_layer(s5: list[dict], sensitivity: dict) -> list[str]:
    s5_own = ((sensitivity.get("by_scenario") or {}).get("S5") or {}).get("own_oat") or {}
    weight_row = {row["key"]: row for row in (s5_own.get("summary") or [])}.get("merchant_adoption_cost_weight")
    if not weight_row or sensitivity.get("skipped"):
        return ["- 本次未跑敏感性（`--no-study`）或本场景无 own-OAT 结果 ⇒ **第二层缺证据**，"
                "不得据第一层下结论。"]
    funded_ratio = s5[0]["metrics"]["M-04"]["ratio"] if s5 else None
    output_cells = ["不可评估" if v is None else "%.4f" % v for v in weight_row["outputs"]]
    crossings = [(value, out) for value, out in zip(weight_row["values"], weight_row["outputs"])
                 if out is not None and funded_ratio is not None and out < funded_ratio]
    lines = [f"- 扫描点 {weight_row['values']} ⇒ 自费臂走秤率 {output_cells}，方向 `{weight_row['direction']}`",
             f"- 同配置下**出资臂**走秤率 = {funded_ratio}"]
    if crossings:
        threshold = min(value for value, _out in crossings)
        below = [value for value in weight_row["values"] if value < threshold]
        low = max(below) if below else None
        lines.append(
            f"- ⇒ **翻转窗口 = ({low}, {threshold}]**（该区间内『自费 ⇒ 被弃用』由不成立翻成成立）。"
            f"`R6` 的结论因此只能写成**条件句**：" f"① 权重 ≲ {low} 时自费组与出资组"
            "**没有可观测差别** ⇒ 模型里的『自费』没有真实成本 ⇒ `结论 26` **在本模型里无法被检验**；"
            f"② 权重 > {threshold} 时自费组被弃用（实测采用率直接掉到 0）⇒ `结论 26` 成立。"
            "⚠️ 该权重的出处是 A-18（**假设，无数据**）⇒ **不许**声称现实中的商户落在哪一侧；"
            "本报告只给出『要多强的参与摩擦才分得开』这一个**关于参数**的命题。")
    else:
        lines.append("- ⇒ 在扫描范围内**没有出现**低于出资臂的权重点 ⇒ 模型里的『自费』在该区间内没有真实成本，"
                     "`结论 26` 无法被本模型检验（这是结论，不是缺陷掩盖）。")
    if any(value == 0.0 for value in (weight_row.get("outputs") or [])):
        lines.append("- 机制说明（**必须一起读，否则会读成「自费只是稍微差一点」**）：走秤率掉到 0 是因为"
                     "**采用是吸收态** —— 不采用 ⇒ 不发价目表 ⇒ 一笔走秤都没有。"
                     "这个「全有或全无」的形态是本模型的简化，属已知边界。")
    return lines


def _r6_third_layer(s5: list[dict]) -> list[str]:
    if len(s5) != 2:
        return ["- 缺 S5 两臂结果 ⇒ 无法比较。"]
    m19 = s5[1]["metrics"].get("M-19", {})
    detail = m19.get("detail", {})
    return [
        f"- 自费臂的商户月净收入分布（`M-19`，取自自费臂）："
        f"p10={detail.get('p10')} · p50={detail.get('p50')} · p90={detail.get('p90')}（单位：分/摊位-月）",
        "- 一次性支出（3750 元/台）与月净收入相差**两个量级以内**，故在权重 ≈ 1 时"
        "『一年忍耐期收益 ≫ 一次性支出』恒成立 ⇒ 意愿通道不触发；"
        "财务通道（`device_share_cents` 改为自费摊销）也**不足以**把商户推离 `comply`。",
        f"- 证据：自费臂的三份额 `M-06` = {s5[1]['metrics'].get('M-06', {}).get('ratio')}，"
        f"出资臂 = {s5[0]['metrics'].get('M-06', {}).get('ratio')}（若完全相同 ⇒ 两条通道都没起作用）。",
    ]