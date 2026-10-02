"""确定性随机流：**每 agent、每用途一条独立子流**（`docs/sim-design.md` §2.4）。

## 为什么必须分流（不是可选的整洁）

若所有 agent 共用一个 `random.Random`，那么"多抽一个消费者"就会**平移后续所有随机数** ——
于是 A/B 两个场景的差异里混进了随机噪声，单因子对照实验直接失效。
分流后：**同一 `seed` 下，同一 agent 同一用途的随机数完全一致**，场景之间只差被显式改掉的那个参数。

## 为什么用 `hashlib` 而不是内置 `hash()`

内置 `hash()` 对字符串加盐（`PYTHONHASHSEED`），**跨进程不稳定** —— 而"同 seed 逐字节可复现"
是本仿真的地基判据（`T-SIM-01` 验收①）。`hashlib.sha256` 跨进程、跨机器稳定。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

#: 用途标签（分开取流，避免"多加一次抽样就改变别人结果"）
PURPOSES = (
    "arrival",  # 客流到达
    "choice",  # 消费者选摊
    "cheat",  # 商户作弊/短秤决策
    "trust",  # 消费者信任更新
    "breakdown",  # 设备故障
    "repair",  # 维修时长
    "adapt",  # 学习/探索噪声
)


def derive_seed(seed: int, agent_id: str, purpose: str) -> int:
    """由 (seed, agent_id, purpose) 稳定派生一个 64 位整数种子。"""
    material = f"{int(seed)}|{agent_id}|{purpose}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


@dataclass
class StreamSet:
    """按 (agent_id, purpose) 惰性构造并缓存 `random.Random` 实例。

    **缓存是刻意的**：同一个 (agent, purpose) 反复取流必须拿到同一个序列推进位置，
    否则"每笔交易重新播种"会让随机数退化成常数。
    """

    seed: int
    _cache: dict[tuple[str, str], random.Random] = field(default_factory=dict)

    def stream(self, agent_id: str, purpose: str) -> random.Random:
        if purpose not in PURPOSES:
            raise ValueError(f"未登记的用途 {purpose!r}；允许：{list(PURPOSES)}")
        key = (agent_id, purpose)
        rng = self._cache.get(key)
        if rng is None:
            rng = random.Random(derive_seed(self.seed, agent_id, purpose))
            self._cache[key] = rng
        return rng

    def independent_of_agent_count(self, purpose: str, agent_id: str, draws: int = 1) -> float:
        """取某 agent 某用途的一个 [0,1) 值。

        存在意义：让"**加一个 agent 不影响其他 agent 的随机数**"成为可断言的属性
        （`tests/sim/test_seed_reproducibility.py` 用它做对照）。
        """
        rng = self.stream(agent_id, purpose)
        value = rng.random()
        for _ in range(draws - 1):
            value = rng.random()
        return value
