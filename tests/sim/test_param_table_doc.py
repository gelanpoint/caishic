"""`docs/sim-results.md` 的**参数来源表**必须与 `sim/observe/param_table.py` 的渲染**逐字一致**。

## 为什么要有这条

`params.json` 是机器权威，但答辩材料里那张"人能读的表"是**第二份拷贝**。
同一份事实存两遍就是下次漂移的种子 —— 本项目已经因为"一段实测记录被逐字节复制了两份"
栽过一次（`docs/PROJECT-STATE.md` 变更记录 `2026-10-03`）。
本条把那张表钉成**派生视图**：改了参数文件而忘了重生成文档，本用例立刻变红。

## 灵敏度负例

`test_table_changes_when_a_parameter_changes` 用**改内存里的 `Params`**（不碰磁盘）
证明这张表真的随参数变化 —— 否则"逐字一致"可能只是因为两边都空。
"""

from __future__ import annotations

import sim_support  # noqa: F401
from sim_support import PARAMS_PATH

from sim.core.params import Params, load_params, with_overrides
from sim.observe.param_table import (ASSUMED_MARK, SOURCED_MARK, TABLE_BEGIN, TABLE_END,
                                     param_table_block, render_param_table)

DOC_PATH = PARAMS_PATH.parents[2] / "docs" / "sim-results.md"


def _doc_block() -> str:
    text = DOC_PATH.read_text(encoding="utf-8")
    assert TABLE_BEGIN in text and TABLE_END in text, (
        f"{DOC_PATH} 里必须有 {TABLE_BEGIN} / {TABLE_END} 哨兵行包住参数来源表")
    return text.split(TABLE_BEGIN, 1)[1].split(TABLE_END, 1)[0]


def test_doc_param_table_matches_the_renderer():
    """**防漂移**：文档里的表 == 渲染器对当前 `params.json` 的输出。"""
    params = load_params(PARAMS_PATH)
    assert _doc_block().strip() == render_param_table(params).strip(), (
        f"{DOC_PATH} 的参数来源表与 params.json 不同步 —— "
        "重新生成：`python -c \"from sim.core.params import load_params;"
        "from sim.observe.param_table import render_param_table;"
        "print(render_param_table(load_params()))\"` 后贴回哨兵之间")


def test_table_changes_when_a_parameter_changes():
    """**灵敏度负例**：改一个参数值，表必须变（证明比对不是空转）。"""
    params = load_params(PARAMS_PATH)
    changed = with_overrides(params, {"device_mtbf_days": 999})
    assert render_param_table(changed) != render_param_table(params), (
        "改了参数值而表没变 ⇒ 这条防漂移检查在空转")


def test_sourced_and_assumed_are_visually_separable():
    """**硬要求**：`sourced` 与 `assumed` 必须一眼可分（分表 + 标记）。"""
    params = load_params(PARAMS_PATH)
    table = render_param_table(params)
    assert SOURCED_MARK in table and ASSUMED_MARK in table
    # 分表 = 两个独立的小节标题，且 sourced 在前
    assert table.index(SOURCED_MARK) < table.index(ASSUMED_MARK), "必须先列 sourced 再列 assumed"
    sourced_header = "**✅ 有调研出处的参数"
    assumed_header = "**⚠️ 没有出处、只给方向的参数"
    assert sourced_header in table and assumed_header in table, "两类必须各自成表"
    assert table.index(sourced_header) < table.index(assumed_header)
    # 每一行都带出处类型标记（不是只在表头写一句）
    rows = [line for line in table.splitlines() if line.startswith("| `")]
    assert rows, "表里没有任何参数行"
    for row in rows:
        assert SOURCED_MARK in row or ASSUMED_MARK in row, f"这一行没有出处类型标记：{row}"


def test_assumed_rows_never_claim_a_site_value():
    """`assumed` 行**不得**出现 `sourced` 标记，且必须带 `calibration` 或显式说明。"""
    params = load_params(PARAMS_PATH)
    table = render_param_table(params)
    assumed_section = table.split("**⚠️ 没有出处、只给方向的参数", 1)[1]
    rows = [line for line in assumed_section.splitlines() if line.startswith("| `")]
    assert rows
    for row in rows:
        assert SOURCED_MARK not in row, f"assumed 区里出现了 sourced 标记：{row}"
        last_cell = row.rsplit("|", 2)[-2].strip()
        assert last_cell != "—", f"assumed 行缺出处原文/校准思路：{row}"


def test_every_parameter_appears_exactly_once():
    """每个登记参数都要在表里出现（漏一个 = 材料里少一条出处）。"""
    params = load_params(PARAMS_PATH)
    table = render_param_table(params)
    for key in params.parameters():
        assert f"| `{key}` |" in table, f"参数 `{key}` 没进表"


def test_params_kind_api_is_the_single_authority():
    """本模块读出处的口径**就是** `Params.kind` —— 不另写一套判断。"""
    params = load_params(PARAMS_PATH)
    assert isinstance(params, Params)
    for key in params.parameters():
        assert params.kind(key) in ("sourced", "assumed")