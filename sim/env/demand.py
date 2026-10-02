"""客流到达过程：营业日 → 时段块（`docs/sim-design.md` §2.2/§4）。

## 口径（每一步都可由明细复算，`T-SIM-02` 验收④）

- 参数 `daily_arrivals_per_market` 给出**整日期望到达数**（`V_arr`）；
- 时段块强度来自 `params.json` 的 `blocks[].intensity`（相对强度，无量纲）；
- 于是某块的期望到达数 `lambda_block = V_arr × intensity_block / Σ intensity` —— **按总强度归一**，
  这样"多加一个时段块"不会凭空抬高整日期望（否则改结构就等价于偷偷改客流总量，
  参数扫描立刻失去可比性）。

## 为什么用 Knuth 的泊松抽样而不是"取整正态近似"

计数是离散的，取整会引入零点附近的偏差；Knuth 算法只用标准库、无依赖，且在
`lambda` 为数百时仍是 O(lambda)，对 90 营业日 × 4 块的规模完全够用。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


def poisson(rng: random.Random, lam: float) -> int:
    """泊松抽样（Knuth）。`lam` 很大时逐项相乘会下溢，故 `lam > 30` 走正态近似分支。"""
    if lam <= 0:
        return 0
    if lam > 30.0:
        # 正态近似（均值=方差=lam），取整后夹到非负
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    limit = math.exp(-lam)
    product = 1.0
    count = -1
    while product > limit:
        count += 1
        product *= rng.random()
    return count


@dataclass(frozen=True)
class BlockArrivals:
    """一个时段块的到达口径与抽样结果。`lam` 是**期望**（可复算），`count` 是**实测**。"""

    block: str
    start: str
    intensity: float
    lam: float
    count: int


def block_lambdas(blocks, daily_arrivals: float) -> list[tuple[str, str, float, float]]:
    """由时段块强度算出各块期望到达数 `[(名称, 起始, 强度, lambda)]`。**纯函数**，可复算判据用它。"""
    if daily_arrivals < 0:
        raise ValueError(f"整日期望到达数不能为负：{daily_arrivals}")
    total_intensity = sum(b.intensity for b in blocks)
    if total_intensity <= 0:
        raise ValueError("时段块总强度为 0：无法分配客流（检查 params.json 的 blocks 强度）")
    return [
        (b.name, b.start, float(b.intensity), daily_arrivals * float(b.intensity) / total_intensity)
        for b in blocks
    ]


def day_arrivals(blocks, daily_arrivals: float, rng: random.Random) -> list[BlockArrivals]:
    """按块的期望值逐个抽样，得到当日各块到达数。"""
    return [
        BlockArrivals(block=name, start=start, intensity=intensity, lam=lam, count=poisson(rng, lam))
        for name, start, intensity, lam in block_lambdas(blocks, daily_arrivals)
    ]


def split_on_scale(total: int, on_scale_rate: float, rng: random.Random) -> tuple[int, int]:
    """把当日到达拆成"走秤"与"私下"两路。

    **守恒**：两者之和**恒等于** `total`（逐笔判定，不是两次独立抽样）——
    两次独立抽样会得到"和不一定等于总数"，那正是守恒断言要防的错。
    """
    if not 0.0 <= on_scale_rate <= 1.0:
        raise ValueError(f"走秤比例必须在 [0,1]，收到 {on_scale_rate}")
    on_scale = sum(1 for _ in range(total) if rng.random() < on_scale_rate)
    return on_scale, total - on_scale
