"""`model` 侧账目：**只由事件流复算**（`T-SIM-08`；`docs/sim-design.md` §7.3 的一部分）。

## 为什么单独一个模块

从 `sim/verify/consistency.py` 按语义搬出（`quality-gates.md` §1.2 的 400 行门禁，`Q-16`：按语义拆）。
语义边界：**"系统侧应当看到什么账"**（本模块，纯函数、无 I/O、不起服务）与
**"系统侧实际报了什么"**（`consistency.py`，要起真服务）是两件事。

## 这条边界不是随手划的

`model_ledger` 是**纯函数**：输入事件流与价目表，输出逐营业日的笔数 / 金额 / 佣金 / 三指标分子分母。
它**不读** `run_scenario` 的返回值、不读任何系统侧数字。`Q-19` 的教训是"同一套数字换个地方再写一遍"，
所以"model 侧的账"必须只有一个来源 —— 事件流。
"""

from __future__ import annotations

from pathlib import Path

from ..bridge.model_adapter import read_events, run_scenario
from ..bridge.scenario import arm_flags, load_scenario, merged_overrides, scenario_files
from ..core.params import with_overrides

#: 容差（`§7.3`：整数分与笔数要求完全相等；本项目给的显式容差是 **≤1 分 / ≤1 笔**）
TOLERANCE_CENTS = 1
TOLERANCE_TXNS = 1

#: 模型的价是「每 500g」，系统是「每公斤」——换算因子（与 `model_adapter.UNIT_GRAMS` 同源）
PER_KG = 2

#: 一致性对账的运行档。**为什么必须写明**：`live_run.py` 缺省 30 营业日 × **4 笔/日**，
#: 逐笔对账密度不够（整轮只有 120 笔）；本档把成交密度提到 **20 笔/日**（600 笔）。
#: model 侧到达数同步降到 140/日，让走秤笔数落在密度上限附近 ——
#: **密度上限是唯一被削减的维度，且削减发生在已算好的决策流上**，不改变任何金额口径。
CONSISTENCY_PROFILE = {
    "id": "C",
    "label": "C · 30 营业日 × ≤20 走秤笔/日（成交密度 = live_run 缺省的 5 倍）",
    "days": 30,
    "max_txns_per_day": 20,
    "arrivals": 140,
    "consumers": 40,
    "scenario": "S1",
    "arm_index": 0,
    "rate_bp": 200,
    "seed": 20261002,
    "start": "2026-10-01",
    "why": "见模块 docstring 第 3 节；这是 live 侧参数，不改模型参数。",
}


def half_up_div(numerator: int, denominator: int) -> int:
    """整数四舍五入除法 —— 与 `app/domain/pricing.py::half_up_div` **同一取整规则**。"""
    return (numerator + denominator // 2) // denominator


# ---------------------------------------------------------------------------
# model 侧：由事件流独立复算出"系统侧应当看到的账"
# ---------------------------------------------------------------------------
def scenario_run(params, *, profile: dict = CONSISTENCY_PROFILE, out_dir: Path | str = "data/sim/consistency/_model"):
    """跑一次 model，返回 `(事件流, 该档的最终 Params)`。"""
    scenario = None
    for path in scenario_files():
        loaded = load_scenario(path)
        if loaded["id"] == profile["scenario"]:
            scenario = loaded
            break
    if scenario is None:
        raise KeyError(f"没有场景 {profile['scenario']}")
    arm = scenario["arms"][profile["arm_index"]]
    overrides = merged_overrides(scenario, arm)
    overrides["daily_arrivals_per_market"] = profile["arrivals"]
    overrides["consumer_agent_count"] = profile["consumers"]
    scoped = with_overrides(params, overrides)
    target = Path(out_dir)
    run_scenario(scoped, scenario_id=f"{profile['scenario']}::{arm['name']}", overrides=overrides,
                 days=profile["days"], seed=profile["seed"], out_dir=target,
                 self_funded=arm_flags(scenario, arm)["self_funded"])
    return read_events(target / "events.jsonl"), scoped


def price_books_for(scoped, *, profile: dict = CONSISTENCY_PROFILE) -> dict:
    """重建该档的**每摊位价目表**（每 500g 单价）—— 同 seed 同参数 ⇒ 逐值可复现。"""
    from ..bridge.world_setup import build_world

    world = build_world(scoped, days=profile["days"], seed=profile["seed"], start=profile["start"])
    return {stall_no: dict(stall.price_book) for stall_no, stall in world.stalls.items()}


def model_ledger(events: list[dict], books: dict, *, rate_bp: int,
                 max_txns_per_day: int | None = None, skip_txns: int = 0) -> dict:
    """由**事件流**复算 model 侧逐日账目（走秤通道），并单列三处口径差。

    `skip_txns` = **灵敏度负例专用**：跳过前 N 笔走秤成交，等价于"篡改 model 少记一笔"。
    """
    buckets: dict[str, dict] = {}
    shadow: dict[str, int] = {}
    rounding_gap = 0
    skipped = 0
    for event in events:
        if event.get("kind") != "txn":
            continue
        day = event["business_date"]
        if event.get("channel") == "shadow":
            shadow[day] = shadow.get(day, 0) + 1
            continue
        bucket = buckets.setdefault(day, {"txns": 0, "gross": 0, "cash": 0, "stalls": set(), "received": {}})
        if max_txns_per_day is not None and bucket["txns"] >= max_txns_per_day:
            continue
        if skipped < skip_txns:
            skipped += 1
            continue
        stall_no, product_id, grams = event["stall_no"], event["product_id"], int(event["grams"])
        unit_500 = books[stall_no][product_id]
        cents = half_up_div(unit_500 * PER_KG * grams, 1000)
        if int(round(unit_500 * grams / 500)) != cents:
            rounding_gap += 1
        bucket["txns"] += 1
        bucket["gross"] += cents
        bucket["stalls"].add(stall_no)
        bucket["received"][stall_no] = bucket["received"].get(stall_no, 0) + cents
        if event.get("method") == "cash":
            bucket["cash"] += 1
    days = {}
    for day, bucket in buckets.items():
        days[day] = {
            "scale_txns": bucket["txns"], "gross_amount_cents": bucket["gross"],
            "commission_cents": sum(half_up_div(amount * rate_bp, 10000)
                                    for amount in bucket["received"].values()),
            "cash_txn_numerator": bucket["cash"], "stall_usage_numerator": len(bucket["stalls"]),
            "shadow_txns": shadow.get(day, 0), "per_stall_cents": dict(bucket["received"]),
        }
    for day, count in shadow.items():
        days.setdefault(day, {"scale_txns": 0, "gross_amount_cents": 0, "commission_cents": 0,
                              "cash_txn_numerator": 0, "stall_usage_numerator": 0, "shadow_txns": 0,
                              "per_stall_cents": {}})["shadow_txns"] = count
    scale_total = sum(row["scale_txns"] for row in days.values())
    shadow_total = sum(row["shadow_txns"] for row in days.values())
    return {"days": days, "rounding_gap_txns": rounding_gap, "skipped_txns": skipped,
            "caliber_gap": {
                "shadow_txns_total": shadow_total, "scale_txns_total": scale_total,
                "scale_use_rate_model_side": round(scale_total / (scale_total + shadow_total), 6) if (scale_total + shadow_total) else None,
                "note": "model 侧含私下成交、系统侧不可见（Q-15 的外部基准）。一致性对账只在走秤通道上做；"
                        "shadow 笔数在此单列，并给出它把 model 侧 M-04 拉低到多少 —— **量化，不是忽略**"}}


# ---------------------------------------------------------------------------
