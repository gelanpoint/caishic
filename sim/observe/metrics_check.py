"""指标的**复算自检**（`T-SIM-06` 验收①②）：一致性、非退化（抗写死）与两条路径对账。

## 为什么需要三样东西，而不是一个"复算通过"

`Q-19` 的教训写死在这里：**退化状态下"写死"与"真查"输出相同，检查根本发现不了**。
所以本模块分三层，各自防一种假绿：

1. `cross_check_problems` —— **两条路径对账**：同一件事由两族**独立落盘**的事件分别汇总
   （逐笔 `txn` 明细 vs 逐块 `block_summary`／逐台 `device_day` vs 逐日 `device_state`），
   两者不一致即判红。这一层能发现"某一族事件被写死/漏记"。
2. `mutation_sensitivity_problems` —— **非退化自检**：把某指标的**来源事件整族删掉**再算一遍，
   值必须变；不变 = 该指标对事实不敏感（**疑似写死**）。它同时给出"哪些指标在当前数据下退化"
   （分子/分母无信号），**退化必须被如实报出来**，不能因为"检查没红"就说检查有效。
3. `verify_against_declared` —— **产物与事件流对账**：`metrics.json` 里记的值必须能从
   `events.jsonl` 重新算出。它的能力边界要说清：产出方与复算方若共用同一个函数，这一层是恒真的，
   故它只防"写盘与事件不同步/产物被手改"，**不防写死**（防写死是第 2 层的事）。
"""

from __future__ import annotations

import json

from .metric_specs import SPECS
from .metrics import compute_all, FUNCTIONS
from .metric_util import of


def _canonical(row: dict) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)


def _has_signal(row: dict) -> bool:
    """当前数据下这个指标有没有信号（分子存在且分母非 0）—— 无信号的指标**不能**用扰动法检验。"""
    if not row.get("available"):
        return False
    if row.get("numerator") is None:
        return False
    return row.get("denominator") not in (None, 0)


def verify_against_declared(declared: dict, events) -> list[str]:
    """`metrics.json` 里记的值必须与"从 `events.jsonl` 重算"一致（能力边界见模块 docstring）。"""
    fresh = compute_all(events)
    problems: list[str] = []
    for spec in SPECS:
        if spec.id not in declared:
            problems.append(f"{spec.id}（{spec.name}）在产物里缺失 —— 22 个指标必须都在")
            continue
        if _canonical(declared[spec.id]) != _canonical(fresh[spec.id]):
            problems.append(f"{spec.id}（{spec.name}）产物值与事件流复算不一致")
    extra = sorted(set(declared) - {spec.id for spec in SPECS})
    if extra:
        problems.append(f"产物里出现了 §6 之外的指标：{extra}（设计只定义 22 个）")
    return problems


def _drop_sources(events, sources) -> list[dict]:
    return [event for event in events if event.get("kind") not in set(sources)]


def mutation_sensitivity_problems(events) -> dict:
    """逐指标删掉它的来源事件整族 → 值必须变；不变即判红（**抗写死**）。"""
    problems: list[str] = []
    degenerate: list[dict] = []
    checked: list[str] = []
    for spec in SPECS:
        before = FUNCTIONS[spec.id](events)
        if not _has_signal(before):
            degenerate.append({"id": spec.id, "name": spec.name, "why": "当前数据下分子/分母无信号"})
            continue
        after = FUNCTIONS[spec.id](_drop_sources(events, spec.sources))
        checked.append(spec.id)
        if _canonical(after) == _canonical(before):
            problems.append(
                f"{spec.id}（{spec.name}）在来源事件 {list(spec.sources)} 被整族删除后**值完全没变** —— "
                "该指标对事实不敏感（疑似写死成常量）"
            )
    return {"problems": problems, "checked": checked, "degenerate": degenerate}


def cross_check_problems(events) -> list[str]:
    """**两条路径对账**：同一事实由两族独立事件分别汇总，必须一致。"""
    problems: list[str] = []

    scale_txns = sum(1 for e in of(events, "txn") if e.get("channel") == "scale")
    shadow_txns = sum(1 for e in of(events, "txn") if e.get("channel") == "shadow")
    lost_txns = len(of(events, "lost_sale"))
    block_scale = sum(e["on_scale"] for e in of(events, "block_summary"))
    block_shadow = sum(e["off_scale"] for e in of(events, "block_summary"))
    block_lost = sum(e["lost"] for e in of(events, "block_summary"))
    if scale_txns != block_scale:
        problems.append(f"走秤笔数两条路径不一致：txn 明细 {scale_txns} vs block_summary {block_scale}")
    if shadow_txns != block_shadow:
        problems.append(f"私下笔数两条路径不一致：txn 明细 {shadow_txns} vs block_summary {block_shadow}")
    if lost_txns != block_lost:
        problems.append(f"未成交笔数两条路径不一致：lost_sale 明细 {lost_txns} vs block_summary {block_lost}")

    arrivals = sum(e["count"] for e in of(events, "block_arrivals"))
    declared_total = sum(e["count"] for e in of(events, "day_arrivals_total"))
    if arrivals != declared_total:
        problems.append(f"到达数两条路径不一致：block_arrivals 之和 {arrivals} vs day_arrivals_total {declared_total}")
    if arrivals != scale_txns + shadow_txns + lost_txns:
        problems.append(
            f"客流守恒被破坏：到达 {arrivals} ≠ 走秤 {scale_txns} + 私下 {shadow_txns} + 未成交 {lost_txns}"
        )

    for event in of(events, "device_state"):
        total = event["working"] + event["waiting"] + event["repairing"]
        if total != event["total"]:
            problems.append(f"{event['business_date']} 设备三态之和 {total} ≠ 机队规模 {event['total']}")
    per_day_devices: dict[str, int] = {}
    for event in of(events, "device_day"):
        per_day_devices[event["business_date"]] = per_day_devices.get(event["business_date"], 0) + 1
    for event in of(events, "device_state"):
        counted = per_day_devices.get(event["business_date"], 0)
        if counted != event["total"]:
            problems.append(f"{event['business_date']} device_day 台数 {counted} ≠ 机队规模 {event['total']}")

    price_lists = of(events, "price_list")
    complete = sum(1 for e in price_lists if e.get("complete"))
    if complete > len(price_lists):
        problems.append("价目表完整数超过在营摊位-日数（不可能）")
    return problems
