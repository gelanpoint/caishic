"""设备机队：故障 → 报修队列 → 维修工台（`docs/sim-design.md` §3.6）。

## 三态与守恒

每台设备任一时刻恰处于三态之一：`working`（在用）、`waiting`（已报修、等工台）、`repairing`（维修中）。
**三态计数之和恒等于机队总数**，且总数不随时间变化 —— 这是 `T-SIM-02` 验收②的守恒判据。
机队只增不改的转移全程落在事件日志里，故守恒可由明细**独立复算**，不是"代码自己说没问题"。

## 为什么故障与维修用两条独立的流

故障是"设备自己的事"，维修时长是"工台的事"。共用一条流会让"多一台设备"改变维修时长，
把两类不同性质的随机性搅在一起（`T-SIM-01` 的 `test_adding_an_agent_does_not_shift_other_agents`
验的就是这条纪律）。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

WORKING = "working"
WAITING = "waiting"
REPAIRING = "repairing"


@dataclass
class DeviceFleet:
    """机队。设备 id 形如 `DEV-001`；**按 id 升序处理**，保证跨进程可复现。"""

    total: int
    mtbf_days: float
    repair_mean_days: float
    states: dict[str, str] = field(default_factory=dict)
    _remaining_repair: dict[str, int] = field(default_factory=dict)
    breakdowns_total: int = 0
    repairs_done_total: int = 0
    device_days_lost_total: int = 0

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError(f"机队规模不能为负：{self.total}")
        if self.mtbf_days <= 0:
            raise ValueError(f"MTBF 必须为正（天）：{self.mtbf_days}")
        if self.repair_mean_days <= 0:
            raise ValueError(f"平均维修时长必须为正（天）：{self.repair_mean_days}")
        if not self.states:
            self.states = {f"DEV-{i + 1:03d}": WORKING for i in range(self.total)}

    # -- 查询 -----------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        """三态计数。**不做任何缓存**：缓存一旦忘记失效，守恒断言就会变成自说自话。"""
        out = {WORKING: 0, WAITING: 0, REPAIRING: 0}
        for state in self.states.values():
            out[state] += 1
        return out

    def conservation_holds(self) -> bool:
        return sum(self.counts().values()) == len(self.states)

    def ids_in(self, state: str) -> list[str]:
        return sorted(dev for dev, st in self.states.items() if st == state)

    # -- 推进 -----------------------------------------------------------------
    def step_day(
        self,
        breakdown_rng: random.Random,
        repair_rng: random.Random,
        max_admissions_per_day: int | None = None,
    ) -> dict[str, int]:
        """推进一天：先完成维修 → 再让在用设备按 `p = 1/MTBF` 故障。

        顺序是刻意的：当天修好的设备当天就可以再坏（对 MTBF 的语义更贴近"连续时间"），
        且顺序固定 → 同 seed 可复现。

        `max_admissions_per_day`（`T-SIM-05` 新增，**缺省 `None` = 不限产能 ⇒ 本项原有行为逐字段不变**）：
        每天最多从报修队列放进工台几台，把"**维护预算 → 每天能开工几台**"接进模型。
        预算为 0 ⇒ 一台都放不进去 ⇒ 设备**永远停在报修队列** —— 这就是"设备闲置"的机制形态，
        而不是"修得慢一点"（绵阳=可用但不用 / 武汉=黑屏不可用，见结论 6/4/5）。
        按设备 id 升序判定，故同 seed 可复现。
        """
        repaired_today = 0
        broke_today = 0

        for device_id in self.ids_in(REPAIRING):
            remaining = self._remaining_repair[device_id] - 1
            if remaining <= 0:
                self._remaining_repair.pop(device_id, None)
                self.states[device_id] = WORKING
                self.repairs_done_total += 1
                repaired_today += 1
            else:
                self._remaining_repair[device_id] = remaining

        break_probability = 1.0 / self.mtbf_days
        for device_id in self.ids_in(WORKING):
            if breakdown_rng.random() < break_probability:
                self.states[device_id] = WAITING
                self.breakdowns_total += 1
                broke_today += 1

        # 报修队列 → 维修工台：产能受**维护预算**约束
        # `max_admissions_per_day=None` = 不限产能 ⇒ `T-SIM-02` 的原有行为**逐字段不变**。
        waiting = self.ids_in(WAITING)
        if max_admissions_per_day is None:
            admitted = waiting
        else:
            if max_admissions_per_day < 0:
                raise ValueError(f"每天可开班数不能为负：{max_admissions_per_day}")
            admitted = waiting[: int(max_admissions_per_day)]
        for device_id in admitted:
            self.states[device_id] = REPAIRING
            self._remaining_repair[device_id] = self._draw_repair_days(repair_rng)

        counts = self.counts()
        self.device_days_lost_total += counts[WAITING] + counts[REPAIRING]
        if not self.conservation_holds():
            raise RuntimeError(f"设备守恒被破坏：{counts} 之和 ≠ 机队规模 {len(self.states)}")
        return {
            "working": counts[WORKING],
            "waiting": counts[WAITING],
            "repairing": counts[REPAIRING],
            "breakdowns": broke_today,
            "repaired": repaired_today,
            "admitted": len(admitted),
        }

    def _draw_repair_days(self, rng: random.Random) -> int:
        """维修时长：均值 `repair_mean_days` 的指数分布，至少 1 天（当天不可能修好）。"""
        draw = rng.expovariate(1.0 / self.repair_mean_days)
        return max(1, int(math.ceil(draw)))
