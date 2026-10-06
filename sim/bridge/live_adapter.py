"""31 个端点的 **stdlib** HTTP 客户端（`T-SIM-07`；`docs/sim-design.md` §2.3）。

## 三条纪律（都是"写歪了这条设计就失效"的根因，不是风格偏好）

1. **只用标准库**（`http.client` + `json`），**不引任何新依赖** ——
   `ADR-0004` 的运行期白名单只放 Flask；给仿真开个 `requests` 口子等于让"完全离线可跑"变成一句承诺。
2. **不复用 `tests/contract/contract_support.py` 的 HTTP 夹具** —— 夹具可以绕过契约，
   而"`--mode=live` 跑通 = 给契约做了一次业务级压测"这句话只有在**自己按契约拼请求**时才成立。
3. **端点覆盖由实际请求反推**：每次请求拿**真实路径**去匹配契约模板（`{transaction_no}` 之类
   段位用正则吃掉），命中才记账。**调用方不能自报"我调过这个端点"** ——
   自报的清单只能证明清单写对了，证明不了请求真的发出去过。

## 端点清单从哪来

`ENDPOINTS` 是契约 §2 端点总表的**内置副本**（冻结契约的 32 条，按契约顺序）。
`tests/sim/test_live_endpoints.py` 会把这份副本与 `specs/market-trade-flow/contracts/rest-api.md`
§2 表**双向逐条比对** —— 契约改了而这里没改（或反过来），立刻变红。
**不解析 Markdown 来跑**：契约文档不是运行期依赖，它只用来做一致性比对。
"""

from __future__ import annotations

import http.client
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

#: 契约 §2 端点总表（32 条，顺序与契约一致；编号即契约里的序号）
ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("GET", "/healthz"),
    ("POST", "/api/merchant/session"),
    ("GET", "/api/merchant/price-list"),
    ("POST", "/api/merchant/price-list"),
    ("GET", "/api/merchant/products"),
    ("POST", "/api/merchant/transactions"),
    ("GET", "/api/merchant/transactions"),
    ("GET", "/api/merchant/transactions/{transaction_no}"),
    ("POST", "/api/merchant/transactions/{transaction_no}/price-change"),
    ("POST", "/api/merchant/transactions/{transaction_no}/payment"),
    ("POST", "/api/merchant/transactions/{transaction_no}/refund"),
    ("GET", "/api/merchant/dashboard"),
    ("GET", "/api/merchant/offline/queue"),
    ("POST", "/api/merchant/offline/queue"),
    ("POST", "/api/merchant/offline/sync"),
    ("POST", "/api/mock/scale/reading"),
    ("POST", "/api/mock/payment/callback"),
    ("GET", "/api/customer/stalls/{stall_no}/profile"),
    ("GET", "/api/customer/receipts/{transaction_no}"),
    ("GET", "/api/admin/categories"),
    ("POST", "/api/admin/categories"),
    ("POST", "/api/admin/aliases"),
    ("GET", "/api/admin/commission-rules"),
    ("PUT", "/api/admin/commission-rules"),
    ("GET", "/api/admin/dashboard"),
    ("POST", "/api/admin/daily-aggregate"),
    ("POST", "/api/admin/settlements"),
    ("GET", "/api/admin/settlements"),
    ("GET", "/api/admin/reconciliation"),
    ("GET", "/api/admin/metrics/usage"),
    ("GET", "/api/admin/audit-logs"),
    # 契约 §2 第 32 条（`2026-10-06` 新增，`REQ-049`/`AC-039`）：上架／下架本摊位商品。
    # 由演示游戏需求反查发现的**已建模却未实现**的能力缺口（`product.status` 字段与索引
    # 自 `0001_init.sql` 起就存在，却无任何端点能改它）。**按契约顺序追加在末尾**。
    ("POST", "/api/merchant/products/{product_id}/status"),
)

#: 端点键 = `METHOD 模板`；覆盖清单的记账单位
ENDPOINT_KEYS: tuple[str, ...] = tuple(f"{method} {path}" for method, path in ENDPOINTS)

SESSION_HEADER = "X-Stall-Session"
IDEMPOTENCY_HEADER = "Idempotency-Key"

#: 模板里的 `{...}` 段在真实路径里对应一段非 `/` 的字符（交易号、摊位号、单号）
_TEMPLATE_RE = re.compile(r"\{[a-z_]+\}")


class ApiError(RuntimeError):
    """被测系统按契约 §1.2 返回的错误信封（4xx/5xx）。

    **保留原始信封**：`code` / `detail` 原样带出去 —— 仿真侧要能断言"到底是 `MT-1006`
    还是 `MT-1012`"，只留一句 message 等于把契约的判别力丢掉。
    """

    def __init__(self, status: int, payload: Any) -> None:
        envelope = payload if isinstance(payload, dict) else {}
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        self.status = status
        self.code = str(error.get("code") or f"HTTP-{status}")
        self.message = str(error.get("message") or "")
        self.detail = error.get("detail")
        super().__init__(f"{self.code}（HTTP {status}）{self.message}")


# ---------------------------------------------------------------------------
# 覆盖记账
# ---------------------------------------------------------------------------
@dataclass
class Coverage:
    """**由实际请求反推**的端点覆盖清单（判据①的机读产物）。

    `unmatched` 单独留一份：发出去的请求若匹配不到任何契约模板，那本身就是问题
    （要么拼错了路径，要么仿真发明了契约外端点），不能悄悄丢掉。
    """

    hits: dict[str, dict] = field(default_factory=dict)
    unmatched: list[dict] = field(default_factory=list)
    total_requests: int = 0

    def record(self, method: str, path: str, status: int) -> None:
        self.total_requests += 1
        key = match_endpoint(method, path)
        if key is None:
            self.unmatched.append({"method": method, "path": path, "status": status})
            return
        row = self.hits.setdefault(key, {"count": 0, "statuses": []})
        row["count"] += 1
        if status not in row["statuses"]:
            row["statuses"].append(status)

    def as_dict(self) -> dict:
        covered = sorted(self.hits)
        return {
            "expected": list(ENDPOINT_KEYS),
            "covered": covered,
            "covered_count": len(covered),
            "expected_count": len(ENDPOINT_KEYS),
            "missing": [key for key in ENDPOINT_KEYS if key not in self.hits],
            "counts": {key: self.hits[key] for key in covered},
            "unmatched": self.unmatched,
            "total_requests": self.total_requests,
        }


def _compile(path_template: str) -> re.Pattern:
    parts = _TEMPLATE_RE.split(path_template)
    return re.compile("^" + "[^/]+".join(parts) + "$")


#: 模板 → 正则（首次用到时编译；`ENDPOINTS` 在导入时定下，故映射是静态可推导的）
_MATCHERS: dict[tuple[str, str], re.Pattern] = {
    (method, path): _compile(path) for method, path in ENDPOINTS
}


def match_endpoint(method: str, path: str) -> str | None:
    """真实 `method` + 路径 → 契约端点键；匹配不到返回 `None`。**纯函数**（负例可直接喂）。"""
    clean = path.split("?", 1)[0]
    for (tpl_method, tpl_path), regex in _MATCHERS.items():
        if tpl_method == method and regex.match(clean):
            return f"{method} {tpl_path}"
    return None


def coverage_problems(coverage: dict, expected: tuple[str, ...] = ENDPOINT_KEYS) -> list[str]:
    """覆盖清单的问题（空 = 31/31 全覆盖）。**纯函数**：缺一即红，可直接喂合成负例。"""
    problems: list[str] = []
    missing = [key for key in expected if key not in set(coverage.get("covered") or [])]
    if missing:
        problems.append(f"端点覆盖缺口 {len(missing)}/{len(expected)}：{missing}")
    if coverage.get("unmatched"):
        problems.append(f"有请求匹配不到任何契约端点（拼错路径或用了契约外端点）：{coverage['unmatched'][:5]}")
    unknown = sorted(set(coverage.get("covered") or []) - set(expected))
    if unknown:
        problems.append(f"覆盖清单里有契约外的端点：{unknown}")
    return problems


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class JsonClient:
    """按契约拼请求、按契约读响应，并把**每次真实请求**记进覆盖清单与时延样本。

    `record_bodies=True` 时把响应体留在 `bodies` 里（供判据④的敏感扫描逐条扫）——
    **扫描的是真的收到过的字节**，不是"应该返回什么"的推测。
    """

    def __init__(self, base_url: str, *, timeout_s: float = 30.0, record_bodies: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.record_bodies = record_bodies
        self.coverage = Coverage()
        self.latencies_ms: list[float] = []
        self.calls: list[dict] = []
        self.bodies: list[dict] = []
        parsed = self.base_url.split("://", 1)[-1]
        self.host, _, port = parsed.partition(":")
        self.port = int(port or 80)

    def request(self, method: str, path: str, *, body: Any = None, headers: dict | None = None,
                query: dict | None = None, expect: tuple[int, ...] = ()) -> tuple[int, Any]:
        """发一次请求 → `(状态码, JSON 负载)`；不在 `expect` 内则抛 `ApiError`。"""
        url = f"{path}?{urlencode(query)}" if query else path
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        send_headers = {"Accept": "application/json"}
        if payload is not None:
            send_headers["Content-Type"] = "application/json"
        send_headers.update(headers or {})

        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
        started = time.perf_counter()
        try:
            connection.request(method, url, body=payload, headers=send_headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            raise ApiError(0, {"error": {"code": "TRANSPORT", "message": str(exc)}}) from exc
        finally:
            connection.close()
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        try:
            data = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {"__raw__": raw.decode("utf-8", errors="replace")[:2000]}

        key = match_endpoint(method, path)
        self.coverage.record(method, path, status)
        self.latencies_ms.append(elapsed_ms)
        self.calls.append({"endpoint": key or f"{method} {path}", "status": status,
                           "latency_ms": round(elapsed_ms, 3)})
        if self.record_bodies:
            self.bodies.append({"endpoint": key or f"{method} {path}", "status": status, "payload": data})

        if expect and status not in expect:
            raise ApiError(status, data)
        return status, data


class LiveAdapter:
    """31 个端点的类型化调用（每条都注明契约小节号，便于回查）。"""

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
