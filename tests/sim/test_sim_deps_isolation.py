"""`T-SIM-01` 依赖隔离：**`sim/**` 只依赖 Python 标准库**（`ADR-0004` 的两期边界）。

与 `test_sim_no_app_import.py` 分开的理由：两者的**失效方式不同** —— 一个被"顺手 import 主系统"废掉，
一个被"顺手装个 numpy 图方便"废掉。合在一起只会涨破 400 行门禁。

本项目**不新增运行期依赖**是硬约束（演示机只装 `Flask`，见 `ADR-0003`/`ADR-0004`）：
仿真若引入第三方包，等于把"仿真环境"变成演示机上的第二个装不上的可能。
"""

from __future__ import annotations

import pytest

from sim_support import (
    ALLOWED_LOCAL_ROOTS,
    KNOWN_THIRD_PARTY,
    RELATIVE_IMPORT,
    REPO_ROOT,
    imported_roots,
    sim_python_files,
)


def test_sim_only_imports_stdlib_and_itself():
    """`sim/**` 的顶层 import 只允许：标准库 + `sim` 自己（含相对导入）。"""
    bad: list[str] = []
    for path in sim_python_files():
        roots = imported_roots(path.read_text(encoding="utf-8"))
        for root in sorted(roots & KNOWN_THIRD_PARTY):
            bad.append(f"{path.relative_to(REPO_ROOT)}: import {root}")
        for root in sorted(roots - ALLOWED_LOCAL_ROOTS - KNOWN_THIRD_PARTY - {RELATIVE_IMPORT}):
            # 既非第三方、也非相对导入 → 只可能是标准库；标准库一律允许（宪法 §1）。
            # 唯一要拦的是"产品代码 import 测试件"（会反向绑死测试目录结构）。
            if root in {"conftest", "sim_support", "clock_support", "contract_support"}:
                bad.append(f"{path.relative_to(REPO_ROOT)}: import 测试支撑 {root}（产品代码不得依赖测试件）")
    assert bad == [], "仿真包出现禁止依赖：\n  " + "\n  ".join(bad)
    print(f"[T-SIM-01] {len(sim_python_files())} 个 sim 源文件只依赖标准库与自身")


def test_third_party_scanner_flags_numpy():
    """**灵敏度负例**：`import numpy` / `from pandas import ...` 必须判红。"""
    for source, hint in (("import numpy\n", "numpy"), ("from pandas import DataFrame\n", "pandas")):
        roots = imported_roots(source)
        assert roots & KNOWN_THIRD_PARTY, f"未判红：{source!r} → {roots}"
        assert hint in roots
    print("[T-SIM-01] 第三方依赖负例（numpy / pandas）判红")


def test_stdlib_imports_are_not_flagged():
    """反向断言：标准库 import 不得误报（本仿真的统计与抽样全部自写，靠标准库实现）。"""
    source = "import json, statistics, hashlib, random\nfrom pathlib import Path\nfrom dataclasses import dataclass\n"
    assert not (imported_roots(source) & KNOWN_THIRD_PARTY)
    print("[T-SIM-01] 标准库（json/statistics/hashlib/random/pathlib/dataclasses）零误报")


def test_requirements_runtime_stays_flask_only():
    """仿真**不得**改动运行期依赖清单：`requirements.txt` 仍只允许 `Flask`（`ADR-0003`/`0004`）。"""
    lines = [
        line.strip()
        for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    offenders = [line for line in lines if line.lower().split("=")[0].split(">")[0].strip() != "flask"]
    assert offenders == [], f"运行期依赖清单被污染（只允许 Flask）：{offenders}"
    print(f"[T-SIM-01] requirements.txt 运行期依赖仍只有 Flask（{len(lines)} 行）")
