"""`R1`~`R6` 判据的**可执行声明**（`T-SIM-08`；`docs/sim-design.md` §7.1）。

## 为什么判据要写成数据而不是散在代码里

`§7.1` 的六条判据是本设计**唯一一条可否证的证据链**。若把它们写成 `if` 分支散在函数里，
就会出现两种典型的自我欺骗：

1. 判据与产出方共用一份状态 ⇒ "判据通过"与"我算出来是这样"是同一件事；
2. 判据里混进**没有出处的绝对阈值** ⇒ 把假设写成了事实（`Q-19` 教训的同类）。

故本模块只做一件事：把六条判据的**文本、判据形式、依赖参数、所用档位**写成数据，
判定交给 [`backtest.py`](backtest.py) 的纯函数。报告与 `tests/sim/` 读的是**同一份**，
不存在"文档写一套、代码另一套"。

## 判据形式的纪律（`§7.1` 文末「绝对阈值 vs 序关系的取舍原则」，本项目最重要的一条）

| 形式 | 允许的条件 |
| --- | --- |
| `ordering`（序关系 / 单调性 / 区间不重叠） | 总是允许 |
| `absolute`（绝对月份 / 绝对比率） | **判据依赖的每一个参数都必须是 `sourced`** |

`backtest.judge_clause` 对 `absolute` 判据逐个查 `params.kind(key)`，
只要有一个依赖项是 `assumed`，该判据就判 **`不可评估`** 并写明是哪一个参数 ——
**不是**"顺手放宽阈值"，也不是"把它当成通过"。理由见本项目铁律：
**在无数据处给精确阈值，等于把假设包装成事实。**
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 三态判定的**唯一**取值集合（验收判据①：不许只有二态）
VERDICTS = ("成立", "不成立", "不稳健")

#: 判据子句的状态（比 `VERDICTS` 细一层；`R` 级判定由它们按固定优先级收敛，见 `backtest.verdict_of`）
CLAUSE_STATES = ("通过", "不通过", "不可评估")

#: 判据形式
ORDERING = "ordering"
ABSOLUTE = "absolute"


@dataclass(frozen=True)
class Clause:
    """一条可判定的子句。

    * `kind`：`ordering` 或 `absolute`；
    * `depends_on`：判据结论**依赖哪些参数**。这是纪律的抓手 —— `absolute` 判据只要
      依赖一个 `assumed` 参数就必须降级为「不可评估」；
    * `measure`：取哪个指标（`M-xx`）或哪个自定义标量；
    * `direction`：`ordering` 判据的期望方向（`<` / `>`），`None` 表示只看绝对阈值；
    * `measure_arm`：**这条子句测的是哪一臂**（`"A"` / `"B"`，缺省 `A`）。单臂序列类判据
      （单调性、收敛）必须显式声明 —— 判据文本里说的"B（维护预算）""真实来源组"指的是**组**，
      不是本条臂表的 A/B 顺序。第一版把这类子句一律读到 A 臂上，于是 `R4-c`（真实来源组收敛高位）
      实际量的是**空壳组**，判据与被测量完全错位而报告还"绿"着。
    """

    id: str
    text: str
    kind: str
    measure: str
    depends_on: tuple[str, ...] = ()
    direction: str | None = None
    threshold: float | None = None
    measure_arm: str = "A"
    #: 该子句在**哪个档位**下才有载体（`None` = 中心档即可）。
    #: 写成档位 id 是为了强制"用了哪一档"出现在报告里，而不是藏在注释中。
    profile: str | None = None
    note: str = ""


@dataclass(frozen=True)
class Criterion:
    """一条 `R` 判据：臂的构成 + 子句 + 稳健性扫描键。"""

    id: str
    title: str
    basis: str
    arms: dict = field(default_factory=dict)
    clauses: tuple[Clause, ...] = ()
    #: 稳健性扫描：无出处参数在本条判据里**起决定作用**的那些（`§7.2` 第 2 条）
    scan_keys: tuple[str, ...] = ()
    profile: str = "L1"
    question: str = ""


#: 中心档（L1 缩减到达档）。**为什么必须写明档位**：机制有显形时间尺度与显形规模，
#: 档位不对就看不到机制（`docs/sim-验证结论实况.md` V-01 的教训）。本档的 `arrivals`
#: 取 300 而不是参数默认的 1000，是为了让**退出机制**有载体：在默认档下摊位月净收入
#: 约为参考点的 2.8 倍 ⇒ `merchant_exit_breach_periods` 永远不触发 ⇒ 与退出有关的
#: 判据全部"测不到"。**这个代价（规模不等于 Q-9 默认）必须随报告一起给出。**
L1_PROFILE = {
    "id": "L1",
    "label": "L1 · 360 营业日 / 300 到达·日 / 40 消费者 / 10 摊",
    "days": 360,
    "arrivals": 300,
    "consumers": 40,
    "replications": 2,
    "param_overrides": {},
    "why": "退出阈值（merchant_exit_reference_point = 1e6 分/期，无出处 A-05）在 "
           "参数默认的 1000 到达/日下永远不会被突破 ⇒ 与退出相关的判据在该档**没有载体**；"
           "300 到达/日让摊位月净收入落在参考点附近，退出机制才可观测。"
           "⚠️ 因此本档**不是 Q-9 的默认规模**（Q-9 默认 1000，区间 800~1500），"
           "任何引用本档结论的场合都必须带这句。",
}

#: 设备故障压力档（沿用 V-01 的复现档：MTBF=10 天 / 修复均值 6 天）。
STRESS_PROFILE = {
    "id": "S-压力",
    "label": "S · 90 营业日 / MTBF=10 天 / 修复均值=6 天（V-01 复现档）",
    "days": 90,
    "arrivals": 300,
    "consumers": 40,
    "replications": 2,
    "param_overrides": {"device_mtbf_days": 10, "repair_mean_days": 6},
    "why": "基线档 device_mtbf_days=540、60~90 日内期望故障仅约 1~2 次（V-01 实测 0 次）"
           "⇒ 一切与设备停摆有关的判据在该档**没有载体**。本档与 V-01 的复现命令一致，"
           "且明确声明这两个取值**超出 `params.json` 登记区间**（[180,1095] / [1,15]），"
           "结论只表述为关于参数的命题。",
}

#: 预算约束压力档：在压力档之上再把**维修单价**推到使「预算档位真正成为约束」的量级。
BINDING_PROFILE = {
    "id": "S-约束",
    "label": "S-约束 · 240 营业日 / MTBF=10 天 / 修复均值=6 天 / repair_cost_cents=1.6e6（预算真正成为约束）",
    "days": 240,
    "arrivals": 300,
    "consumers": 40,
    "replications": 2,
    "param_overrides": {"device_mtbf_days": 10, "repair_mean_days": 6, "repair_cost_cents": 1600000},
    "why": "两个量级都要改到机制有载体，缺一个就是『测不到』："
           "① **故障载体**：`device_mtbf_days=540`、90 日内期望故障仅约 1~2 次（V-01 实测 0 次）"
           "⇒ 一切与设备停摆有关的判据在该档没有载体（MTBF=10 天，V-01 复现档）；"
           "② **预算载体**：`repair_capacity = 维护预算 // 维修单价`。在 params.json 登记区间内"
           "（预算 1e6~5e7 分、维修单价 1e4~1e5 分、MTBF ≥180 天），该值恒 ≥ 10 台/日，"
           "而最大故障到达率仅 10 台 ÷ 180 天 ≈ 0.056 台/日 ⇒ **预算档位在合法区间内不可能成为约束**；"
           "本档把维修单价推到 1.6e6 分（约 1.6 万元/次，**超出登记区间约 16~160 倍**）。"
           "③ **迟滞载体**：`market_decision_delay_periods = 6` ⇒ 预算调整要 6 期后才生效，"
           "所以 90 日（3 期）档里『B 单调下降』**必然测不到** —— 本档取 240 日（8 期）才有载体。"
           "⚠️ 两个越界取值都是**压力档，不是实测值**；结论只表述为关于参数的命题。",
}

PROFILES = {p["id"]: p for p in (L1_PROFILE, STRESS_PROFILE, BINDING_PROFILE)}

#: `S1` 三臂 = 佣金口径对照（抽 200bp / 不抽 / 温州式）。**`R1` 组额外关掉免维护承诺**，
#: 因为 §7.1 的 `R1` 行写的是「2% + **无**免维护承诺」，而 `S1` 的基线没有声明这一项。
_NO_MAINTENANCE_PROMISE = {"market_maintenance_floor_share": 0.0}
#: `R2` 组 = 不抽佣 + 市场方出资 + **统一检定维修**（"维保含在采购内"是它的落地形式，
#: 见 S3 的 `market_maintenance_floor_share`）。
_UNIFIED_MAINTENANCE = {"market_maintenance_floor_share": 0.5}

CRITERIA: tuple[Criterion, ...] = (
    Criterion(
        id="R1",
        title="向商户抽佣 2% + 无免维护承诺 ⇒ 数月即垮",
        basis="[源:结论 9]（2‰ 就已被国务院督查并全额退还 → 2% 必然更严重，单调性判据）"
              "[源:结论 10]（商户搬离）[源:结论 8]（2024-10 运营、数月即垮）",
        question="抽佣是不是 Q1 说的「把商户推走」的那个机制？",
        arms={
            "A": {"scenario": "S1", "arm_index": 0, "label": "抽 200bp + 无免维护", "extra": _NO_MAINTENANCE_PROMISE},
            "B": {"scenario": "S1", "arm_index": 1, "label": "不抽佣 + 无免维护（对照）", "extra": _NO_MAINTENANCE_PROMISE},
        },
        clauses=(
            Clause("R1-a", "t_first_exit ≤ 6 个月", ABSOLUTE, "M-02",
                   depends_on=("merchant_exit_reference_point", "merchant_exit_breach_periods",
                               "gross_margin_rate", "commission_rate_bp"),
                   threshold=6,
                   note="设计给出的绝对阈值出处是**结论 8 的真实时间量级**（数月即垮），"
                        "但**模型里决定退出时点的两个参数都没有出处**（A-05）⇒ 按 §7.1 的取舍原则，"
                        "本子句**不得**用绝对月份判定；改以 `merchant_exit_reference_point` 的"
                        "区间扫描给出「在什么参数区域成立」。"),
            Clause("R1-b", "12 月 ExitRate 显著高于不抽佣组且 90% 区间不重叠", ORDERING, "M-01",
                   depends_on=("merchant_exit_reference_point", "merchant_exit_breach_periods"),
                   direction=">"),
            Clause("R1-c", "ScaleUseRate 单调下降", ORDERING, "M-04",
                   depends_on=("commission_rate_bp", "gross_margin_rate", "merchant_q_temperature"),
                   direction="decreasing"),
        ),
        scan_keys=("merchant_exit_reference_point", "gross_margin_rate", "daily_arrivals_per_market"),
        profile="L1",
    ),
    Criterion(
        id="R2",
        title="不抽佣 + 市场方出资 + 统一检定维修 ⇒ 严格优于 R1 组，且与 S0 无显著差异",
        basis="[源:结论 14]（衡阳/杭州：免费配秤 + 统一检定维修 ⇒ 愿意用）[源:D-02]",
        question="「免费 + 免维护」这条正向机制在模型里写对了吗？",
        arms={
            "A": {"scenario": "S1", "arm_index": 1, "label": "不抽佣 + 统一检定维修（floor 0.5）",
                  "extra": _UNIFIED_MAINTENANCE},
            "B": {"scenario": "S1", "arm_index": 0, "label": "抽 200bp + 无免维护（R1 组）",
                  "extra": _NO_MAINTENANCE_PROMISE},
            "C": {"scenario": "S0", "arm_index": 0, "label": "S0 基线", "extra": {}},
        },
        clauses=(
            Clause("R2-a", "ScaleUseRate 高于 R1 组", ORDERING, "M-04", direction=">"),
            Clause("R2-b", "12 月 ExitRate 低于 R1 组且区间不重叠", ORDERING, "M-01", direction="<"),
            Clause("R2-c", "DeviceIdleRate 低于 R1 组", ORDERING, "M-10", direction="<"),
            Clause("R2-d", "与 S0 基线无显著差异（区间重叠）", ORDERING, "M-04", direction="overlap"),
        ),
        scan_keys=("market_maintenance_floor_share", "device_mtbf_days"),
        profile="L1",
    ),
    Criterion(
        id="R3",
        title="维护预算 0.3× ⇒ 修复等待上升、设备闲置加速上升、走秤率单调下降",
        basis="[源:结论 6]（维修不及时 → 落灰）[源:结论 4/5]（武汉：2021 回访 20+ 市场仍闲置）"
              "⚠️ 绵阳案例的 21~32 个月跨度**只用于判断量级，不作为通过/不通过阈值**",
        question="「死亡螺旋」假设复现得出来吗？",
        arms={
            "A": {"scenario": "S3", "arm_index": 0, "label": "预算足额·保底 50%", "extra": {}},
            "B": {"scenario": "S3", "arm_index": 4, "label": "预算 0.3×·保底 50%", "extra": {}},
        },
        clauses=(
            #: 方向约定：`direction` 说的是 **A 与 B 的关系**，本判据 `A=预算足额` / `B=0.3×`
            Clause("R3-a", "W_q 上升（0.3× 组 > 足额组）", ORDERING, "M-11", direction="<",
                   profile="S-约束"),
            Clause("R3-b", "DeviceIdleRate（口径①摊位-日）显著高于足额组", ORDERING, "M-10",
                   direction="<", profile="S-约束",
                   note="臂序 `A=足额` / `B=0.3×` ⇒ 『0.3× 更闲置』写成 `A<B`。"
                        "『加速上升』需逐期序列；本子句先判**量级**方向，"
                        "序列形状在报告的臂表与稳健性一节给出（加速与否由数据说话，不预设）"),
            Clause("R3-c", "0.3× 组的维护预算单调下降", ORDERING, "M-17-B", direction="decreasing",
                   profile="S-约束", measure_arm="B",
                   note="测的是 **B 臂**（0.3× 组）的预算序列；本子句的载体是 "
                        "`market_decision_delay_periods = 6`（6 期后才生效）⇒ **至少要 7 个仿真月"
                        "（210 营业日）**才测得到，90 日档下必然测不到（故本判据的档位取 240 日）"),
            Clause("R3-d", "0.3× 组的 ScaleUseRate 同时单调下降", ORDERING, "M-04",
                   direction="decreasing", profile="S-约束", measure_arm="B"),
        ),
        scan_keys=("repair_cost_cents", "device_mtbf_days"),
        profile="S-约束",
    ),
    Criterion(
        id="R4",
        title="扫码页空壳字段 ⇒ 信任单调不增且不可恢复；真实来源 ⇒ 收敛高位；两组序关系必须成立",
        basis="[源:结论 16]（扫过好几次空码、无效信息之后，再也不信这个标签了）",
        question="信任更新的不对称权重真的起作用吗？",
        arms={
            "A": {"scenario": "S4", "arm_index": 0, "label": "空壳字段（info_real_share=0.1）", "extra": {}},
            "B": {"scenario": "S4", "arm_index": 2, "label": "真实来源+电子屏（info_real_share=0.95）", "extra": {}},
        },
        clauses=(
            Clause("R4-a", "空壳组 TrustIndex 单调不增", ORDERING, "M-15", direction="non_increasing"),
            Clause("R4-b", "空壳组 t_half = ∞ 或 > 12 个月", ABSOLUTE, "M-16",
                   depends_on=("trust_update_eta_neg", "trust_update_eta_pos", "trust_decay_delta",
                               "consumer_scan_baseline_rate", "consumer_scan_floor"),
                   threshold=12, note="A-08（信任权重）与 A-09（扫码基线）**完全无出处** ⇒ 禁止用绝对月份"),
            Clause("R4-c", "真实来源组 T 收敛到高位", ORDERING, "M-15", direction="converging_high",
                   measure_arm="B",
                   note="测的是 **B 臂**（真实来源组）的信任序列；臂序 `A=空壳` / `B=真实来源`，"
                        "判据文本说的是**组**不是臂序 —— 这两件事搞混会量到完全相反的一组"),
            Clause("R4-d", "两组序关系成立（空壳 < 真实）", ORDERING, "M-15", direction="<"),
        ),
        scan_keys=("trust_update_eta_neg", "trust_update_eta_pos", "consumer_scan_floor"),
        profile="L1",
    ),
    Criterion(
        id="R5",
        title="温州口径（向买方 0.5%~6% + 不收摊位费 + 出门查验）⇒ 存活 12 个月",
        basis="[源:结论 12]（2001 年温州市物价局批复，运行至今 20 余年）"
              "⚠️ 存活判据的四个阈值（0 / 70% / 60% / 0.5）**全是假设**，见 §6.9",
        question="模型会不会把「向买方收」和「向商户收」当成同一件事？",
        arms={
            "A": {"scenario": "S1", "arm_index": 2, "label": "温州式：向买方 60bp + 不收摊位费 + 出门查验",
                  "extra": {}},
            "B": {"scenario": "S1", "arm_index": 1, "label": "不抽佣（对照）", "extra": {}},
        },
        clauses=(
            Clause("R5-a", "存活判据①CumCash(360) ≥ 0", ABSOLUTE, "survival:1", threshold=0,
                   depends_on=("market_budget_initial_cents", "bank_funding_cents_3y", "stall_fee_cents_per_month",
                               "subsidy_ratio_bp", "market_labor_cents_per_month")),
            Clause("R5-b", "存活判据②第 360 日在营摊位数 ≥ 70% × 期初", ABSOLUTE, "M-03", threshold=0.70,
                   depends_on=("merchant_exit_reference_point", "merchant_exit_breach_periods",
                               "daily_arrivals_per_market")),
            Clause("R5-c", "存活判据③第 300~360 日平均走秤率 ≥ 60%", ABSOLUTE, "survival:3", threshold=0.60,
                   depends_on=("evade_feasibility", "merchant_q_temperature", "daily_inspection_rate")),
            Clause("R5-d", "存活判据④TrustIndex(360) ≥ 0.5", ABSOLUTE, "M-15", threshold=0.5,
                   depends_on=("trust_update_eta_neg", "trust_decay_delta", "info_real_share")),
            Clause("R5-e", "第 12 月摊位占有率 ≥ 期初", ABSOLUTE, "M-03", threshold=1.0,
                   depends_on=("merchant_exit_reference_point", "merchant_exit_breach_periods",
                               "daily_arrivals_per_market")),
        ),
        scan_keys=("merchant_exit_reference_point", "trust_update_eta_neg", "daily_arrivals_per_market"),
        profile="L1",
    ),
    Criterion(
        id="R6",
        title="反例检验：商户自费购秤 ⇒ 走秤率必须显著低于市场方出资组",
        basis="[源:结论 26]（3750~4600 元/台由商户承担 + 云软件 5000 元/年）[源:结论 14]"
              "—— 若自费组活得一样好，说明模型里的「自费」没有真实成本",
        question="模型是不是「怎么调都能活」？",
        arms={
            "A": {"scenario": "S5", "arm_index": 0, "label": "市场方出资（结论 14 口径）", "extra": {}},
            "B": {"scenario": "S5", "arm_index": 1, "label": "商户自费（结论 26 口径）", "extra": {}},
        },
        clauses=(
            Clause("R6-a", "走秤率显著低于出资组", ORDERING, "M-04", direction=">",
                   note="⚠️ 方向约定：`ordering` 判据的 `direction` 说的是 **A 与 B 的关系**，"
                        "而本条臂序是 `A=出资` / `B=自费` ⇒ 『自费更低』= `A>B`，不是 `A<B`。"
                        "写反的后果是这条反例被自己的判定器判成『不成立』——反例就白设了"),
            Clause("R6-b", "自费通道真的有成本：自费组的一次性支出高于出资组", ORDERING, "R6-upfront",
                   direction="<", depends_on=("merchant_self_funded_scale_cents",),
                   note="这条子句本身有出处（`merchant_self_funded_scale_cents` = `sourced` / 结论 26，"
                        "3750 元/台），所以它可以用序关系直接判；它挡的是"
                        "**「自费臂其实没花钱」这种更隐蔽的失败**（第一版就出现过：扫描跑在出资臂上，"
                        "upfront=0 ⇒ 扫的是一个乘 0 的数）"),
        ),
        scan_keys=("merchant_adoption_cost_weight", "gross_margin_rate", "merchant_adoption_horizon_months_range"),
        profile="L1",
    ),
)

CRITERIA_BY_ID = {c.id: c for c in CRITERIA}


def criterion(criterion_id: str) -> Criterion:
    if criterion_id not in CRITERIA_BY_ID:
        raise KeyError(f"未登记的判据：{criterion_id!r}（允许 {sorted(CRITERIA_BY_ID)}）")
    return CRITERIA_BY_ID[criterion_id]


def profile(profile_id: str) -> dict:
    if profile_id not in PROFILES:
        raise KeyError(f"未登记的档位：{profile_id!r}（允许 {sorted(PROFILES)}）")
    return PROFILES[profile_id]