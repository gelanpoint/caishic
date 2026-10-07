"""契约 §2 六类端点的**类型化调用**（`T-SIM-07`；`docs/sim-design.md` §2.3）。

沿用 `live_adapter.py` 的三条纪律：只用标准库、不复用契约夹具、覆盖由**实际请求**反推。
本模块只负责"按契约拼请求 + 用 `expect=` 断言状态码"；业务级断言（脱敏、幂等、201/200 之分）
在 `live_run.py` 的首日探针里对着**真实响应**做。

"""

from __future__ import annotations

from typing import Any

from .live_adapter import IDEMPOTENCY_HEADER, SESSION_HEADER, JsonClient


class LiveAdapter:
    """38 个端点的类型化调用（每条都注明契约小节号，便于回查）。

    ## 为什么单独成文件（而不是留在 `live_adapter.py` 里）

    契约 §2 的端点从 32 条长到 38 条后，`live_adapter.py`（清单 + 覆盖记账 + 客户端 + 调用层）
    会超 `quality-gates.md` §1.2 的 400 行上限。按**职责**拆而不是按行数切：
    本文件只放"怎么按契约拼请求"（类型化调用），清单与覆盖记账留在 `live_adapter.py`。
    **不得**为了少一个文件而把清单与调用揉在一起 —— 那正是"检查退化成摆设"的温床。
    """

    def __init__(self, client: JsonClient) -> None:
        self.client = client

    # -- 秤端 -------------------------------------------------------------
    def healthz(self):  # §3.1
        return self.client.request("GET", "/healthz", expect=(200,))[1]

    def create_session(self, stall_no: str):  # §3.2
        return self.client.request("POST", "/api/merchant/session", body={"stall_no": stall_no},
                                   expect=(201,))[1]

    def get_price_list(self, session: str, business_date: str):  # §3.3
        return self.client.request("GET", "/api/merchant/price-list",
                                   headers={SESSION_HEADER: session}, query={"business_date": business_date},
                                   expect=(200,))[1]

    def put_price_list(self, session: str, business_date: str, *, copy_previous: bool = True,
                       items: list | None = None):  # §3.4
        body: dict[str, Any] = {"business_date": business_date, "copy_from_previous_day": copy_previous}
        if items is not None:
            body["items"] = items
        return self.client.request("POST", "/api/merchant/price-list", body=body,
                                   headers={SESSION_HEADER: session}, expect=(200,))[1]

    def list_products(self, session: str):  # §3.5
        return self.client.request("GET", "/api/merchant/products", headers={SESSION_HEADER: session},
                                   expect=(200,))[1]

    def set_product_status(self, session: str, product_id: int, *, status: str):  # §3.32
        """上架／下架本摊位商品（`REQ-049`/`AC-039`）。`status` 合法取值 `active` / `inactive`。

        路径参数用**字符串**拼接（与契约 §2 表一致）：本仓既有惯例是**不用** `<int:...>`
        转换器 —— 坏标识若在路由层就被拦掉，会变成契约之外的 404，掩盖领域层的错误码。
        """
        return self.client.request("POST", f"/api/merchant/products/{product_id}/status",
                                   body={"status": status}, headers={SESSION_HEADER: session},
                                   expect=(200,))[1]

    def upsert_product(self, session: str, name: str, unit_price_cents: int, *,
                       category_code: str | None = None, icon_key: str | None = None,
                       hotkey: str | None = None, product_id: int | None = None):  # §3.38
        """新增（201）或修改（200）本摊位商品，并写**当日**价目表（`REQ-058`/`AC-048`）。

        **必须带 `X-Stall-Session`**：`stall_id` 只从会话取（`REQ-032`）—— 不带会话头会回
        `MT-1005`(401)，这条由首日探针显式验证（不能只在注释里声明）。
        不带 `product_id` ⇒ 新增；带上 ⇒ 修改（须属本摊位，否则 `MT-1004`）。

        **回 `(状态码, 响应体)`**（与 `create_transaction` 同约定）：契约把 **201=新增 / 200=修改**
        写进了响应定义，状态码本身就是判据，不能把它吞掉只留响应体。
        """
        body: dict[str, Any] = {"name": name, "unit_price_cents": unit_price_cents}
        for field_name, value in (("category_code", category_code), ("icon_key", icon_key),
                                  ("hotkey", hotkey), ("product_id", product_id)):
            if value is not None:
                body[field_name] = value
        return self.client.request("POST", "/api/merchant/products", body=body,
                                   headers={SESSION_HEADER: session}, expect=(200, 201))  # (状态, 体)

    def create_transaction(self, session: str, items: list, idempotency_key: str):  # §3.6
        return self.client.request("POST", "/api/merchant/transactions",
                                   body={"items": items, "client_idempotency_key": idempotency_key},
                                   headers={SESSION_HEADER: session, IDEMPOTENCY_HEADER: idempotency_key},
                                   expect=(200, 201))

    def list_transactions(self, session: str, *, business_date: str | None = None,
                          limit: int | None = None):  # §3.7
        query = {k: v for k, v in (("business_date", business_date), ("limit", limit)) if v is not None}
        return self.client.request("GET", "/api/merchant/transactions", headers={SESSION_HEADER: session},
                                   query=query or None, expect=(200,))[1]

    def get_transaction(self, session: str, transaction_no: str):  # §3.8
        return self.client.request("GET", f"/api/merchant/transactions/{transaction_no}",
                                   headers={SESSION_HEADER: session}, expect=(200,))[1]

    def price_change(self, session: str, transaction_no: str, body: dict):  # §3.9
        return self.client.request("POST", f"/api/merchant/transactions/{transaction_no}/price-change",
                                   body=body, headers={SESSION_HEADER: session}, expect=(200,))[1]

    def pay(self, session: str, transaction_no: str, *, method: str, idempotency_key: str,
            operator: str | None = None):  # §3.10
        body: dict[str, Any] = {"method": method}
        if operator is not None:
            body["operator"] = operator
        return self.client.request("POST", f"/api/merchant/transactions/{transaction_no}/payment",
                                   body=body, headers={SESSION_HEADER: session,
                                                       IDEMPOTENCY_HEADER: idempotency_key},
                                   expect=(200, 202))

    def refund(self, session: str, transaction_no: str, *, amount_cents: int,
               idempotency_key: str):  # §3.11
        return self.client.request("POST", f"/api/merchant/transactions/{transaction_no}/refund",
                                   body={"amount_cents": amount_cents},
                                   headers={SESSION_HEADER: session, IDEMPOTENCY_HEADER: idempotency_key},
                                   expect=(200,))[1]

    def merchant_dashboard(self, session: str, *, business_date: str | None = None):  # §3.12
        query = {"business_date": business_date} if business_date else None
        return self.client.request("GET", "/api/merchant/dashboard", headers={SESSION_HEADER: session},
                                   query=query, expect=(200,))[1]

    # -- 离线暂存 ---------------------------------------------------------
    def offline_queue_status(self, session: str):  # §3.13
        return self.client.request("GET", "/api/merchant/offline/queue", headers={SESSION_HEADER: session},
                                   expect=(200,))[1]

    def stage_offline(self, session: str, items: list, idempotency_key: str):  # §3.14
        return self.client.request("POST", "/api/merchant/offline/queue",
                                   body={"items": items, "client_idempotency_key": idempotency_key},
                                   headers={SESSION_HEADER: session, IDEMPOTENCY_HEADER: idempotency_key},
                                   expect=(201,))[1]

    def offline_sync(self, session: str):  # §3.15
        return self.client.request("POST", "/api/merchant/offline/sync", body={},
                                   headers={SESSION_HEADER: session}, expect=(200,))[1]

    # -- 进程内 Mock ------------------------------------------------------
    def mock_scale_reading(self, weight_grams: int):  # §3.16
        return self.client.request("POST", "/api/mock/scale/reading", body={"weight_grams": weight_grams},
                                   expect=(200,))[1]

    def mock_payment_callback(self, callback_no: str, payment_no: str, result: str):  # §3.17
        return self.client.request("POST", "/api/mock/payment/callback",
                                   body={"callback_no": callback_no, "payment_no": payment_no, "result": result},
                                   expect=(200,))[1]

    # -- 顾客扫码页 -------------------------------------------------------
    def customer_stall_profile(self, stall_no: str):  # §3.18
        return self.client.request("GET", f"/api/customer/stalls/{stall_no}/profile", expect=(200,))[1]

    def customer_receipt(self, transaction_no: str):  # §3.19
        return self.client.request("GET", f"/api/customer/receipts/{transaction_no}", expect=(200,))[1]

    # -- 运营端 -----------------------------------------------------------
    def admin_categories(self):  # §3.20
        return self.client.request("GET", "/api/admin/categories", expect=(200,))[1]

    def admin_create_category(self, code: str, name: str, status: str = "active"):  # §3.21
        return self.client.request("POST", "/api/admin/categories",
                                   body={"code": code, "name": name, "status": status}, expect=(201,))[1]

    def admin_create_alias(self, stall_no: str, alias_name: str, category_id: int):  # §3.22
        return self.client.request("POST", "/api/admin/aliases",
                                   body={"stall_no": stall_no, "alias_name": alias_name,
                                         "category_id": category_id}, expect=(201,))[1]

    def admin_commission_rules(self):  # §3.23
        return self.client.request("GET", "/api/admin/commission-rules", expect=(200,))[1]

    def admin_put_commission_rule(self, *, pay_object: str, rate_bp: int, effective_from: str,
                                  category_tier: str | None = None,
                                  effective_to: str | None = None):  # §3.24
        body: dict[str, Any] = {"pay_object": pay_object, "rate_bp": rate_bp, "effective_from": effective_from}
        if category_tier is not None:
            body["category_tier"] = category_tier
        if effective_to is not None:
            body["effective_to"] = effective_to
        return self.client.request("PUT", "/api/admin/commission-rules", body=body, expect=(200,))[1]

    def admin_dashboard(self, *, business_date: str | None = None):  # §3.25
        query = {"business_date": business_date} if business_date else None
        return self.client.request("GET", "/api/admin/dashboard", query=query, expect=(200,))[1]

    def admin_daily_aggregate(self, business_date: str, stall_no: str | None = None):  # §3.26
        body: dict[str, Any] = {"business_date": business_date}
        if stall_no is not None:
            body["stall_no"] = stall_no
        return self.client.request("POST", "/api/admin/daily-aggregate", body=body, expect=(200,))[1]

    def admin_create_settlement(self, stall_no: str, period_start: str, period_end: str):  # §3.27
        return self.client.request("POST", "/api/admin/settlements",
                                   body={"stall_no": stall_no, "period_start": period_start,
                                         "period_end": period_end}, expect=(201,))[1]

    def admin_settlements(self, *, stall_no: str | None = None, period_start: str | None = None,
                          period_end: str | None = None):  # §3.28
        query = {k: v for k, v in (("stall_no", stall_no), ("period_start", period_start),
                                   ("period_end", period_end)) if v is not None}
        return self.client.request("GET", "/api/admin/settlements", query=query or None, expect=(200,))[1]

    def admin_reconciliation(self, business_date: str, stall_no: str | None = None):  # §3.29
        query = {"business_date": business_date}
        if stall_no is not None:
            query["stall_no"] = stall_no
        return self.client.request("GET", "/api/admin/reconciliation", query=query, expect=(200,))[1]

    def admin_usage_metrics(self, business_date: str):  # §3.30
        return self.client.request("GET", "/api/admin/metrics/usage", query={"business_date": business_date},
                                   expect=(200,))[1]

    def admin_audit_logs(self, *, stall_no: str | None = None, event_type: str | None = None,
                         date_from: str | None = None, date_to: str | None = None,
                         limit: int | None = None):  # §3.31
        query = {k: v for k, v in (("stall_no", stall_no), ("event_type", event_type), ("from", date_from),
                                   ("to", date_to), ("limit", limit)) if v is not None}
        return self.client.request("GET", "/api/admin/audit-logs", query=query or None, expect=(200,))[1]

    # -- 演示控制台（§3.33~§3.37：**无需认证**，仅本机演示；与 Mock 组同一纪律） -----
    def demo_merchants(self):  # §3.33
        """已注册商家列表（只回**脱敏**值；空列表回 200 + `items: []`）。"""
        return self.client.request("GET", "/api/demo/merchants", expect=(200,))[1]

    def demo_register_merchant(self, merchant_name: str, phone: str, receiver_code: str):  # §3.34
        """商家注册 → 201。`receiver_code` 是**收款码原文**：本方法只把它放进请求体一次，
        绝不落进 sim 的日志或报告（契约：明文只存在于本次请求的内存中）。"""
        return self.client.request(
            "POST", "/api/demo/merchants",
            body={"merchant_name": merchant_name, "phone": phone, "receiver_code": receiver_code},
            expect=(201,),
        )[1]

    def demo_cancel_transaction(self, transaction_no: str, idempotency_key: str):  # §3.35
        """取消并撤销一笔**尚未确认**的交易 → 200。

        幂等键在**请求体**里（契约 §3.35 只列了 `client_idempotency_key`，没有请求头），
        与 §3.6/§3.10 的 `Idempotency-Key` 头不是同一处 —— 重复提交同键 ⇒ `is_duplicate: true`。
        """
        return self.client.request("POST", f"/api/demo/transactions/{transaction_no}/cancel",
                                   body={"client_idempotency_key": idempotency_key}, expect=(200,))[1]

    def demo_hub(self, business_date: str | None = None):  # §3.36
        """中台视图：事件表 + 各商家应缴金额。"""
        query = {"business_date": business_date} if business_date else None
        return self.client.request("GET", "/api/demo/hub", query=query, expect=(200,))[1]

    def demo_sms_reminders(self, business_date: str | None = None):  # §3.37
        """触发催缴短信（**每隔一天**；本期不接真实网关，落表留痕）。"""
        body = {"business_date": business_date} if business_date else {}
        return self.client.request("POST", "/api/demo/sms-reminders", body=body, expect=(200,))[1]

