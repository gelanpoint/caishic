"""**纯静态报告装配**（`T-SIM-09`）：`sim/` 的产物 → 单文件 `report.html`（内联 SVG、零外部资源）。

## 报告的四块内容与它们各自的数据来源（**一个数都不许硬编码**）

| 块 | 内容 | 数据来源 |
| --- | --- | --- |
| ① | 七场景 `S0`~`S6` 走秤率对照（每臂一行，带分子/分母） | `study/scenarios.json` |
| ② | `R1`~`R6` 三态结论 | `verify/backtest/backtest.json` |
| ③ | 四类性质分解（`docs/sim-验证结论实况.md` V-05 的四类） | 同上，**由规则从子句机械算出** |
| ④ | `V-01` 的三臂走秤率对照（`S6` 三臂） | `study/scenarios.json` |

## 缺产物时怎么办：**显示「未生成」，不填假数据**

产物在 `data/` 下（`.gitignore` 忽略），**入库的是代码不是数据**。所以这份报告在
**没跑过仿真**的机器上打开，四块全是「未生成」+ 复现命令 —— 这比"图是齐的但数是编的"诚实得多，
也正是 `T-SIM-09` 的验收⑤（反向断言：注入构造产物后必须能显示真数据，防止"永远显示未生成"的假绿）。

## 三态与四类都是**从产物算出来的**，不是抄进来的

* 三态直接读 `backtest.json` 的 `verdict`（收敛规则在 `sim/verify/backtest.py::verdict_of` 一处）；
* 四类按 [`classify_nature`] 的**规则**从子句状态与原因文本机械分类。规则写在代码里并可被用例喂负例 ——
  分类若靠人手点，报告就会与产物漂移，而漂移的方向通常是"把自己写成好的"。

## 零外部资源（`REQ-025` / `AC-013` / `AGENTS.md` §3 硬要求 4）

全部图元内联、无脚本、无外部字体。**注意 SVG 的 `xmlns` 也不能写** —— 它是字面外链，
详见 [`svg_charts.svg`](svg_charts.py) 的 docstring。验收由
`tests/contract/test_no_external_assets.py::scan_static_text()` 执行，**本模块不另写正则**。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from . import svg_charts as charts
from .svg_charts import NOT_GENERATED, esc

#: 产物登记表：**路径缺省相对产物根**，可用命令行逐个覆盖；复现命令写在这里，报告直接印出来。
#: 为什么能逐个覆盖：回测与场景研究是**两条独立的产线**（见两条 `repro`），本来就落在不同的根下，
#: 硬绑成一个根等于逼着操作者为报告重跑一遍已经跑完的东西。
ARTIFACTS = (
    {"key": "study", "relative": "study/scenarios.json", "override": "svg_study",
     "title": "① 七场景对照 · ④ `V-01` 三臂",
     "repro": "python -m sim --scenario all --days 360 --replications 3 --no-study --out-dir <产物根>"},
    {"key": "backtest", "relative": "verify/backtest/backtest.json", "override": "svg_backtest",
     "title": "② `R1`~`R6` 三态 · ③ 四类性质",
     "repro": "python -m sim --backtest --out-dir <产物根>"},
)

#: 四类性质的**名称与定义**（`docs/sim-验证结论实况.md` V-05「四类性质」）。
#: **这里只有分类学，没有结论** —— 每条判据落进哪一类由 [`classify_nature`] 从产物算出。
NATURE_BINS = (
    ("不可评估", "判据依赖 `provenance.kind=assumed` 的参数，机制上就不能判。这是诚实纪律在起作用，不是失败。"),
    ("真不通过", "判据可判，值没达到。"),
    ("欠功效", "两组在中心档逐位相同 —— 是该档位可分辨度不够，不是『没有效应』。"),
    ("判据本身有问题", "被判量与对照实验的设计不自洽；这一类必须改判据，不许用『实测不通过』解释。"),
)
NATURE_TONE = {"不可评估": "warn", "真不通过": "bad", "欠功效": "muted", "判据本身有问题": "ink"}

#: 三态 → 标记色（`不成立` 与 `不稳健` **绝不合并成一个词**，见 `r_criteria.VERDICTS`）
VERDICT_TONE = {"成立": "good", "不成立": "bad", "不稳健": "warn"}

#: 「这一类本轮为空」的措辞。**刻意与 `NOT_GENERATED` 分成两个词** ——
#: 「产物没生成」与「这一类没有判据落入」是相反的两件事，混用会让人以为判据没跑。
NATURE_EMPTY = "（本轮无判据落入这一类）"

#: 「七场景」的**权威清单**（`sim/scenarios/*.json` 落的就是这 7 个）；`V-01` 三臂在 `S6`
#: （堵死 / 不堵 / 堵死但考核只看装机量）。报告只印产物里有的 ⇒ 产物不全必须显式点名。
EXPECTED_SCENARIOS = ("S0", "S1", "S2", "S3", "S4", "S5", "S6")
V01_SCENARIO = "S6"

CSS = ("body{font-family:system-ui,sans-serif;color:#1f2933;background:#fff;margin:0 auto;max-width:960px;"
       "padding:24px;line-height:1.6}h1{font-size:22px}h2{font-size:18px;border-bottom:1px solid #d6dde3;"
       "padding-bottom:6px}h3{font-size:15px;margin-bottom:4px}table{border-collapse:collapse;font-size:12px;"
       "width:100%}td,th{border:1px solid #d6dde3;padding:4px 6px;text-align:left}"
       "th{background:#f4f6f8}code{background:#f4f6f8;padding:1px 4px;border-radius:3px}"
       ".card{border:1px solid #d6dde3;border-radius:6px;padding:12px;margin:14px 0}"
       ".row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0}"
       ".warn{color:#9a6a12}.bad{color:#a83232}.muted{color:#7b8794}")


@dataclass(frozen=True)
class Source:
    """一份产物的**登记信息**（含指纹）。`missing=True` 时 `payload` 必为 `None`。"""

    key: str
    title: str
    path: Path
    relative: str
    repro: str
    sha256: str
    missing: bool
    payload: dict | None

    @property
    def shown_path(self) -> str:
        """报告里印的路径：一律正斜杠；在当前工作目录之下的一律印**相对路径**（报告是要交付的产物，
        印一串本机盘符既没信息量、又把它带进交付物）。"""
        try:
            return self.path.resolve().relative_to(Path.cwd()).as_posix()
        except ValueError:
            return self.path.as_posix()

    def missing_reason(self) -> str:
        return f"未找到产物 `{self.shown_path}`；复现命令：`{self.repro}`"


def load_source(data_root: Path | str, spec: dict, explicit: Path | str | None = None) -> Source:
    """读一份产物并登记指纹。**缺文件不是异常** —— 它就是"未生成"这一合法状态。"""
    root = Path(data_root)
    path = Path(explicit) if explicit else root / spec["relative"]
    relative = Path(explicit).as_posix() if explicit else spec["relative"]
    if not path.is_file():
        return Source(spec["key"], spec["title"], path, relative, spec["repro"], "", True, None)
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return Source(spec["key"], spec["title"], path, relative, spec["repro"],
                      hashlib.sha256(raw).hexdigest(), True,
                      {"__parse_error__": f"产物存在但读不出（{error.__class__.__name__}）"})
    return Source(spec["key"], spec["title"], path, relative, spec["repro"],
                  hashlib.sha256(raw).hexdigest(), False, payload)


def load_sources(data_root: Path | str, overrides: dict | None = None) -> dict:
    """按 `ARTIFACTS` 登记表读全部产物。`overrides` = `{artifact 键: 显式路径}`。"""
    overrides = overrides or {}
    return {spec["key"]: load_source(data_root, spec, overrides.get(spec["key"])) for spec in ARTIFACTS}


# ---------------------------------------------------------------------------
# 数据整形（纯函数，可被合成产物直接喂）
# ---------------------------------------------------------------------------
def arm_rows(source: Source) -> list:
    """`scenarios.json` → `[(场景, 臂, 走秤率 | None, 分子/分母)]`；缺产物时返回空列表。"""
    if source.missing or not source.payload:
        return []
    rows = []
    for result in ((source.payload.get("results")) or []):
        ratio = ((result.get("metrics") or {}).get("M-04") or {})
        rows.append((result.get("scenario") or "?", result.get("arm") or "?",
                     ratio.get("ratio"), f"{ratio.get('numerator')}/{ratio.get('denominator')}"))
    return rows


def missing_scenarios(source: Source) -> list:
    """这份产物**没有覆盖**的场景 id。必须有它：产物不全时图看起来仍然是齐的 —— 事实得由报告说。"""
    present = {row[0] for row in arm_rows(source)}
    return [scenario for scenario in EXPECTED_SCENARIOS if scenario not in present]


def coverage_note(source: Source) -> str:
    absent = missing_scenarios(source)
    return "" if not absent else (
        f"<p class='warn'><b>本次产物未覆盖：{'、'.join(absent)}</b> —— "
        f"上面的对照只画了产物里有的场景，**不是完整的七场景**；"
        f"要补齐请按本报告「数据来源」表里的复现命令重跑。</p>")


def s6_arms(source: Source) -> list:
    """`V-01` 的三臂 = `S6` 的三臂（考核口径 / 堵不堵 的对照）。按臂序原样给出，不排序。"""
    return [(arm, ratio, note) for scenario, arm, ratio, note in arm_rows(source) if scenario == V01_SCENARIO]


def backtest_rows(source: Source) -> list:
    """`backtest.json` → `[(判据, 三态, 理由, 档位)]`。"""
    if source.missing or not source.payload:
        return []
    rows = []
    for result in ((source.payload.get("results")) or []):
        profile = result.get("profile") or {}
        rows.append((result.get("id") or "?", result.get("verdict") or NOT_GENERATED,
                     result.get("reason") or "", profile.get("label") or "（档位未登记）"))
    return rows


#: `judge_clause` 在"两组逐位相同"时写的原因片段 —— 欠功效的**机械**判据（不是看数字，是看判定器怎么说）
UNDERSIZED_MARK = "测不出差异"


def classify_nature(result: dict) -> str:
    """一条 `R` 判据的**性质**（`docs/sim-验证结论实况.md` V-05 的四类）。

    **规则（按优先级，机械可复算）**：

    1. 判据**显式声明**了 `criterion_issue` ⇒ **④ 判据本身有问题**（这一条优先于一切实测结果 ——
       判据本身错了的时候，"实测不通过"这句话是在给一个坏尺子量东西）；
    2. 有子句 `不可评估` ⇒ **① 不可评估**（机制上不能判，与实测无关）；
    3. 有子句 `不通过`，且其原因含「测不出差异」（两组逐位相同）⇒ **③ 欠功效**；
    4. 有子句 `不通过`（其它原因）⇒ **② 真不通过**；
    5. 全通过 ⇒ **成立**（不落进任何"不成立"的分箱）。

    顺序固定且写死：**同一份产物任何时候都给出同一个分类**，不许"看着像"手工调。
    """
    if result.get("criterion_issue"):
        return "判据本身有问题"
    clauses = result.get("clauses") or []
    if any(c.get("state") == "不可评估" for c in clauses):
        return "不可评估"
    failed = [c for c in clauses if c.get("state") == "不通过"]
    if any(UNDERSIZED_MARK in (c.get("reason") or "") for c in failed):
        return "欠功效"
    if failed:
        return "真不通过"
    return "成立"


def nature_rows(source: Source) -> list:
    """`backtest.json` → `[(判据, 三态, 四类之一)]`。"""
    if source.missing or not source.payload:
        return []
    return [(result.get("id") or "?", result.get("verdict") or NOT_GENERATED, classify_nature(result))
            for result in (source.payload.get("results") or [])]


# ---------------------------------------------------------------------------
# 排版
# ---------------------------------------------------------------------------
def section_sources(sources: dict) -> str:
    """来源登记表：路径 + SHA-256 + 复现命令。**报告自报的指纹必须与磁盘一致**，用例会核对。"""
    lines = ["", "<div class='card'><h3>数据来源（报告里的每一个数字都来自下列产物）</h3>",
             "<table><tr><th>块</th><th>产物（相对产物根）</th><th>SHA-256</th><th>复现命令</th></tr>"]
    for spec in ARTIFACTS:
        src = sources[spec["key"]]
        state = f'<span class="bad">{NOT_GENERATED}</span>' if src.missing else '<span class="muted">已生成</span>'
        digest = src.sha256[:16] + "…" if src.sha256 else "—"
        lines.append(f"<tr><td>{esc(spec['title'])}<br>{state}</td><td><code>{esc(src.shown_path)}</code></td>"
                     f"<td><code>{esc(digest)}</code></td><td><code>{esc(src.repro)}</code></td></tr>")
    lines += ["</table>", "<p class='muted'>产物根由命令行 <code>--svg-data</code> 指定；"
              "<code>data/</code> 在 .gitignore 里 ⇒ <b>入库的是代码不是数据</b>，"
              "换机器后按上表命令重跑即可复现同一份报告。</p></div>"]
    return "".join(lines)


def _block(title: str, source: Source, inner) -> str:
    """一个数据块：标题 + 产物缺失时显式渲染「未生成」，绝不渲染占位数字。"""
    if source.missing:
        body = charts.panel(title, [], missing_reason=source.missing_reason())
    else:
        body = inner(source)
    return f"<div class='card'><h2>{esc(title)}</h2>{body}</div>"


def block_scenarios(source: Source) -> str:
    """① 七场景对照。`S0`~`S6` 按场景 id 排序；每个臂一行，附分子/分母。"""
    rows = sorted(arm_rows(source), key=lambda row: (row[0], row[1]))
    chart = (charts.bar_chart([(f"{scenario}·{arm}", ratio, note) for scenario, arm, ratio, note in rows])
             if rows else charts.panel("七场景对照", [], missing_reason="产物里没有任何臂结果"))
    lines = [chart, coverage_note(source),
             "<table><tr><th>场景</th><th>臂</th><th>走秤率</th><th>分子/分母</th></tr>"]
    for scenario, arm, ratio, note in rows:
        lines.append(f"<tr><td><code>{esc(scenario)}</code></td><td>{esc(arm)}</td>"
                     f"<td>{esc(charts.pct(ratio))}</td><td><code>{esc(note)}</code></td></tr>")
    lines.append("</table>")
    return chart + "".join(lines)


def block_verdicts(source: Source) -> str:
    """② 三态结论。每条一行：判据 + 三态徽标 + 理由 + 所用档位。"""
    rows = backtest_rows(source)
    out = []
    for criterion_id, verdict, reason, profile in rows:
        out.append("<div class='row'>" + charts.badge(criterion_id, "ink")
                   + charts.badge(verdict, VERDICT_TONE.get(verdict, "ink"))
                   + f"<span class='muted'>{esc(profile)}</span></div>"
                   + f"<div class='muted'>{esc(reason)}</div>")
    return "".join(out)


def block_nature(source: Source) -> str:
    """③ 四类性质分解。**分类是算出来的**（见 `classify_nature`），本函数只负责排版。"""
    assigned = nature_rows(source)
    out = []
    for name, definition in NATURE_BINS:
        members = [row for row in assigned if row[2] == name]
        out.append(charts.badge(f"{name}（{len(members)}）", NATURE_TONE.get(name, "ink")))
        out.append(f"<div class='muted'>{esc(definition)}</div>")
        #: 空分箱要说清是"这一类本轮为空"，**不是"数据没生成"** —— 两者含义相反，
        #: 混用会让人以为这条判据没跑。`NATURE_EMPTY` 与 `NOT_GENERATED` 是两个词。
        out.append("<div>" + ("　".join(esc(row[0]) for row in members) or
                              f'<span class="muted">{NATURE_EMPTY}</span>') + "</div>")
        if name == "判据本身有问题" and not members:
            out.append("<div class='muted'>本轮为空的原因：`R3` 曾属这一类（`R3-c` 原判「预算单调下降」，"
                       "而预算是市场方的内生决策变量），已按父代理裁定在 `2026-10-03` 修订判据；"
                       "修订留痕见 `docs/sim-design.md` §7.1，`R3` 重跑实测见同一节第 ⑧ 条。</div>")
    table = ["<table><tr><th>判据</th><th>门禁三态</th><th>真实性质（规则算出）</th></tr>"]
    for criterion_id, verdict, nature in assigned:
        table.append(f"<tr><td><code>{esc(criterion_id)}</code></td><td>{esc(verdict)}</td>"
                     f"<td>{esc(nature)}</td></tr>")
    table.append("</table>")
    table.append("<p class='warn'>「四类性质」不是结论的分级，而是"
                 "<b>引用时必须区分的四种失败原因</b>：把六条『不成立』整体说成『设计被证伪』是灾难性误读。"
                 "分类规则与优先级写在 <code>sim/observe/svg_report.py::classify_nature</code>，"
                 "并有灵敏度负例（改子句状态或原因文本，分类必须随之改变）。</p>")
    return "".join(out) + "".join(table)


def _series_charts(source: Source) -> str:
    """子句里的**序列证据**（单调性 / 加速上升这类**形状**判据）—— 条形图看不出的就是形状。
    只画真有序列的子句（"该子句本来就不判序列"与"画不出来"是两回事）；`R3-c` 的闲置率序列
    第 1 期触顶 1.0、其后七期持平 ⇒「没有加速」画出来比读数字直观。"""
    if source.missing or not source.payload:
        return ""
    out = []
    for result in source.payload.get("results") or []:
        for clause in result.get("clauses") or []:
            observed = clause.get("observed")
            series = observed.get("series") if isinstance(observed, dict) else observed
            if not isinstance(series, list):
                continue
            values = [v for v in series if isinstance(v, (int, float))]
            label = f"{result.get('id')}·{clause.get('clause')}：{clause.get('text', '')[:24]}"
            out.append(charts.line_chart(values, title=f"{label}（{clause.get('state')}）",
                                         width=880, height=140, y_label=clause.get("measure")))
    return "".join(out)


def block_v01(source: Source) -> str:
    """④ `V-01` 三臂走秤率对照。`V-01` 的结论在压力档下测的 ⇒ 这里印的是**本次所读产物**的实测值与档位。"""
    arms = s6_arms(source)
    chart = (charts.bar_chart([(f"S6·{arm}", ratio, "") for arm, ratio, _note in arms])
             if arms else charts.panel("V-01 三臂", [],
                                       missing_reason=f"产物里没有 `{V01_SCENARIO}` 的臂结果"))
    lines = [chart, "<table><tr><th>臂</th><th>走秤率</th><th>分子/分母</th></tr>"]
    for scenario, arm, ratio, note in arm_rows(source):
        if scenario == V01_SCENARIO:
            lines.append(f"<tr><td>{esc(arm)}</td><td>{esc(charts.pct(ratio))}</td>"
                         f"<td><code>{esc(note)}</code></td></tr>")
    lines += ["</table>", "<p class='warn'><b>引用前必读</b>：<code>V-01</code>（只堵不养，比不堵更糟）"
              "的原始实测在<b>高故障率压力档</b>下取得（<code>docs/sim-验证结论实况.md</code> V-01，"
              "含基线档为何测不到该机制的完整解释）。本块印的是<b>本次所读这份产物</b>的值与档位 —— "
              "若产物不是压力档产物，这里的数字<b>不能</b>当成 V-01 的结论。</p>"]
    return "".join(lines)


def render_document(sources: dict) -> str:
    """拼出整份 `report.html`（单文件、无脚本、内联 SVG）。"""
    study, backtest = sources["study"], sources["backtest"]
    return "\n".join([
        "<!DOCTYPE html>", '<html lang="zh-CN"><head><meta charset="utf-8">',
        "<title>仿真结果静态报告（T-SIM-09）</title>",
        f"<style>{CSS}</style></head><body>",
        "<h1>菜市场数字化交易与佣金系统 · 仿真结果静态报告</h1>",
        "<p class='muted'>本报告<b>零外部资源</b>：全部图形为内联 SVG，无脚本、无外部字体、"
        "无网络请求，断网可完整打开（<code>AGENTS.md</code> §3 硬要求 4 / <code>REQ-025</code> / "
        "<code>AC-013</code>）。自检：<code>tests/sim/test_svg_report_no_external.py</code> 复用 "
        "<code>tests/contract/test_no_external_assets.py::scan_static_text()</code>。</p>",
        section_sources(sources),
        _block("① 七场景对照（S0–S6 走秤率）", study, block_scenarios),
        _block("② R1–R6 三态结论", backtest, block_verdicts),
        _block("③ 四类性质分解", backtest, block_nature),
        _block("④ 序列证据：形状类子句（单调性 / 加速上升）", backtest, _series_charts),
        _block("⑤ V-01 三臂走秤率对照（S6）", study, block_v01),
        "<p class='muted'>生成命令：<code>python -m sim --svg-report --svg-data &lt;产物根&gt;</code>。</p>",
        "</body></html>", "",
    ])


def write_report(out_path: Path | str, html: str) -> dict:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8", newline="\n")
    return {"path": str(out_path), "bytes": len(html.encode("utf-8")),
            "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest()}


def build_report(*, data_root: Path | str, out_path: Path | str, overrides: dict | None = None) -> dict:
    """读产物 → 渲染 → 落盘。返回来源登记与落盘信息（供命令行如实汇报）。"""
    sources = load_sources(data_root, overrides)
    html = render_document(sources)
    written = write_report(out_path, html)
    return {"sources": sources, "written": written,
            "missing": [key for key, src in sources.items() if src.missing]}


def svg_report_command(args, base_out: Path) -> int:
    """`--svg-report` 的命令行分支（`sim/verify_cli.py` 是验证类入口，本入口是**呈现类**入口）。"""
    data_root = Path(getattr(args, "svg_data", None) or base_out)
    out_path = Path(getattr(args, "svg_out", None) or (base_out / "report.html"))
    overrides = {spec["key"]: getattr(args, spec["override"], None) for spec in ARTIFACTS}
    out = build_report(data_root=data_root, out_path=out_path,
                       overrides={key: value for key, value in overrides.items() if value})
    for key, src in out["sources"].items():
        state = f"缺失（{src.missing_reason()}）" if src.missing else f"sha256={src.sha256[:16]}…"
        print(f"[静态报告] {key} ← {src.shown_path}　{state}")
    print(f"[静态报告] {out['written']['path']}　{out['written']['bytes']} 字节"
          f"　sha256={out['written']['sha256'][:16]}…")
    if out["missing"]:
        print(f"[静态报告] ⚠️ {len(out['missing'])} 份产物缺失 ⇒ 对应数据块显示「{NOT_GENERATED}」"
              f"（**没有填假数据**）；复现命令见报告的「数据来源」表")
    absent = missing_scenarios(out["sources"]["study"])
    if absent and not out["sources"]["study"].missing:
        print(f"[静态报告] ⚠️ 该产物**未覆盖场景 {'、'.join(absent)}** ⇒ ①/④ 两块不是完整的七场景对照"
              f"（报告里也已点名，不会看起来像齐的）")
    return 0