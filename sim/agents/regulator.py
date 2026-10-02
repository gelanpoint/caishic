"""监管 Agent：抽检 → 发现 → 罚金（`T-SIM-05`；`docs/sim-design.md` §3.5）。

## 它在机制链里的位置

监管的抽检率就是商户效用里的 `p_check`（"被抓的概率"），故它与 `T-SIM-03` 的商户效用**直接相连**：
抽检率一升，`Ψ = p_check·(F_short + Loss_rep)` 就升，转暗的吸引力下降。
本模块只负责"抽检与发现"这条链，**不重复实现商户决策**。

## 口径纪律

抽检率、罚款额都**没有本情形的公开数据**（结论 2 罚的是市场开办者、事由是明码标价；
结论 4 的 10% 是**验收**抽检）—— 故本模块的判据**只允许序关系**：
抽检率↑ ⇒ 发现数不降；覆盖率↑ ⇒ 发现数不降。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RegulatorParams:
    inspection_rate: float
    fine_short_cents: float
    fine_market_operator_cents: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.inspection_rate <= 1.0:
            raise ValueError(f"抽检率必须在 [0,1]：{self.inspection_rate}")
        if self.fine_short_cents < 0 or self.fine_market_operator_cents < 0:
            raise ValueError("罚款额不能为负")


def regulator_params(params, **overrides) -> RegulatorParams:
    base = {
        "inspection_rate": float(params.value("daily_inspection_rate")),
        "fine_short_cents": float(params.value("merchant_fine_short_cents")),
        "fine_market_operator_cents": float(params.value("admin_fine_market_operator_cents")),
    }
    unknown = set(overrides) - set(base)
    if unknown:
        raise KeyError(f"未知的监管参数覆盖项：{sorted(unknown)}")
    base.update(overrides)
    return RegulatorParams(**base)


@dataclass
class RegulatorAgent:
    """监管。状态量（累计抽检/发现/罚金）**可快照**。"""

    rp: RegulatorParams
    inspected_total: int = 0
    findings_total: int = 0
    fines_short_cents: float = 0.0
    fines_operator_cents: float = 0.0
    history: list[dict] = field(default_factory=list)

    def inspect_period(self, rng: random.Random, *, stalls_in_scope: int, offenders: int, period: int) -> dict:
        """抽检一期。

        `stalls_in_scope`：可被抽检的摊位总数；`offenders`：其中真正在短秤的摊位数。
        抽检是**不放回**地从在营摊位里抽 `ceil(rate × stalls)` 个；发现数 = 抽中的违规摊位数。

        为什么用"不放回抽样"而不是"逐个独立伯努利"：抽检的**最小单位是摊位**，
        同一期不会把同一个摊位抽两次 —— 独立伯努利会重复计同一摊位，把发现数算高。
        """
        if stalls_in_scope < 0 or offenders < 0:
            raise ValueError("摊位数与违规数不能为负")
        if offenders > stalls_in_scope:
            raise ValueError(f"违规摊位数 {offenders} 不能超过可抽检摊位总数 {stalls_in_scope}")
        sample_size = min(stalls_in_scope, int(-(-self.rp.inspection_rate * stalls_in_scope // 1)))
        picked = rng.sample(range(stalls_in_scope), sample_size) if sample_size else []
        # 前 `offenders` 个摊位是违规的（调用方按 id 升序传入违规摊位，顺序固定 ⇒ 可复现）
        findings = sum(1 for index in picked if index < offenders)

        self.inspected_total += sample_size
        self.findings_total += findings
        self.fines_short_cents += findings * self.rp.fine_short_cents
        # 场内屡次违规 → 罚市场开办者（结论 2 的实际情形）
        operator_fine = self.rp.fine_market_operator_cents if findings > 0 else 0.0
        self.fines_operator_cents += operator_fine

        record = {
            "period": period,
            "stalls_in_scope": stalls_in_scope,
            "offenders": offenders,
            "inspected": sample_size,
            "findings": findings,
            "fine_short_cents": round(findings * self.rp.fine_short_cents, 6),
            "fine_operator_cents": round(operator_fine, 6),
        }
        self.history.append(record)
        return record

    def snapshot(self) -> dict:
        return {
            "inspection_rate": self.rp.inspection_rate,
            "inspected_total": self.inspected_total,
            "findings_total": self.findings_total,
            "fines_short_cents": round(self.fines_short_cents, 6),
            "fines_operator_cents": round(self.fines_operator_cents, 6),
            "periods": len(self.history),
        }


def sweep_inspection_rate(
    params, rates, *, periods: int, seed: int, stalls_in_scope: int, offenders: int
) -> list[dict]:
    """**单因子扫描**：只改抽检率，看累计发现数怎么变（只给序关系，不给绝对阈值）。"""
    from ..core.streams import StreamSet

    out: list[dict] = []
    for rate in rates:
        rp = regulator_params(params, inspection_rate=float(rate))
        agent = RegulatorAgent(rp)
        streams = StreamSet(seed)
        for period in range(periods):
            agent.inspect_period(
                streams.stream("regulator", "adapt"),
                stalls_in_scope=stalls_in_scope,
                offenders=offenders,
                period=period,
            )
        out.append(
            {
                "inspection_rate": float(rate),
                "inspected_total": agent.inspected_total,
                "findings_total": agent.findings_total,
                "fines_short_cents": round(agent.fines_short_cents, 6),
            }
        )
    return out
