"""**参数来源表的人读可读版**（`T-SIM-10`；把 `sim-design.md` §9 落到一张可核对的表）。

## 为什么要有这一层，而不是让人直接读 `params.json`

`params.json` 的权威在**机器可校验的结构**上（`provenance.kind` + `ref`/`calibration`，
由 `tests/sim/test_param_provenance.py` 强制）。但答辩材料要回答的是
"**这条参数凭什么取这个值**"，那需要一张**按出处类型分组**的表：

* `sourced`（仓库内可点开核对的出处）与 `assumed`（**没有出处、只给方向**）
  **必须视觉可区分** —— 混在一张表里，读者会把假设读成事实，这是本项目最贵的错误；
* 每行给出 **值 / 区间 / 单位 / `ref` / 校准思路**，缺一不可（`ref` 缺失的行在
  `params.json` 里根本不允许存在，机械检查已把关，这里只是把它显示出来）；
* **绝不替 `assumed` 参数编现场数值**：这类行只显示文件里登记的 `value`/`range`
  并显式标出"只给方向"。

## 防漂移

本模块是**纯函数**（吃 `Params`，吐 Markdown），`docs/sim-results.md` 的参数来源表
由它生成，并由 `tests/sim/test_param_table_doc.py` 核对文档里的表与本模块逐字一致 ——
**同一份事实存两遍就是下次漂移的种子**（`docs/PROJECT-STATE.md` 已记过一次同类事故）。
"""

from __future__ import annotations

#: 文档里包住这张表的哨兵行。测试按这两行切片比对，因此**改了本模块就要同步重生成文档那一段**。
TABLE_BEGIN = "<!-- param-table:begin -->"
TABLE_END = "<!-- param-table:end -->"

#: 两类出处的视觉标记（**必须一眼可分**：`sourced` 才允许拿去做绝对阈值判据）
SOURCED_MARK = "✅ `sourced`"
ASSUMED_MARK = "⚠️ `assumed`"

_COLUMNS = "| 参数键 | 值 | 登记区间 | 单位 | 出处 | `ref` | 出处原文 / 校准思路 |\n| --- | --- | --- | --- | --- | --- | --- |"


def _fmt_value(value) -> str:
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, float):
        return f"`{value:g}`"
    return f"`{value}`"


def _fmt_range(entry: dict) -> str:
    rng = entry.get("range")
    if not rng:
        return "—（未登记区间）"
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        return f"`{rng}`"
    return f"`[{_fmt_value(rng[0])}, {_fmt_value(rng[1])}]`"


def _cell(text) -> str:
    """表格单元格里的文本必须**不换行**（否则整张表散架）。"""
    return str(text if text is not None else "—").replace("|", "\\|").replace("\n", " ").strip()


def _row(key: str, entry: dict, mark: str) -> str:
    prov = entry.get("provenance") or {}
    detail = prov.get("quote") or prov.get("calibration") or "—"
    return (f"| `{key}` | {_fmt_value(entry.get('value'))} | {_fmt_range(entry)} | "
            f"{_cell(entry.get('unit'))} | {mark} | {_cell(prov.get('ref'))} | {_cell(detail)} |")


def _section(title: str, note: str, keys: list[str], parameters: dict, mark: str) -> list[str]:
    lines = [f"**{title}**（{len(keys)} 项）", "", note, "", _COLUMNS]
    lines += [_row(key, parameters[key], mark) for key in keys]
    lines.append("")
    return lines


def render_param_table(params) -> str:
    """把 `Params` 渲染成**两张表**（`sourced` / `assumed` 分开）。**纯函数**。"""
    parameters = params.parameters()
    sourced = sorted(k for k, v in parameters.items() if (v.get("provenance") or {}).get("kind") == "sourced")
    assumed = sorted(k for k, v in parameters.items() if (v.get("provenance") or {}).get("kind") != "sourced")
    lines = [
        f"> 共 **{len(parameters)}** 项：`sourced` **{len(sourced)}** 项 / `assumed` **{len(assumed)}** 项。",
        "> 出处类型的判定与强制校验在 `tests/sim/test_param_provenance.py`，本表只是它的**人读视图**。",
        "",
    ]
    lines += _section(
        f"✅ 有调研出处的参数（{SOURCED_MARK}）",
        "这些参数在 `params.json` 里 `provenance.kind = \"sourced\"`，"
        "`ref` 指向《调研报告-现实情况.md》的**具体结论号**（或 `D-xx` / `P-xx` 标签），**可点开逐条核对**。"
        "**只有这一组允许用于绝对阈值判据**（`sim-design.md` §7.1 的取舍原则）。",
        sourced, parameters, SOURCED_MARK)
    lines += _section(
        f"⚠️ 没有出处、只给方向的参数（{ASSUMED_MARK}）",
        "**这一组没有现场出处**：出处的「为什么没有」写在 `calibration` 里。"
        "按本项目铁律（`AGENTS.md` RL-* 与 `sim-design.md` §7.1），"
        "**它们只允许出现在序关系 / 单调性 / 区间不重叠的判据里，不允许支撑任何绝对阈值、绝对月份或绝对比率**。"
        "`Q-xx`（项目自认的待确认假设，如 `Q-9` 的单摊日均笔数与峰段）**一律归 `assumed`**，不算查证事实。",
        assumed, parameters, ASSUMED_MARK)
    return "\n".join(lines).rstrip() + "\n"


def param_table_block(params) -> str:
    """带哨兵行的完整块（文档里就是这一段，测试按哨兵切片比对）。"""
    return f"{TABLE_BEGIN}\n\n{render_param_table(params)}{TABLE_END}\n"


def render_params_readme(params) -> str:
    """「可读版」的抬头：说明**怎么读**这张表、哪些数字不许直接引用。纯文本，供人先读再读表。"""
    parameters = params.parameters()
    sourced = sum(1 for v in parameters.values() if (v.get("provenance") or {}).get("kind") == "sourced")
    return "\n".join([
        "# 仿真参数 · 人读版（`sim/calibration/params.json` 的表格视图）",
        "",
        "## 这份表是什么",
        "",
        f"`sim/calibration/params.json` 是**机器权威**：每个参数都带 `provenance.kind`（`sourced`/`assumed`）、",
        "`ref`（出处标签）与 `calibration`（校准思路），缺任何一项都会被 `tests/sim/test_param_provenance.py` 判红。",
        "本文件是它的**人读视图**，由 `sim/observe/param_table.py` 生成，不新增任何数值 ——",
        "**数值只有一个来源，改了 `params.json` 必须重新生成本文件**。",
        "",
        "## 怎么读",
        "",
        f"1. **✅ `sourced`**（{sourced} 项）：`ref` 指向《调研报告-现实情况.md》的具体结论号，**可点开逐条核对**；",
        "   只有这一组允许支撑**绝对阈值**类判据。",
        "2. **⚠️ `assumed`**（{len(parameters) - sourced} 项）：**没有现场出处**，`calibration` 里写的是"
        "「为什么没有」与「打算怎么校准」；",
        "   按本项目铁律，它们**只允许出现在序关系 / 单调性 / 区间不重叠的判据里**。",
        "   **引用任何依赖这一组的结论时，必须把这一组的存在一并写出来。**",
        "3. **区间**（`range`）是登记的取值范围，**不是置信区间**；超出区间取值（如压力档的 `MTBF=10`）"
        "必须在结论里显式标注为**越界档位**。",
        "",
        "## 参数表",
        "",
        param_table_block(params),
    ])