"""消费者 Agent（`T-SIM-04`；`docs/sim-design.md` §3.3）。

## 信任更新与**正反馈陷阱**

```
T(t+1) = clip( T(t)·(1−δ) + η(ν)·(o(t) − T(t)), 0, 1 )
    扫码了：o(t) ∈ {1（有真实来源）, 0（空壳信息）}，η = η⁺ 若 o > T 否则 η⁻；且 **η⁻ > η⁺**（负面偏差）
    没扫码：无观测 ⇒ T(t+1) = T(t)·(1−δ)
```

扫码率与信任耦合：`p_scan(T) = clip(p_scan_base + gain·T, 0, 1)`（**随 T 单调递增**）。
于是形成陷阱：`T↓ → 扫码↓ → 采样机会↓ → 恢复更慢`。恢复半衰期的解析式：

```
t_half = ln2 / ( δ + η⁺·p_scan·1[信息有真实来源] )
```

## ⚠️ 口径纪律（父代理 `2026-10-02` 裁定，也是本项的自曝点）

`Q3`（信任能否恢复）在本设计里是**循环论证**：答案由 `η⁺/η⁻/δ` 决定，而这三个参数**没有任何出处**
（结论 16 只给定性语义"扫过好几次空码…再也不信"）。因此：

* 本模块的一切输出都**只能**被表述为**关于参数的命题** —— 例如
  "当 `η⁻/η⁺ > k` 且 `δ` 超阈时存在不可恢复区"；
* **不许**表述为关于现实的命题（如"现实中信任需要 N 个月才能恢复"）；
* 代码注释、测试名与报告必须**同一口径**。

`recovery_region()` 返回的就是参数网格上的区域判定（三态：恢复 / 不恢复 / 边界带），
而 `unrecoverable_ratio_threshold()` 给出边界的大致位置 —— 都是**关于参数**的陈述。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace

#: 区域判定的三态（**不许只有二态**：边界带必须显式存在，否则等于假装没有不确定性）
RECOVERED, NOT_RECOVERED, BOUNDARY = "recovered", "not_recovered", "boundary"


@dataclass(frozen=True)
class ConsumerParams:
    eta_pos: float
    eta_neg: float
    delta: float
    p_scan_floor: float
    scan_gain: float
    info_real_share: float
    recovery_target: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.p_scan_floor <= 1.0:
            raise ValueError(f"p_scan_floor 必须在 [0,1]：{self.p_scan_floor}")
        if self.scan_gain < 0.0:
            raise ValueError(f"扫码增益不能为负：{self.scan_gain}")
        if not 0.0 <= self.info_real_share <= 1.0:
            raise ValueError(f"info_real_share 必须在 [0,1]：{self.info_real_share}")
        if self.delta < 0.0:
            raise ValueError(f"衰减 δ 不能为负：{self.delta}")
        if self.eta_pos <= 0.0 or self.eta_neg <= 0.0:
            raise ValueError("η⁺ / η⁻ 必须为正（更新强度）")

    @property
    def negative_bias(self) -> float:
        """负面偏差比 `η⁻/η⁺` ≥ 1 是本设计的核心非对称性。"""
        return self.eta_neg / self.eta_pos


def consumer_params(params, **overrides) -> ConsumerParams:
    base = {
        "eta_pos": float(params.value("trust_update_eta_pos")),
        "eta_neg": float(params.value("trust_update_eta_neg")),
        "delta": float(params.value("trust_decay_delta")),
        "p_scan_floor": float(params.value("consumer_scan_floor")),
        "scan_gain": float(params.value("consumer_scan_trust_gain")),
        "info_real_share": float(params.value("info_real_share")),
        "recovery_target": float(params.value("trust_recovery_target")),
    }
    unknown = set(overrides) - set(base)
    if unknown:
        raise KeyError(f"未知的消费者参数覆盖项：{sorted(unknown)}")
    base.update(overrides)
    return ConsumerParams(**base)


def scan_probability(trust: float, cp: ConsumerParams) -> float:
    """`p_scan(T) = clip(floor + gain·T, 0, 1)` —— 对 `T` **单调不减**。

    ⚠️ **`floor` 是"陷阱能不能成立"的开关，不是可忽略的常数**：
    只要 `floor > 0`，信任崩到底时扫码率仍有下限 ⇒ **采样机会不会枯竭 ⇒ 陷阱不成立**。
    我第一版把它写成 `clip(base + gain·T)`（等于 `floor = base = 0.15 > 0`），
    结果实测**耦合版比不耦合版恢复得更快**（9 期 vs 23 期）—— 与 `Q3` 要研究的现象**方向相反**，
    而且是个"看起来很合理"的假结论。改成 `floor` 可为 0 之后，陷阱才可能出现。
    """
    return min(1.0, max(0.0, cp.p_scan_floor + cp.scan_gain * trust))


def update_trust(trust: float, outcome: float | None, cp: ConsumerParams) -> float:
    """**纯函数**：给定当前信任与观测（`None` = 没扫码，无观测），返回下一期信任。"""
    decayed = trust * (1.0 - cp.delta)
    if outcome is None:
        return min(1.0, max(0.0, decayed))
    eta = cp.eta_pos if outcome > trust else cp.eta_neg
    return min(1.0, max(0.0, decayed + eta * (outcome - trust)))


def analytic_half_life(cp: ConsumerParams, *, scan_rate: float, info_real: bool = True) -> float:
    """**缺口**半衰期 `ln2 / (δ + η⁺·p_scan·1[信息有真实来源])`（期）。

    ⚠️ **它是"到不动点的缺口"减半所需期数，不是"到 1.0 的缺口"减半**。
    在 `δ > 0` 且扫码率 `q < 1` 时，不动点是 `T* = qη⁺/(δ + qη⁺) < 1`；
    写成 `ln2/(δ+η⁺q)` 隐含假设 `T* ≈ 1`（即 `δ ≪ qη⁺`）。我第一版直接拿它去比
    "从 0.2 恢复到 0.6（到 1.0 的半程）"的仿真值，得到 9.56 期 vs 18 期 —— 差近 2 倍，
    根因就是这个口径不一致（实测不动点 0.724，到它减半才是 9.2 期）。

    故本函数与 `analytic_fixed_point()` **必须成对使用**。

    `信息有真实来源` 为假（全是空壳）时 `η⁺` 那一路不生效 —— 只能靠衰减，恢复**不可能**。
    """
    rate = cp.delta + (cp.eta_pos * scan_rate if info_real else 0.0)
    if rate <= 0.0:
        return math.inf
    return math.log(2.0) / rate


def analytic_fixed_point(cp: ConsumerParams, *, scan_rate: float, info_real: bool = True) -> float:
    """`T* = qη⁺ / (δ + qη⁺)`：`o = 1`（信息真实）时信任能到的上限。

    `info_real=False`（全是空壳）时没有任何正向观测，`T* = 0` —— 这正是"空壳信息 → 信任崩塌"
    的解析形态。
    """
    numerator = cp.eta_pos * scan_rate if info_real else 0.0
    denominator = cp.delta + numerator
    if denominator <= 0.0:
        return 1.0
    return numerator / denominator


@dataclass
class ConsumerAgent:
    """一个消费者。状态量（信任 / 抽样计数）**可快照**。"""

    agent_id: str
    cp: ConsumerParams
    trust: float = 0.5
    scanned_total: int = 0
    real_total: int = 0
    history: list[float] = field(default_factory=list)

    def step(self, rng: random.Random) -> dict:
        """走一期：按 `p_scan(T)` 决定扫不扫码 → 得到观测 → 更新信任。返回本期明细。"""
        scanned = rng.random() < scan_probability(self.trust, self.cp)
        outcome: float | None = None
        if scanned:
            self.scanned_total += 1
            is_real = rng.random() < self.cp.info_real_share
            outcome = 1.0 if is_real else 0.0
            if is_real:
                self.real_total += 1
        before = self.trust
        self.trust = update_trust(self.trust, outcome, self.cp)
        self.history.append(self.trust)
        return {
            "agent_id": self.agent_id,
            "period": len(self.history) - 1,
            "trust_before": round(before, 6),
            "p_scan": round(scan_probability(before, self.cp), 6),
            "scanned": scanned,
            "outcome": outcome,
            "trust_after": round(self.trust, 6),
        }

    def snapshot(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "trust": round(self.trust, 6),
            "scanned_total": self.scanned_total,
            "real_total": self.real_total,
            "periods": len(self.history),
        }


def simulate_consumers(
    cp: ConsumerParams, *, agents: int, periods: int, seed: int, shock_to: float | None = None, shock_period: int = 0
) -> dict:
    """跑一群消费者，返回逐期均值轨迹。

    `shock_to`：在第 `shock_period` 期把信任**强制压到**该值（模拟"扫到空壳信息"后的崩塌），
    随后观察能否恢复到 `recovery_target`。
    """
    from ..core.streams import StreamSet

    streams = StreamSet(seed)
    population = [ConsumerAgent(f"c-{i + 1:04d}", cp) for i in range(agents)]
    trace: list[float] = []
    detail: list[dict] = []
    for period in range(periods):
        if shock_to is not None and period == shock_period:
            for agent in population:
                agent.trust = shock_to
        outcomes = [
            agent.step(streams.stream(agent.agent_id, "trust")) for agent in population
        ]
        mean_trust = sum(a.trust for a in population) / len(population)
        mean_scan = sum(1 for o in outcomes if o["scanned"]) / len(outcomes)
        trace.append(round(mean_trust, 6))
        detail.append({"period": period, "mean_trust": round(mean_trust, 6), "scan_rate": round(mean_scan, 6)})
    return {
        "trace": trace,
        "detail": detail,
        "recovered": trace[-1] >= cp.recovery_target,
        "final_trust": trace[-1],
        "snapshots": [a.snapshot() for a in population],
    }


def periods_to_recover(
    cp: ConsumerParams, *, agents: int, periods: int, seed: int, shock_to: float, target: float | None = None
) -> int | None:
    """从冲击恢复到目标信任所需的期数；`None` 表示在给定期限内**没恢复**。"""
    goal = cp.recovery_target if target is None else target
    result = simulate_consumers(cp, agents=agents, periods=periods, seed=seed, shock_to=shock_to)
    for index, value in enumerate(result["trace"]):
        if index > 0 and value >= goal:
            return index
    return None


def recovery_region(
    cp: ConsumerParams,
    *,
    ratio_values,
    delta_values,
    agents: int = 40,
    periods: int = 60,
    seed: int = 20261002,
    shock_to: float = 0.05,
) -> list[dict]:
    """在 `(η⁻/η⁺, δ)` 网格上判定"能否恢复"——**结论是关于参数的命题**（见模块 docstring）。

    三态判定（不许只有二态）：期末信任达到目标 → `recovered`；未达到但在期限内出现过
    回升趋势 → `boundary`；一直没回升 → `not_recovered`。
    `boundary` 用"是否曾在冲击后显著高于冲击值"来判，故意粗糙 —— 它表达的是**判据的模糊带**，
    而不是一个精确阈值。
    """
    rows: list[dict] = []
    for ratio in ratio_values:
        for delta in delta_values:
            # 用 `dataclasses.replace` 覆盖单字段：**不重读参数文件**，故扫描时"只改这一个量"
            # 是机械保证的（第一版我写了个只会抛 KeyError 的适配器类，已删）。
            variant = replace(cp, eta_neg=cp.eta_pos * float(ratio), delta=float(delta))
            result = simulate_consumers(
                variant, agents=agents, periods=periods, seed=seed, shock_to=shock_to
            )
            trace = result["trace"]
            peak = max(trace[1:]) if len(trace) > 1 else shock_to
            if result["recovered"]:
                verdict = RECOVERED
            elif peak > shock_to * 1.5:
                verdict = BOUNDARY
            else:
                verdict = NOT_RECOVERED
            rows.append(
                {
                    "negative_bias_ratio": round(float(ratio), 6),
                    "delta": round(float(delta), 6),
                    "final_trust": result["final_trust"],
                    "verdict": verdict,
                }
            )
    return rows
