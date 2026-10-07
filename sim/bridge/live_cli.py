"""`--mode=live` 的命令行分支（`T-SIM-07`）。

## 为什么单独成文件

`docs/standards/quality-gates.md` §1.2 的「单文件 ≤ 400 行」（`Q-16` 裁定：按语义拆分、
不放宽阈值）。`sim/cli.py` 本来 377 行（`T-SIM-06` 收口时），live 分支再加 90 行就会越线。
语义边界清楚：**参数解析在 `cli.py`，live 的编排与汇报在这里**。

## 为什么汇报要逐条打印六条判据

CLI 的输出是**验收现场的第一手证据**：判据①～⑥每一条都有自己的一行，
而不是只丢一个"通过/不通过"。④ 明确打印"CLI 不自己扫"并指出禁令集合的唯一权威位置
（`tests/contract/sensitive_scan.py`）—— 仿真侧抄一份禁令集合就是第二处会漂移的地方。
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..core.params import ParamsError, load_params
from .live_evidence import verification_problems
from .live_run import LiveRunConfig, run_live
from .live_scenario import LIVE_DEFAULT_DAYS

#: ④ 由 `tests/sim/test_live_run.py` 用 `tests/contract/sensitive_scan.py` 真扫；CLI 只登记"未执行"
_PENDING_MARKER = "④ 敏感扫描"


def live_command(args) -> int:
    """跑一次 live 模式并逐条打印六条判据；返回退出码（0 = 全部通过，5 = 有未通过项）。"""
    try:
        params = load_params(args.params)
    except ParamsError as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        return 4
    if args.live_txns < 1:
        print("[用法错误] --live-txns ≥1", file=sys.stderr)
        return 2

    config = LiveRunConfig(
        scenario_id=(args.scenario or "S0"),
        arm_index=args.live_arm,
        days=args.live_days if args.live_days is not None else LIVE_DEFAULT_DAYS,
        seed=args.seed,
        start=args.start,
        txns_per_day=args.live_txns,
        data_dir=args.live_data_dir,
        port=args.live_port,
        timeout_s=args.live_timeout,
        run_id=args.live_run_id,
    )
    base_out = Path(args.out_dir) if args.out_dir else Path("data") / "sim" / f"live-{args.seed}"
    report = run_live(params, config, base_out)
    for line in _report_lines(report):
        print(line)

    problems = verification_problems(report)
    for item in problems:
        print(f"[判定] {item}")
    failed = [item for item in problems if not item.startswith(_PENDING_MARKER)]
    print(f"[报告] {report['report_path']}")
    return 0 if not failed else 5


def _report_lines(report: dict) -> list[str]:
    """六条判据 + 佣金口径的一行式汇报（**不含判定**，判定在 `verification_problems`）。"""
    coverage = report["coverage"]
    replay = report["idempotency_replay"]
    latency = report["latency"]
    days = report["days"]
    unbalanced = [row["business_date"] for row in days if not row["reconciliation"].get("balanced")]
    lines = [
        f"[live] 场景 {report['scenario']}/{report['arm']} · 营业日 {report['days_count']} 天 · "
        f"每营业日 {report['txns_per_day']} 笔 · HTTP 调用 {report['calls']} 次 · 就绪耗时 "
        f"{report['ready_elapsed_s']}s",
        f"[判据①] 端点覆盖 {coverage['covered_count']}/{coverage['expected_count']}"
        + (f"；缺口 {coverage['missing']}" if coverage["missing"]
           else f"（{coverage['covered_count']}/{coverage['expected_count']} 全覆盖）"),
        f"[判据②] 全营业日对账：{report['days_count'] - len(unbalanced)}/{report['days_count']} 天 balanced=true"
        + (f"；不平的日期 {unbalanced}" if unbalanced else ""),
        f"[判据③] 幂等重放：{replay.get('transaction_no')} 重发 → {replay.get('replayed_transaction_no')}"
        f"（HTTP {replay.get('first_status')} → {replay.get('replay_status')}），列表总数 "
        f"{replay.get('list_total_before')} → {replay.get('list_total_after')}",
        f"[判据④] 敏感扫描：**CLI 不自己扫**（禁令集合的唯一权威在 `tests/contract/sensitive_scan.py`）；"
        f"本次记录响应体 {report['response_bodies']} 条 → {report['responses_path']}",
        f"[判据⑤] 数据目录 {report['data_dir']}（盘符 {Path(report['data_dir']).drive.upper()}，"
        f"{report['data_dir_reason']}）；实测 p50={latency['p50']}ms / p95={latency['p95']}ms"
        f"（样本 {latency['count']}）",
        f"[判据⑥] 系统只读：app={report['system_digest_before']['app'][:12]}… → "
        f"{report['system_digest_after']['app'][:12]}…；specs="
        f"{report['system_digest_before']['specs'][:12]}… → {report['system_digest_after']['specs'][:12]}…",
        f"[佣金口径] 写进系统的规则：pay_object={report['commission']['pay_object']} "
        f"rate_bp={report['commission']['rate_bp']}（场景声明 {report['commission']['requested_rate_bp']}bp，"
        f"pay_object={report['commission_evidence']['created_rule']['pay_object']}）；"
        f"**由 `app/domain/commission.py` 算出的**日佣金合计 "
        f"{report['commission_evidence']['system_commission_total_cents']} 分",
    ]
    if report["commission"]["clamped"]:
        lines.append(f"[佣金口径] 注意：{report['commission']['note']}")
    return lines
