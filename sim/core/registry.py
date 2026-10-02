"""agent 注册表：仿真里"有谁"的唯一登记处。

`T-SIM-01` 阶段只登记**骨架占位 agent**（证明分流与事件落盘可用）；
`T-SIM-02` 之后由环境层（`sim/env/`）按市场/摊位/客流登记真实 agent —— 届时本模块不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: agent 种类（与 `docs/sim-design.md` §3 的四类一一对应）
KINDS = ("merchant", "consumer", "market_admin", "regulator")


@dataclass(frozen=True)
class AgentRef:
    """一个 agent 的稳定标识。`agent_id` 是随机流分流的键，**必须稳定且唯一**。"""

    agent_id: str
    kind: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"未登记的 agent 种类 {self.kind!r}；允许：{list(KINDS)}")
        if not self.agent_id:
            raise ValueError("agent_id 不能为空（随机流靠它分流）")


@dataclass
class Registry:
    """agent 登记表。**同名重复登记直接报错**：id 撞车会让两条流合并，随机性静默退化。"""

    _by_id: dict[str, AgentRef] = field(default_factory=dict)

    def register(self, agent_id: str, kind: str) -> AgentRef:
        if agent_id in self._by_id:
            raise ValueError(f"agent_id 重复登记：{agent_id!r}")
        ref = AgentRef(agent_id, kind)
        self._by_id[agent_id] = ref
        return ref

    def get(self, agent_id: str) -> AgentRef:
        return self._by_id[agent_id]

    def by_kind(self, kind: str) -> list[AgentRef]:
        return [ref for ref in self._by_id.values() if ref.kind == kind]

    def all(self) -> list[AgentRef]:
        return list(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)
