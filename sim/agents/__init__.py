"""`sim/agents/`：决策机制层（`T-SIM-03` 商户 / `T-SIM-04` 消费者）。

**为什么与环境层（`sim/env/`）分开**：环境是"世界长什么样"，机制是"人怎么反应"。
分开才做得到**只换策略、不换环境**的单因子对照 —— 否则"商户变了"和"客流变了"永远搅在一起。
"""

from __future__ import annotations

from .consumer import ConsumerAgent, ConsumerParams, consumer_params
from .merchant import MerchantAgent, MerchantParams, merchant_params

__all__ = [
    "ConsumerAgent",
    "ConsumerParams",
    "MerchantAgent",
    "MerchantParams",
    "consumer_params",
    "merchant_params",
]
