"""`sim/cli.py` 的**验证类**三个入口（`T-SIM-08`；`docs/sim-design.md` §7）。

## 为什么从 `cli.py` 里搬出来

`quality-gates.md` §1.2 的「单文件 ≤ 400 行」（`Q-16` 裁定：按语义拆分、不放宽阈值）。
语义边界：**跑什么**（`cli.py` 的 `model` / `live` / `study` 三条已验收路径）与
**怎么判**（本模块：回测判定 / 一致性对账 / 匹配矩）是两件事。

搬出来还有一个副作用上的好处：`cli.py` 因此**不需要 import 三个验证模块**，
`python -m sim` 的 model 路径的 import 图与改动前保持一致（`T-SIM-07` 验收⑧要的就是这个）。

## 三者为什么是三个开关而不是一个 `--verify`

耗时与产出完全不同。混在一个开关里就会出现"我只想要一致性结果，却顺带跑了半小时回测"。
**没跑的绝不说跑了** —— 每个开关都在自己的产物里标明实际跑了什么。
"""

from __future__ import annotations

import sys
from pathlib import Path


def verify_command(args, params, base_out: Path) -> int:
    """`T-SIM-08` 的三个入口：回测 / 一致性对账 / 档 2 匹配矩。"""
    if args.backtest:
        from .verify.backtest import run_backtest

        only = [item.strip().upper() for item in (args.backtest_only or "").split(",") if item.strip()] or None
        out = run_backtest(params, out_dir=base_out / "backtest", only=only,
                           with_scan=not args.no_robustness_scan, progress=lambda m: print(m))
        for criterion_id, verdict in out["verdicts"].items():
            print(f"[回测] {criterion_id} ⇒ **{verdict}**")
        print(f"[回测产物] {out['paths']['markdown']} · {out['paths']['json']}")
        return 0
    if args.consistency:
        from .verify.consistency import run_consistency

        out = run_consistency(params, out_dir=base_out / "consistency", days=args.consistency_days,
                              max_txns_per_day=args.consistency_txns,
                              tamper=not args.no_consistency_tamper, progress=lambda m: print(m))
        print(f"[一致性] 判定：{'通过' if not out['problems'] else '不通过'}（{len(out['problems'])} 条问题）")
        print(f"[一致性产物] {out['paths']['markdown']} · {out['paths']['json']}")
        return 0 if not out["problems"] else 1
    if args.calibrate:
        from .verify.calibration import run_calibration

        out = run_calibration(params, out_dir=base_out / "calibration", samples=args.calibrate_samples,
                              progress=lambda m: print(m))
        print(f"[匹配矩] 满足全部 6 个矩的样本：{out['accepted']}/{out['samples']}")
        print(f"[匹配矩产物] {out['paths']['markdown']} · {out['paths']['json']}")
        return 0
    print("[用法错误] 需要 `--backtest` / `--consistency` / `--calibrate` 三者之一", file=sys.stderr)
    return 2