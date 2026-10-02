"""`T-SIM-06` 验收③④：**自写 Spearman + 分层拉丁超立方 + OAT + 稳健性**（不引新依赖）。

为什么这块必须自己写、且必须带负例：设计 §5.1 要的是"报告每个参数的秩相关"，
而**秩相关的并列秩处理错会把大量取常数的参数算出假相关**；LHS 若不检验分层，
"我抽了 512 组"可能只是一堆挤在区间中部的样本。故本文件逐条给机制 + 负例：

* `average_ranks` / `spearman`：并列秩取均值、单调变换不变、打乱后趋 0、无秩变化返回 `None`；
* `latin_hypercube` / `stratum_coverage`：每维每层恰一个样本，**退化必须被抓到**；
* `oat_scan` / `oat_summary`：方向与单调，**扫描点算不出值时如实标为 unavailable**
  （第一版直接 `max(None)` 崩掉，而那是"指标在短档上退化"的正常形态）；
* `ordering_stability`：只要有一组翻转，整条结论就是 `not_robust`（§7.2 第 2 条）。
"""

from __future__ import annotations

import random

import pytest

from sim_support import REPO_ROOT  # noqa: F401  （放进 sys.path，使 sim 可导入）

from sim.verify.sensitivity import (
    average_ranks,
    latin_hypercube,
    oat_scan,
    oat_summary,
    ordering_stability,
    rank_correlations,
    spearman,
    stratum_coverage,
    survival_region_shape,
)


# ---------------------------------------------------------------------------
# Spearman
# ---------------------------------------------------------------------------
def test_average_ranks_handles_ties_by_averaging():
    assert average_ranks([10, 10, 20]) == [1.5, 1.5, 3.0]
    assert average_ranks([5, 5, 5]) == [2.0, 2.0, 2.0]
    assert average_ranks([3, 1, 2]) == [3.0, 1.0, 2.0]
    print("[T-SIM-06] 并列秩取均值：编组边界逐个核对通过")


def test_spearman_is_invariant_under_monotone_transforms():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [1.0, 2.0, 3.0, 10.0, 5.0]  # 秩 = [1,2,3,5,4] ⇒ Σd²=2，ρ = 1 − 12/(5·24) = 0.9
    base = spearman(xs, ys)
    assert base == pytest.approx(0.9)
    assert spearman([value ** 3 for value in xs], [10 * value + 7 for value in ys]) == pytest.approx(base)
    assert spearman(xs, [value * -1 for value in ys]) == pytest.approx(-base)
    print(f"[T-SIM-06] Spearman 单调变换不变（ρ={base:.6f}），反向变换取负")


def test_spearman_is_near_zero_after_shuffling_and_none_when_no_rank_change():
    rng = random.Random(7)
    xs = [float(index) for index in range(40)]
    ys = [value * 2 for value in xs]
    shuffled = list(ys)
    rng.shuffle(shuffled)
    assert abs(spearman(xs, shuffled)) < 0.45, "打乱后仍强相关 —— 秩相关算错了"
    assert spearman([1.0, 2.0, 3.0], [4.0, 4.0, 4.0]) is None, "无秩变化必须是 None，不是 0"
    assert spearman([1.0], [1.0]) is None, "样本不足必须是 None"
    print("[T-SIM-06] 打乱后趋 0；无秩变化/样本不足返回 None（不是 0）")


def test_rank_correlations_reports_none_instead_of_zero():
    samples = [{"a": 1.0, "b": float(index)} for index in range(5)]
    rows = {row["key"]: row for row in rank_correlations(samples, [float(index) for index in range(5)], ["a", "b"])}
    assert rows["a"]["rho"] is None and "无秩变化" in rows["a"]["note"]
    assert rows["b"]["rho"] == pytest.approx(1.0)
    print("[T-SIM-06] rank_correlations：常数列给 None + 说明，不冒充『不敏感』")


# ---------------------------------------------------------------------------
# LHS
# ---------------------------------------------------------------------------
SPACE = {"x": (0.0, 10.0), "y": (100.0, 200.0)}


def test_latin_hypercube_is_stratified_per_dimension():
    samples = latin_hypercube(SPACE, 16, random.Random(11))
    assert len(samples) == 16
    assert stratum_coverage(samples, SPACE, 16) == []
    for key in SPACE:
        values = sorted(sample[key] for sample in samples)
        assert values[0] >= SPACE[key][0] and values[-1] <= SPACE[key][1]
    print("[T-SIM-06] LHS 16 样本：每维 16 层各 1 个（覆盖度校验零问题）")


def test_stratum_coverage_catches_degeneracy():
    """**合成负例**：把一批样本挤进同一层 ⇒ 覆盖度检验必须报出是哪一维哪一层。"""
    samples = latin_hypercube(SPACE, 8, random.Random(3))
    samples[0]["x"] = samples[1]["x"]  # 两层里出现两例、某层空缺
    problems = stratum_coverage(samples, SPACE, 8)
    assert problems and "x" in problems[0], f"分层退化没被抓到：{problems}"
    assert stratum_coverage(samples, {"z": (1.0, 1.0)}, 4), "区间退化成点也必须被报出来"
    print(f"[T-SIM-06] 负例：分层退化 ⇒ 判红（{problems[0][:40]}…）")


# ---------------------------------------------------------------------------
# OAT
# ---------------------------------------------------------------------------
def test_oat_scan_reports_direction_and_monotonicity():
    def runner(overrides):
        return overrides["k"] * 2.0

    result = oat_scan(runner, {"k": 1.0}, {"k": [1.0, 2.0, 3.0]})
    row = result["summary"][0]
    assert row["direction"] == "increasing" and row["monotone"] is True
    assert row["outputs"] == [2.0, 4.0, 6.0] and row["span"] == pytest.approx(4.0)

    def flat(overrides):
        return 5.0

    assert oat_scan(flat, {"k": 1.0}, {"k": [1.0, 2.0]})["summary"][0]["direction"].startswith("flat")
    print("[T-SIM-06] OAT：方向/单调/跨度判定通过（含 flat 形态）")


def test_oat_summary_degrades_instead_of_crashing_on_none_outputs():
    """**回归（第一版崩溃的形态）**：扫描点算不出值时记为 unavailable，不当 0、不崩溃。"""
    rows = [{"key": "k", "value": 1.0, "output": None}, {"key": "k", "value": 2.0, "output": 0.5}]
    summary = oat_summary(rows)[0]
    assert summary["span"] is None and summary["monotone"] is False
    assert "unavailable" in summary["direction"] and summary["unavailable_points"] == 1
    print("[T-SIM-06] OAT 退化点：如实标 unavailable（不当 0、不崩）")


def test_ordering_stability_flags_a_single_flip():
    assert ordering_stability([(0.6, 0.4), (0.7, 0.3)], "A>B")["verdict"] == "robust"
    flipped = ordering_stability([(0.6, 0.4), (0.3, 0.7)], "A>B")
    assert flipped["verdict"] == "not_robust" and len(flipped["flips"]) == 1
    with pytest.raises(ValueError):
        ordering_stability([(1.0, 2.0)], "A≈B")
    print("[T-SIM-06] 稳健性：任一组翻转即 not_robust（§7.2 第 2 条）")


def test_survival_region_shape_reports_the_ratio_not_just_a_boolean():
    rows = [{"alive": index % 4 == 0, "params": {"k": float(index)}} for index in range(8)]
    shape = survival_region_shape(rows)
    assert shape["samples"] == 8 and shape["alive"] == 2 and shape["alive_ratio"] == pytest.approx(0.25)
    assert shape["most_sensitive"], "存活区域必须给出最敏感参数，不许只说『存在能活的组合』"
    assert survival_region_shape([])["alive_ratio"] is None
    print(f"[T-SIM-06] 存活区域：占比 {shape['alive_ratio']}，最敏感 {shape['most_sensitive'][:2]}")


def test_sensitivity_module_uses_only_the_standard_library():
    """`ADR-0004`：仿真只用标准库 —— 自写 Spearman 的存在理由就是**不引 `numpy`/`scipy`**。

    判据是**解析 import 语句**，不是"文本里有没有 numpy 字样"：docstring 里写着
    "不引 numpy" 是完全正常的，用文本匹配会把注释当代码（本文件第一版就是这么假红的）。
    """
    from sim_support import KNOWN_THIRD_PARTY, imported_roots, sim_python_files

    offenders = []
    for path in sim_python_files():
        roots = imported_roots(path.read_text(encoding="utf-8"))
        offenders += [f"{path.name}: import {root}" for root in sorted(roots & KNOWN_THIRD_PARTY)]
    assert offenders == [], f"敏感性模块不得引第三方依赖：{offenders}"
    print("[T-SIM-06] 敏感性（及全部 sim）源码：零第三方 import（自写 Spearman，约 30 行）")


# ---------------------------------------------------------------------------
# LHS 播种的**跨进程稳定性**（`T-SIM-06` 收口时发现的真缺陷的回归守卫）
# ---------------------------------------------------------------------------
_PROBE = r"""
import json, sys
sys.path.insert(0, %(root)r)
from sim.bridge.study import lhs_rng
from sim.verify.sensitivity import latin_hypercube
space = {"a": [1.0, 2.0], "b": [3.0, 4.0], "c": [-1.0, 1.0]}
fixed = latin_hypercube(space, 6, lhs_rng(space, 20261002))
# 旧实现（用内置 hash() 播种）——负例对照，必须与新实现不同
import random
legacy = latin_hypercube(space, 6, random.Random(hash(json.dumps(space, sort_keys=True)) %% (2**32)))
print(json.dumps({"fixed": fixed, "legacy": legacy, "hash": hash(json.dumps(space, sort_keys=True))},
                 sort_keys=True))
"""


def _probe_lhs_sampling(hash_seed: str) -> dict:
    """在**子进程**里抽一次 LHS 样本矩阵（可用 `PYTHONHASHSEED` 控制字符串哈希加盐）。"""
    import json
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hash_seed
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE % {"root": str(REPO_ROOT)}],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_lhs_sampling_is_process_stable():
    """**同 seed 的 LHS 样本集必须跨进程逐位一致**（设计 §2.4「同 seed 逐字节可复现」）。

    ## 这条用例是为一个实测到的真缺陷写的

    原实现 `random.Random(hash(rng_seed) % 2**32)` 用**内置 `hash()`** 播种。内置 `hash()` 对字符串
    按 `PYTHONHASHSEED` 加盐、**跨进程不稳定** —— 实测同一条表达式在两个进程里得到
    `5006813868002780648` 与 `8756317105516960903`。后果：同一 `--seed` 跑两次，LHS 样本集不同
    ⇒ `sensitivity.json` 的秩相关与存活区域**不可复现**；而且样本集与 `--seed` 无关 ⇒ `--seed` 对
    敏感性档失效。现改用 `sim/core/streams.py::derive_seed`（sha256）。

    ## 负例是真的负例

    用例同时在**同两个子进程**里用旧公式抽一份样本，断言它**确实不同** ——
    否则本用例即便在旧实现下也会绿（那就成了摆设）。
    """
    first = _probe_lhs_sampling("0")
    second = _probe_lhs_sampling("1")

    assert first["fixed"] == second["fixed"], (
        "PYTHONHASHSEED 不同导致 LHS 样本集变化 ⇒ 敏感性产物不可复现："
        f"{first['fixed']} != {second['fixed']}")
    assert first["hash"] != second["hash"], (
        "负例失效：两个 PYTHONHASHSEED 下内置 hash() 竟然相同，本用例无法证明它能抓到旧实现")
    assert first["legacy"] != second["legacy"], (
        "负例失效：旧实现（内置 hash() 播种）在两种 PYTHONHASHSEED 下样本相同 ⇒ 本用例抓不到它")

    #: 同一进程内、同 seed 必须可重复（播种不能退化成"每次调用换一个"）
    from sim.bridge.study import lhs_rng
    from sim.verify.sensitivity import latin_hypercube
    space = {"a": [1.0, 2.0], "b": [3.0, 4.0]}
    assert (latin_hypercube(space, 8, lhs_rng(space, 7)) == latin_hypercube(space, 8, lhs_rng(space, 7))), \
        "同 seed 两次抽样的样本集不同 ⇒ 播种不稳定"
    #: 不同 seed ⇒ 样本集必须不同（否则 --seed 这个旋钮是摆设）
    assert latin_hypercube(space, 8, lhs_rng(space, 7)) != latin_hypercube(space, 8, lhs_rng(space, 8)), \
        "不同 --seed 抽出了同一套样本 ⇒ --seed 对敏感性档无效"
    print(f"[T-SIM-06] LHS 播种跨进程稳定（PYTHONHASHSEED 0/1 样本一致）；"
          f"旧实现（内置 hash）实测两进程 hash={first['hash']} vs {second['hash']} ⇒ 样本不同（负例有效）")

