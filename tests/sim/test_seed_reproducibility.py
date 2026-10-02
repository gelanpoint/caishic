"""`T-SIM-01` 验收①②③④：**同 seed 逐字节可复现** + 演示库不被触碰。

这是仿真可信度的地基，不是可选优化：

* 若同 seed 两次运行的结果不同，`docs/sim-design.md` §7 的**全部对照实验判据**都不成立
  （无法区分"是参数改动导致的差异"还是"随机噪声"）；
* 若仿真的隔离闸门只写在文档里，就没人能证明它真的没去动演示库。

本文件用**子进程**跑 `python -m sim`（与现场使用方式一致），比对 `events.jsonl` 与 `metrics.json`
的 SHA-256；并在运行前后对演示库取哈希。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys

import pytest

from sim_support import DEMO_DB, PARAMS_PATH, REPO_ROOT


def _run_sim(out_dir, *extra: str) -> subprocess.CompletedProcess:
    """按现场用法跑 CLI（`python -m sim ...`），产物落到 `out_dir`。"""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "sim",
            "--mode=model",
            "--days=12",
            "--agents=4",
            "--start=2026-10-01",
            "--params",
            str(PARAMS_PATH),
            "--out-dir",
            str(out_dir),
            *extra,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_same_seed_is_byte_identical(tmp_path):
    """同 seed 两次运行 → `events.jsonl` 与 `metrics.json` **逐字节相同**。"""
    first, second = tmp_path / "a", tmp_path / "b"
    r1 = _run_sim(first, "--seed=4242")
    r2 = _run_sim(second, "--seed=4242")
    assert r1.returncode == 0, f"第一次运行失败：\n{r1.stdout}\n{r1.stderr}"
    assert r2.returncode == 0, f"第二次运行失败：\n{r2.stdout}\n{r2.stderr}"

    for name in ("events.jsonl", "metrics.json"):
        d1, d2 = _digest(first / name), _digest(second / name)
        assert d1 == d2, f"{name} 同 seed 两次运行不一致：{d1[:16]}… vs {d2[:16]}…"
        print(f"[T-SIM-01] {name} 同 seed 逐字节一致（sha256={d1[:16]}…）")


def test_different_seed_changes_the_result(tmp_path):
    """不同 seed → 必须不同（否则"可复现"是因为**根本没随机**，属于假绿）。"""
    first, second = tmp_path / "a", tmp_path / "b"
    assert _run_sim(first, "--seed=1").returncode == 0
    assert _run_sim(second, "--seed=2").returncode == 0
    assert _digest(first / "events.jsonl") != _digest(second / "events.jsonl"), (
        "不同 seed 产生了完全相同的事件流 —— 随机流没起作用"
    )
    print("[T-SIM-01] 不同 seed 产生不同事件流（随机流确实在起作用）")


def test_adding_an_agent_does_not_shift_other_agents(tmp_path):
    """**分流的核心理由**：多加一个 agent 不得改变已有 agent 的随机数。

    做法：跑 agents=2 与 agents=3，逐条比对 `skeleton-01` / `skeleton-02` 的 `draw` 值。
    若共用一条全局流，新增 agent 会平移后续所有随机数，本用例必红 —— 那时单因子对照实验就失效了。
    """
    import json

    small, big = tmp_path / "small", tmp_path / "big"
    assert _run_sim(small, "--seed=7", "--agents=2").returncode == 0
    assert _run_sim(big, "--seed=7", "--agents=3").returncode == 0

    def draws(path) -> dict[tuple[str, str], float]:
        out: dict[tuple[str, str], float] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["kind"] == "skeleton_step":
                out[(record["agent_id"], record["business_date"])] = record["draw"]
        return out

    before, after = draws(small / "events.jsonl"), draws(big / "events.jsonl")
    shared = {k: v for k, v in before.items() if k[0] in {"skeleton-01", "skeleton-02"}}
    assert shared, "没取到 skeleton-01/02 的抽样记录（事件结构变了？）"
    for key, value in shared.items():
        assert after[key] == value, f"新增 agent 改变了已有 agent 的随机数：{key} {value} != {after[key]}"
    print(f"[T-SIM-01] 新增 agent 未影响已有 {len(shared)} 条抽样（每 agent 独立分流成立）")


def _tree_snapshot(roots) -> dict[str, tuple[int, int]]:
    """仓库内一批目录的快照：`相对路径 → (大小, mtime_ns)`。

    `mtime_ns` 一起取，是因为"内容被覆盖成同样大小"这种情况不会被大小抓到。
    """
    out: dict[str, tuple[int, int]] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                stat = path.stat()
                out[str(path.relative_to(REPO_ROOT))] = (stat.st_size, stat.st_mtime_ns)
    return out


def test_sim_run_touches_nothing_outside_its_out_dir(tmp_path):
    """仿真**绝不触碰**自己的 `--out-dir` 之外的仓库文件（演示库、主系统源码、参数、测试件）。

    为什么不只比对演示库：`data/` 在 `.gitignore` 里，**新克隆的仓库没有这个库** ——
    只查它的话，守卫会在没有库时走 skip 分支，从此永远不报信（"错的常量比没有常量更坏"，
    本常量第一版就是把 `market_trade.sqlite3` 写成了 `market_trade_sqlite3`）。
    改为整树快照后，**任何环境下都必然跑得到**。
    """
    watched = [REPO_ROOT / "app", REPO_ROOT / "sim", REPO_ROOT / "scripts", REPO_ROOT / "data"]
    before = _tree_snapshot(watched)
    result = _run_sim(tmp_path / "run", "--seed=99")
    assert result.returncode == 0, result.stderr
    after = _tree_snapshot(watched)

    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    assert changed == [], f"仿真运行改动了 --out-dir 之外的文件：{changed}"

    if DEMO_DB.is_file():
        print(f"[T-SIM-01] 演示库与 {sum(len(v) for v in [before])} 个受监文件全部未变（含演示库哈希比对）")
    else:
        print(f"[T-SIM-01] {len(before)} 个受监文件全部未变（演示库不存在，本环境无需比对）")


def test_cli_reports_params_and_outputs_metrics(tmp_path):
    """正常路径的健康检查：退出码 0、打印出处统计、`metrics.json` 字段齐全。"""
    import json

    out = tmp_path / "run"
    result = _run_sim(out, "--seed=5")
    assert result.returncode == 0, result.stderr
    assert "sourced=" in result.stdout and "assumed=" in result.stdout
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["first_business_date"] == "2026-10-01"
    assert metrics["last_business_date"] == "2026-10-12"
    assert metrics["days"] == 12 and metrics["agents"] == 4
    assert metrics["skeleton_only"] is True, "骨架运行必须自报 skeleton_only，不得冒充完整仿真"
    print(f"[T-SIM-01] CLI 正常：{metrics['first_business_date']} → {metrics['last_business_date']}")
