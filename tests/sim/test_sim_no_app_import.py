"""`T-SIM-01` 隔离闸门之一：**`sim/**` 不得 `import app`**（`docs/sim-design.md` §2.1）。

为什么这条必须有机械检查：`sim` 与主系统的**唯一耦合面是 HTTP 契约的 38 个端点**。
一旦允许 `import app`，sim 就能绕过端点、会话鉴权、错误码、幂等与日聚合直接改库 ——
那时"`--mode=live` 证明这套设计能跑"这句话就退化成"领域函数能跑"，**证据作废**。

反向也检查：`run.py` / `app/**` **不得** `import sim`（仿真不是启动路径，主系统不依赖仿真）。
"""

from __future__ import annotations

import pytest

from sim_support import ALLOWED_LOCAL_ROOTS, KNOWN_THIRD_PARTY, REPO_ROOT, imported_roots, sim_python_files


def _offenders() -> list[str]:
    bad: list[str] = []
    for path in sim_python_files():
        roots = imported_roots(path.read_text(encoding="utf-8"))
        for root in sorted(roots & (KNOWN_THIRD_PARTY - ALLOWED_LOCAL_ROOTS)):
            bad.append(f"{path.relative_to(REPO_ROOT)}: import {root}")
        for root in sorted(roots & {"app", "run"}):
            bad.append(f"{path.relative_to(REPO_ROOT)}: import {root}（唯一耦合面是 HTTP 契约）")
    return bad


def test_sim_does_not_import_app_or_third_party():
    """扫描 `sim/**`：既不得 import 主系统，也不得 import 任何第三方包。"""
    bad = _offenders()
    assert bad == [], "隔离闸门被判红：\n  " + "\n  ".join(bad)
    print(f"[T-SIM-01] 扫描 {len(sim_python_files())} 个 sim 源文件：零 app 依赖、零第三方依赖")


def test_scanner_flags_forbidden_imports():
    """**灵敏度负例**：四种违规形态逐个必须被判红（否则上面的扫描等于没扫）。"""
    samples = [
        ("import app\n", "app"),
        ("from app import clock\n", "app"),
        ("import numpy as np\n", "numpy"),
        ("import sys\nimport numpy\nx = 1\n", "numpy"),
    ]
    for source, hint in samples:
        roots = imported_roots(source)
        assert hint in roots, f"解析没抓到 {hint}：{roots}"
        assert roots & (KNOWN_THIRD_PARTY | {"app", "run"}), f"该形态未被判红：{source!r}"
    print("[T-SIM-01] 合成负例（import app / from app / import numpy）全部判红")


def test_scanner_stays_green_on_legal_imports():
    """反向断言：标准库与相对导入不得误报（否则闸门会被顺手放宽）。"""
    legal = "import json\nimport sys\nfrom .core import clock\nfrom collections import deque\n"
    roots = imported_roots(legal)
    assert not (roots & (KNOWN_THIRD_PARTY | {"app", "run"})), f"合法导入被误报：{roots}"
    print(f"[T-SIM-01] 合法导入零命中：{sorted(roots)}")


def test_run_py_and_app_do_not_import_sim():
    """反向隔离：`run.py` 与 `app/**` 不得 import `sim`（主系统不依赖仿真）。

    ⚠️ 判据是"**顶层包名恰为 `sim`**"，不是"包含 sim 字样" —— 后者会被 `sqlite3`、
    `from .seed import ...` 这类正常的相对导入搞成全红（本文件第一版就是这么错的）。
    """
    targets = [REPO_ROOT / "run.py"] + sorted((REPO_ROOT / "app").rglob("*.py"))
    bad = [str(p.relative_to(REPO_ROOT)) for p in targets if "sim" in imported_roots(p.read_text(encoding="utf-8"))]
    assert bad == [], f"主系统引用了仿真包（仿真不是启动路径）：{bad}"
    print(f"[T-SIM-01] 主系统 {len(targets)} 个源文件均未 import sim")
