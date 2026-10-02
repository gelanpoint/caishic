"""仿真时钟：营业日 → 时段块 → 周 → 月（`docs/sim-design.md` §4）。

**与系统 `business_date` 同轴**：仿真时钟产出的日期字符串就是主系统业务日字段的取值，
`--mode=live` 时由 `MT_CLOCK_FILE`（`REQ-033`）把它喂给被测系统 —— 两边同一套日期，
否则日聚合与结算根本对不上。

时段块的**取值不写死在本模块**：由 `sim/calibration/params.json` 提供（有出处/假设都标在那里），
本模块只负责结构与推进 —— 同一个数值写两遍就是下次漂移的种子。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

#: 一个营业日的推进粒度（`T3` 周 / `T4` 月 / `T5` 季）
DAYS_PER_MONTH = 30
DAYS_PER_QUARTER = 90


@dataclass(frozen=True)
class Block:
    """一个时段块：名称 + 起始时刻 + 相对客流强度（`lambda` 的乘子）。"""

    name: str
    start: str
    intensity: float


@dataclass
class SimClock:
    """营业日时钟。`start` 为第一个营业日；`days` 为营业日总数。"""

    start: date
    days: int
    blocks: tuple[Block, ...]

    def __post_init__(self) -> None:
        if self.days < 1:
            raise ValueError(f"营业日数必须 ≥1，收到 {self.days}")
        if not self.blocks:
            raise ValueError("时段块不能为空（取值来自 params.json）")
        if any(b.intensity < 0 for b in self.blocks):
            raise ValueError("时段块强度不能为负")

    def business_date(self, day_index: int) -> str:
        """第 `day_index` 个营业日（0 起）的 `YYYY-MM-DD` —— 直接可喂给主系统。"""
        if not 0 <= day_index < self.days:
            raise IndexError(f"营业日下标越界：{day_index}（共 {self.days} 日）")
        return (self.start + timedelta(days=day_index)).isoformat()

    def business_day_sequence(self) -> list[str]:
        return [self.business_date(i) for i in range(self.days)]

    def iter_days(self):
        """产 `(day_index, business_date, 是否月末, 是否季末)`。"""
        for index in range(self.days):
            yield (
                index,
                self.business_date(index),
                (index + 1) % DAYS_PER_MONTH == 0,
                (index + 1) % DAYS_PER_QUARTER == 0,
            )

    def total_intensity(self) -> float:
        return sum(b.intensity for b in self.blocks)

