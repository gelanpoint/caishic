"""live 模式的**场景装载**（`T-SIM-07`；`docs/sim-design.md` §2.3「场景装载阶段」）。

## 这一步为什么是整条链路里最要紧的一步

**抽佣不是 sim 里的一个变量。** 场景臂里的 `commission_rate_bp` /
`commission_charged_to_merchant` 在 live 模式下只用来决定**往系统里写哪条口径**
（`PUT /api/admin/commission-rules`），此后佣金数字**一律由 `app/domain/commission.py` 算出**
（日聚合 → 结算单 → 看板）。若 sim 自己也保留一份佣金口径去算，"live 模式不是纸上模型"这句话
就没有任何证据支撑 —— 那只是把同一套数字换个地方再写一遍。

本模块因此只做三件事，全部可单独核对：

1. `find_scenario` —— 按 id 取场景定义；
2. `commission_plan` —— 场景臂 → **要写进系统的口径**（纯函数，负例可直接喂）；
3. `load_commission_rule` —— 真把口径 `PUT` 进系统，并对"库里已有同样一条口径"给出**显式**处置。

## 拆出本模块的原因

`docs/standards/quality-gates.md` §1.2 的「单文件 ≤ 400 行」（`Q-16` 裁定：按语义拆分、
不放宽阈值）。语义边界：**场景 → 系统佣金口径的装载**与**逐日编排**是两件事。
"""

from __future__ import annotations

from pathlib import Path

from .live_adapter import ApiError
from .scenario import load_scenario, merged_overrides, scenario_files

#: live 模式的默认营业日数：**必须 ≥ 30**，否则走不到月末，`POST/GET /api/admin/settlements`
#: 这两个端点没机会被调用 ⇒ 31/31 的覆盖判据就不可能满足
#: （这不是"默认取整好看"，是**端点覆盖的约束**）。
LIVE_DEFAULT_DAYS = 30

#: 契约 §3.24 的 `rate_bp` 下限（1 = 0.01%）
RATE_BP_FLOOR = 1


def find_scenario(scenario_id: str, scenarios_dir: Path | str | None = None) -> dict:
    """按 id 取场景定义（`sim/scenarios/S*.json`）。"""
    base = Path(scenarios_dir) if scenarios_dir else None
    for path in (scenario_files(base) if base else scenario_files()):
        scenario = load_scenario(path)
        if scenario["id"] == scenario_id:
            return scenario
    raise KeyError(f"没有场景 {scenario_id!r}")


def commission_plan(scenario: dict, arm: dict, params) -> dict:
    """场景臂 → **要写进系统的佣金口径**（`PUT /api/admin/commission-rules` 的请求体 + 标注）。

    **纯函数**：口径映射（`commission_charged_to_merchant=false` ⇒ 向买方收 ⇒ `pay_object=customer`）
    在这里定死一处；仿真不保留第二份佣金口径。

    `rate_bp` 的下界处理如实报告：场景里的「抽佣 **0%**」在契约下**不可表达**
    （`rate_bp ∈ 1~10000`），取下界 `1bp` 并标 `clamped=true`。**这是系统侧边界，未改 `app/**`。**
    """
    overrides = merged_overrides(scenario, arm)
    requested = int(round(float(overrides.get("commission_rate_bp", params.value("commission_rate_bp")))))
    charged_to_merchant = bool(
        overrides.get("commission_charged_to_merchant", params.value("commission_charged_to_merchant"))
    )
    rate_bp = max(RATE_BP_FLOOR, requested)
    note = ""
    if requested < RATE_BP_FLOOR:
        note = (f"场景声明 {requested}bp（0%），但契约 §3.24 的 `rate_bp` 下限是 {RATE_BP_FLOOR}bp "
                f"⇒ live 模式取下界 {RATE_BP_FLOOR}bp（0.01%）。**这是系统侧边界，未改 `app/**`。**")
    return {
        "scenario_id": scenario["id"],
        "arm": arm["name"],
        "pay_object": "merchant" if charged_to_merchant else "customer",
        "requested_rate_bp": requested,
        "rate_bp": rate_bp,
        "charged_to_merchant": charged_to_merchant,
        "clamped": requested < RATE_BP_FLOOR,
        "note": note,
    }


def _is_same_rule(rule: dict, plan: dict, effective_from: str) -> bool:
    return (
        rule.get("pay_object") == plan["pay_object"]
        and int(rule.get("rate_bp", -1)) == plan["rate_bp"]
        and rule.get("effective_from") == effective_from
        and not rule.get("category_tier")
    )


def load_commission_rule(adapter, plan: dict, effective_from: str) -> dict:
    """把口径**写进系统**并复核；返回可落盘的证据（含"是否复用了既有口径"）。

    `PUT` 的语义是**新增一条口径**（`data-model.md` §2.14 保留历史生效期），不是"替换" ⇒
    对着**已有该口径的库**再跑一次必然 `MT-1012`。若库里那条与本次要写的**完全一致**，
    复用它是正确的（"口径已经装载好了"），并**在证据里标明复用了**；不一致则**原样抛错**，
    不静默改口径、也不换个费率蒙混过去。
    """
    rules_before = adapter.admin_commission_rules()
    try:
        created = adapter.admin_put_commission_rule(
            pay_object=plan["pay_object"], rate_bp=plan["rate_bp"], effective_from=effective_from,
        )
        reused = False
    except ApiError as exc:
        identical = [rule for rule in (rules_before or []) if _is_same_rule(rule, plan, effective_from)]
        if not identical:
            raise
        created, reused = identical[0], True
    rules_after = adapter.admin_commission_rules()
    return {
        "rules_before": len(rules_before or []),
        "rules_after": len(rules_after or []),
        "created_rule": created,
        "reused_existing_rule": reused,
    }
