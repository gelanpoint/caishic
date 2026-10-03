"""`python -m sim --env`：**零决策基线运行**（`T-SIM-02`；从 `sim/cli.py` 按语义搬出）。

## 为什么搬

`docs/standards/quality-gates.md` §1.2 的「单文件 ≤ 400 行」（`Q-16`：按语义拆、不放宽阈值、
不删注释凑行数）。语义边界：`cli.py` 管**参数解析与分流**，本模块管**环境层一次运行里到底发生了什么**
（到达过程的流号分配 / 设备机队推进 / 守恒自检 / 逐条明细落盘）。这两件事行数涨速不一样 ——
明细与守恒那一段会随 `§2.4` 环境层继续长，混在解析器里迟早顶破门禁。

## 兼容性

`sim/cli.py` 原样转出 `run_env_baseline_once` / `_sum_block_field` / `SCHEMA_VERSION`，
**既有调用方式与既有用例一行不改**。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from .cli_support import SCHEMA_VERSION, _write_json
from .core.clock import Block, SimClock
from .core.events import EventLog
from .core.streams import StreamSet
from .env.demand import block_lambdas, day_arrivals, split_on_scale
from .env.devices import DeviceFleet
from .env.market import build_market

def run_env_baseline_once(args, params, seed: int, out_dir: Path) -> dict:
    """跑一次**零决策基线**：环境层推进 90 营业日也不该出现任何异常或守恒破坏。

    零决策 = 没有任何策略变化，随机流与参数都取基线值。它的用途是**对照基线**：
    后续场景（`S0`~`S6`）与它的差异才是"策略造成的差异"，而不是"随机噪声造成的差异"。

    天气/结构事实全部落进事件日志，**守恒与强度都能由明细独立复算**（`T-SIM-02` 验收②④）——
    故本函数只负责"产生事实与自检"，判定交给 `tests/sim/test_env_timing.py`。
    """
    clock = SimClock(
        start=date.fromisoformat(args.start),
        days=args.days,
        blocks=tuple(Block(name, start, intensity) for name, start, intensity in params.blocks()),
    )
    streams = StreamSet(seed)
    market = build_market(params, streams)
    fleet = DeviceFleet(
        total=int(params.value("device_count")),
        mtbf_days=float(params.value("device_mtbf_days")),
        repair_mean_days=float(params.value("repair_mean_days")),
    )
    daily_arrivals = float(params.value("daily_arrivals_per_market"))
    on_scale_rate = float(params.value("scale_use_baseline_rate"))
    lambdas = {name: lam for name, _start, _intensity, lam in block_lambdas(clock.blocks, daily_arrivals)}

    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"
    totals = {"arrivals": 0, "on_scale": 0, "off_scale": 0, "breakdowns": 0, "repaired": 0}
    with EventLog(events_path) as log:
        log.emit(
            "run_started",
            mode="model",
            layer="env-baseline",
            seed=seed,
            days=args.days,
            start=args.start,
            stalls=len(market.stalls),
            products=len(market.products),
            devices=fleet.total,
            blocks=[b.name for b in clock.blocks],
        )
        for index, business_date, month_end, quarter_end in clock.iter_days():
            log.emit("day_opened", day_index=index, business_date=business_date)

            arrivals = day_arrivals(clock.blocks, daily_arrivals, streams.stream("market", "arrival"))
            day_total = 0
            for block in arrivals:
                on_scale, off_scale = split_on_scale(block.count, on_scale_rate, streams.stream("market", "choice"))
                if on_scale + off_scale != block.count:
                    raise RuntimeError(f"客流守恒被破坏：{on_scale}+{off_scale}≠{block.count}")
                day_total += block.count
                log.emit(
                    "block_arrivals",
                    business_date=business_date,
                    block=block.block,
                    start=block.start,
                    intensity=block.intensity,
                    lam=round(block.lam, 12),
                    count=block.count,
                    on_scale=on_scale,
                    off_scale=off_scale,
                )
            totals["arrivals"] += day_total
            log.emit("day_arrivals_total", business_date=business_date, count=day_total)

            fleet_state = fleet.step_day(streams.stream("market", "breakdown"), streams.stream("market", "repair"))
            totals["breakdowns"] += fleet_state["breakdowns"]
            totals["repaired"] += fleet_state["repaired"]
            log.emit(
                "device_state",
                business_date=business_date,
                total=len(fleet.states),
                **fleet_state,
            )
            log.emit(
                "day_closed",
                day_index=index,
                business_date=business_date,
                month_end=month_end,
                quarter_end=quarter_end,
            )
        log.emit("run_finished", days=args.days, event_count=log.count)
        event_count = log.count

    # 逐日明细复算出的日总量（用于 metrics 自洽，也是"可由明细复算"的最小演示）
    on_scale_total, off_scale_total = _sum_block_field(events_path, "on_scale"), _sum_block_field(events_path, "off_scale")
    metrics = {
        "schema_version": SCHEMA_VERSION,
        "mode": "model",
        "layer": "env-baseline",
        "skeleton_only": False,
        "seed": seed,
        "days": args.days,
        "start": args.start,
        "first_business_date": clock.business_date(0),
        "last_business_date": clock.business_date(args.days - 1),
        "stalls": len(market.stalls),
        "products": len(market.products),
        "devices": fleet.total,
        "device_states_final": fleet.counts(),
        "block_lambdas": {name: round(lam, 12) for name, lam in sorted(lambdas.items())},
        "arrivals_total": totals["arrivals"],
        "on_scale_total": on_scale_total,
        "off_scale_total": off_scale_total,
        "flow_conservation_ok": on_scale_total + off_scale_total == totals["arrivals"],
        "device_conservation_ok": sum(fleet.counts().values()) == len(fleet.states),
        "breakdowns_total": totals["breakdowns"],
        "repairs_done_total": fleet.repairs_done_total,
        "device_days_lost_total": fleet.device_days_lost_total,
        "event_count": event_count,
        "events_sha256": hashlib.sha256(events_path.read_bytes()).hexdigest(),
        "params_sourced": params.sourced_ids(),
        "params_assumed": params.assumed_ids(),
    }
    _write_json(out_dir / "metrics.json", metrics)
    return metrics


def _sum_block_field(events_path: Path, field: str) -> int:
    """由事件明细求和某个 `block_arrivals` 字段（"可由明细复算"的实现）。"""
    total = 0
    for line in events_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("kind") == "block_arrivals":
            total += int(record[field])
    return total
