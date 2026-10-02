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

KINDS = ("sourced", "assumed")

DEFAULT_PARAMS_PATH = Path(__file__).resolve().parent.parent / "calibration" / "params.json"


class ParamsError(ValueError):
    """参数文件缺失、结构非法或出处不合规。**故意继承 `ValueError`**：它属于"配置错"，
    调用方（CLI）本就应当立刻失败退出，不存在"被业务代码顺手接住"的风险。"""


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
