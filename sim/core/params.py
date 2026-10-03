"""参数与**出处**的加载与校验（`docs/sim-design.md` §2.5）。

**本模块的核心不是"读 JSON"，而是把"诚实标注"变成机械约束。**

设计文档里"参数必须标来源"是一句承诺，承诺会漂移；把出处写进参数文件并由测试强制，它才成为约束
（沿用本项目 `T-036` 家族"不验证灵敏度的验证是摆设"的同一条逻辑）。

出处只有两种，**没有第三种**：

- `sourced` —— **仓库内可核出处**（`结论 N` / `D-xx`）：必须有非空 `ref`，且 ref 必须匹配上述形态。
  ⚠️ **`Q-xx` 不算 `sourced`**：`Q` 是项目**自己登记的未决项/待确认假设**，把它当"有出处"就是
  把假设写成事实。这类条目必须走 `assumed` 并写清校准思路。
- `assumed` —— **假设，待校准**：必须有非空 `calibration`；`ref` 可选（用于指回它想对标的
  真实数字或自认缺口，例如 `《报告》第九节` 明确承认"设备每年坏多少、修一次多少钱，没有公开数据"）。

校验函数写成**纯函数**（`provenance_problems`），故合成负例可直接喂给它 —— 这是灵敏度可验证的前提。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: `sourced` 允许的 ref 形态（**故意不含 `Q-\d+`**，理由见模块 docstring）
SOURCED_REF_RE = re.compile(r"^(?:结论 \d+|D-\d+)$")

#: `ref` 里**指向《调研报告》结论号**的片段。允许裸写（`结论 4`）与方括号包裹（`[源:结论 4]`）两种形态；
#: 也允许一条 ref 里指向多条结论 —— 真实写法是 `结论 4 / 5 / 6（时间线）`，
#: **后续的 `/ N` 省略了重复的「结论」二字**，所以必须由第 2 组一并吃掉，否则 `5`、`6` 会被漏检
#: （漏检的后果是"指向不存在编号"检不出来 —— 那是本函数存在的理由）。
CONCLUSION_REF_RE = re.compile(r"结论\s*(\d+)\s*((?:[/／]\s*\d+\s*)*)")

#: `Q-xx`（项目自认的待确认假设）在 `ref` 里的形态。父代理裁定：**`Q-xx` 一律 `assumed`**。
Q_REF_RE = re.compile(r"Q-\d+")

KINDS = ("sourced", "assumed")

DEFAULT_PARAMS_PATH = Path(__file__).resolve().parent.parent / "calibration" / "params.json"


class ParamsError(ValueError):
    """参数文件缺失、结构非法或出处不合规。**故意继承 `ValueError`**：它属于"配置错"，
    调用方（CLI）本就应当立刻失败退出，不存在"被业务代码顺手接住"的风险。"""


# ---------------------------------------------------------------------------
# `ref` 指向的**结论号**是否真实存在（`T-SIM-12`：出处机械对照的第一道闸门）
# ---------------------------------------------------------------------------
def ref_conclusion_numbers(ref) -> set[int]:
    """从一条 `ref` 里抽出它指向的**全部结论号**。**纯函数**：`ref` 缺字段/非串 ⇒ 空集。

    一条 `ref` 可以指向多条结论（`结论 4 / 5 / 6（时间线）`），那不是格式错误 ——
    参数的出处常常是"几条事实合起来支撑一个取值"。注意 `5`、`6` 省略了重复的「结论」二字，
    由 `CONCLUSION_REF_RE` 的第 2 组（`/ N` 续写）一并吃掉。
    """
    if not isinstance(ref, str):
        return set()
    numbers: set[int] = set()
    for match in CONCLUSION_REF_RE.finditer(ref):
        numbers.add(int(match.group(1)))
        numbers.update(int(token) for token in re.findall(r"\d+", match.group(2) or ""))
    return numbers


def conclusion_numbers(text: str) -> set[int]:
    """从《调研报告》原文里抽出**全部结论号**。**纯函数**：这是"结论号集合"的唯一算法。

    识别形态为行首的粗体结论标题（`**结论 7：…**`）。刻意**不用**全文任意位置的 `结论 \\d+`：
    正文里也有"结论 13、14"这样的交叉引用，把它们算进来会让"编号集合"虚高，
    于是"ref 指向了一个不存在的结论"这类错误反而检不出来。
    """
    return {int(number) for number in re.findall(r"^\*\*结论\s*(\d+)\s*[：:]", text, flags=re.MULTILINE)}


def conclusion_blocks(text: str) -> dict[int, str]:
    """`{结论号: 该结论的正文}`。**纯函数** —— 供"参数取值 vs 报告原文"的人工对照做成机械断言。

    切法：以行首粗体结论标题为起点，到下一条结论标题（或文末）为止。
    交叉引用落在别的结论块里，故 `conclusion_blocks(…)[13]` 拿到的是结论 13 **自己的**段落。
    """
    heads = list(re.finditer(r"^\*\*结论\s*(\d+)\s*[：:]", text, flags=re.MULTILINE))
    out: dict[int, str] = {}
    for index, match in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        out[int(match.group(1))] = text[match.start():end]
    return out


def ref_target_problems(entry_id: str, ref, known_conclusions: set[int]) -> list[str]:
    """`ref` 指向的每个结论号都必须**真实存在于**报告里（空 = 合规）。**纯函数**。

    这道闸门堵的是这类事故：`ref` 写 `结论 27`，而报告只到 26 —— 参数**看起来**有出处，
    实际那个编号根本不存在。`provenance_problems` 只看形态，看不出编号是否存在；
    两道闸门分工不同，故并存（**不是重复实现**）。
    """
    problems = []
    for number in sorted(ref_conclusion_numbers(ref)):
        if number not in known_conclusions:
            problems.append(
                f"参数 {entry_id} 的 `ref` 指向结论 {number}，但报告里没有这一条"
                f"（现有结论号：{sorted(known_conclusions)}）—— 这是**指向不存在编号**，不是格式问题"
            )
    return problems


def q_ref_problems(entry_id: str, kind: str, ref) -> list[str]:
    """`ref` 里出现 `Q-xx` 时，`kind` **必须**是 `assumed`。**纯函数**（父代理 2026-10-02 裁定）。

    `provenance_problems` 只在 `kind == "sourced"` 时用 `SOURCED_REF_RE` 卡掉 `Q-xx`；
    但 `assumed` 条目也允许写 `ref`，若那里塞一个 `Q-xx` 再被下游读成"有出处"，
    就绕过了那道闸门。故 `Q-xx` 的约束单独成条，覆盖两种 `kind`。
    """
    if not isinstance(ref, str) or not Q_REF_RE.search(ref):
        return []
    if kind == "assumed":
        return []
    return [f"参数 {entry_id} 的 `ref` 含 {Q_REF_RE.search(ref).group(0)}，但 `kind` = {kind!r}；"
            "**`Q-xx` 是项目自认的待确认假设，必须归 `assumed`**"]


def provenance_problems(entry_id: str, entry: Any, where: str) -> list[str]:
    """返回该条目的**出处**问题清单（空 = 合规）。**纯函数**，供合成负例直接调用。

    只管"出处标得对不对"，**不管取值字段叫 `value` 还是 `intensity`** —— 那是结构校验的事
    （见 `load_params`）。两者混在一个函数里，会让"时段块没有 `value`"被误报成"出处不合规"。
    """
    problems: list[str] = []
    if not isinstance(entry, dict):
        return [f"{where}: 参数 {entry_id} 不是对象"]

    prov = entry.get("provenance")
    if not isinstance(prov, dict):
        problems.append(f"{where}: 参数 {entry_id} 缺少 `provenance`（每个参数都必须标出处或标假设）")
        return problems

    kind = prov.get("kind")
    if kind not in KINDS:
        problems.append(f"{where}: 参数 {entry_id} 的 `provenance.kind` = {kind!r}，只允许 {list(KINDS)}")
        return problems

    ref = prov.get("ref")
    if kind == "sourced":
        if not (isinstance(ref, str) and ref.strip()):
            problems.append(f"{where}: 参数 {entry_id} 标为 sourced 但 `ref` 为空（有出处就必须写清哪一条）")
        elif not SOURCED_REF_RE.match(ref.strip()):
            problems.append(
                f"{where}: 参数 {entry_id} 的 sourced `ref` = {ref!r} 不合规 —— "
                "只允许 `结论 N` 或 `D-xx`；**`Q-xx` 属项目自认的待确认假设，必须改标 assumed**"
            )
    else:  # assumed
        calibration = prov.get("calibration")
        if not (isinstance(calibration, str) and calibration.strip()):
            problems.append(f"{where}: 参数 {entry_id} 标为 assumed 但 `calibration` 为空（假设必须给校准思路）")
    return problems


class Params:
    """校验通过的参数集合。"""

    def __init__(self, raw: dict, source: str) -> None:
        self.raw = raw
        self.source = source

    @property
    def schema_version(self) -> int:
        return int(self.raw.get("schema_version", 0))

    def parameters(self) -> dict[str, dict]:
        return dict(self.raw.get("parameters", {}))

    def blocks(self) -> list[tuple[str, str, float]]:
        """`[(名称, 起始时刻, 强度)]`，供 `sim.core.clock.SimClock` 构造时段块。"""
        out: list[tuple[str, str, float]] = []
        for item in self.raw.get("blocks", []):
            out.append((str(item["name"]), str(item["start"]), float(item["intensity"])))
        return out

    def value(self, param_id: str) -> Any:
        entry = self.parameters().get(param_id)
        if entry is None:
            raise KeyError(f"未定义的参数：{param_id}（参数文件：{self.source}）")
        return entry.get("value", entry.get("range"))

    def kind(self, param_id: str) -> str:
        return str(self.parameters()[param_id]["provenance"]["kind"])

    def sourced_ids(self) -> list[str]:
        return sorted(k for k in self.parameters() if self.kind(k) == "sourced")

    def assumed_ids(self) -> list[str]:
        return sorted(k for k in self.parameters() if self.kind(k) == "assumed")


def with_overrides(params: Params, overrides: dict[str, Any]) -> Params:
    """按场景覆盖若干参数的**取值**（`value`），出处字段原样保留。**不修改入参**（深拷贝）。

    `T-SIM-06` 的对照实验靠它成立：`sim/scenarios/*.json` 只声明"这一臂改了哪几个参数"，
    其余一律取基线 —— 于是"两组只差一件事"是**数据结构保证**的，而不是靠人记得别改别的。
    出处字段（`provenance`）**不因覆盖而消失**：报告要能说清"这个数被谁改成了多少、它本来是什么出处"。
    """
    import copy

    raw = copy.deepcopy(params.raw)
    parameters = raw.get("parameters") or {}
    unknown = sorted(set(overrides) - set(parameters))
    if unknown:
        raise ParamsError(f"场景覆盖了未定义的参数：{unknown}（参数文件：{params.source}）")
    for key, value in overrides.items():
        parameters[key]["value"] = value
    return Params(raw, f"{params.source} (+scenario overrides)")


def load_params(path: Path | str | None = None) -> Params:
    """读入并校验参数文件；任何出处不合规都**立刻报错**（不静默放过）。"""
    target = Path(path) if path is not None else DEFAULT_PARAMS_PATH
    if not target.is_file():
        raise ParamsError(f"参数文件不存在：{target}")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ParamsError(f"参数文件不是合法 JSON：{target}（{exc}）") from exc

    where = target.name
    problems: list[str] = []
    for param_id, entry in (raw.get("parameters") or {}).items():
        if not isinstance(entry, dict) or ("value" not in entry and "range" not in entry):
            problems.append(f"{where}: 参数 {param_id} 必须给出 `value` 或 `range`")
        problems.extend(provenance_problems(param_id, entry, where))
    for index, block in enumerate(raw.get("blocks") or []):
        label = f"blocks[{index}]({block.get('name') if isinstance(block, dict) else '?'})"
        if not isinstance(block, dict) or "intensity" not in block or "start" not in block:
            problems.append(f"{where}: 时段块 {label} 必须给出 `start` 与 `intensity`")
        problems.extend(provenance_problems(label, block, where))
    if problems:
        raise ParamsError("参数文件不合规（每个参数必须标 `sourced` 或 `assumed`）：\n  " + "\n  ".join(problems))
    return Params(raw, str(target))
