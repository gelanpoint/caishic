"""首日用一次性的**契约端点探针**：§3.33~§3.38 六个端点各真实调用一次（`T-SIM-07` 判据①）。

**为什么单独成文件**：这六条端点的调用与断言是"契约覆盖"这一件事，
和 `live_run.py` 的"30 个营业日业务流"不是同一个职责；混在一起只会让两边都难读，
也会撞上 `quality-gates.md` §1.2 的 400 行上限。**按职责拆，不按行数切。**

**为什么必须真调**（而不是"把端点名字加进 `ENDPOINTS`"）：判据①的覆盖清单是从**实际请求**
反推的（`live_adapter.Coverage`）—— 只写清单而没人调，`covered_count` 不会涨，判据当场变红。
所以"清单补齐到 38"与"38 条都真被调过一次"是同一条验收的两半，缺一半就等于没做。

**副作用隔离**：探针会注册商家、建会话、新增并改价一个商品、新开一笔交易再取消 ——
全部落在**新注册的 `D-xx` 摊位**上，不碰场景交易的摊位。这样 30 天的商品集合与流水不被探针改写，
场景仍然可复现；代价只是系统里多一个零流水的摊位。

本模块**不 import `app`**（`sim/**` 的硬约束）：它只通过 `LiveAdapter` 走 HTTP 契约。
"""

from __future__ import annotations

import re


def probe_contract_endpoints(adapter, business_date: str, seed: int) -> dict:
    """**首日一次性探针**：把 §3.33~§3.38 六个端点各**真实调用**一次（`T-SIM-07` 判据①）。

    为什么必须真调而不是"把名字加进清单"：判据①的覆盖清单是从**实际请求**反推的
    （`live_adapter.Coverage`），只写清单而没人调 ⇒ `covered_count` 不涨、判据当场变红。
    "清单补齐"与"真调一次"是同一条验收的两半。

    **全部动作落在一个新建的 `D-xx` 摊位上**（§3.34 注册时服务端分配的），而不是既有场景摊位：
    探针会新增商品、新开交易并取消，落在场景摊位上就会改变后续 30 天的商品集合与流水。
    隔离到新摊位的代价只是"系统里多一个零流水的摊位"，换来的是**场景可复现性**。

    业务级断言放在这里（不在类型化调用里），因为只有这里同时握着**真实数据**与**上下文**：

    * §3.34 收款码**只能回脱敏值**（`REQ-024`）—— 断言响应既不出现原文，也不出现 16~19 位数字串；
    * §3.35 先造一笔**未确认**的交易（`priced`）再取消：同键重发 ⇒ `is_duplicate=true`；
      换键再取消 ⇒ `MT-1001`(409) 终态；**不拿已 `paid` 的交易当取消对象**（契约 §3.35 会回 409）；
    * §3.36 的 `business_date` 必须回显请求的那一天；
    * §3.37 的 `interval_days` 必须是 2（契约硬值）；
    * §3.38 **必须带 `X-Stall-Session`**：不带会话头 ⇒ `MT-1005`(401)（`REQ-032`）；
      并且**新增回 201、修改回 200**（状态码由领域层按"是否新建"给出，本层不猜）。
    """
    # --- §3.33 已注册商家列表（注册前后各一次：注册确实改变了列表） ---
    before = adapter.demo_merchants()
    assert isinstance(before.get("items"), list), f"§3.33 应回 items 数组：{before}"

    # --- §3.34 商家注册（收款码原文只进请求体一次，绝不进报告/日志） ---
    tag = str(seed)[-6:]
    plain_code = f"SIMCODE-{tag}-8888"
    registered = adapter.demo_register_merchant(f"仿真商家{tag}", "13800000000", plain_code)
    after = adapter.demo_merchants()
    codes = [item.get("receiver_token_masked") for item in after["items"]]
    assert registered.get("stall_no") and registered.get("merchant_id"), f"§3.34 应回商家与摊位：{registered}"
    assert len(after["items"]) == len(before["items"]) + 1, "§3.34 注册后商家数应 +1"
    assert any(code and "****" in code for code in codes), f"§3.34 列表里应有脱敏收款码：{codes}"
    assert all(code != plain_code for code in codes), "§3.34 明文收款码不得回显（REQ-024）"
    assert not any(re.search(r"\d{16,19}", code or "") for code in codes), "§3.34 脱敏值不得是长数字串"

    # --- 新摊位建会话（§3.2）；下面 §3.38/§3.6/§3.35 都在这个摊位上做 ---
    probe_session = adapter.create_session(registered["stall_no"])["session_token"]

    # --- §3.38 秤端会话端点：**不带会话头必须 401**，带上则新增 201 / 修改 200 ---
    unauth_status, unauth_payload = adapter.client.request(
        "POST", "/api/merchant/products",
        body={"name": f"仿真商品{tag}", "unit_price_cents": 199}, expect=(401,),
    )
    assert unauth_payload["error"]["code"] == "MT-1005", f"§3.38 无会话头应回 MT-1005：{unauth_payload}"
    created_status, created_product = adapter.upsert_product(probe_session, f"仿真商品{tag}", 199)
    updated_status, updated_product = adapter.upsert_product(
        probe_session, f"仿真商品{tag}", 299, product_id=int(created_product["product_id"]))
    assert created_status == 201, f"§3.38 新增必须回 201（实得 {created_status}）：{created_product}"
    assert updated_status == 200, f"§3.38 修改必须回 200（实得 {updated_status}）：{updated_product}"
    assert int(updated_product["product_id"]) == int(created_product["product_id"]), "§3.38 修改应改同一商品"
    assert int(updated_product["unit_price_cents"]) == 299, f"§3.38 当日单价应更新：{updated_product}"

    # --- §3.6 计价产生一笔**未确认**交易，§3.35 把它取消掉（取消对象绝不能是已 paid 的） ---
    probe_key = f"sim-probe-cancel-{business_date}"
    listed = adapter.list_products(probe_session)
    probe_product_id = int(next(row["id"] for row in listed
                               if int(row["id"]) == int(created_product["product_id"])))
    _created_status, created = adapter.create_transaction(
        probe_session, [{"product_id": probe_product_id, "weight_grams": 500}], probe_key,
    )
    probe_txn = created["transaction_no"]
    assert created["status"] != "paid", f"§3.35 取消对象必须是未确认交易，实得 {created['status']}"
    cancelled = adapter.demo_cancel_transaction(probe_txn, probe_key)
    assert cancelled["status"] == "cancelled" and cancelled["is_duplicate"] is False, \
        f"§3.35 首次取消应回 cancelled/非重复：{cancelled}"
    replayed = adapter.demo_cancel_transaction(probe_txn, probe_key)
    assert replayed["is_duplicate"] is True, f"§3.35 同键重发应回 is_duplicate=true：{replayed}"
    end_status, end_payload = adapter.client.request(
        "POST", f"/api/demo/transactions/{probe_txn}/cancel",
        body={"client_idempotency_key": f"{probe_key}-other"}, expect=(409,),
    )
    assert end_payload["error"]["code"] == "MT-1001", f"§3.35 换键再取消应为终态 MT-1001：{end_payload}"

    # --- §3.36 中台视图（事件表 + 各商家应缴） ---
    hub = adapter.demo_hub(business_date)
    assert hub["business_date"] == business_date, f"§3.36 应回显请求的营业日：{hub}"
    assert isinstance(hub["events"], list) and isinstance(hub["payables"], list), f"§3.36 结构：{hub}"

    # --- §3.37 催缴短信（每隔一天；本期不接网关，落表留痕） ---
    sms = adapter.demo_sms_reminders(business_date)
    assert sms["interval_days"] == 2, f"§3.37 间隔必须是 2（契约硬值）：{sms}"
    assert isinstance(sms["sent"], list) and isinstance(sms["skipped"], list), f"§3.37 结构：{sms}"

    return {
        "business_date": business_date,
        "demo_stall_no": registered["stall_no"],
        "demo_merchant_id": int(registered["merchant_id"]),
        "demo_receiver_masked": registered["receiver_token_masked"],
        "product_id": int(created_product["product_id"]),
        "product_http": {"created": created_status, "updated": updated_status},
        "product_price_cents": int(updated_product["unit_price_cents"]),
        "cancelled_transaction_no": probe_txn,
        "cancel_replayed_is_duplicate": replayed["is_duplicate"],
        "cancel_other_key_http": end_status,
        "hub_events": len(hub["events"]),
        "hub_payables": len(hub["payables"]),
        "sms_sent": len(sms["sent"]),
        "sms_skipped": len(sms["skipped"]),
        "product_without_session_http": unauth_status,
    }
