"""`test_demo_console.py` 的助手（`task-27`）。

与 `contract_support.py` / `scale_support.py` 同一纪律：**助手单独成模块**，
测试文件只放判据 —— 既避免单文件超 `quality-gates.md` §1.2 的行数阈值，也让"怎么造前置"与
"断言什么"分开看。

这里**只做请求与取数**，不做任何判据；判据一律留在测试文件里。
"""

from __future__ import annotations

import time
from pathlib import Path

from contract_support import json_of, session_headers

DEMO_MERCHANTS = "/api/demo/merchants"
DEMO_HUB = "/api/demo/hub"
DEMO_SMS = "/api/demo/sms-reminders"

REGISTER_KEYS = {"merchant_id", "stall_no", "merchant_name", "phone_masked", "receiver_token_masked"}
MERCHANT_KEYS = {"merchant_id", "merchant_name", "stall_no", "stall_name",
                 "receiver_token_masked", "registered_at"}
PAYABLE_KEYS = {"merchant_id", "merchant_name", "stall_no", "paid_txn_count",
                "received_amount_cents", "commission_cents", "payable_cents"}


def half_up(numerator: int, denominator: int) -> int:
    """`data-model.md` §0 的整数四舍五入口径 —— **本文件自己实现**，不调 `app/domain/**`。

    走查要能独立复算服务端的数：用**同一份文档口径**重算一遍，再比对；复用实现函数就是自证。
    """
    return (numerator + denominator // 2) // denominator


def suffix() -> str:
    """每次调用生成唯一后缀（会话级共享库，避免与历史数据撞车）。"""
    return f"{time.time_ns() % 10**9:09d}"


def register(client, *, name: str | None = None, phone: str | None = None, code: str | None = None):
    """注册一个全新商家；返回 `(response, 请求体)`（**请求体留着重放明文用**）。"""
    tag = suffix()
    body = {
        # 名字**必须以非数字收尾**：否则"名字末尾的数字"与紧随其后的 12 位手机号在库文件里
        # 会连成 16~19 位连续数字，把 `test_sensitive_scan.py::test_test_database_file_is_clean`
        # 误判成"银行卡号"（本文件曾因此把别人的用例弄红）。
        "merchant_name": name if name is not None else f"演示商户{tag[:6]}号",
        "phone": phone if phone is not None else f"138{tag}",
        "receiver_code": code if code is not None else f"99{tag}12345",
    }
    return client.post(DEMO_MERCHANTS, json=body), body


def registered(client, **kwargs):
    """注册并断言 201，返回 `(响应体, 请求体)`。"""
    response, body = register(client, **kwargs)
    assert response.status_code == 201, (
        f"契约 §3.34 期望 201，实际 {response.status_code}：{response.get_data(as_text=True)[:300]}"
    )
    return json_of(response), body


def hub(client, day: str | None = None) -> dict:
    """`GET /api/demo/hub`（`day=None` 时让**服务端**决定营业日）。"""
    response = client.get(DEMO_HUB, query_string={"business_date": day} if day else None)
    assert response.status_code == 200, (
        f"契约 §3.36 期望 200，实际 {response.status_code}：{response.get_data(as_text=True)[:300]}"
    )
    return json_of(response)


def payable_of(hub_payload: dict, merchant_id: int) -> dict | None:
    return next((row for row in hub_payload["payables"] if row["merchant_id"] == merchant_id), None)


def pay_cash(client, token: str, transaction_no: str, key: str):
    """契约 §3.10 现金收款（立即完成）。"""
    return client.post(
        f"/api/merchant/transactions/{transaction_no}/payment",
        json={"method": "cash", "operator": "演示"},
        headers=session_headers(token, key),
    )


def add_product(client, token: str, *, price_cents: int = 480, name: str | None = None) -> dict:
    """契约 §3.38：给本摊位建一个**立刻可计价**的商品。

    新注册的演示商家其摊位是**空的**（没有种子商品、也没有当日价目表），§3.6 直接计价会 `MT-1006`；
    §3.38 会同时写 `product` 与**当日** `price_item`，故它是"给新摊位铺货"的正确前置。
    """
    response = client.post(
        "/api/merchant/products",
        json={"name": name or f"演示商品{suffix()[:5]}类", "unit_price_cents": price_cents},
        headers=session_headers(token),
    )
    assert response.status_code == 201, (
        f"契约 §3.38 新增期望 201，实际 {response.status_code}：{response.get_data(as_text=True)[:300]}"
    )
    return json_of(response)


def cancel(client, transaction_no: str, key: str | None):
    """契约 §3.35 取消（`key=None` ⇒ 不带幂等键，用于测 `MT-1008`）。"""
    body = {} if key is None else {"client_idempotency_key": key}
    return client.post(f"/api/demo/transactions/{transaction_no}/cancel", json=body)


def pay_qr(client, token: str, transaction_no: str, key: str):
    """契约 §3.10 收款码收款 ⇒ 202 + `payment_no`（交易仍 `priced`，等回调）。"""
    return client.post(
        f"/api/merchant/transactions/{transaction_no}/payment",
        json={"method": "qr"}, headers=session_headers(token, key),
    )


def callback(client, callback_no: str, payment_no: str, result: str):
    """契约 §3.17 演示回调（`success` / `failed` / `timeout`）。"""
    return client.post(
        "/api/mock/payment/callback",
        json={"callback_no": callback_no, "payment_no": payment_no, "result": result},
    )


def refund(client, token: str, transaction_no: str, amount_cents: int, key: str):
    """契约 §3.11 退货冲正。"""
    return client.post(
        f"/api/merchant/transactions/{transaction_no}/refund",
        json={"amount_cents": amount_cents}, headers=session_headers(token, key),
    )


def dashboard(client, token: str, day: str) -> dict:
    """契约 §3.12 商户看板（其 `commission_amount_cents` 就是 §2.15 日聚合口径）。"""
    response = client.get("/api/merchant/dashboard", query_string={"business_date": day},
                          headers=session_headers(token))
    assert response.status_code == 200, (
        f"契约 §3.12 期望 200，实际 {response.status_code}：{response.get_data(as_text=True)[:200]}"
    )
    return json_of(response)


def usage_metrics(client, day: str) -> dict:
    """契约 §3.30 使用率指标。"""
    response = client.get("/api/admin/metrics/usage", query_string={"business_date": day})
    assert response.status_code == 200, (
        f"契约 §3.30 期望 200，实际 {response.status_code}：{response.get_data(as_text=True)[:200]}"
    )
    return json_of(response)


def db_path(db_conn) -> Path:
    for row in db_conn.execute("PRAGMA database_list"):
        if row["name"] == "main":
            return Path(row["file"])
    raise AssertionError("取不到库文件路径")


def effective_rate_bp(db_conn, day: str) -> int:
    """**自己**按生效期口径挑出当日费率（读数据，不调实现的佣金函数）。"""
    rows = [
        row for row in db_conn.execute(
            "SELECT rate_bp, effective_from, effective_to FROM commission_rule"
        )
        if row["effective_from"] <= day and (row["effective_to"] or "9999-12-31") >= day
    ]
    assert rows, f"{day} 没有生效佣金口径 —— 前置未满足（应缴断言会变成 0 == 0 的假绿）"
    return int(sorted(rows, key=lambda row: row["effective_from"])[-1]["rate_bp"])


def ensure_rule(client, day: str) -> None:
    """确保当日有生效口径；`MT-1012`(409) = 已有一条，容忍（既有 §3.24 的声明行为）。"""
    response = client.put("/api/admin/commission-rules",
                          json={"pay_object": "merchant", "rate_bp": 250, "effective_from": day})
    assert response.status_code in (200, 409), (
        f"契约 §3.24 期望 200 或 409，实际 {response.status_code}：{response.get_data(as_text=True)[:200]}"
    )
