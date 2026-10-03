"""live 模式的**日 / 月编排**（`T-SIM-07`；`docs/sim-design.md` §2.3）。

## 一次 live 运行做的事

```
树指纹(app,specs) ──► 拉起 run.py 子进程（隔离 MT_DATA_DIR 落 C: + 独立端口 + /healthz 就绪探测）
   │
   ├─ 场景装载阶段：GET /api/admin/commission-rules → **PUT** 同端点（写进系统的佣金口径）
   │                 → GET 复核（这条口径现在在系统里，而不是在 sim 里）
   ├─ 日初配置阶段：GET /healthz · GET/POST /api/admin/categories · POST /api/admin/aliases
   │                 · 逐摊位：POST session → GET products → POST/GET price-list（复制上一营业日）
   ├─ 营业日（逐日推进业务日：写 MT_CLOCK_FILE，`app/clock.py` 每次调用重读）
   │     成交：POST transactions → GET detail → [改价] → POST payment(cash|qr) → [mock 回调] → [退货]
   │           → GET /api/customer/receipts/{no} · GET /api/customer/stalls/{stall}/profile
   │     离线：POST offline/queue → GET offline/queue → POST offline/sync
   │     日终：POST daily-aggregate → GET reconciliation → GET metrics/usage
   │           → GET admin/dashboard → GET audit-logs
   └─ 月末：POST settlements（逐摊位）→ GET settlements
树指纹(app,specs) ──► 与运行前比对（判据⑥：live 模式只读系统）
```

## 关键设计意图（写歪了就失去意义）

**抽佣不是 sim 里的一个变量。** 场景里的 `commission_rate_bp` / `commission_charged_to_merchant`
只用来决定**往系统里写哪条口径**（`PUT /api/admin/commission-rules`）；此后佣金数字**一律由
`app/domain/commission.py` 算出**（日聚合 → 结算单 → 看板），sim 不再自己算一遍。
报告里的 `commission_evidence` 就是这条链路的证据（写入前后的规则条数 + 系统算出的日佣金）。

**不引任何新依赖、不复用契约夹具**：`live_adapter` 自己用 `http.client` 按契约拼请求。

## 两处系统侧边界（如实登记，不自行改 `app/**`）

1. 契约 §3.24 的 `rate_bp ∈ 1~10000` ⇒ 「抽佣 **0%**」在 live 模式下**不可精确表达**：
   取下界 `1bp = 0.01%`，并在报告的 `commission` 段标 `clamped=true` + 原因。
   （这不影响 `Q1` 的**序关系**结论 —— 1bp 与 200bp 的差距仍然是 200 倍。）
2. 补传产生的是 `priced`（未收款）交易；按 §3.29「只算已结算」的口径，**不影响对账等式**
   （该口径已由 `app/domain/settlement.py` 的模块 docstring 锁定并被 `test_offline` 钉死）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ..core.clock import Block, SimClock
from ..core.streams import StreamSet
from .live_adapter import JsonClient, LiveAdapter
from .live_evidence import latency_summary, write_report, write_responses
from .live_scenario import LIVE_DEFAULT_DAYS, commission_plan, find_scenario, load_commission_rule
from .server_launcher import launch_server, system_tree_digest

LAYER = "live-adapter"

__all__ = ["LAYER", "LIVE_DEFAULT_DAYS", "LiveRunConfig", "commission_plan", "find_scenario", "run_live"]


@dataclass
class LiveRunConfig:
    """一次 live 运行的全部开关（**都是运行规模与端口，不是业务参数**）。"""

    scenario_id: str = "S0"
    arm_index: int = 0
    days: int = LIVE_DEFAULT_DAYS
    seed: int = 20261002
    start: str = "2026-10-01"
    txns_per_day: int = 4
    data_dir: str | None = None
    port: int | None = None
    timeout_s: float = 180.0
    #: 隔离数据目录名。**每次运行默认一个新目录**：同一目录里重跑会撞
    #: `MT-1012`（佣金口径生效期重叠，`PUT` 是"新增一条口径"而不是"替换"）——
    #: 那不是缺陷，是契约语义；让重复运行撞它不如让每次运行自带一份干净库。
    run_id: str | None = None


@dataclass
class _DayState:
    """日循环的累加器（**只放事实**；判据由 `live_evidence` 按报告字段判定）。"""

    created: int = 0
    paid: int = 0
    refunded: int = 0
    cash: int = 0
    qr: int = 0
    scanned: int = 0
    staged: int = 0
    backfilled: int = 0
    settlements: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def _pick_stall(stall_nos: list, index: int) -> str:
    """成交落在哪个摊位（确定性：按序轮转，同 seed 逐日可复现）。"""
    return stall_nos[index % len(stall_nos)]


def run_live(params, config: LiveRunConfig, out_dir: Path | str) -> dict:
    """跑一次 live 模式：真起被测服务、走完 31 个端点、落 `live_report.json`。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    scenario = find_scenario(config.scenario_id)
    arm = scenario["arms"][config.arm_index]
    plan = commission_plan(scenario, arm, params)
    clock = SimClock(
        start=date.fromisoformat(config.start),
        days=config.days,
        blocks=tuple(Block(name, start_at, intensity) for name, start_at, intensity in params.blocks()),
    )
    streams = StreamSet(config.seed)
    choice = streams.stream("market", "choice")
    weight_rng = streams.stream("market", "arrival")
    cash_share = float(params.value("cash_payment_share"))

    digest_before = system_tree_digest()
    run_id = config.run_id or (
        f"live-{plan['scenario_id']}-{plan['arm']}-{config.seed}-{config.start}-"
        f"{time.strftime('%Y%m%d-%H%M%S')}"
    ).replace(" ", "")
    server = launch_server(
        run_id=run_id, out_dir=out, data_dir=config.data_dir, business_date=config.start,
        port=config.port, timeout_s=config.timeout_s,
    )
    client = JsonClient(server.base_url)
    adapter = LiveAdapter(client)
    days_rows: list[dict] = []
    replay_evidence: dict = {}
    commission_evidence: dict = {}
    health: dict = {}

    try:
        # ---- 场景装载阶段：佣金口径**写进系统**（不是 sim 里再算一遍） ----
        rule_evidence = load_commission_rule(adapter, plan, clock.business_date(0))
        created_rule = rule_evidence["created_rule"]

        # ---- 日初配置阶段（非业务流的三个端点） ----
        health = adapter.healthz()
        categories = adapter.admin_categories()
        new_category = adapter.admin_create_category(
            code=f"SIM{str(config.seed)[-6:]}", name=f"仿真品类{str(config.seed)[-6:]}",
        )
        stall_nos = [row["stall_no"] for row in (adapter.admin_dashboard() or {}).get("stalls", [])]
        if not stall_nos:
            raise RuntimeError("运营端看板没有返回任何在营摊位 —— 无法建立秤端会话")
        adapter.admin_create_alias(stall_nos[0], f"仿真别名{config.seed % 1000}", int(new_category["id"]))

        sessions: dict[str, str] = {}
        products: dict[str, list] = {}
        month_start = clock.business_date(0)

        for day_index, business_date, month_end, _quarter_end in clock.iter_days():
            server.clock.set(business_date)
            state = _DayState()
            health = adapter.healthz()

            # ---- 日初：会话 / 商品 / 价目表（复制上一营业日） ----
            for stall_no in stall_nos:
                session = sessions.get(stall_no)
                if session is None:
                    session = adapter.create_session(stall_no)["session_token"]
                    sessions[stall_no] = session
                products[stall_no] = adapter.list_products(session)
                adapter.put_price_list(session, business_date, copy_previous=True)
                adapter.get_price_list(session, business_date)

            # ---- 逐笔成交 ----
            for index in range(config.txns_per_day):
                stall_no = _pick_stall(stall_nos, index)
                session = sessions[stall_no]
                catalog = products[stall_no]
                if not catalog:
                    continue
                product = catalog[choice.randrange(len(catalog))]
                grams = weight_rng.randint(200, 4500)
                items = [{"product_id": int(product["id"]), "weight_grams": grams}]
                key = f"sim-{business_date}-{index:03d}"

                _status, created = adapter.create_transaction(session, items, key)
                transaction_no = created["transaction_no"]
                detail = adapter.get_transaction(session, transaction_no)
                state.created += 1

                # 判据③ 幂等重放：同键同体重发一次（首日第一笔），比对交易号与列表总数
                if day_index == 0 and index == 0 and not replay_evidence:
                    before_list = adapter.list_transactions(session, business_date=business_date)
                    replay_status, replayed = adapter.create_transaction(session, items, key)
                    after_list = adapter.list_transactions(session, business_date=business_date)
                    replay_evidence = {
                        "business_date": business_date,
                        "idempotency_key": key,
                        "transaction_no": transaction_no,
                        "replayed_transaction_no": replayed["transaction_no"],
                        "first_status": _status,
                        "replay_status": replay_status,
                        "list_total_before": int(before_list["total"]),
                        "list_total_after": int(after_list["total"]),
                    }

                # 改价（收款前；幅度 <50% 故不需要 `confirm_over_threshold`）
                if index == 0:
                    line = detail["items"][0]
                    original = int(line.get("original_unit_price_cents") or line.get("final_unit_price_cents") or 1)
                    adapter.price_change(session, transaction_no, {
                        "item_id": int(line["id"]),
                        "final_unit_price_cents": max(1, int(round(original * 0.97))),
                    })

                # **当日最后一笔固定走收款码**：`POST /api/mock/payment/callback` 是 31 个端点之一，
                # 若把它的出现与否交给随机数，「31/31 全覆盖」就变成碰运气的事
                # （cash_share=0.7、每天 2 笔时，整轮一次都不出现收款码的概率 ≈ 0.09）。
                # 覆盖判据不能靠运气，故这里固定留一个确定性的收款码样本。
                force_qr = index == config.txns_per_day - 1
                method = "qr" if force_qr else ("cash" if choice.random() < cash_share else "qr")
                _pay_status, payment = adapter.pay(
                    session, transaction_no, method=method, idempotency_key=f"{key}-pay",
                    operator="sim-operator" if method == "cash" else None,
                )
                if method == "cash":
                    state.cash += 1
                else:
                    state.qr += 1
                    callback = adapter.mock_payment_callback(f"CB{key}", payment["payment_no"], "success")
                    #: 重复送达一次（契约 §1.7 / `AC-010`）：必须回 `is_duplicate=true` 且**不重复记账**
                    repeat = adapter.mock_payment_callback(f"CB{key}", payment["payment_no"], "success")
                    if repeat.get("is_duplicate"):
                        state.extra["duplicate_callback"] = state.extra.get("duplicate_callback", 0) + 1
                state.paid += 1

                adapter.customer_receipt(transaction_no)
                adapter.customer_stall_profile(stall_no)

                # 退货（最后一笔、已收款）：只冲减一次
                if index == config.txns_per_day - 1:
                    refund = adapter.refund(
                        session, transaction_no, amount_cents=max(1, int(created["total_amount_cents"]) // 2),
                        idempotency_key=f"{key}-refund",
                    )
                    if refund.get("replayed"):
                        state.extra["replayed_refund"] = state.extra.get("replayed_refund", 0) + 1
                    state.refunded += 1

            # ---- 离线暂存与补传（一天一次，取第一个摊位） ----
            first_stall = stall_nos[0]
            first_session = sessions[first_stall]
            first_product = products[first_stall][0]
            adapter.offline_queue_status(first_session)
            staged = adapter.stage_offline(first_session, [
                {"product_id": int(first_product["id"]), "weight_grams": 500},
            ], f"sim-offline-{business_date}")
            state.staged = int(staged["pending_count"])
            synced = adapter.offline_sync(first_session)
            state.backfilled = int(synced.get("backfilled", 0))

            # ---- 模拟秤读数 + 秤端读面 ----
            adapter.mock_scale_reading(1000 + day_index)
            adapter.list_transactions(sessions[stall_nos[0]], business_date=business_date)
            adapter.merchant_dashboard(sessions[stall_nos[0]], business_date=business_date)

            # ---- 日终：聚合 / 对账 / 三指标 / 看板 / 留痕 ----
            aggregate = adapter.admin_daily_aggregate(business_date)
            reconciliation = adapter.admin_reconciliation(business_date)
            usage = adapter.admin_usage_metrics(business_date)
            market = adapter.admin_dashboard(business_date=business_date)
            audit = adapter.admin_audit_logs(limit=50)
            stall_dashboard = adapter.merchant_dashboard(sessions[stall_nos[0]], business_date=business_date)

            commission_cents = int((market.get("market") or {}).get("commission_amount_cents", 0))

            # ---- 月末：结算单 ----
            if month_end:
                for stall_no in stall_nos:
                    settlement = adapter.admin_create_settlement(stall_no, month_start, business_date)
                    state.settlements.append({"stall_no": stall_no, **settlement})
                listed = adapter.admin_settlements(period_start=month_start, period_end=business_date)
                state.extra["settlements_listed"] = len(listed)

            days_rows.append({
                "business_date": business_date,
                "day_index": day_index,
                "created": state.created,
                "paid": state.paid,
                "cash": state.cash,
                "qr": state.qr,
                "refunded": state.refunded,
                "offline_staged": state.staged,
                "offline_backfilled": state.backfilled,
                "aggregate_stalls": aggregate["stalls_aggregated"],
                "reconciliation": reconciliation,
                "usage_metrics": usage,
                "market_commission_cents": commission_cents,
                "merchant_commission_cents": int(stall_dashboard.get("commission_amount_cents", 0)),
                "audit_log_rows": len((audit or {}).get("items", []) or []),
                "settlements": state.settlements,
                **state.extra,
            })

            if month_end:
                month_start = (date.fromisoformat(business_date) + timedelta(days=1)).isoformat()

        commission_evidence = {
            **rule_evidence,
            "system_commission_total_cents": sum(row["market_commission_cents"] for row in days_rows),
            "note": plan["note"],
        }
    finally:
        server.stop()

    responses_path = write_responses(out, client.bodies)
    report = {
        "schema_version": 1,
        "mode": "live",
        "layer": LAYER,
        "scenario": plan["scenario_id"],
        "arm": plan["arm"],
        "commission": plan,
        "commission_evidence": commission_evidence,
        "seed": config.seed,
        "days_count": len(days_rows),
        #: 判据②逐日读这个列表（`live_evidence.verification_problems`）
        "days": days_rows,
        "start": config.start,
        "txns_per_day": config.txns_per_day,
        "stalls": stall_nos,
        "health_last": health,
        "categories_seen": len((categories or {}).get("categories", [])),
        "data_dir": str(server.data_dir),
        "data_dir_reason": server.data_dir_reason,
        "port": server.port,
        "run_id": run_id,
        "ready_elapsed_s": round(server.ready_elapsed_s, 3),
        "system_digest_before": digest_before,
        "system_digest_after": system_tree_digest(),
        "coverage": client.coverage.as_dict(),
        "latency": latency_summary(client.latencies_ms),
        "calls": len(client.calls),
        "response_bodies": len(client.bodies),
        "responses_path": str(responses_path),
        "idempotency_replay": replay_evidence,
        "server_log": str(server.log_path),
    }
    write_report(out, report)
    return report
