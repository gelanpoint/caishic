"""§6 的 **22 个指标的目录**（`T-SIM-06`）：每个指标的分子/分母口径、来源事件与**适用条件**。

拆出本模块的唯一原因是 `quality-gates.md` §1.2 的"单文件 ≤ 400 行"（`Q-16` 裁定：按语义拆分、
不放宽阈值）。语义边界：**本模块只说"这个指标是什么、口径是什么、什么条件下才算数"**，
复算逻辑在 `metrics.py`。

`condition` 这一列不是装饰：本项目已确立的判据是 —— **有真实数据的参数才允许绝对阈值；
无数据的（MTBF / 罚款 / 扫码基线 / 信任权重 / 退出阈值）只允许序关系与区间不重叠**。
报告必须把这一列原样带上，否则就是在无数据处把假设写成了事实。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    id: str
    name: str
    numerator: str
    denominator: str
    sources: tuple[str, ...]
    condition: str
    ratio: bool = True
    live_only: bool = False


SPECS: tuple[MetricSpec, ...] = (
    MetricSpec("M-01", "商户月度流失率 ExitRate(m)", "该月退出租约的摊位数", "该月初在营摊位数",
               ("month_closed",),
               "无数据的参数（退出阈值 θ_exit / 连续破线月数）⇒ **只允许序关系**，不得把某个月份当阈值"),
    MetricSpec("M-02", "首次流失月份 t_first_exit", "首次出现退出那一月的退出数", "该月初在营摊位数",
               ("month_closed",),
               "「数月即垮」的量级约束来自结论 8；本指标只报**首次发生**，不报点预测", ratio=False),
    MetricSpec("M-03", "12 个月存活率 Survival12", "第 360 个营业日仍在营摊位数", "期初在营摊位数",
               ("month_closed", "run_finished"),
               "Q5 的四个阈值全部是假设（见报告「存活判据」节），故只能作**区域陈述**"),
    MetricSpec("M-04", "走秤率 ScaleUseRate(d)", "当日走秤交易笔数", "当日实际交易总笔数（走秤+私下）",
               ("txn",),
               "分母是 Q-15 承认系统拿不到的外部基准；**live 模式必须分别标注两侧来源**"),
    MetricSpec("M-05", "转暗份额 ShadowRate(d)", "当日私下成交笔数", "当日实际交易总笔数",
               ("txn",), "私下交易量不可被现场数据证伪（§7.4）⇒ 只允许序关系"),
    MetricSpec("M-06", "三份额 ShareComply/ShareShadow/ShareExit(m)", "各行动被选中的摊位-月数", "全部摊位-月数",
               ("merchant_period",), "三者之和 = 1；它是 Q2 的直接答案，仍只是**关于参数**的命题"),
    MetricSpec("M-07", "摊位使用率 stall_usage_bp", "当日有走秤交易的摊位数", "当日在营摊位数",
               ("txn", "price_list"), "与 AC-005 完全同口径；可直接与系统 /admin/metrics/usage 对照"),
    MetricSpec("M-08", "现金交易占比 cash_txn_share_bp", "当日现金收款笔数", "当日走秤笔数",
               ("txn",), "现金占比高 = 支付链路没被接受（结论 11）；无绝对阈值"),
    MetricSpec("M-09", "价目表维护率 price_list_maintenance_bp", "当日价目表完整且已更新的摊位数", "当日在营摊位数",
               ("price_list",), "它是走秤的前置条件；不采用的摊位**必须进分母**，否则结论会被算漂亮"),
    MetricSpec("M-10", "设备闲置率 DeviceIdleRate（两个口径都给）",
               "① 无走秤交易的摊位-日数 ② 停摆超过 τ_repair 的设备-日数", "① 在营摊位-日总数 ② 设备-日总数",
               ("txn", "price_list", "device_day"),
               "τ_repair 无出处（A-13）⇒ 设备口径的**绝对值不可用**，只报口径与序关系；报告须写明用的是哪一个口径"),
    MetricSpec("M-11", "平均修复等待 W_q", "Σ(修复完成 − 报修) 天", "完成修复的报修台数",
               ("device_day",), "维修单价/技师数均无出处（A-12）⇒ 只允许序关系"),
    MetricSpec("M-12", "八两秤发生率 ShortWeightRate(d)", "存在短秤（d>0）的实际交易笔数", "当日实际交易总笔数",
               ("txn",), "⚠️ 系统观测不到，**不可被现场数据证伪**（§1 边界）⇒ 只允许序关系"),
    MetricSpec("M-13", "消费者扫码率 ScanRate(d)（两个口径）",
               "① 扫码且读到有效信息的笔数 ② 扫码笔数（不论结果）",
               "① 走秤成功交易笔数 ② 全部实际交易笔数",
               ("txn",), "口径②的分母是 Q-15 拿不到的基准；扫码基线无统计（A-09）⇒ 只允许序关系"),
    MetricSpec("M-14", "空壳信息比例 HollowRate", "扫码得到空壳信息的笔数", "全部扫码笔数",
               ("txn",), "结论 16 只给「再也不信」的定性语义（A-08/A-09 无出处）⇒ 只允许序关系"),
    MetricSpec("M-15", "信任存量 TrustIndex(t)", "全部消费者对全部摊位的 T 之和", "消费者数 × 摊位数",
               ("consumer_period", "run_started"),
               "A-08 是全设计最弱一环 ⇒ 结论必须表述为**关于参数的命题**，不给点值"),
    MetricSpec("M-16", "信任恢复半衰期 t_half", "解析式 ln2/(δ+η⁺q)", "观测率 q（每笔一次观测机会）",
               ("txn", "consumer_period", "run_started"),
               "解析值与仿真实测值**两个口径并列 + 偏差**；δ/η 无出处 ⇒ 绝不能当现实的时间承诺", ratio=False),
    MetricSpec("M-17", "市场方月度净现金流 NetCash(m)", "月收入（佣金+买方费+摊位费+银行+补贴）",
               "月支出（设备摊销+维护+检定+网络+人力+通道费）", ("market_cash_month",),
               "收入侧多数项目是假设 ⇒ 只作**区域陈述**；补贴的摊销口径本身是假设"),
    MetricSpec("M-18", "12 个月累计净现金流 CumCash(360)", "Σ 月净现金流", "月数",
               ("market_cash_month",),
               "Q5 判据①；对 market_labor / verification / budget 三项假设高度敏感，必须与假设分区展示",
               ratio=False),
    MetricSpec("M-19", "商户月度净收入分布", "各摊位月净收入之和", "在营摊位-月数",
               ("merchant_month",), "报 p10/p50/p90；p10 先走比均值更有信息量；无经营损益数据（A-05）⇒ 只允许序关系",
               ratio=False),
    MetricSpec("M-20", "对账等式成立率", "账本自洽的营业日数", "已执行的营业日总数",
               ("txn", "run_started", "market_cash_month"),
               "live 模式取系统 /admin/reconciliation（REQ-020/AC-003）；本模块的 model 口径是"
               "**仿真账本自身的自洽率**，两者口径不同、报告分开写"),
    MetricSpec("M-21", "留痕完整性", "已产生的留痕事件数", "应产生的留痕事件数",
               ("audit_event", "price_change", "refund"),
               "model 口径只覆盖仿真真的会产生留痕的两类（改价/退货）；补传与幂等命中属 live（T-SIM-07）"),
    MetricSpec("M-22", "model↔live 偏差", "|值_live − 值_model|", "值_model",
               ("live_daily", "model_daily"),
               "**仅 live 模式可算**（T-SIM-07/08）；model 模式下如实标为不可用，不得当 0 处理",
               live_only=True),
)

SPEC_BY_ID = {spec.id: spec for spec in SPECS}

#: 存活的四个阈值 —— **全部是假设**（`docs/sim-design.md` §6.9），且必须逐条分别报告
SURVIVAL_THRESHOLDS = {
    "CumCash(360) >= 0": "市场方 12 个月累计净现金流不为负",
    "active_ratio >= 0.70": "第 360 日在营摊位数 ≥ 70% × 期初",
    "scale_use_rate_300_360 >= 0.60": "第 300–360 日平均走秤率 ≥ 60%",
    "TrustIndex(360) >= 0.5": "顾客还信（否则标签已作废）",
}
