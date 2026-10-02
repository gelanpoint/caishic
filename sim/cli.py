"""命令行入口：`python -m sim --mode=model --days=... --seed=...`（`docs/sim-design.md` §2.6）。

`T-SIM-01` 阶段只实现 **`--mode=model` 的骨架运行**：推进营业日、按 agent 分流取随机数、
落 `events.jsonl` 与 `metrics.json`，并把"同 seed 逐字节可复现"变成可执行判据。
真实环境层（市场/客流/设备）由 `T-SIM-02` 接入；`--mode=live` 由 `T-SIM-07` 接入。

**骨架运行不假装是市场仿真**：登记的 agent 命名为 `skeleton-*`，事件类型明确写 `skeleton_step`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

from .core.clock import Block, SimClock
from .core.events import EventLog
from .core.params import DEFAULT_PARAMS_PATH, ParamsError, load_params
from .core.registry import Registry
from .core.streams import StreamSet

SCHEMA_VERSION = 1

#: 骨架占位 agent 的数量（对应演示规模的 10 个摊位；`T-SIM-02` 起由环境层决定）
DEFAULT_AGENTS = 10

#: 固定默认起始营业日：**刻意不用 `date.today()`** —— 否则"同 seed 两次运行逐字节相同"会因为
#: 跨天而假红，复现判据就失去了意义（`T-SIM-01` 验收①）。
DEFAULT_START = "2026-10-01"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sim",
        description="菜市场数字化交易与佣金系统 · 多智能体仿真（ABM）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--mode", choices=("model", "live"), default="model", help="运行模式")
    parser.add_argument("--days", type=int, default=30, help="营业日数（12 个月 = 360）")
    parser.add_argument("--seed", type=int, default=20261002, help="随机种子（决定可复现性）")
    parser.add_argument("--agents", type=int, default=DEFAULT_AGENTS, help="骨架占位 agent 数（T-SIM-02 起由环境层决定）")
    parser.add_argument("--start", default=DEFAULT_START, help="首个营业日 YYYY-MM-DD")
    parser.add_argument("--replications", type=int, default=1, help="重复次数（每次用 seed+i）")
    parser.add_argument("--params", default=str(DEFAULT_PARAMS_PATH), help="参数文件（含出处，必填 provenance）")
    parser.add_argument("--out-dir", default=None, help="产物目录（默认 data/sim/<mode>-<seed>）")
    return parser


def _write_json(path: Path, payload: dict) -> str:
    """稳定写出 JSON（键序固定、LF 换行、不转义中文）并返回内容 SHA-256。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_model_once(args, params, seed: int, out_dir: Path) -> dict:
    """跑一次骨架仿真，落 `events.jsonl` 与 `metrics.json`，返回指标。"""
    clock = SimClock(
        start=date.fromisoformat(args.start),
        days=args.days,
        blocks=tuple(Block(name, start, intensity) for name, start, intensity in params.blocks()),
    )
    registry = Registry()
    for index in range(args.agents):
        registry.register(f"skeleton-{index + 1:02d}", "merchant")
    streams = StreamSet(seed)

    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"
    with EventLog(events_path) as log:
        log.emit(
            "run_started",
            mode="model",
            seed=seed,
            days=args.days,
            agents=len(registry),
            start=args.start,
            blocks=[b.name for b in clock.blocks],
        )
        checked_days = 0
        for index, business_date, month_end, quarter_end in clock.iter_days():
            log.emit("day_opened", day_index=index, business_date=business_date)
            for ref in registry.all():
                # 骨架占位：每 agent 每日从**自己的**流取一个值，证明"分流且可复现"
                value = streams.stream(ref.agent_id, "adapt").random()
                log.emit(
                    "skeleton_step",
                    agent_id=ref.agent_id,
                    agent_kind=ref.kind,
                    business_date=business_date,
                    draw=round(value, 12),
                )
            log.emit(
                "day_closed",
                day_index=index,
                business_date=business_date,
                month_end=month_end,
                quarter_end=quarter_end,
            )
            checked_days += 1
        log.emit("run_finished", days=checked_days, event_count=log.count)
        event_count = log.count

    metrics = {
        "schema_version": SCHEMA_VERSION,
        "mode": "model",
        "skeleton_only": True,
        "seed": seed,
        "days": args.days,
        "agents": len(registry),
        "start": args.start,
        "first_business_date": clock.business_date(0),
        "last_business_date": clock.business_date(args.days - 1),
        "blocks": [{"name": b.name, "start": b.start, "intensity": b.intensity} for b in clock.blocks],
        "total_block_intensity": round(clock.total_intensity(), 12),
        "event_count": event_count,
        "events_sha256": hashlib.sha256(events_path.read_bytes()).hexdigest(),
        "params_sourced": params.sourced_ids(),
        "params_assumed": params.assumed_ids(),
    }
    _write_json(out_dir / "metrics.json", metrics)
    return metrics


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.mode == "live":
        print(
            "[未实现] --mode=live（真实 HTTP 调用 31 个端点）由 T-SIM-07 接入。\n"
            "  本阶段（T-SIM-01/02）只提供 --mode=model 的骨架与环境层。",
            file=sys.stderr,
        )
        return 3

    try:
        params = load_params(args.params)
    except ParamsError as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        return 4
    if args.days < 1 or args.agents < 0 or args.replications < 1:
        print("[用法错误] --days ≥1、--agents ≥0、--replications ≥1", file=sys.stderr)
        return 2

    base_out = Path(args.out_dir) if args.out_dir else Path("data") / "sim" / f"model-{args.seed}"
    results = []
    for rep in range(args.replications):
        seed = args.seed + rep
        out_dir = base_out if args.replications == 1 else base_out / f"rep-{rep:02d}"
        metrics = run_model_once(args, params, seed, out_dir)
        results.append({"rep": rep, "seed": seed, "out_dir": str(out_dir), **metrics})
        print(
            f"[model] rep={rep} seed={seed} days={args.days} agents={args.agents} "
            f"events={metrics['event_count']} → {out_dir}"
        )
    if args.replications > 1:
        summary_path = base_out / "summary.json"
        _write_json(summary_path, {"schema_version": SCHEMA_VERSION, "replications": results})
        print(f"[model] 汇总 → {summary_path}")
    print(
        f"[params] sourced={len(params.sourced_ids())} 项 / assumed={len(params.assumed_ids())} 项"
        f"（出处合规；参数文件 {params.source}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

