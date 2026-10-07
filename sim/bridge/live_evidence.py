"""live 运行的**证据**与**六条判据的判定**（`T-SIM-07`；`docs/sim-design.md` §8 `T-SIM-07`）。

## 为什么判定要单独成文件、且写成纯函数

`T-036` 家族的教训是"**不验证灵敏度的验证是摆设**"。若六条判据的判定散落在
`live_run.py` 的 `assert` 里，"把判据关掉会不会变红"就只能靠人回答。

所以这里把判定收敛成一个**纯函数** `verification_problems(report, ...)`：
输入一份**机器可读的运行报告**，输出问题清单（空 = 绿）。
于是每条判据的灵敏度负例都是"喂一份改坏的报告 → 必须报出对应的问题"，**不依赖真的再跑一遍**，
而且负例里改坏的是什么、期望报出什么，都写在用例里可复核。

## 六条判据与它们的机械形式

| 判据 | 报告里对应的字段 | 判定 |
| --- | --- | --- |
| ① 38/38 端点覆盖 | `coverage` | 缺一即红（清单由**实际请求**反推，见 `live_adapter.Coverage`） |
| ② 全营业日对账 | `days[].reconciliation` | 每一行 `balanced=true` 且 `diff_cents=0` |
| ③ 幂等重放不产生新交易 | `idempotency_replay` | 同一 `transaction_no` + 列表 `total` 前后相等 |
| ④ 敏感扫描零命中 | `response_bodies` + 调用方传入的 `sensitive_hits` | **必须由调用方**用 `tests/contract/sensitive_scan.py` 真扫一遍 |
| ⑤ 数据目录在 `C:` | `data_dir` / `data_dir_drive` / `latency` | 不在 `C:` 时必须带上实测 p50/p95 与降级说明 |
| ⑥ 系统只读 | `system_digest_before/after` | `app/**`、`specs/**` 两个指纹都必须相等 |

④ 刻意**不允许仿真侧自己判**：禁令集合（`FORBIDDEN_FIELD_RE` 等）的唯一权威是
`tests/contract/sensitive_scan.py`（同源三处复用），在 `sim/` 里抄一份就是下一处漂移。
未传入扫描结果时返回一条"待外部扫描"的问题 —— **不许因为没人扫就静默判绿**。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Sequence

from .live_adapter import ENDPOINT_KEYS, coverage_problems


def percentile(samples: Sequence[float], q: float) -> float | None:
    """最近秩分位数（`q ∈ [0,1]`；空样本返回 `None`）。

    **为什么用最近秩而不是线性插值**：门禁阈值是"p95 不得超过多少毫秒"，
    线性插值会造出一个**没有真实样本**的数；最近秩报出来的永远是某一笔实测值。
    """
    if not samples:
        return None
    ordered = sorted(samples)
    index = max(1, min(len(ordered), int(round(q * len(ordered) + 0.5))))
    return float(ordered[index - 1])


def latency_summary(samples: Iterable[float]) -> dict:
    """时延样本 → `{count, p50, p95, max}`（毫秒，四舍五入到 0.001）。"""
    values = [round(float(v), 3) for v in samples]
    return {
        "count": len(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def _drive_of(path: Path | str) -> str:
    """取路径所在盘符（大写，无盘符则返回空串）。"""
    drive = Path(path).drive or ""
    return drive.rstrip(":").upper()


def verification_problems(
    report: dict,
    *,
    sensitive_hits: Sequence[str] | None = None,
    expected_endpoints: tuple[str, ...] = ENDPOINT_KEYS,
) -> list[str]:
    """逐条判定六条判据；返回问题清单（**空 = 全部通过**）。**纯函数**。

    `sensitive_hits` 由调用方用 `tests/contract/sensitive_scan.py` 扫 `report["response_bodies"]`
    后传入；不传 ⇒ 判据④记为"未执行"（不静默判绿）。
    """
    problems: list[str] = []

    # ① 38/38 端点覆盖
    coverage = report.get("coverage") or {}
    problems.extend(f"① 端点覆盖：{item}" for item in coverage_problems(coverage, expected_endpoints))
    if coverage.get("covered_count") != len(expected_endpoints):
        problems.append(
            f"① 端点覆盖：实际覆盖 {coverage.get('covered_count')} / 应为 {len(expected_endpoints)}"
        )

    # ② 全部营业日 balanced=true
    days = report.get("days") or []
    if not days:
        problems.append("② 对账：报告里没有任何营业日（live 运行没跑？）")
    for row in days:
        reconciliation = row.get("reconciliation") or {}
        if not reconciliation.get("balanced") or int(reconciliation.get("diff_cents", 1)) != 0:
            problems.append(
                f"② 对账：{row.get('business_date')} 不平（order={reconciliation.get('order_total_cents')} "
                f"payment={reconciliation.get('payment_total_cents')} "
                f"split={reconciliation.get('split_total_cents')} diff={reconciliation.get('diff_cents')}）"
            )

    # ③ 幂等重放不产生新交易
    replay = report.get("idempotency_replay")
    if not replay:
        problems.append("③ 幂等重放：报告里没有重放证据")
    else:
        if replay.get("transaction_no") != replay.get("replayed_transaction_no"):
            problems.append(
                f"③ 幂等重放：重发后交易号变了（{replay.get('transaction_no')} → "
                f"{replay.get('replayed_transaction_no')}）"
            )
        if replay.get("list_total_before") != replay.get("list_total_after"):
            problems.append(
                f"③ 幂等重放：重发后交易列表总数变了（{replay.get('list_total_before')} → "
                f"{replay.get('list_total_after')}）⇒ 产生了新交易"
            )

    # ④ 敏感扫描零命中（扫描由调用方执行）
    if sensitive_hits is None:
        problems.append(
            "④ 敏感扫描：**未执行**（须用 tests/contract/sensitive_scan.py 扫 `response_bodies` 后把命中传入）"
        )
    elif list(sensitive_hits):
        problems.append(f"④ 敏感扫描：命中 {len(list(sensitive_hits))} 处 → {list(sensitive_hits)[:5]}")

    # ⑤ 数据目录在 C:
    drive = _drive_of(report.get("data_dir") or "")
    latency = report.get("latency") or {}
    if drive != "C":
        if not latency.get("p50") or not latency.get("p95"):
            problems.append(f"⑤ 数据目录：盘符 {drive or '（无盘符）'} ≠ C，且**没有实测 p50/p95**（不可判定）")
        else:
            problems.append(
                f"⑤ 数据目录：盘符 {drive} ≠ C ⇒ 已降级（实测 p50={latency['p50']}ms / "
                f"p95={latency['p95']}ms，样本 {latency.get('count')}；"
                f"备注：{report.get('data_dir_reason', '')}）"
            )

    # ⑥ 运行前后系统只读
    before = report.get("system_digest_before") or {}
    after = report.get("system_digest_after") or {}
    if not before or not after:
        problems.append("⑥ 系统只读：报告缺少运行前/后的树指纹（不可判定）")
    else:
        changed = sorted(key for key in ("app", "specs") if before.get(key) != after.get(key))
        if changed:
            problems.append(f"⑥ 系统只读：{changed} 的树指纹在运行期间变了（live 模式只允许读系统）")

    return problems


def write_responses(out_dir: Path | str, bodies: Iterable[dict]) -> Path:
    """把**真实收到过的响应体**逐条落成 `live_responses.jsonl`（只追加、键序固定），返回路径。

    单独成文件而不是塞进报告：一次 30 营业日的运行会有上千次调用，响应体（含交易列表）
    全进报告会让报告体膨胀，而判据④只需要能**逐条扫**这些字节。
    """
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "live_responses.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in bodies:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def write_report(out_dir: Path | str, report: dict) -> dict:
    """稳定写出 `live_report.json`（键序固定、LF、不转义中文）并返回内容 SHA-256。

    键序固定不是为了好看：`T-SIM-01` 的"同 seed 逐字节可复现"靠的就是这一条。
    """
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "live_report.json"
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    report["report_path"] = str(path)
    report["report_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (target / "coverage.json").write_text(
        json.dumps(report.get("coverage") or {}, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    return report
