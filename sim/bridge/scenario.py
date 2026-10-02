"""场景装载与校验（`T-SIM-06`；`docs/sim-design.md` §5）。

**"对照实验只差一件事"必须是机械保证，不能靠人记得别改别的**：
每个场景声明 `baseline_overrides`（所有臂共用的基线配置）与 `varying_keys`/`varying_flags`
（本场景的对照变量），每一臂只允许给出这些键 —— 多一个键就是偷改别的参数，少一个键就是对照没写全。
校验函数 `scenario_problems` 是**纯函数**，故合成负例可以直接喂它。
"""

from __future__ import annotations

import json
from pathlib import Path

#: 场景目录（`docs/sim-design.md` §2.2）
SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"


# ---------------------------------------------------------------------------
class ScenarioError(ValueError):
    """场景文件缺失、结构非法，或**某一臂动了本场景没声明的对照变量**。"""


def load_scenario(path: Path | str) -> dict:
    """读入一个场景 json（`sim/scenarios/S*.json`）。"""
    target = Path(path)
    if not target.is_file():
        raise ScenarioError(f"场景文件不存在：{target}")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - 结构错直接报错
        raise ScenarioError(f"场景文件不是合法 JSON：{target}（{exc}）") from exc


def scenario_problems(scenario: dict) -> list[str]:
    """返回该场景的**结构**问题清单（空 = 合规）。**纯函数**，合成负例可直接喂它。

    这是"**对照实验只差一件事**"的机械形式（设计 §5「其余全部参数取 §9 的中位数」）：

    * `baseline_overrides`（基线配置，**每一臂都一样**）与 `varying_keys`/`varying_flags` 必须不相交；
    * 每一臂的 `overrides` 的键必须**恰好**等于 `varying_keys`（不许多、不许少）——
      少一个键就说明该臂没把对照变量写全，多一个键就是在偷偷改别的东西；
    * 不许有两臂完全重复（重复臂会让"效应量"变成 0 却看起来像"不敏感"）；
    * 非 `S0` 场景必须至少声明一个对照变量。
    """
    problems: list[str] = []
    for key in ("id", "title", "what_changes", "control_note", "baseline_overrides",
                "varying_keys", "varying_flags", "arms", "basis"):
        if scenario.get(key) is None:
            problems.append(f"场景缺少字段 `{key}`")
    if problems:
        return problems

    varying = list(scenario["varying_keys"])
    flags = list(scenario["varying_flags"])
    baseline = dict(scenario["baseline_overrides"])
    if len(set(varying)) != len(varying):
        problems.append(f"`varying_keys` 有重复：{varying}")
    if scenario["id"] != "S0" and not (varying or flags):
        problems.append("非基线场景必须至少声明一个对照变量（`varying_keys` 或 `varying_flags`）")
    if not scenario["arms"]:
        problems.append("场景至少要有一臂")
    #: 每个声明的对照变量都必须在**至少一臂**里真的与基线取值不同 —— 否则那条"对照"是空的
    for key in varying:
        if baseline.get(key, object()) == object():
            continue
        if all((arm.get("overrides") or {}).get(key, baseline.get(key)) == baseline.get(key) for arm in scenario["arms"]):
            problems.append(f"对照变量 {key} 在所有臂里都等于基线取值 {baseline.get(key)!r} —— 这条对照是空的")
    for key in flags:
        if all(bool((arm.get("flags") or {}).get(key)) == bool(baseline.get(key, False))
               for arm in scenario["arms"]) and len(scenario["arms"]) > 1:
            values = {bool((arm.get("flags") or {}).get(key)) for arm in scenario["arms"]}
            if len(values) < 2:
                problems.append(f"对照开关 {key} 在所有臂里取值相同 —— 这条对照是空的")

    seen: set = set()
    for arm in scenario["arms"]:
        name = arm.get("name")
        if not name:
            problems.append("每个臂都必须有 `name`")
            continue
        keys = set(arm.get("overrides", {}))
        if keys != set(varying):
            problems.append(
                f"臂 {name!r} 的 overrides 键 = {sorted(keys)}，与声明的 `varying_keys` = {sorted(varying)} 不一致 —— "
                "对照臂之间**只允许差这些键**（多一个键 = 偷改别的参数，少一个键 = 对照没写全）"
            )
        arm_flags = set(arm.get("flags", {}))
        if arm_flags != set(flags):
            problems.append(f"臂 {name!r} 的 flags 键 = {sorted(arm_flags)}，与声明的 `varying_flags` = {sorted(flags)} 不一致")
        signature = json.dumps([sorted((arm.get("overrides") or {}).items()), sorted((arm.get("flags") or {}).items())],
                               sort_keys=True, ensure_ascii=False)
        if signature in seen:
            problems.append(f"臂 {name!r} 与另一臂的取值完全相同（重复臂 = 假对照）")
        seen.add(signature)

    reference = scenario["arms"][0] if scenario["arms"] else None
    if reference is not None and len(scenario["arms"]) > 1:
        different = any(
            (arm.get("overrides") or {}) != (reference.get("overrides") or {})
            or (arm.get("flags") or {}) != (reference.get("flags") or {})
            for arm in scenario["arms"][1:]
        )
        if not different:
            problems.append("所有臂的取值都一样 —— 这不是对照实验")
    return problems


def merged_overrides(scenario: dict, arm: dict) -> dict:
    """某一臂的**最终参数覆盖** = 场景基线配置 + 该臂的对照变量（后写的覆盖前者）。"""
    merged = dict(scenario.get("baseline_overrides") or {})
    merged.update(arm.get("overrides") or {})
    return merged


def arm_flags(scenario: dict, arm: dict) -> dict:
    """某一臂的**非参数开关**（目前只有 `self_funded` = 商户自费购秤）。"""
    flags = dict(arm.get("flags") or {})
    return {"self_funded": bool(flags.get("self_funded", False))}


def scenario_param_problems(scenario: dict, params) -> list[str]:
    """场景里声明的每个参数键都必须在参数文件里存在（否则跑起来才炸，太晚）。"""
    known = set(params.parameters())
    declared = set(scenario.get("baseline_overrides") or {}) | set(scenario.get("varying_keys") or [])
    unknown = sorted(declared - known)
    return [f"场景 {scenario.get('id')} 引用了参数文件里没有的参数：{unknown}"] if unknown else []


def scenario_files(scenarios_dir: Path | str = SCENARIOS_DIR) -> list[Path]:
    return sorted(Path(scenarios_dir).glob("S*.json"))


# ---------------------------------------------------------------------------
