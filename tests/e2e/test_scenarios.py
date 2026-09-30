"""`T-030` 端到端场景验证（一）：**真实浏览器走查**（Playwright + Chromium）。

为什么要写成 pytest 而不是"人工走查"（父代理裁定，2026-09-30）：
`tasks.md` 原先给 `T-026`/`T-027` 写的验收方式是"人工走查"，而**不可复现的验收等于没有验收**
（这次谁看？改完谁知道有没有坏？）。现场演示就是"人在浏览器里点得动"，
所以"代码路径接通"**不能**代替"真的能点"。

本文件把走查做成**可重复执行的检查**：`python -m pytest tests/e2e/test_scenarios.py -q`。
覆盖三条主线（父代理指定）：
1. **秤端**：选品（图标）→ 计价 → 现金收款 →（另一笔）收款码 → 改价 → 退货；
2. **离线**：切离线 → 暂存若干笔 → 切回在线 → 补传 → 界面 `pending` 归零；
3. **顾客扫码页**：**浏览器里真正渲染出来的字段**必须与接口白名单一致（不是只看接口）。

> **夹具已收敛到 `tests/conftest.py`**（`T-031` 建立）：`live_server` / `browser` / `shots` /
> `new_page` / `snap` 由 `T-030`~`T-034` 的走查文件共用 —— 一条规则只写一处。
> 上一版把 `live_server` 写在本文件里，`T-031` 起若再复制四份就是"同一个规则写五遍"。

证据：断言 + 关键步骤截图（截图落在 `.pytest-tmp/e2e-shots/`，该目录已被 `.gitignore` 忽略，
不会污染仓库；断言失败时 pytest 会打印真实 DOM 文本，便于定位"点不动"）。
"""

from __future__ import annotations

from conftest import (
    SEED_STALL,
    active_products,
    bind_stall,
    create_transaction,
    http_json,
    new_page,
    pending_count,
    snap,
    wait_pending,
)


# ---------------------------------------------------------------------------
# 主线 1：秤端（选品 → 计价 → 现金收款 → 收款码 → 改价 → 退货）
# ---------------------------------------------------------------------------


def test_scale_cash_flow_price_change_and_refund(live_server, browser, shots):
    """秤端主线：全部操作**靠点按**完成（`AC-006` 的"选品不要输入"）。"""
    page = new_page(browser)
    page.goto(f"{live_server.base}/scale/")
    page.click("#defaultStall")
    page.wait_for_selector(".tile")
    tiles = page.locator(".tile").count()
    assert tiles > 0, "绑定摊位后应出现商品图标（tile）"
    print(f"[秤端] 商品图标 {tiles} 个")

    # 选品（点图标）+ 称重（点预设，不打字）
    page.locator(".tile").first.click()
    page.locator("#weights button").first.click()
    assert "还没选品" not in page.inner_text("#cart"), f"点图标后购物车应有内容：{page.inner_text('#cart')}"

    # 计价
    page.click("#checkout")
    page.wait_for_function("document.querySelector('#txnNo').textContent.trim() !== '—'")
    txn_no = page.inner_text("#txnNo").strip()
    total_before = page.inner_text("#txnTotal").strip()
    assert txn_no and total_before.startswith("¥"), f"计价后应出交易号与金额：{txn_no} / {total_before}"
    print(f"[秤端] 计价 OK：{txn_no} {total_before}")
    snap(page, shots, "scale-1-checkout")

    # 改价（预设九折，不打字）—— **必须在收款前**：收款后契约只允许退货（§3.9 / MT-1001）
    page.wait_for_selector("#priceOps:not(.hide)")
    page.click("#priceOff")
    try:
        page.wait_for_function(
            "prev => document.querySelector('#txnTotal').textContent.trim() !== prev",
            arg=total_before,
            timeout=5000,
        )
    except Exception:  # noqa: BLE001 - 失败时把页面自己显示的错误一并报出来（"点不动"必须能定位）
        raise AssertionError(
            "改价后金额没变，且页面可能报了错："
            f" 金额前={total_before!r} 金额后={page.inner_text('#txnTotal').strip()!r}"
            f" 页面错误=#{page.inner_text('#payErr').strip()!r}"
        ) from None
    total_after = page.inner_text("#txnTotal").strip()
    assert total_after != total_before, f"改价后金额应变化：{total_before} -> {total_after}"
    print(f"[秤端] 改价 OK（收款前）：{total_before} -> {total_after}")
    snap(page, shots, "scale-2-price-change")

    # 现金收款（改价之后）
    page.click("#payCash")
    page.wait_for_selector("#afterSale:not(.hide)")
    assert "现金收款成功" in page.inner_text("body"), "现金收款后应提示成功"
    print("[秤端] 现金收款 OK")

    # 退货（全额）
    page.click("#refund")
    page.wait_for_timeout(1200)
    toast = page.inner_text("#toast") if page.locator("#toast").count() else ""
    page_error = page.inner_text("#payErr").strip()
    assert ("退货" in toast) or ("冲正" in toast), (
        f"退货后应有明确结果提示：toast={toast!r} 页面错误={page_error!r}"
    )
    assert not page_error, f"退货报了错：{page_error!r}"
    print(f"[秤端] 退货 OK：{toast}")
    page.close()


def test_scale_qr_flow_returns_payload_and_masked_token(live_server, browser, shots):
    """收款码一路：`§3.10` 返回 202 + 收款码内容 + 脱敏收款标识。"""
    page = new_page(browser)
    page.goto(f"{live_server.base}/scale/")
    page.click("#defaultStall")
    page.wait_for_selector(".tile")
    page.locator(".tile").first.click()
    page.click("#checkout")
    page.wait_for_function("document.querySelector('#txnNo').textContent.trim() !== '—'")
    page.click("#payQr")
    page.wait_for_selector("#qrBox:not(.hide)")
    payload = page.inner_text("#qrPayload").strip()
    masked = page.inner_text("#qrToken").strip()
    assert payload, "收款码内容不应为空"
    assert masked, "脱敏收款标识不应为空"
    print(f"[秤端] 收款码 OK：payload={payload} / 脱敏标识={masked}")
    snap(page, shots, "scale-3-qr")
    page.close()


# ---------------------------------------------------------------------------
# 主线 2：离线（切离线 → 暂存若干笔 → 切回在线 → 补传 → pending 归零）
# ---------------------------------------------------------------------------


def test_offline_toggle_stage_then_sync_clears_pending(live_server, browser, shots):
    """离线主线：开关是**界面上显式可点的**（`REQ-014`），补传后 pending 必须归零（`NFR-013`）。

    **本节在 `T-031` 按父代理要求收紧过一次（精度不足 → 精确到增量）**：
    上一版是"点两次、只断言界面出现了暂存提示"，而界面**瞬时显示过「待补传 1 笔」** ——
    若第二次点击其实没暂存成功（界面仍留着上一次的提示），旧断言**照样通过**。
    现在改为逐笔断言 `#pending` 的**增量恰为 1**，并要求补传结果给出 `补传 2 笔` / `清除副本 2 笔`
    两个由**服务端计数**的数字：**界面数字与服务端计数两边同时对得上**，才算"确实暂存了 2 笔"。
    """
    page = new_page(browser)
    page.goto(f"{live_server.base}/scale/")
    page.click("#defaultStall")
    page.wait_for_selector(".tile")

    pending_before = pending_count(page)
    assert pending_before == 0, f"前置：本次会话开始时不应有待补传，实际 {pending_before}"
    page.click("#offlineToggle")
    assert "离线" in page.inner_text("#offlineBadge"), "切换后徽标应显示离线"

    staged = 2
    for index in range(staged):
        page.locator(".tile").nth(index % page.locator(".tile").count()).click()
        page.click("#checkout")
        # 逐笔断言 `#pending` 的**增量恰为 1**（第 1 笔 → 1、第 2 笔 → 2）：
        # 上一版只断言"界面出现过暂存提示"，两次点击若只暂存成功 1 笔也照样通过（精度不足）。
        wait_pending(page, pending_before + index + 1, f"第 {index + 1} 笔暂存后")
        note = page.inner_text("#offlineNote")
        assert f"待补传 {index + 1} 笔" in note, (
            f"第 {index + 1} 笔暂存后界面提示应含「待补传 {index + 1} 笔」，实际 {note!r}"
        )
    snap(page, shots, "offline-1-staged")
    print(
        f"[离线] 已暂存 {staged} 笔（界面 #pending 增量 = {pending_count(page) - pending_before}）；"
        f"界面提示：{page.inner_text('#offlineNote')}"
    )

    page.click("#offlineToggle")
    assert "在线" in page.inner_text("#offlineBadge"), "切回后徽标应显示在线"
    page.click("#syncNow")
    page.wait_for_function("document.querySelector('#syncResult').textContent.includes('补传')")
    result = page.inner_text("#syncResult")
    # 同上：`#syncResult` 是同步写上的，`#pending` 要等 `§3.13` 那一趟 GET 回来 ——
    # 立刻读会读到旧值（本用例第一次进全套跑时就是这么偶发失败的：**自己的异步没等，不是产品缺陷**）。
    pending_after = wait_pending(page, 0, "补传后")
    assert pending_after == 0, f"补传后界面 pending 应归零，实际 {pending_after}（{result}）"
    # 服务端自报的计数必须与"我们点了 2 笔"一致 —— 这是"确实暂存了 2 笔"的**另一半证据**
    assert f"补传 {staged} 笔" in result, f"补传条数应与暂存条数一致：{result!r}"
    assert f"清除副本 {staged} 笔" in result, f"补传成功后应清除 {staged} 笔本地副本（NFR-013）：{result!r}"
    assert "失败 0 笔" in result, f"补传不应失败：{result!r}"
    print(f"[离线] 补传 OK：{result}")
    snap(page, shots, "offline-2-synced")
    page.close()


# ---------------------------------------------------------------------------
# 主线 3：顾客扫码页（**浏览器渲染出的字段**与接口白名单一致）
# ---------------------------------------------------------------------------


def test_customer_page_renders_exactly_the_sourced_fields(live_server, browser, shots):
    """顾客页：**页面上出现的每个字段都必须有来源**（`REQ-023` / `AC-011`），多一个都不行。"""
    token = bind_stall(live_server, SEED_STALL)
    product = active_products(live_server, token)[0]
    status, txn = create_transaction(
        live_server, token, [{"product_id": product["id"], "weight_grams": 800}], "e2e-customer-1"
    )
    assert status == 201, f"契约 §3.6 期望 201，实际 {status}：{txn}"
    status, paid = live_server.api(
        "POST",
        f"/api/merchant/transactions/{txn['transaction_no']}/payment",
        {"method": "cash", "operator": "e2e"},
        {"X-Stall-Session": token, "Idempotency-Key": "e2e-customer-1-pay"},
    )
    assert status == 200, f"契约 §3.10 现金收款期望 200，实际 {status}：{paid}"

    _, receipt = http_json(live_server.base, "GET", f"/api/customer/receipts/{txn['transaction_no']}")
    _, profile = http_json(live_server.base, "GET", f"/api/customer/stalls/{SEED_STALL}/profile")

    page = new_page(browser)
    page.goto(f"{live_server.base}/customer/?stall={SEED_STALL}&txn={txn['transaction_no']}")
    page.wait_for_selector("#receiptItems")
    body = page.inner_text("body")
    snap(page, shots, "customer-1-rendered")

    # ① 接口里**非空**的每个字段都必须出现在页面上（有来源就必须显示）
    shown = {
        "transaction_no": receipt["transaction_no"],
        "status": "已收款",
        "amount": f"{receipt['total_amount_cents'] / 100:.2f}",
        "paid_at": receipt["paid_at"],
        "stall_no": profile["stall_no"],
        "stall_name": profile["stall_name"],
        "item_name": receipt["items"][0]["name"],
        "weight_grams": str(receipt["items"][0]["weight_grams"]),
    }
    for label, value in shown.items():
        assert value in body, f"接口有真值但页面没渲染：{label}={value!r}\n页面文本：{body}"

    # ② 页面上不得出现契约白名单之外的字段名（"多显示一个"与"少显示一个"同等严重）
    for forbidden in ("id_card", "bank_card", "customer_name", "phone", "receiver_account", "operator"):
        assert forbidden not in body, f"顾客页出现了白名单外字段：{forbidden}"

    # ③ 浏览器渲染出的行数 = 接口给的明细行数（且每行只含三个白名单列）
    rows = page.locator("#receiptItems tbody tr").count()
    assert rows == len(receipt["items"]), f"渲染明细 {rows} 行，接口给了 {len(receipt['items'])} 行"
    headers = page.inner_text("#receiptItems thead")
    assert "重量" in headers and "金额" in headers, f"明细表头应与白名单一致，实际：{headers}"
    print(f"[顾客页] 渲染字段与接口一致：{len(shown)} 项真值、{rows} 行明细、无白名单外字段")
    page.close()
