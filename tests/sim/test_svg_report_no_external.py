"""`T-SIM-09` 验收①②③：纯静态报告**零外部资源** + 缺产物如实显示「未生成」。

对应 `docs/sim-design.md` §8 `T-SIM-09` 的三条验收方式：

| 判据 | 用例 |
| --- | --- |
| ① `scan_static_text()` 扫生成的 `report.html` → **0 命中** | `test_generated_report_has_zero_external_references` |
| ② **合成负例必红**（协议外链 + 包管理器目录引用） | `test_synthetic_external_references_make_the_same_check_go_red` |
| ③ 断网可看 | `test_report_is_fully_self_contained`（连 `src`/`href` 属性都不许有） |
| 缺产物显式「未生成」**且不填假数据** | `test_missing_artifacts_render_not_generated_without_fake_numbers` |
| 反向断言：注入构造产物后必须显示真数据 | `test_synthetic_artifacts_render_real_numbers`（防"永远显示未生成"的假绿） |
| 报告自报的数据来源与磁盘一致 | `test_reported_source_fingerprints_match_disk` |

## 为什么要**复用** `tests/contract/test_no_external_assets.py::scan_static_text()`

外链判据已经是本项目的一条**机器判据**（`REQ-025` / `AC-013`）。同一件事再写一套正则，
就是下次漂移的种子 —— 而且两份正则里较宽松的那份会决定 CI 的颜色。故本文件**只 import，不复制**。

## 「验证要能失败」

本文件自己就是一条检查，所以它**必须自带负例**：往**同一份报告**里塞一个协议外链与一个包管理器
目录引用，用**同一个函数**扫，必须判红；再把这两段去掉，必须恢复绿。
**先跑正例绿、再跑负例红** —— 顺序反了，负例证明不了任何东西。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

from sim_support import REPO_ROOT  # noqa: F401  （把仓库根放进 sys.path，`import sim` 才可用）

#: `scan_static_text` 住在契约测试目录里；该目录不是包，故显式加进 `sys.path`。
#: ⚠️ 不能依赖"契约用例先被收集所以路径已就位" —— 那是**收集顺序**，pytest 换了 rootdir 就崩。
CONTRACT_DIR = REPO_ROOT / "tests" / "contract"
if str(CONTRACT_DIR) not in sys.path:
    sys.path.insert(0, str(CONTRACT_DIR))

from test_no_external_assets import scan_static_text  # noqa: E402  （必须在上一步之后导入）

from sim.observe import svg_charts as charts  # noqa: E402
from sim.observe import svg_report  # noqa: E402

REPORT_NAME = "report.html"


# ---------------------------------------------------------------------------
# 构造产物（**合成数据，不来自 data/**：data/ 在 .gitignore 里，用例不能依赖它存在）
# ---------------------------------------------------------------------------
def _arm(scenario: str, arm: str, ratio: float, num: int, den: int) -> dict:
    return {"scenario": scenario, "arm": arm, "days": 90, "replications": 2,
            "metrics": {"M-04": {"ratio": ratio, "numerator": num, "denominator": den}}}


def _study_payload() -> dict:
    """七场景各两臂（`S6` 三臂，`V-01` 用的就是它）。数值刻意互不相同，便于肉眼核对渲染。"""
    rows = []
    for index in range(7):
        scenario = f"S{index}"
        arms = ["①第一臂", "②第二臂"] if scenario != "S6" else ["①堵死", "②不堵", "③只看装机量"]
        for position, arm in enumerate(arms):
            rows.append(_arm(scenario, arm, round(0.5 - index * 0.05 + position * 0.02, 4),
                             1000 + index * 10 + position, 2000))
    return {"results": rows}


def _backtest_payload() -> dict:
    """六条判据 + 各自的三态、子句状态与理由。**四类性质全占一格**，好让分类规则被真的跑到。"""
    def clause(state: str, reason: str) -> dict:
        return {"clause": "X-1", "state": state, "reason": reason}

    shapes = [
        ("R1", "不成立", [clause("不通过", "两侧**完全相同**（A=B=0.19）⇒ 测不出差异")], ""),
        ("R2", "不成立", [clause("不通过", "两侧**完全相同**（A=B=0.19）⇒ 测不出差异")], ""),
        ("R3", "不成立", [clause("不可评估", "绝对阈值依赖在无出处参数上"), clause("通过", None)], ""),
        ("R4", "不成立", [clause("通过", None), clause("不通过", "末期均值 0.335 未达高位阈值 0.5")], ""),
        ("R5", "不成立", [clause("不可评估", "绝对阈值依赖在无出处参数上")], ""),
        ("R6", "成立", [clause("通过", None)], ""),
    ]
    #: 给 `R4-c` 挂一条**形状类**序列（真实产物里"单调性 / 收敛"类子句的 `observed` 就是序列），
    #: 用来跑通"序列证据"那一块。R4 的分类不受影响（已有"通过"子句 + 不含"测不出差异"的不通过子句）。
    shapes[3][2][1]["observed"] = [0.085, 0.140, 0.205, 0.255, 0.290, 0.315, 0.330, 0.335]
    return {"results": [{"id": cid, "verdict": verdict, "reason": f"{cid} 的理由", "criterion_issue": issue,
                         "profile": {"label": "L1 · 360 营业日", "days": 360},
                         "clauses": clauses, "robustness": {"flips": [], "flipped_keys": []}}
                        for cid, verdict, clauses, issue in shapes]}


def _write_artifacts(root: Path, *, study: bool = True, backtest: bool = True) -> Path:
    """在 `root` 下铺出产物目录；`study=False` / `backtest=False` 即"该产物未生成"。"""
    if study:
        target = root / "study" / "scenarios.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_study_payload(), ensure_ascii=False), encoding="utf-8")
    if backtest:
        target = root / "verify" / "backtest" / "backtest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_backtest_payload(), ensure_ascii=False), encoding="utf-8")
    return root


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    """**产物齐全**的数据根。"""
    return _write_artifacts(tmp_path / "data")


@pytest.fixture()
def report_path(data_root: Path, tmp_path: Path) -> Path:
    """跑一次真实入口 `build_report`，返回落盘路径。"""
    out = svg_report.build_report(data_root=data_root, out_path=tmp_path / REPORT_NAME)
    assert out["missing"] == [], f"产物齐全却报缺失：{out['missing']}"
    return Path(out["written"]["path"])


def test_explicit_paths_override_the_product_root(data_root: Path, tmp_path: Path):
    """**两条产线可以各指各的**：回测与场景研究本来就落在不同根下（见 `ARTIFACTS` 的两条 `repro`）。
    反向断言：不覆盖时按缺省相对路径找 ⇒ 同一份产物读不到（否则上面的绿可能只是"到处都能找到"）。"""
    elsewhere = _write_artifacts(tmp_path / "elsewhere")
    out = svg_report.build_report(data_root=tmp_path / "void", out_path=tmp_path / REPORT_NAME,
                                  overrides={"study": elsewhere / "study" / "scenarios.json",
                                             "backtest": elsewhere / "verify" / "backtest" / "backtest.json"})
    assert out["missing"] == []
    html = Path(out["written"]["path"]).read_text(encoding="utf-8")
    assert "S0" in html and "R6" in html, "覆盖路径后没读到产物"
    assert svg_report.load_sources(tmp_path / "void")["study"].missing is True
    print("[T-SIM-09] 逐个覆盖产物路径生效；缺省路径在同一份产物上确实读不到（反向断言）")


# ---------------------------------------------------------------------------
# ① 正例：0 命中
# ---------------------------------------------------------------------------
def test_generated_report_has_zero_external_references(report_path: Path):
    """验收①：复用契约侧的扫描器扫本报告 → **0 命中**。"""
    hits = scan_static_text(report_path.read_text(encoding="utf-8"), "report.html")
    assert hits == [], "静态报告出现外部依赖（断网会直接坏页面）：\n" + "\n".join(hits)
    print(f"[T-SIM-09] 验收①：{REPORT_NAME} 外部依赖扫描 0 命中"
          f"（{report_path.stat().st_size} 字节）")


# ---------------------------------------------------------------------------
# ② 合成负例：同一检查必须判红（**先正例绿、再负例红**，否则负例无效）
# ---------------------------------------------------------------------------
#: 两个故意注入的外部依赖：一个协议外链、一个包管理器产物目录（验收②点名的两种形态）
INJECTED = (
    '<script src="https://cdn.example.com/chart.min.js"></script>',
    '<script src="node_modules/echarts/dist/echarts.js"></script>',
)


def test_synthetic_external_references_make_the_same_check_go_red(report_path: Path):
    """验收②：**同一个 `scan_static_text`** 对注入的两种外部依赖必须判红（两条都要，各自命中各自的规则）。"""
    clean = report_path.read_text(encoding="utf-8")
    assert scan_static_text(clean, "report.html") == [], "前提不成立：干净报告已经不干净 ⇒ 负例无效"

    for index, injected in enumerate(INJECTED, start=1):
        tainted = clean.replace("</body>", f"{injected}</body>")
        assert tainted != clean, "注入失败（`</body>` 没找到）⇒ 负例无效"
        hits = scan_static_text(tainted, f"负例{index}")
        assert hits, f"注入的外部依赖未被判红，检查存在盲区：{injected!r}"
        expected = "外链" if index == 1 else "构建产物"
        assert any(expected in hit for hit in hits), f"判红了但提示不对（期望含「{expected}」）：{hits}"
    #: 复原后必须恢复绿 —— 只判红不判绿，那不是检查
    assert scan_static_text(clean, "report.html") == []
    print(f"[T-SIM-09] 验收②：注入协议外链与包管理器目录引用各判红一次，复原后恢复绿")


def test_svg_namespace_is_not_emitted_as_a_literal_url():
    """**实测踩出来的坑，独立钉一条**：内联 SVG 若写 `xmlns="http://www.w3.org/2000/svg"`，
    浏览器不会去请求它，但 `scan_static_text` 判的是**字面** ⇒ 报告会被判红。HTML5 内联 SVG 不需要它。"""
    body = charts.svg(10, 10, "t", "")
    assert "xmlns" not in body
    assert scan_static_text(body, "内联 svg") == []
    print("[T-SIM-09] 内联 SVG 不含 xmlns 字面外链（独立 .svg 文件才需要，本项目不产出）")


# ---------------------------------------------------------------------------
# ③ 断网可看：连 src/href 属性都不许有
# ---------------------------------------------------------------------------
def test_report_is_fully_self_contained(report_path: Path):
    """验收③：报告里**不得出现任何 `src` / `href` 属性** —— 全内联 ⇒ 断网打开必然完整。"""
    html = report_path.read_text(encoding="utf-8")
    for token in ("src=", "href=", "<script", "<iframe", "<link", "@import", "url("):
        assert token not in html, f"报告里出现了 `{token}` ⇒ 存在需要另外取的资源，断网会缺东西"
    assert html.count("<svg") >= 4, "四块图都没渲染出来（SVG 数量异常）"
    print(f"[T-SIM-09] 验收③：{html.count('<svg')} 段内联 SVG，零 src/href/脚本/外部资源")


# ---------------------------------------------------------------------------
# 缺产物：显式「未生成」，且**不填假数据**
# ---------------------------------------------------------------------------
def test_missing_artifacts_render_not_generated_without_fake_numbers(tmp_path: Path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    out = svg_report.build_report(data_root=empty, out_path=tmp_path / REPORT_NAME)
    assert sorted(out["missing"]) == ["backtest", "study"]
    html = Path(out["written"]["path"]).read_text(encoding="utf-8")
    assert html.count(charts.NOT_GENERATED) >= 4, "四块都该显式写『未生成』"
    assert "未找到产物" in html and "复现命令" in html, "缺产物时必须给出复现命令"
    assert scan_static_text(html, REPORT_NAME) == [], "缺产物这份也必须是零外部资源"
    print(f"[T-SIM-09] 缺产物 ⇒ 显式『{charts.NOT_GENERATED}』+ 复现命令，零外部资源")


def test_broken_artifact_is_reported_as_not_generated_not_as_zero(tmp_path: Path):
    """产物存在但**读不出**（截断/乱码）也走「未生成」，**不许**当 0 处理 —— 那是"看起来齐"的假数据。"""
    root = tmp_path / "broken"
    target = root / "study" / "scenarios.json"
    target.parent.mkdir(parents=True)
    target.write_text("{ not json", encoding="utf-8")
    sources = svg_report.load_sources(root)
    assert sources["study"].missing is True
    assert svg_report.arm_rows(sources["study"]) == []
    assert charts.pct(None) == charts.NOT_GENERATED
    print("[T-SIM-09] 坏产物读不出 ⇒ 判『未生成』而不是 0")


def test_synthetic_artifacts_render_real_numbers(data_root: Path, report_path: Path):
    """**反向断言**（防"永远显示未生成"的假绿）：产物齐全时，真实数字必须出现在报告里。"""
    html = report_path.read_text(encoding="utf-8")
    sources = svg_report.load_sources(data_root)
    rows = svg_report.arm_rows(sources["study"])
    assert len(rows) == 15, f"合成产物应有 15 个臂（S0~S5 各 2 = 12 + S6 三臂），实得 {len(rows)}"
    for scenario, arm_name, ratio, note in rows:
        assert scenario in html and arm_name in html
        assert charts.pct(ratio) in html, f"走秤率 {charts.pct(ratio)} 没出现在报告里 ⇒ 数字不是从产物来的"
        assert note in html, f"分子/分母 `{note}` 没出现 ⇒ 违反『每个数字带分子分母』（AC-005 精神）"
    assert "S6" in html and "③只看装机量" in html, "`V-01` 的三臂缺一不可"
    for criterion_id, verdict, _reason, _profile in svg_report.backtest_rows(sources["backtest"]):
        assert criterion_id in html and verdict in html
    #: 已生成的数据块**不许**出现「未生成」（否则就是另一种假绿）
    block = html.split("① 七场景对照")[1].split("② R1–R6")[0]
    assert charts.NOT_GENERATED not in block, "产物齐全的数据块里出现了『未生成』"
    print(f"[T-SIM-09] 反向断言：{len(rows)} 个臂 + 6 条判据的数字逐位来自产物")


# ---------------------------------------------------------------------------
# 数据来源可核对 + 四类性质的分类规则可被负例打翻
# ---------------------------------------------------------------------------
def test_partial_artifact_says_which_scenarios_are_missing(tmp_path: Path):
    """**产物不全必须自己说**：只画产物里有的场景，图看起来仍然是齐的 ⇒ 报告要点名缺哪几个。

    这条防的是一类很难发现的假齐：跑了个 `--scenario S0,S6` 的缩减档，报告照样四块漂亮，
    读者却以为那是七场景对照。
    """
    root = tmp_path / "partial"
    target = root / "study" / "scenarios.json"
    target.parent.mkdir(parents=True)
    payload = {"results": [_arm("S0", "基线", 0.9, 900, 1000), _arm("S6", "①堵死", 0.5, 500, 1000)]}
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    sources = svg_report.load_sources(root)
    absent = svg_report.missing_scenarios(sources["study"])
    assert absent == ["S1", "S2", "S3", "S4", "S5"]
    html = svg_report.build_report(data_root=root, out_path=tmp_path / REPORT_NAME)["written"]
    text = Path(html["path"]).read_text(encoding="utf-8")
    assert "本次产物未覆盖" in text and "S1" in text, "产物不全时报告没有点名缺失场景"
    print(f"[T-SIM-09] 产物不全时点名缺失：{'、'.join(absent)}")


def test_complete_artifact_has_no_missing_scenario_warning(data_root: Path, report_path: Path):
    """**反向断言**（防"永远显示未生成"的假绿）：七场景齐全时不得出现缺失告警。"""
    sources = svg_report.load_sources(data_root)
    assert svg_report.missing_scenarios(sources["study"]) == []
    assert "本次产物未覆盖" not in report_path.read_text(encoding="utf-8")
    print("[T-SIM-09] 七场景齐全时无缺失告警（反向断言：不是永远报警）")


def test_every_inline_svg_is_not_clipped():
    """**图形元素必须落在自己的画布里**（截图实测抓到的一类缺陷，扫描查不出来）。

    第一版的 `bar_chart` 里条宽的局部变量与画布宽参数**同名**，循环结束后 `width` 被改成
    "最后一根条"的像素宽，再被 `svg()` 当成画布宽 ⇒ 生成 `viewBox="0 0 351.58 …"`
    而条形画在 `x=330..770` ⇒ **整张图只剩最左边 21px**，看上去"每行一个小方块"。
    纯静态扫描**判不出来**（既没有外链也没有非法属性），只有真浏览器截图能看见。

    故把"元素坐标落在 viewBox 内"变成机械判据。
    """
    rows = [("臂甲", 0.5, "1/2"), ("臂乙", 0.99, "99/100"), ("臂丙", 0.01, "1/100")]
    for svg in (charts.bar_chart(rows), charts.line_chart([0.1, 0.4, 0.9], title="序列"),
                charts.badge("不成立", "bad"), charts.panel("标题", ["一行"])):
        head = svg.split(">", 1)[0]
        view_box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', head)
        assert view_box, f"SVG 缺 viewBox：{head[:120]}"
        width, height = float(view_box.group(1)), float(view_box.group(2))
        for x, y, w, h in re.findall(r'<rect x="([-\d.]+)" y="([-\d.]+)" width="([\d.]+)" height="([\d.]+)"', svg):
            assert float(x) >= 0 and float(y) >= 0, f"元素起点在画布外：{svg[:80]}"
            assert float(x) + float(w) <= width + 0.5, (
                f"矩形右缘 {float(x) + float(w):.1f} 超出画布宽 {width:.1f} ⇒ 图形被裁掉"
            )
            assert float(y) + float(h) <= height + 0.5, (
                f"矩形下缘 {float(y) + float(h):.1f} 超出画布高 {height:.1f} ⇒ 图形被裁掉")
    print(f"[T-SIM-09] 所有内联 SVG 的矩形元素都在画布内（画布宽 {width:.0f}~{width:.0f}）")


def test_generated_report_has_no_clipped_inline_svg(report_path: Path):
    """**产物级**的同一条守卫：把生成出来的报告里**每一段**内联 SVG 都单独核对一遍。

    单张图核对（图元级）在上一条；这里核对**真实产物**的全部段落 —— 因为那条错是
    "某一类图的画布宽被顶掉"，只有把真实报告整体扫一遍才敢说"这份报告没问题"。
    """
    html = report_path.read_text(encoding="utf-8")
    checked = 0
    for svg in re.findall(r"<svg .*?</svg>", html, re.S):
        view_box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg.split(">", 1)[0])
        assert view_box, "SVG 缺 viewBox"
        width, height = float(view_box.group(1)), float(view_box.group(2))
        for x, y, w, h in re.findall(r'<rect x="([-\d.]+)" y="([-\d.]+)" width="([\d.]+)" height="([\d.]+)"', svg):
            assert float(x) + float(w) <= width + 0.5, (
                f"画布宽 {width:.1f} 装不下矩形（x={x} width={w}）⇒ 图形被裁掉")
            assert float(y) + float(h) <= height + 0.5, (
                f"画布高 {height:.1f} 装不下矩形（y={y} height={h}）⇒ 图形被裁掉")
        checked += 1
    assert checked >= 4, f"只核对了 {checked} 段 SVG，四块图都没渲染出来"
    print(f"[T-SIM-09] 真实报告 {checked} 段内联 SVG 全部无裁剪（矩形均落在各自画布内）")


def test_sequence_evidence_block_renders_series_shaped_clauses(report_path: Path):
    """「序列证据」那块只画**真有序列**的子句：条形图看不出的正是形状（单调性 / 加速）。"""
    html = report_path.read_text(encoding="utf-8")
    block = html.split("④ 序列证据")[1].split("⑤ V-01")[0]
    assert "<svg" in block, "序列证据块没有渲染任何折线"
    assert "R4·X-1" in block, "折线标题里没有标出它属于哪条判据的哪个子句"
    assert "R1·X-1" not in block and "R2·X-1" not in block, "给没有序列的子句编了一条线"
    print(f"[T-SIM-09] 序列证据块只渲染有序列的子句（{block.count('<svg')} 段折线）")


def test_reported_source_fingerprints_match_disk(data_root: Path, report_path: Path):
    """报告自报的路径与 SHA-256 必须**与磁盘上的文件一致** —— 否则"来源登记表"只是一句装饰。"""
    html = report_path.read_text(encoding="utf-8")
    sources = svg_report.load_sources(data_root)
    for spec in svg_report.ARTIFACTS:
        source = sources[spec["key"]]
        assert source.missing is False
        assert source.sha256 == hashlib.sha256(source.path.read_bytes()).hexdigest()
        assert source.shown_path in html, f"报告里没有登记产物路径 {source.shown_path}"
        assert source.sha256[:16] in html, "报告里没有登记该产物的指纹前 16 位"
    print("[T-SIM-09] 报告自报的来源路径与 SHA-256 与磁盘逐位一致")


def test_reported_paths_are_relative_to_the_working_directory(data_root: Path, report_path: Path):
    """报告里印的是**相对路径**（正斜杠），不是本机盘符绝对路径 ——
    报告是要给人看、可能要提交的产物，印一串盘符没有信息量。

    同时这条也是零外部资源检查的隐含要求：那条正则认的是**反斜杠**形态的盘符路径，
    所以这里顺带断言"报告里连盘符形态都没有"，而不是靠扫描器兜底。
    """
    html = report_path.read_text(encoding="utf-8")
    assert "D:" not in html and ":\\" not in html, "报告正文里出现了盘符绝对路径"
    cwd = Path.cwd().resolve()
    for spec in svg_report.ARTIFACTS:
        source = svg_report.load_sources(data_root)[spec["key"]]
        shown = source.shown_path
        assert "\\" not in shown, f"路径里出现了反斜杠：{shown}"
        #: 源文件在工作目录之下 ⇒ 必须印相对路径；在工作目录之外才允许绝对 posix 路径
        if cwd in source.path.resolve().parents:
            assert not shown.startswith("/"), f"在工作目录之下却印了绝对路径：{shown}"
    print(f"[T-SIM-09] 来源路径均为 posix 形态（示例：{svg_report.load_sources(data_root)['study'].shown_path}）")


@pytest.mark.parametrize(("state_reason", "expected"), [
    ({"criterion_issue": "判据把外生与内生混为一谈"}, "判据本身有问题"),
    ({"clauses": [{"state": "不可评估", "reason": "无出处"}]}, "不可评估"),
    ({"clauses": [{"state": "不通过", "reason": "两侧完全相同 ⇒ 测不出差异"}]}, "欠功效"),
    ({"clauses": [{"state": "不通过", "reason": "末期均值 0.335 未达 0.5"}]}, "真不通过"),
    ({"clauses": [{"state": "通过", "reason": None}]}, "成立"),
    ({"criterion_issue": "判据本身有问题",
      "clauses": [{"state": "不可评估", "reason": "无出处"}]}, "判据本身有问题"),  # ④ 优先于一切
])
def test_nature_classification_rule_is_sensitive(state_reason, expected):
    """**灵敏度负例**：四类性质的分类必须随子句状态/理由改变，且「判据本身有问题」优先级最高。

    最后一条是关键：判据本身错了的时候，"实测不可评估"是在**给一把坏尺子量东西** ——
    分类必须报出更根本的那一类，否则报告会替自己开脱。
    """
    assert svg_report.classify_nature(state_reason) == expected


def test_nature_bins_cover_every_classification():
    """反向断言：分类结果**永远**落在四类（或「成立」）里，不得冒出第五类（否则表里画不出来）。"""
    for shape in ({"clauses": []}, {"clauses": [{"state": "通过"}]}, {"clauses": [{"state": "未知态"}]}):
        assert svg_report.classify_nature(shape) in {name for name, _ in svg_report.NATURE_BINS} | {"成立"}
    print(f"[T-SIM-09] 四类性质共 {len(svg_report.NATURE_BINS)} 类，分类结果不越界")


def test_empty_bucket_is_not_reported_as_missing_data(data_root: Path, report_path: Path):
    """**空分箱 ≠ 产物没生成**：两个词必须分开，否则读的人会以为判据没跑。

    合成产物里 `R6` 是"成立"、没有判据落进 ④ ⇒ 该行必须写"本轮无判据落入这一类"，
    而**不得**出现 `NOT_GENERATED`（那是"产物缺失"的措辞）。
    """
    html = report_path.read_text(encoding="utf-8")
    nature_block = html.split("③ 四类性质分解")[1].split("④ V-01")[0]
    assert charts.NOT_GENERATED not in nature_block, "四类性质块里出现了『未生成』——那是产物缺失的措辞"
    assert svg_report.NATURE_EMPTY in nature_block, "空的分类没有给出明确措辞"
    assert svg_report.NATURE_EMPTY != charts.NOT_GENERATED, "两个措辞必须是不同的词"
    print("[T-SIM-09] 空分类写『本轮无判据落入这一类』，与『未生成』严格区分")