"""`model ↔ live` 一致性对账（`T-SIM-08`；`docs/sim-design.md` §7.3）。

## 它到底在比什么（先把话说清，否则"一致"这个词没有意义）

**同一笔成交决策流**（同一 `seed`、同一场景臂、同一批 `(摊位, 商品, 重量, 支付方式)`），
一边喂给 `model_adapter` 的进程内账本，一边经 HTTP 打到**真实被测系统**（`app/`），
然后**逐营业日**比对：走秤笔数 / 总额 / 佣金 / 三指标六个分子分母 / 结算单金额，
外加 `reconciliation.balanced`。容差 **≤1 分 / ≤1 笔**。

## 三处**口径差**必须显式处理，不许静默忽略（上一任明确提醒的难点）

1. **私下交易（shadow）**：`model` 侧账本含私下成交，系统侧**不可见**（`M-04` 的分母正是
   `Q-15` 承认系统拿不到的那个外部基准）。**处置**：一致性对账只在**走秤通道**上做；
   shadow 笔数作为 `caliber_gap` 单列，并给出它对 `M-04` 的影响 —— 不是忽略，是量化。
2. **取整与计量单位**：`model` 用「每 500g 单价 + 内置 `round`（银行家舍入）」，
   系统用「每公斤单价 + `half_up_div`（四舍五入）」。**处置**：单价换算成每公斤（×2）后，
   model 侧的期望金额用**系统那一条取整规则**重算；两套规则差 1 分的笔数作为 `rounding_gap` 单列。
   在 sim 侧复刻 `half_up_div` 是**口径对齐**，不是独立校验 —— 真正被校验的是**账本聚合**
   （笔数 / 佣金 / 三指标 / 结算单），报告里写明这一点。
3. **佣金**：`model` 自己按「当日金额 × 费率」浮点累加，系统按**逐摊位逐日**的实收金额 `half_up`
   折算。**处置**：model 侧按系统的逐摊逐日口径**独立复算**再比；两条 model 内部口径的差作为
   `commission_gap` 单列。

## 灵敏度负例（`T-SIM-08` 验收③，**必做**）

`consistency_problems` 是**纯函数**。`run_consistency(tamper=True)` 在拿到**同一份** live 报告后，
真的从 model 侧事件流里**删掉一笔走秤成交**（`skip_txns=1`）再对账，断言结果由绿变红。
「不验证灵敏度的验证是摆设」在本项目没有豁免权。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from ..bridge.live_adapter import JsonClient
from ..bridge.live_calls import LiveAdapter
from ..bridge.server_launcher import launch_server
from ..bridge.live_scenario import commission_plan, find_scenario, load_commission_rule

#: 容差与口径常量在 `sim/verify/model_ledger.py`（两边必须用**同一份**，否则"对账"是自说自话）：
#: `TOLERANCE_CENTS` / `TOLERANCE_TXNS` / `PER_KG` / `half_up_div` / `model_ledger` / `scenario_run` /
#: `price_books_for` / `CONSISTENCY_PROFILE`。本模块再原样转出，保证 `consistency.XXX` 的既有用法不变。
from .model_ledger import (  # noqa: F401  （转出，不是"顺手导入"）
    CONSISTENCY_PROFILE,
    PER_KG,
    TOLERANCE_CENTS,
    TOLERANCE_TXNS,
    half_up_div,
    model_ledger,
    price_books_for,
    scenario_run,
)


# ---------------------------------------------------------------------------
# live 侧：把同一批成交真打到被测系统上
# ---------------------------------------------------------------------------
def replay_live(events, books, ledger, *, plan: dict, profile: dict = CONSISTENCY_PROFILE,
                run_id: str | None = None, out_dir: Path | str = "data/sim/consistency/_live",
                progress=print) -> dict:
    """起被测服务、逐日回放 `ledger` 里的成交，返回逐日（及月末结算单）的系统侧读数。"""
    used: dict[str, list] = {}
    counts: dict[str, int] = {}
    for event in events:
        if event.get("kind") != "txn" or event.get("channel") != "scale":
            continue
        day = event["business_date"]
        if counts.get(day, 0) >= profile["max_txns_per_day"]:
            continue
        counts[day] = counts.get(day, 0) + 1
        used.setdefault(day, []).append(event)
    #: `run_id` **每次必须新一份库**：`MT_DATA_DIR` 复用同一个目录时，上一次跑剩的交易还在库里，
    #: 于是"今天的笔数"会是两次之和 —— 第一版就这样把 20 笔算成了 40 笔，
    #: 而且错得非常"像真的"（差异集中在头几天、比例不是整数、更难一眼看出）。
    token = run_id or f"consistency-{profile['scenario']}-{profile['seed']}-{uuid4().hex[:8]}"
    server = launch_server(run_id=token, out_dir=Path(out_dir), data_dir=None,
                           business_date=profile["start"], port=None, timeout_s=180.0)
    client = JsonClient(server.base_url)
    adapter = LiveAdapter(client)
    sessions: dict[str, str] = {}
    prices: dict[str, dict[int, int]] = {}
    rows: list[dict] = []
    settlements: list[dict] = []
    month_start = profile["start"]
    try:
        #: 佣金口径**复用** `live_scenario` 的装载器（`PUT` 是"新增一条口径"，对着已有库重跑
        #: 必然 `MT-1012`；自己再写一遍 PUT 就会踩同一个坑，且两处处置会分叉）。
        commission_evidence = load_commission_rule(adapter, plan, profile["start"])
        progress(f"  live 侧佣金口径：{plan['rate_bp']}bp pay_object={plan['pay_object']}"
                 f"（规则数 {commission_evidence['rules_before']} → {commission_evidence['rules_after']}）")
        days = sorted(used)
        for index, day in enumerate(days):
            server.clock.set(day)
            for stall_no in sorted(books):
                session = sessions.get(stall_no)
                if session is None:
                    session = adapter.create_session(stall_no)["session_token"]
                    sessions[stall_no] = session
                if stall_no not in prices:
                    adapter.put_price_list(session, day, copy_previous=True)
                    current = adapter.get_price_list(session, day) or {}
                    catalog = sorted(adapter.list_products(session), key=lambda row: int(row["id"]))
                    table = {int(row["id"]): int(item.get("unit_price_cents") or 100)
                             for row in catalog for item in [row]}
                    for item in (current.get("items") or []):
                        table[int(item["product_id"])] = int(item["unit_price_cents"])
                    #: 模型的 3 个商品对到该摊位**按 id 排序的前 3 个**系统商品：
                    #: 两侧品类分类法不同（模型 4 类 × 3 商品 vs 系统 58 标准品类），
                    #: 按序号对齐是唯一不引入语义假设的做法，对齐关系写进报告。
                    for slot, model_product in enumerate(sorted(books[stall_no])[:len(catalog)]):
                        table[int(catalog[slot]["id"])] = max(1, books[stall_no][model_product] * PER_KG)
                    prices[stall_no] = table
                adapter.put_price_list(sessions[stall_no], day, copy_previous=False,
                                       items=[{"product_id": pid, "unit_price_cents": cents}
                                              for pid, cents in sorted(prices[stall_no].items())])
            slots = {stall_no: {product: pid for pid, product in
                                zip(sorted(prices[stall_no]), sorted(books[stall_no])[:len(prices[stall_no])])}
                     for stall_no in books}
            for sequence, event in enumerate(used[day]):
                stall_no = event["stall_no"]
                session = sessions[stall_no]
                product_id = slots[stall_no][event["product_id"]]
                #: 幂等键必须**每笔唯一**（同一顾客同一天买同一商品是完全合法的）——
                #: 少了序号会撞 `MT-1012`（幂等键复用于不同请求体）。
                key = f"cons-{day}-{sequence:04d}-{stall_no}"
                _status, created = adapter.create_transaction(
                    session, [{"product_id": product_id, "weight_grams": int(event["grams"])}], key)
                method = event.get("method") or "cash"
                _pay, payment = adapter.pay(session, created["transaction_no"], method=method,
                                            idempotency_key=f"{key}-pay",
                                            operator="sim-operator" if method == "cash" else None)
                if method == "qr":
                    adapter.mock_payment_callback(f"CB{key}", payment["payment_no"], "success")
            aggregate = adapter.admin_daily_aggregate(day)
            reconciliation = adapter.admin_reconciliation(day)
            usage = adapter.admin_usage_metrics(day)
            market = adapter.admin_dashboard(business_date=day)
            rows.append({"business_date": day, "aggregate": aggregate, "reconciliation": reconciliation,
                         "usage": usage,
                         "txn_count": int((market.get("market") or {}).get("txn_count", 0)),
                         "gross_amount_cents": int((market.get("market") or {}).get("gross_amount_cents", 0)),
                         "commission_cents": int((market.get("market") or {}).get("commission_amount_cents", 0))})
            if index + 1 == len(days) or index + 1 == 30 or (index + 1) % 30 == 0:
                for stall_no in sorted(books):
                    item = adapter.admin_create_settlement(stall_no, month_start, day)
                    settlements.append({"business_date": day, "period_start": month_start,
                                        "period_end": day, "stall_no": stall_no, **item})
                    progress(f"    · 结算单 {item.get('settlement_no')} "
                             f"{item.get('gross_amount_cents')}/{item.get('commission_amount_cents')}")
            if (index + 1) % 30 == 0:
                from datetime import date, timedelta

                month_start = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    finally:
        server.stop()
    return {"days": rows, "settlements": settlements, "rate_bp": plan["rate_bp"],
            "commission_evidence": commission_evidence, "run_id": token,
            "coverage_note": (
                f"按营业日截断到 ≤{profile['max_txns_per_day']} 笔走秤成交 ⇒ 尾部摊位可能整期颗粒无收"
                f"（model 侧走秤率本来就只有 {ledger['caliber_gap']['scale_use_rate_model_side']}）。"
                f"这类摊位两侧同为 0 ⇒ 一致；它们的出现**只影响代表性**，不影响对账结论"),
            "calls": len(client.calls), "coverage": client.coverage.as_dict()}


# ---------------------------------------------------------------------------
# 对账：纯函数（灵敏度负例的靶子）
# ---------------------------------------------------------------------------
def compare(ledger: dict, live: dict, *, stall_count: int) -> dict:
    """逐日比对。**纯函数** —— 喂一份改坏的 `ledger` 必须报出对应的不一致。"""
    live_days = {row["business_date"]: row for row in live["days"]}
    mismatches: list[dict] = []
    checked = 0
    for day in sorted(set(ledger["days"]) & set(live_days)):
        want = ledger["days"][day]
        row = live_days[day]
        usage = row["usage"]
        pairs = [
            ("scale_txns", want["scale_txns"], row["txn_count"]),
            ("gross_amount_cents", want["gross_amount_cents"], row["gross_amount_cents"]),
            ("commission_cents", want["commission_cents"], row["commission_cents"]),
            ("stall_usage_numerator", want["stall_usage_numerator"], usage["stall_usage_numerator"]),
            ("stall_usage_denominator", stall_count, usage["stall_usage_denominator"]),
            ("cash_txn_numerator", want["cash_txn_numerator"], usage["cash_txn_numerator"]),
            ("cash_txn_denominator", want["scale_txns"], usage["cash_txn_denominator"]),
            ("price_list_numerator", stall_count, usage["price_list_numerator"]),
            ("price_list_denominator", stall_count, usage["price_list_denominator"]),
        ]
        for name, expect, actual in pairs:
            checked += 1
            tol = TOLERANCE_TXNS if name.endswith(("txns", "numerator", "denominator")) else TOLERANCE_CENTS
            if abs(int(actual) - int(expect)) > tol:
                mismatches.append({"business_date": day, "field": name, "model": expect,
                                   "live": int(actual), "diff": int(actual) - int(expect), "tolerance": tol})
        if not row["reconciliation"].get("balanced") or int(row["reconciliation"].get("diff_cents", 1)) != 0:
            mismatches.append({"business_date": day, "field": "reconciliation.balanced",
                               "model": True, "live": row["reconciliation"].get("balanced"), "tolerance": 0})
    settlement_mismatches = []
    for item in live.get("settlements", []):
        period_start = item.get("period_start") or item["business_date"]
        period_end = item.get("period_end") or item["business_date"]
        #: 结算单覆盖的是**整个期间**，不是那一天 —— 拿单日金额去比会得到"差一个数量级"这种
        #: 看起来很吓人的假不一致（第一版就是这么写的）。
        want = sum((ledger["days"].get(day, {}).get("per_stall_cents") or {}).get(item["stall_no"], 0)
                   for day in sorted(ledger["days"])
                   if period_start <= day <= period_end)
        if not want and int(item["gross_amount_cents"]) == 0:
            #: 模型侧 0、系统侧也是 0 ⇒ 一致，只是这个摊位当天没摊到成交（走秤率本来就只有 ~0.19，
            #: 而**按日截断**的回放又让尾部摊位更容易颗粒无收）。**这不是不一致，不该报红。**
            continue
        if not want:
            settlement_mismatches.append({"business_date": item["business_date"], "stall_no": item["stall_no"],
                                          "reason": "model 侧该摊位在整个期间没有走秤成交，"
                                                    "但系统结算单有金额 ⇒ 模型账与系统账不是同一批成交"})
            continue
        want_commission = half_up_div(want * live.get("rate_bp", 0), 10000)
        if abs(int(item["gross_amount_cents"]) - int(want)) > TOLERANCE_CENTS:
            settlement_mismatches.append({"business_date": item["business_date"], "stall_no": item["stall_no"],
                                          "period": f"{period_start}~{period_end}",
                                          "field": "gross_amount_cents", "model": want,
                                          "live": int(item["gross_amount_cents"])})
        if abs(int(item["commission_amount_cents"]) - want_commission) > TOLERANCE_CENTS:
            settlement_mismatches.append({"business_date": item["business_date"], "stall_no": item["stall_no"],
                                          "period": f"{period_start}~{period_end}",
                                          "field": "commission_amount_cents", "model": want_commission,
                                          "live": int(item["commission_amount_cents"])})
    return {"days_compared": len(set(ledger["days"]) & set(live_days)), "fields_compared": checked,
            "mismatches": mismatches, "mismatch_count": len(mismatches),
            "settlement_mismatches": settlement_mismatches,
            "settlement_count": len(live.get("settlements", [])),
            "caliber_gap": ledger.get("caliber_gap", {}), "rounding_gap_txns": ledger.get("rounding_gap_txns", 0),
            "skipped_txns": ledger.get("skipped_txns", 0)}


def consistency_problems(comparison: dict) -> list[str]:
    """把比对结果判成问题清单（**空 = 通过**）。**纯函数**：灵敏度负例直接喂改坏的 `comparison`。"""
    problems = []
    if comparison["days_compared"] == 0:
        problems.append("没有任何营业日可比（model 与 live 的营业日集合没有交集）")
    for row in comparison["mismatches"][:20]:
        problems.append(f"{row['business_date']} `{row['field']}`：model={row['model']} live={row['live']}"
                        f" 差 {row['diff']}（容差 {row.get('tolerance')}）")
    if comparison["mismatch_count"] > 20:
        problems.append(f"…… 另有 {comparison['mismatch_count'] - 20} 条不一致")
    for row in comparison["settlement_mismatches"][:20]:
        problems.append(f"结算单 {row.get('stall_no')} {row.get('business_date')} "
                        f"`{row.get('field', '')}`：model={row.get('model')} live={row.get('live')}"
                        f"{row.get('reason', '')}")
    return problems


# ---------------------------------------------------------------------------
# 编排 + 报告
# ---------------------------------------------------------------------------
def run_consistency(params, *, out_dir: Path | str = "data/sim/consistency", days: int | None = None,
                    max_txns_per_day: int | None = None, tamper: bool = True, progress=print) -> dict:
    """跑一次完整的一致性对账（model + live + 灵敏度负例），落 `consistency.json` / `.md`。"""
    from ..observe.verify_report import render_consistency, write_consistency

    profile = dict(CONSISTENCY_PROFILE)
    if days:
        profile["days"] = days
    if max_txns_per_day:
        profile["max_txns_per_day"] = max_txns_per_day
    events, scoped = scenario_run(params, profile=profile)
    scenario = find_scenario(profile["scenario"])
    plan = commission_plan(scenario, scenario["arms"][profile["arm_index"]], params)
    assert plan["rate_bp"] == int(round(float(scoped.value("commission_rate_bp")))), \
        "live 侧要写的佣金口径与 model 侧用的费率不一致 ⇒ 比的就不是同一件事"
    books = price_books_for(scoped, profile=profile)
    ledger = model_ledger(events, books, rate_bp=plan["rate_bp"],
                          max_txns_per_day=profile["max_txns_per_day"])
    progress(f"  model 侧账目：{sum(row['scale_txns'] for row in ledger['days'].values())} 笔走秤 / "
             f"{ledger['caliber_gap']['shadow_txns_total']} 笔私下（口径差，见报告）")
    live = replay_live(events, books, ledger, plan=plan, profile=profile, progress=progress)
    comparison = compare(ledger, live, stall_count=len(books))
    problems = consistency_problems(comparison)
    negative = None
    if tamper:
        tampered = model_ledger(events, books, rate_bp=plan["rate_bp"],
                                max_txns_per_day=profile["max_txns_per_day"], skip_txns=1)
        negative = compare(tampered, live, stall_count=len(books))
        negative["problems"] = consistency_problems(negative)
        negative["expectation"] = "篡改 model 侧一笔成交后，一致性检查**必须**报出至少一条问题"
        negative["red"] = bool(negative["problems"])
        if not negative["red"]:
            problems.append("灵敏度负例失败：篡改 model 侧一笔成交后一致性检查仍然全绿 —— 这套检查是摆设")
        progress(f"  灵敏度负例：篡改 model 一笔 ⇒ {'变红（通过）' if negative['red'] else '仍全绿（不通过）'}"
                 f"（{len(negative['problems'])} 条问题）")
    payload = {"schema_version": 1, "profile": profile, "params_path": params.source,
               "tolerance": {"cents": TOLERANCE_CENTS, "txns": TOLERANCE_TXNS},
               "comparison": comparison, "live_summary": {"calls": live["calls"], "days": len(live["days"]),
                                                          "settlements": len(live["settlements"]),
                                                          "coverage": live["coverage"]},
               "sensitivity_negative": negative, "problems": problems}
    paths = write_consistency(out_dir, payload, render_consistency(payload))
    return {"comparison": comparison, "problems": problems, "negative": negative, "paths": paths,
            "profile": profile}