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

from .cli_support import SCHEMA_VERSION, _write_json
from .core.clock import Block, SimClock
from .core.console import force_utf8_stdio
from .core.events import EventLog
from .core.params import DEFAULT_PARAMS_PATH, ParamsError, load_params
from .core.registry import Registry
from .core.streams import StreamSet
from .env_baseline import _sum_block_field, run_env_baseline_once  # 原样转出（见 `sim/env_baseline.py`）

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
    parser.add_argument("--days", type=int, default=None, help="营业日数（缺省：骨架 30 / 集成运行 360）")
    parser.add_argument("--seed", type=int, default=20261002, help="随机种子（决定可复现性）")
    parser.add_argument("--agents", type=int, default=DEFAULT_AGENTS, help="骨架占位 agent 数（T-SIM-02 起由环境层决定）")
    parser.add_argument("--start", default=DEFAULT_START, help="首个营业日 YYYY-MM-DD")
    parser.add_argument("--replications", type=int, default=1, help="重复次数（每次用 seed+i）")
    parser.add_argument(
        "--env",
        action="store_true",
        help="跑**零决策基线**（市场/客流/设备环境层，T-SIM-02）；缺省为 T-SIM-01 的骨架运行",
    )
    parser.add_argument("--params", default=str(DEFAULT_PARAMS_PATH), help="参数文件（含出处，必填 provenance）")
    parser.add_argument("--out-dir", default=None, help="产物目录（默认 data/sim/<mode>-<seed>）")
    parser.add_argument(
        "--scenario",
        default=None,
        help="跑集成运行（**真跑四个 Agent**，`T-SIM-06`）：S0..S6 之一，或 all（全部场景）；"
             "缺省仍走 T-SIM-01 的骨架运行",
    )
    parser.add_argument("--integrated", action="store_true", help="不指定场景时按 S0 基线跑集成运行")
    parser.add_argument("--days-full", type=int, default=360, help="集成运行/研究档的营业日数")
    parser.add_argument("--arrivals", type=int, default=None, help="集成运行覆盖日到达数（缩减档用；缺省用参数值）")
    parser.add_argument("--consumers", type=int, default=None, help="集成运行覆盖消费者 agent 数")
    parser.add_argument("--sensitivity-days", type=int, default=60, help="敏感性档的营业日数")
    parser.add_argument("--lhs-samples", type=int, default=64, help="分层拉丁超立方样本数")
    parser.add_argument("--no-study", action="store_true", help="只跑场景臂，跳过全局 OAT/LHS（省时间）")
    parser.add_argument("--live-days", type=int, default=None,
                        help="live 模式营业日数（缺省 30；**必须 ≥30** 才走得到月末、才调得到两个结算单端点）")
    parser.add_argument("--live-txns", type=int, default=4, help="live 模式每个营业日的成交笔数")
    parser.add_argument("--live-arm", type=int, default=0, help="live 模式用场景的第几臂（0 起）")
    parser.add_argument("--live-data-dir", default=None,
                        help="live 模式的隔离数据目录（缺省落 `C:/mt-sim/<run_id>`；给了就按它实际所在的卷判定）")
    parser.add_argument("--live-port", type=int, default=None, help="live 模式的服务端口（缺省向内核要一个空闲端口）")
    parser.add_argument("--live-timeout", type=float, default=180.0, help="live 模式的 `/healthz` 就绪探测超时上限（秒）")
    parser.add_argument("--live-run-id", default=None,
                        help="live 模式的隔离数据目录名（缺省带时间戳 ⇒ 每次运行都是一份干净库）")
    parser.add_argument("--backtest", action="store_true",
                        help="跑 `T-SIM-08` 的 `R1`~`R6` 回测并落 `backtest.json` / `backtest.md`"
                             "（三态判定；档位与出处随结论一起给出）")
    parser.add_argument("--backtest-only", default=None,
                        help="只回测指定判据（逗号分隔，如 `R3,R6`）；缺省 = 全部六条")
    parser.add_argument("--no-robustness-scan", action="store_true",
                        help="回测时跳过稳健性扫描（**那不是『没发现翻转』**，报告里会如实标为未跑）")
    parser.add_argument("--consistency", action="store_true",
                        help="跑 `T-SIM-08` 的 model↔live 一致性对账（§7.3；容差 ≤1 分 / 1 笔）")
    parser.add_argument("--consistency-days", type=int, default=30,
                        help="一致性对账的营业日数（必须 ≥30 才走得到月末结算单）")
    parser.add_argument("--consistency-txns", type=int, default=20,
                        help="一致性对账每日喂给 live 的走秤笔数上限（**成交密度**；`live_run` 缺省只有 4）")
    parser.add_argument("--no-consistency-tamper", action="store_true",
                        help="**灵敏度负例默认开**（`T-SIM-08` 验收③：篡改 model 一处 ⇒ 一致性必红）；"
                             "只有显式关掉才会不跑 —— 关掉的那次结果在报告里会写明『本次未跑负例』")
    parser.add_argument("--calibrate", action="store_true",
                        help="跑 `T-SIM-08` 的档 2 匹配矩（POM；领域方法，不假称来自本项目调研）")
    parser.add_argument("--calibrate-samples", type=int, default=48,
                        help="匹配矩的采样点数（每点 = 6 个矩各跑一遍）")
    parser.add_argument("--svg-report", action="store_true",
                        help="`T-SIM-09`：**只读已有产物**生成单文件静态报告 `report.html`"
                             "（内联 SVG、零外部资源；**不跑仿真**，缺产物时该块显式显示『未生成』）")
    parser.add_argument("--svg-data", default=None,
                        help="静态报告的产物根（缺省同 `--out-dir` 推导；"
                             "该目录下的 `study/scenarios.json` 与 `verify/backtest/backtest.json` 是数据来源）")
    parser.add_argument("--svg-out", default=None,
                        help="静态报告的输出路径（缺省 `<产物根>/report.html`）")
    parser.add_argument("--svg-study", default=None,
                        help="逐个覆盖场景研究产物路径（缺省 `<产物根>/study/scenarios.json`）")
    parser.add_argument("--svg-backtest", default=None,
                        help="逐个覆盖回测产物路径（缺省 `<产物根>/verify/backtest/backtest.json`）")
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
        "layer": "skeleton",
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




def _run_integrated(args, params, base_out: Path) -> int:
    """集成运行（`T-SIM-06`）：真跑四个 Agent；指定场景时出报告，不指定时按 S0 基线跑一次。

    **为什么缺省路径仍是骨架运行**：`T-SIM-01` 的验收①（同 seed 逐字节一致）与
    "新增 agent 不平移他人随机数"两条判据**直接断言 `skeleton_step` 事件的存在**，
    那是已验收的判据 —— 不为新功能而改。集成运行由 `--scenario` / `--integrated` 触发。
    """
    from .bridge.study import run_study

    days = args.days if args.days is not None else args.days_full
    scenario_ids = None
    if args.scenario and args.scenario.lower() != "all":
        scenario_ids = [item.strip().upper() for item in args.scenario.split(",") if item.strip()]
    study = run_study(
        params,
        days=days,
        replications=args.replications,
        seed=args.seed,
        out_root=base_out / "study",
        scenario_ids=scenario_ids,
        arrivals=args.arrivals,
        consumers=args.consumers,
        sensitivity_days=args.sensitivity_days,
        lhs_samples=args.lhs_samples,
        with_sensitivity=not args.no_study,
        progress=lambda message: print(message),
    )
    if args.no_study:
        print("[--no-study] 已跳过全局 OAT / LHS / 结论稳健性；报告里的敏感性一节会如实标为空"
              "（**没跑 ≠ 不敏感**）")
    for row in study["results"]:
        m04 = row["metrics"]["M-04"]
        print(
            f"[{row['scenario']}] {row['arm']}: 走秤率 {m04['ratio']:.4f} "
            f"({m04['numerator']}/{m04['denominator']})，事件目录 {row['out_dir']}"
        )
    print(f"[报告] {study['paths']['report']}")
    print(f"[产物] {study['paths']['metrics']} · {study['paths']['scenarios']} · {study['paths']['sensitivity']}")
    return 0


def _run_live_mode(args) -> int:
    """`--mode=live`（`T-SIM-07`）：真起被测服务、走完 38 个端点、逐条打印六条判据。

    编排与汇报都在 `sim/bridge/live_cli.py`（`quality-gates.md` §1.2 的 400 行门禁：按语义拆分）。
    **`model` 分支不受本函数影响** —— 它是已验收路径（`T-SIM-01/02/06` 的判据直接断言它的产物）。
    """
    from .bridge.live_cli import live_command

    return live_command(args)


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    args = build_parser().parse_args(argv)

    if args.mode == "live":
        return _run_live_mode(args)

    try:
        params = load_params(args.params)
    except ParamsError as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        return 4
    # `--days` 缺省按路径分流：骨架/环境层 30 日，集成运行 360 日（12 个月，Q5 的判定窗口）
    if args.days is None:
        args.days = args.days_full if (args.scenario or args.integrated) else 30
    if args.days < 1 or args.agents < 0 or args.replications < 1:
        print("[用法错误] --days ≥1、--agents ≥0、--replications ≥1", file=sys.stderr)
        return 2

    base_out = Path(args.out_dir) if args.out_dir else Path("data") / "sim" / f"model-{args.seed}"

    if args.backtest or args.consistency or args.calibrate:
        from .verify_cli import verify_command

        return verify_command(args, params, base_out)

    if args.svg_report:
        from .observe.svg_report import svg_report_command

        return svg_report_command(args, base_out)

    if args.scenario or args.integrated:
        return _run_integrated(args, params, base_out)

    runner = run_env_baseline_once if args.env else run_model_once
    results = []
    for rep in range(args.replications):
        seed = args.seed + rep
        out_dir = base_out if args.replications == 1 else base_out / f"rep-{rep:02d}"
        metrics = runner(args, params, seed, out_dir)
        results.append({"rep": rep, "seed": seed, "out_dir": str(out_dir), **metrics})
        print(
            f"[model] rep={rep} seed={seed} days={args.days} layer={metrics['layer']} "
            f"events={metrics['event_count']} → {out_dir}"
        )
        if args.env:
            print(
                f"[env] 到达 {metrics['arrivals_total']} 笔（走秤 {metrics['on_scale_total']} / 私下 "
                f"{metrics['off_scale_total']}，守恒={metrics['flow_conservation_ok']}）；"
                f"设备三态 {metrics['device_states_final']}（守恒={metrics['device_conservation_ok']}）"
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

