"""报告落盘（`T-SIM-06`）：`report.md` + `metrics.json` / `scenarios.json` / `sensitivity.json`。

**纯文本 + 内联表格，零外部资源**（无 CDN、无 JS 库、无字体外链）——报告本身就是离线可读的。

## 报告里必须显眼的三件事（本项目硬约束，不是可选项）

1. **`commission_rate_bp` 的 2% 不是实测费率**，只出现在结论 21 的标题对照口径 —— 放在标题下方，
   不是只躺在 `params.json` 里；
2. **有真实数据支撑的结论 vs 只能给序关系的结论**必须**分列**：有数据的（秤价、补贴比例、
   通道费率、温州买方费率、验收抽检比例、罚款量级、云软件/自费口径）才允许绝对阈值；
   无数据的（MTBF / 维修价 / 罚款 / 扫码基线 / 信任权重 / 退出阈值 / τ_repair）**只允许序关系与区间不重叠**；
3. **每一个数字都带分子与分母**（`AC-005` 精神），且**不稳健的结论单独分区**（§7.2 第 2 条）。
"""

from __future__ import annotations

import json
from pathlib import Path

#: 必须在报告最显眼位置出现的口径声明
COMMISSION_DISCLAIMER = (
    "> ⚠️ **`commission_rate_bp` 的「2%」不是任何市场的实测费率** —— 它只出现在《调研报告》"
    "**结论 21 的标题**里（那是与微信通道费 0.38%–0.6% 作对照的语境），本项目把它当作"
    "**被考察的政策变量**。任何「抽佣 2% 会导致 X」的句子都只能读作"
    "**关于该费率取值的命题**，不能读作调研结论。"
)

#: 有真实出处 ⇒ 允许绝对阈值；无出处 ⇒ **只允许序关系/区间不重叠**
SOURCED_ALLOWED = (
    "秤价（结论 23：1350/2145/2566 元/台）· 摊位小屏（结论 24：1100 元/台）· 网络投入（结论 25：120.5 万/市场）· "
    "补贴比例与上限（结论 3：50%/200 万）· 微信通道费率（结论 21：0.38%–0.6%）· 温州买方费率（结论 12：0.5%–6%）· "
    "商户自费口径（结论 26：3750 元/台、云软件 5000 元/年）· 验收抽检比例（结论 4：10%）· "
    "行政罚款量级（结论 2：2 万元，罚**开办者**）· 银行合作资金（结论 19：200 余万元/三年期）· "
    "八两秤幅度名称口径（结论 13：0.8 ⇒ d=0.2）· 郑州单摊月流水量级（结论 18/P-15，**推算且统计期间未交代**）"
)
ORDINAL_ONLY = (
    "设备 MTBF（A-11）· 维修单价/技师数/MTTR（A-12）· 短秤罚款分布 F_short（A-14）· "
    "日常计量抽检基准 p_check（A-15）· 消费者扫码基线（A-09）· 信任更新权重 η⁺/η⁻/δ（A-08，**全设计最弱一环**）· "
    "商户退出阈值 θ_exit / 连续破线月数（A-05）· 转换成本 Σ_exit（A-06）· τ_repair / 设备闲置阈值（A-13）· "
    "维护预算与考核参数（A-18）· 价格公示拉动系数（A-19）· 出口查验执行率/扫码覆盖（A-20）· **抽佣 2% 本身（A-21）**"
)


def write_json(path: Path, payload) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                          encoding="utf-8", newline="\n")


def _pct(value) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _num(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return f"{value:,}"


def _baseline_result(results: list[dict]) -> dict:
    """报告里的"指标表 / 存活判据"必须取自**基线臂**，不是最后一个臂。

    第一版取 `results[-1]` —— 那是 `S6-③`（堵死但考核只看装机量）的臂，
    于是指标表与存活四条讲的是**场景最极端的那一臂**，而 `metrics.json` 里存的是 `S0`：
    同一份报告里两处口径不同，谁也没说。这是"看起来有数据、其实张冠李戴"的形态。
    """
    for result in results:
        if result.get("scenario") == "S0":
            return result
    return results[0] if results else {}


def metric_table(metrics: dict) -> list[str]:
    lines = ["| 指标 | 分子 | 分母 | 比值 | 可用 | 适用条件（无数据的只允许序关系） |",
             "| --- | --- | --- | --- | --- | --- |"]
    for key in sorted(metrics):
        row = metrics[key]
        if row.get("ratio_is_ratio") is False:
            # 非比值指标（M-02/M-16/M-18/M-19）**不硬凑一个百分比** ——
            # 那会把天数/现金流/月均收入印成"百分比"（M-19 都印到 346740059.30% 了）
            ratio_cell = f"非比值；值 = {_num(row.get('value', row.get('numerator')))}"
        else:
            ratio_cell = _pct(row.get("ratio"))
        lines.append(
            f"| `{key}` {row.get('name', '')} | {_num(row.get('numerator'))} | {_num(row.get('denominator'))} | "
            f"{ratio_cell} | {'是' if row.get('available') else '否（' + str(row.get('reason')) + '）'} | "
            f"{row.get('condition', '')} |"
        )
    return lines


def arm_table(results: list[dict]) -> list[str]:
    """场景×臂的结果表：**每个数字都带分子/分母**（走秤率与转暗率取自 M-04/M-05）。"""
    lines = ["| 场景/臂 | 走秤率(分子/分母) | 转暗率(分子/分母) | 价目表维护率 | 摊位数(末/初) | 累计净现金流 | 存活四条 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for result in results:
        metrics = result["metrics"]
        m04, m05 = metrics["M-04"], metrics["M-05"]
        survival = result.get("survival", {}).get("criteria", {})
        passed = "".join("✅" if item.get("pass") else "❌" for item in survival.values())
        lines.append(
            f"| `{result['arm']}` | {_pct(m04.get('ratio'))} ({_num(m04.get('numerator'))}/{_num(m04.get('denominator'))}) | "
            f"{_pct(m05.get('ratio'))} ({_num(m05.get('numerator'))}/{_num(m05.get('denominator'))}) | "
            f"{_pct(metrics['M-09'].get('ratio'))} | "
            f"{_num(metrics['M-03'].get('numerator'))}/{_num(metrics['M-03'].get('denominator'))} | "
            f"{_num(metrics['M-18'].get('value'))} | {passed or '—'} |"
        )
    return lines


def _as_rows(value) -> list:
    """OAT 的两种形状都接受：完整结果 `{"rows":…, "summary":[…]}` 或**已经压好的 summary 列表**。

    第一版只认前者，于是"每场景一份 summary 列表"的新形状直接 `AttributeError`
    （`'list' object has no attribute 'get'`）—— 报告崩在最后一步，前面几十分钟的仿真白跑。
    """
    if isinstance(value, dict):
        return value.get("summary") or []
    return value or []


def scenario_sensitivity_lines(scenario_id: str, sensitivity: dict) -> list[str]:
    """每个场景都要有"对哪些参数敏感"（`§7.2` 第 3 条：不许只给一条曲线）。

    给**三样**，因为它们各自防一种假结论：
    ① **本场景对照指标**的 OAT（曲线画在本场景真正要判的那个量上，而不是所有场景都画走秤率）；
    ② **全局秩相关**（LHS 的 Spearman，回答"哪个参数在参数空间里最要紧"）；
    ③ 结论**稳健性**（在扰动其他 [假设] 参数后序关系是否翻转）。
    """
    own = sensitivity.get("own_oat") or {}
    lines = [f"#### `{scenario_id}` 的参数敏感性（OAT，一次一参数；**序关系**，不是绝对阈值）", ""]
    if own.get("summary"):
        lines += [f"曲线的输出量 = 本场景的对照指标 `{own.get('metric')}`"
                  f"（**不是**所有场景都画走秤率）；固定配置 = 本场景臂 `{own.get('arm')}`", "",
                  f"| 参数 | 扫描点 | 输出（{own.get('metric')}） | 方向 | 单调 |", "| --- | --- | --- | --- | --- |"]
        for row in own["summary"][:12]:
            rendered = ", ".join(_pct(v) if own.get("metric") != "M-19" else _num(v) for v in row["outputs"])
            lines.append(f"| `{row['key']}` | {', '.join(str(v) for v in row['values'])} | {rendered} | "
                         f"{row['direction']} | {'是' if row['monotone'] else '否'} |")
    else:
        lines.append("- （本场景未跑独立 OAT：见下方全局表）")
    ranks = [row for row in _as_rows(sensitivity.get("rank_correlations")) if row.get("rho") is not None][:5]
    if ranks:
        lines += ["", f"- **全局秩相关（LHS Spearman，前 {len(ranks)}）**："
                      + "；".join(f"`{row['key']}` ρ={row['rho']:+.3f}" for row in ranks)]
    global_rows = _as_rows(sensitivity.get("global_oat"))
    if global_rows:
        top = global_rows[:5]
        lines += ["- **全局 OAT（输出 = 走秤率 M-04，其余固定在 S0）**："
                  + "；".join(f"`{row['key']}` {row['direction']}"
                              + (f"（跨度 {row['span']:.4f}）" if row.get("span") is not None else "（跨度不可评估）")
                              for row in top)]
    stability = sensitivity.get("stability")
    if stability:
        lines.append("")
        lines.append(f"- 结论稳健性（**在扰动其他 [假设] 参数后序关系是否翻转**）：`{stability['verdict']}` "
                     f"（期望 {stability['expected']}，检验 {stability['trials']} 组，翻转 {len(stability['flips'])} 组）")
    return lines


def render_report(*, context: dict, results: list[dict], sensitivity: dict,
                  doubts: list[str], declared_vs_recomputed: list[str],
                  degeneracy: dict, cross_checks: list[str]) -> str:
    """生成 `report.md` 全文。"""
    lines: list[str] = []
    lines += ["# 多智能体仿真结果报告（`T-SIM-06`）", "",
              f"- 场景：{', '.join(context['scenarios'])}；每臂 {context['days']} 营业日 × "
              f"{context['replications']} 次重复（seed 基线 {context['seed']}）",
              f"- 事件目录：`{context['out_root']}`；参数文件：`{context['params_path']}`",
              f"- 指标口径：`docs/sim-design.md` §6（22 个，分子/分母逐条给出）；"
              f"复算入口 `sim/observe/metrics.py::compute_all(events)`", "",
              COMMISSION_DISCLAIMER, "",
              "## 0. 先说清楚：哪些结论有真实数据支撑、哪些只能给序关系", "",
              "**允许用绝对阈值的（有仓库内可核出处）**", "", f"- {SOURCED_ALLOWED}", "",
              "**只允许序关系 / 区间不重叠的（无数据，给精确阈值就是把假设写成事实）**", "", f"- {ORDINAL_ONLY}", "",
              "---", "",
              "## 1. 场景与臂（对照实验只差声明的变量）", "",
              "每个场景的每一臂都只允许改 `varying_keys`/`varying_flags` 里声明的变量，"
              "其余一律取该场景的 `baseline_overrides` —— 这条由 `scenario_problems()` 机械校验。", ""]
    lines += arm_table(results)
    lines.append("")
    baseline = _baseline_result(results)
    for scenario in context.get("scenario_meta", []):
        lines += [f"### `{scenario['id']}` {scenario['title']}", "",
                  f"- 变的是什么：{scenario['what_changes']}",
                  f"- 对照设置：{scenario['control_note']}",
                  f"- 出处：{', '.join(scenario['basis'])}", ""]
        lines += scenario_sensitivity_lines(scenario["id"], (sensitivity.get("by_scenario") or {}).get(scenario["id"], {}))
        lines.append("")
    lines += ["---", "", "## 2. `R6` 反例检验：商户自费 ⇒ 是否真的被弃用", ""]
    lines += context.get("r6_lines", ["（未运行）"])
    lines += ["", "---", "", "## 3. 指标（每个都带分子/分母）", "",
              f"> 本节的 22 个指标取自**基线臂** `{baseline.get('scenario', '?')}::{baseline.get('arm', '?')}`"
              f"（与 `metrics.json` 同一份；其余臂的表在 `scenarios.json` 里），"
              "**不是**最后一个跑到的臂 —— 口径必须与产物一致。", ""]
    lines += metric_table(baseline.get("metrics") or {})
    degenerate_ids = [item["id"] for item in (degeneracy.get("degenerate") or [])]
    lines += ["", "**退化说明（必须如实列出）**：当前数据下以下指标分子/分母无信号，"
                  "因此**不能用扰动法检验**它们是否写死（`Q-19` 的教训）—— "
                  f"`{', '.join(degenerate_ids) if degenerate_ids else '（无）'}`"
                  f"（已用扰动法检验并变红/变绿的：{len(degeneracy.get('checked') or [])} 个）。", "",
              f"- 扰动法判红的问题：`{degeneracy.get('problems') or '（无）'}`", "",
              "**两条路径对账**（`txn` 明细 vs `block_summary`/`device_state`）：",
              f"`{cross_checks if cross_checks else '全部一致'} `", "",
              "**产物 ↔ 事件流对账**："
              f"`{declared_vs_recomputed if declared_vs_recomputed else '一致'}`", "",
              "---", "", "## 4. 存活判据（四条分别给，阈值全部是假设）", "",
              f"> 取自基线臂 `{baseline.get('scenario', '?')}::{baseline.get('arm', '?')}`。", ""]
    survival = baseline.get("survival", {})
    for name, item in (survival.get("criteria") or {}).items():
        lines.append(f"- `{name}`：实际值 {item['value']!r} ⇒ {'通过' if item['pass'] else '不通过'}"
                     f"（**阈值是假设**，不是需求规定）")
        if item.get("note"):
            lines.append(f"  - ⚠️ {item['note']}")
        elif item.get("window"):
            lines.append(f"  - 口径：{item['window']}，分子/分母 = {item.get('numerator')}/{item.get('denominator')}")
    lines += ["", f"> {survival.get('disclaimer', '')}", "", "---", "",
              "## 5. 不确定、未做到、存疑（如实列出，不美化）", ""]
    lines += [f"- {item}" for item in doubts] or ["- （无）"]
    lines += ["", "---", "", "## 6. 本报告自己怎么被检验（否则只是一堆数字）", "",
              "1. `sim/observe/metrics_check.py::cross_check_problems` —— 两条独立事件路径必须一致；",
              "2. `metrics_check.mutation_sensitivity_problems` —— 逐指标删掉其来源事件整族，值必须变"
              "（**把指标写死成常量即判红**）；",
              "3. `metrics_check.verify_against_declared` —— 产物值与事件流复算必须一致；",
              "4. `tests/sim/test_metrics_recomputable.py` 的合成负例 —— 上述三条**故意破坏后必须变红**；",
              "5. `scenario_problems()` —— 场景「只差声明变量」的机械校验（多改一个键即判红）。", ""]
    return "\n".join(lines) + "\n"


def write_report(out_dir: Path, text: str, metrics: dict, scenarios: dict, sensitivity: dict) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(text, encoding="utf-8", newline="\n")
    write_json(out_dir / "metrics.json", metrics)
    write_json(out_dir / "scenarios.json", scenarios)
    write_json(out_dir / "sensitivity.json", sensitivity)
    return {"report": str(out_dir / "report.md"), "metrics": str(out_dir / "metrics.json"),
            "scenarios": str(out_dir / "scenarios.json"), "sensitivity": str(out_dir / "sensitivity.json")}
