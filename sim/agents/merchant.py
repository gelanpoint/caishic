"""商户 Agent（`T-SIM-03`；`docs/sim-design.md` §3.2）。

## 机制链（本项的核心，必须**能被单独观测**）

效用四项分解：`U = Π − Λ − Ψ − Ω − Σ_exit·1[a=exit]`

| 项 | 合规 `comply` | 转暗 `evade` |
| --- | --- | --- |
| 收入 `Π` | `V·g` | `V·(g + (1−g)·d)` ⇒ **短秤增益 `ΔΠ = V·(1−g)·d`** |
| 成本 `Λ` | `V·s·rate + 设备分摊 + f_op·走秤笔数` | `设备分摊`（**私下交易不产生走秤流水 ⇒ 不抽佣**） |
| 罚金 `Ψ` | `0` | `p_check·(F_short + Loss_rep)` |
| 声誉 `Ω` | `0` | `κ_detect · p_detect · V · loss_share` |

**关键耦合**：佣金只按**走秤流水**计 ⇒ **抽佣同时让合规变贵（`Λ` 上升）、让转暗相对更划算
（`Λ` 不变）**。这就是 `Q1`（抽佣→流失）的机制，也是它与 `Q2`（堵八股秤）互相牵制的接点 ——
`p_detect` 一升，`Ω` 就压住 `evade`。因此本模块**必须输出三路径各自的家数与四项均值**，
只给"最终流失率"等于把机制藏起来。

## 口径纪律

`Q3` 之外的另一处诚实要求：`ΔΠ` 是**恒等式**（可精确验证），而"多久出现流失"依赖一批
**无出处参数**（`g`/`d`/`p_check`/`κ_detect`/退出阈值…）。故本模块的判据分两类：

* **恒等式类** → 允许精确断言（如 `ΔΠ`、佣金通道）；
* **行为类** → 只允许**序关系**（抽佣率↑ ⇒ 合规占比不升、转暗占比不降），**禁止**绝对月份/比率。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

#: 三个动作。`exit` 是**吸收态**，由 EWMA 破线规则触发（不是效用最大化选出来的）
ACTIONS = ("comply", "evade", "exit")
COMPLY, EVADE, EXIT = ACTIONS


@dataclass(frozen=True)
class MerchantTerms:
    """某项动作的效用分解（单位：分/期）。`utility` 就是 `U`。"""

    revenue: float
    cost: float
    penalty: float
    trust_loss: float
    exit_cost: float = 0.0

    @property
    def utility(self) -> float:
        return self.revenue - self.cost - self.penalty - self.trust_loss - self.exit_cost

    def as_dict(self) -> dict[str, float]:
        return {
            "revenue": round(self.revenue, 6),
            "cost": round(self.cost, 6),
            "penalty": round(self.penalty, 6),
            "trust_loss": round(self.trust_loss, 6),
            "exit_cost": round(self.exit_cost, 6),
            "utility": round(self.utility, 6),
        }


@dataclass(frozen=True)
class MerchantParams:
    """商户决策所需的全部取值（**逐项来自 `params.json`**，本模块不写数值常量）。"""

    gross_margin_rate: float
    short_weight_ratio: float
    commission_rate_bp: float
    commission_charged_to_merchant: bool
    device_share_cents: float
    op_fee_per_txn_cents: float
    p_check: float
    fine_short_cents: float
    loss_rep_cents: float
    detect_coef: float
    p_detect: float
    loss_share: float
    exit_cost_cents: float
    exit_reference_point: float
    exit_breach_periods: int
    q_phi: float
    q_rho: float
    q_temperature: float


def merchant_params(params, **overrides) -> MerchantParams:
    """从 `Params` 取参数；`overrides` 用于**单因子扫描**（只改一个量，其余不动）。"""
    base = {
        "gross_margin_rate": float(params.value("gross_margin_rate")),
        "short_weight_ratio": float(params.value("short_weight_ratio")),
        "commission_rate_bp": float(params.value("commission_rate_bp")),
        "commission_charged_to_merchant": bool(params.value("commission_charged_to_merchant")),
        "device_share_cents": float(params.value("merchant_device_share_cents")),
        "op_fee_per_txn_cents": float(params.value("merchant_op_fee_cents_per_txn")),
        "p_check": float(params.value("daily_inspection_rate")),
        "fine_short_cents": float(params.value("merchant_fine_short_cents")),
        "loss_rep_cents": float(params.value("merchant_loss_rep_cents")),
        "detect_coef": float(params.value("merchant_detect_coef")),
        "p_detect": float(params.value("short_weight_detect_rate")),
        "loss_share": float(params.value("merchant_loss_share")),
        "exit_cost_cents": float(params.value("merchant_exit_cost_cents")),
        "exit_reference_point": float(params.value("merchant_exit_reference_point")),
        "exit_breach_periods": int(params.value("merchant_exit_breach_periods")),
        "q_phi": float(params.value("merchant_q_phi")),
        "q_rho": float(params.value("merchant_q_rho")),
        "q_temperature": float(params.value("merchant_q_temperature")),
    }
    unknown = set(overrides) - set(base)
    if unknown:
        raise KeyError(f"未知的商户参数覆盖项：{sorted(unknown)}")
    base.update(overrides)
    return MerchantParams(**base)


def merchant_terms(action: str, mp: MerchantParams, *, volume_cents: float, scale_txn_count: int) -> MerchantTerms:
    """**纯函数**：给定动作与期规模，算出四项分解。机制链的全部内容都在这里。

    `volume_cents` 是该期营业额（按标称重量计）；`scale_txn_count` 是**走秤**笔数
    （私下交易不产生走秤记录 ⇒ 抽佣与单笔运营费都只按它计）。
    """
    if action == EXIT:
        # 退出：本模块把它的即期效用记作 `−Σ_exit`；动作选择不靠效用最大化，而靠 EWMA 破线规则
        return MerchantTerms(0.0, 0.0, 0.0, 0.0, exit_cost=mp.exit_cost_cents)
    if action == COMPLY:
        revenue = volume_cents * mp.gross_margin_rate
        commission = (
            volume_cents * mp.commission_rate_bp / 10000.0 if mp.commission_charged_to_merchant else 0.0
        )
        cost = commission + mp.device_share_cents + mp.op_fee_per_txn_cents * scale_txn_count
        return MerchantTerms(revenue, cost, 0.0, 0.0)
    if action == EVADE:
        # 短秤收益：多出来的那部分重量按成本价卖出去 ⇒ 增益 = V·(1−g)·d
        revenue = volume_cents * (mp.gross_margin_rate + (1.0 - mp.gross_margin_rate) * mp.short_weight_ratio)
        # 转暗 ⇒ 无走秤流水 ⇒ **不抽佣**，但设备已装故仍分摊
        cost = mp.device_share_cents
        penalty = mp.p_check * (mp.fine_short_cents + mp.loss_rep_cents)
        trust_loss = mp.detect_coef * mp.p_detect * volume_cents * mp.loss_share
        return MerchantTerms(revenue, cost, penalty, trust_loss)
    raise ValueError(f"未登记的动作：{action!r}（允许 {list(ACTIONS)}）")


@dataclass
class MerchantAgent:
    """一个商户。状态量（Q 表 / EWMA / 破线计数 / 退出标记）**全部可快照**。"""

    agent_id: str
    mp: MerchantParams
    q: dict[str, float] = field(default_factory=lambda: {COMPLY: 0.0, EVADE: 0.0})
    ewma_utility: float = 0.0
    breach_streak: int = 0
    exited: bool = False
    last_action: str | None = None
    periods_observed: int = 0

    # -- 决策 -----------------------------------------------------------------
    def choose(self, rng: random.Random) -> str:
        """softmax 在 `comply` / `evade` 上选一个（`exit` 不参与，见模块 docstring）。"""
        if self.exited:
            return EXIT
        temperature = max(self.mp.q_temperature, 1e-9)
        weights = {a: math.exp(self.q[a] / temperature) for a in (COMPLY, EVADE)}
        total = weights[COMPLY] + weights[EVADE]
        threshold = rng.random() * total
        return COMPLY if threshold < weights[COMPLY] else EVADE

    def observe(self, action: str, terms: MerchantTerms, volume_cents: float, peer_mean_q: float = 0.0) -> None:
        """按当期结果更新 Q 表、EWMA 与破线计数；破线达到阈值则**进入退出（吸收态）**。

        ⚠️ **两条判据用两种量纲，这是刻意的，不是疏漏**：

        * **Q 学习**用**每元流水效用**（`utility / volume_cents`）：Q 要在不同规模的摊位之间可比，
          否则 `q_temperature` 没法解释（大摊位的绝对效用天然更大）；
        * **退出判据**用**绝对效用**（分/期）对比 `exit_reference_point`（分/期）：
          "赚得够不够活下去"是**绝对**概念，与摊位规模无关。

        我第一版把两处都写成"每元"，于是拿 `0.2`（每元）去比 `1000000`（分/期）——
        **所有商户在第 2 期就全部"退出"**，三条路径又退化成一条。量纲混用不会报错，只会给出漂亮而错误的结论。
        """
        if volume_cents <= 0:
            raise ValueError(f"volume_cents 必须为正（归一化用它做分母），收到 {volume_cents}")
        normalized = terms.utility / volume_cents
        if action in self.q:
            self.q[action] = (
                self.mp.q_phi * self.q[action]
                + (1.0 - self.mp.q_phi) * normalized
                + self.mp.q_rho * (1.0 - self.mp.q_phi) * peer_mean_q
            )
        self.periods_observed += 1

        if action != EXIT:
            # 绝对量纲（分/期）—— 与 exit_reference_point 同量纲
            absolute = terms.utility
            self.ewma_utility = (
                absolute if self.periods_observed <= 1 else 0.5 * self.ewma_utility + 0.5 * absolute
            )
            if self.ewma_utility < self.mp.exit_reference_point:
                self.breach_streak += 1
            else:
                self.breach_streak = 0
            if self.breach_streak >= self.mp.exit_breach_periods:
                self.exited = True
        self.last_action = action

    def step(self, rng: random.Random, *, volume_cents: float, scale_txn_count: int, peer_mean_q: float = 0.0):
        """走一个期：选动作 → 算分解 → 更新状态。返回 `(action, terms)`。"""
        action = self.choose(rng)
        terms = merchant_terms(action, self.mp, volume_cents=volume_cents, scale_txn_count=scale_txn_count)
        self.observe(action, terms, volume_cents, peer_mean_q)
        return action, terms

    def snapshot(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "q": {k: round(v, 6) for k, v in sorted(self.q.items())},
            "ewma_utility": round(self.ewma_utility, 6),
            "breach_streak": self.breach_streak,
            "exited": self.exited,
            "last_action": self.last_action,
            "periods_observed": self.periods_observed,
        }


@dataclass
class PopulationStep:
    """一期的群体结果：**三路径家数 + 四项均值** —— 机制的可观测输出。"""

    period: int
    shares: dict[str, int]
    mean_terms: dict[str, dict[str, float]]
    exited_total: int

    def as_dict(self) -> dict:
        total = max(1, sum(self.shares.values()))
        return {
            "period": self.period,
            "shares": dict(sorted(self.shares.items())),
            "share_ratio": {k: round(v / total, 6) for k, v in sorted(self.shares.items())},
            "mean_terms": self.mean_terms,
            "exited_total": self.exited_total,
        }


def simulate_population(
    mp: MerchantParams,
    *,
    agents: int,
    periods: int,
    seed: int,
    volume_cents: float,
    scale_txn_count: int,
) -> list[PopulationStep]:
    """跑一群商户。**peer_mean_q** 取上一期全群 Q 均值（同伴影响 ρ）。"""
    from ..core.streams import StreamSet

    streams = StreamSet(seed)
    population = [MerchantAgent(f"m-{i + 1:03d}", mp) for i in range(agents)]
    steps: list[PopulationStep] = []
    for period in range(periods):
        peer_mean_q = (
            0.0
            if period == 0
            else sum(sum(a.q.values()) / len(a.q) for a in population) / len(population)
        )
        shares = {COMPLY: 0, EVADE: 0, EXIT: 0}
        buckets: dict[str, list[MerchantTerms]] = {COMPLY: [], EVADE: [], EXIT: []}
        for agent in population:
            action, terms = agent.step(
                streams.stream(agent.agent_id, "cheat"),
                volume_cents=volume_cents,
                scale_txn_count=scale_txn_count,
                peer_mean_q=peer_mean_q,
            )
            shares[action] += 1
            buckets[action].append(terms)
        steps.append(
            PopulationStep(
                period=period,
                shares=shares,
                mean_terms={
                    action: {k: round(sum(getattr(t, k) for t in bucket_list) / max(1, len(bucket_list)), 6)
                             for k in ("revenue", "cost", "penalty", "trust_loss", "utility")}
                    for action, bucket_list in buckets.items()
                },
                exited_total=sum(1 for a in population if a.exited),
            )
        )
    return steps


def summarize(steps: list[PopulationStep]) -> dict:
    """把一段期序列压成"最终三路径占比 + 均值"，供扫描对照使用。"""
    last = steps[-1].as_dict()
    return {
        "periods": len(steps),
        "final_share_ratio": last["share_ratio"],
        "final_shares": last["shares"],
        "final_mean_terms": last["mean_terms"],
        "exited_total": last["exited_total"],
    }


def sweep_commission_rate(
    params, rate_bps, *, agents: int, periods: int, seed: int, volume_cents: float, scale_txn_count: int
) -> list[dict]:
    """**单因子扫描**：只改抽佣率，看三路径占比怎么变（`Q1` 的可观测输出）。"""
    out: list[dict] = []
    for rate in rate_bps:
        mp = merchant_params(params, commission_rate_bp=float(rate))
        steps = simulate_population(
            mp, agents=agents, periods=periods, seed=seed, volume_cents=volume_cents, scale_txn_count=scale_txn_count
        )
        out.append({"commission_rate_bp": float(rate), **summarize(steps)})
    return out


def sweep_detect_rate(
    params, detect_rates, *, agents: int, periods: int, seed: int, volume_cents: float, scale_txn_count: int
) -> list[dict]:
    """**单因子扫描**：只改短秤检测概率，看转暗占比怎么变（`Q2` 的可观测输出）。"""
    out: list[dict] = []
    for p_detect in detect_rates:
        mp = merchant_params(params, p_detect=float(p_detect))
        steps = simulate_population(
            mp, agents=agents, periods=periods, seed=seed, volume_cents=volume_cents, scale_txn_count=scale_txn_count
        )
        out.append({"p_detect": float(p_detect), **summarize(steps)})
    return out


def sweep_commission_and_margin(
    params,
    rate_bps,
    margins,
    *,
    agents: int,
    periods: int,
    seed: int,
    volume_cents: float,
    scale_txn_count: int,
) -> list[dict]:
    """**两因子扫描**：抽佣率 × 毛利率 → 三路径占比栅格。

    为什么必须两维（`Q1` 的真实回答就在这里）：**单看抽佣率得不到退出**。
    在基线假设的毛利率（25%）下，2% 抽佣只是把合规路径的每元效用从 0.2275 压到 0.2075，
    离"赚不够活下去"的生存线还很远 —— 于是**退出占比恒为 0，Q1 的问题被藏起来**。
    把毛利率拉进第二维，才能给出可证伪的回答：**在什么（毛利率, 费率）组合下开始出现退出**。
    这也是"无数据的参数只给序关系/区域、不给绝对月份"的口径落点：栅格是**区域**，不是点预测。
    """
    out: list[dict] = []
    for margin in margins:
        for rate in rate_bps:
            mp = merchant_params(params, gross_margin_rate=float(margin), commission_rate_bp=float(rate))
            steps = simulate_population(
                mp,
                agents=agents,
                periods=periods,
                seed=seed,
                volume_cents=volume_cents,
                scale_txn_count=scale_txn_count,
            )
            summary = summarize(steps)
            out.append(
                {
                    "gross_margin_rate": round(float(margin), 6),
                    "commission_rate_bp": float(rate),
                    "share_ratio": summary["final_share_ratio"],
                    "exit_share": summary["final_share_ratio"].get(EXIT, 0.0),
                    "exited_total": summary["exited_total"],
                    "final_mean_terms": summary["final_mean_terms"],
                }
            )
    return out
