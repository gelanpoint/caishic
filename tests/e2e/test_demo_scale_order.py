"""秤页**交互顺序**的 e2e 判据（`AC-047`④，`2026-10-08` 规格改写后）。

从 `test_demo_three_pages.py` 拆出来，理由是**失败含义不同**：那个文件管"三页能不能开、有没有外链、
中台滚动与催缴"；本文件管**秤页的操作顺序** —— 放置不认品类、选品后才实时算钱、确认才锁价出码。

规格（`REQ-057` / `AC-047`④）：
> 右键放置商品 → 按钮上下选品 → 调整重量并看到金额实时变化 → 确认锁定价格并出收款码 → …
> ④ **放置商品本身不得认定品类、不得显示金额**（秤只感知重量），且点**确认**之前**不得**产生任何
> 交易或收款码 —— 价格**只在确认时锁定**，此前随重量与所选单价**实时**变化。

**期望金额一律用服务端价目表（§3.3）的单价算**，不拿页面自己显示的数去比自己（那会恒真）。
"""

from __future__ import annotations

import re
import time

from e2e_support import (bind_stall, count, http_json, new_page, scalar, session_headers, snap,
                         today_iso)

DEMO_SHOTS = "/home/gelanpoint/caishic-实玩截图/demo"
SCALE_URL = "/demo/scale/"
HUB_URL = "/demo/hub/"
PRODUCT = "测试青菜"
PRICE_CENTS = 777  # 单价取一个"不与任何预置默认价相同"的数，防止把预置价误当服务端价


# --------------------------------------------------------------------------- #
# 助手
# --------------------------------------------------------------------------- #


def _cents(text: str) -> int | None:
    """把页面金额文本读成**分**（页面用 `Demo.yuan` = `(cents/100).toFixed(2)`）。取不到返回 `None`。"""
    match = re.search(r"(\d+)(?:\.(\d{1,2}))?", text.replace(",", ""))
    if match is None:
        return None
    return int(match.group(1)) * 100 + int((match.group(2) or "0").ljust(2, "0"))


def _expected(price_cents: int, grams: int) -> int:
    """秤端实时预览口径：`floor((单价分 × 克 + 500) / 1000)`（与服务端 `half_up_div(金额, 1000)` 同）。"""
    return (price_cents * grams + 500) // 1000


def _choose_merchant(page, name: str) -> None:
    """在秤页选择商家（未选商家不能开工 —— 页面自己的门禁）。"""
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#merchantSel option'))"
        ".some(o => o.textContent.includes(%r))" % name, timeout=15000,
    )
    option = page.eval_on_selector(
        "#merchantSel",
        "(sel, nm) => Array.from(sel.options).find(o => o.textContent.includes(nm)).value",
        name,
    )
    page.select_option("#merchantSel", option)
    page.wait_for_function(
        "() => document.querySelector('#sessionBadge').textContent.length > 0"
        " && !document.querySelector('#sessionBadge').textContent.includes('无商家')", timeout=15000,
    )


def _set_weight(page, grams: int, *, applied: bool = True) -> None:
    """拖动重量滑杆（`REQ-057` 的"调整重量"）。

    `applied=False` 用于**确认之后**：那时页面按设计忽略重量变化（"价格已锁定"），
    等它生效只会等超时 —— 那是**正确的实现行为**，不是判据要的。
    """
    page.evaluate(
        "(g) => { const r = document.querySelector('#weightRange'); r.value = String(g);"
        " r.dispatchEvent(new Event('input', { bubbles: true })); }", grams,
    )
    if applied:
        page.wait_for_function(
            "(g) => document.querySelector('#ledWeight').textContent.trim() === String(g)",
            arg=grams, timeout=15000,
        )
    else:
        page.wait_for_timeout(300)


def _register(live_server, product_name: str, price_cents: int) -> tuple[str, str, dict]:
    """注册商家（§3.34）+ 用 §3.38 建一件挂在 `item_0` 图标上的商品，返回 `(摊位号, 商家名, 价目表)`。

    **必须带 `icon_key`**：秤端网格是 8 个预置图标按 `icon_key` 与商品配对（`scale-view.js` 的
    `merge()`），不带 `icon_key` 的商品**根本不会上网格**（实测：只显示 8 个"未上架"预置图标）。
    返回的价目表取自 **§3.3 + §3.5**（都由服务端给），供"不拿页面的数比页面"用。
    """
    tag = str(time.time_ns() % 10**8).zfill(8)
    status, merchant = live_server.api("POST", "/api/demo/merchants", {
        "merchant_name": f"秤序商户{tag}号", "phone": f"138{tag}", "receiver_code": f"RC{tag}1234"})
    assert status == 201, f"§3.34 期望 201，实际 {status}：{merchant}"
    headers = session_headers(bind_stall(live_server, merchant["stall_no"]))
    status, _ = http_json(live_server.base, "POST", "/api/merchant/products",
                          {"name": product_name, "unit_price_cents": price_cents, "icon_key": "item_0"},
                          headers)
    assert status == 201, f"§3.38 期望 201，实际 {status}"
    _, products = live_server.api("GET", "/api/merchant/products", headers=headers)
    _, price_list = live_server.api(
        "GET", f"/api/merchant/price-list?business_date={today_iso()}", headers=headers)
    by_id = {row["product_id"]: row["unit_price_cents"] for row in price_list["items"]}
    return merchant["stall_no"], merchant["merchant_name"], {
        item["name"]: by_id[item["id"]] for item in products if item["id"] in by_id
    }


def _stall_counts(live_server, stall_no: str) -> tuple[int, int]:
    """该摊位的 `(交易笔数, 支付流水笔数)` —— "确认前不得产生任何交易或收款码"的**库级**证据。"""
    conn = live_server.connect_db()
    try:
        txns = count(conn, 'SELECT COUNT(*) FROM "transaction" t JOIN stall s ON s.id = t.stall_id'
                          " WHERE s.stall_no = ?", (stall_no,))
        payments = count(conn, "SELECT COUNT(*) FROM payment p"
                               ' JOIN "transaction" t ON t.id = p.transaction_id'
                               " JOIN stall s ON s.id = t.stall_id WHERE s.stall_no = ?", (stall_no,))
    finally:
        conn.close()
    return txns, payments


def _ensure_rule(live_server, rate_bp: int = 250) -> None:
    """确保当日有生效佣金口径（否则 §3.36 的佣金与应缴**合法为 0**，逐格比对就无从区分）。"""
    status, payload = live_server.api(
        "PUT", "/api/admin/commission-rules",
        {"pay_object": "merchant", "rate_bp": rate_bp, "effective_from": today_iso()})
    assert status in (200, 409), f"§3.24 期望 200 或 409，实际 {status}：{payload}"


def _payable_row(live_server, stall_no: str) -> dict | None:
    """按摊位号从 §3.36 取应缴行。"""
    status, payload = live_server.api("GET", "/api/demo/hub")
    assert status == 200, f"§3.36 期望 200，实际 {status}：{payload}"
    return next((row for row in payload["payables"] if row["stall_no"] == stall_no), None)


# --------------------------------------------------------------------------- #
# `AC-047`④：新顺序（放置不认品类 → 选品实时算 → 确认才锁价出码）
# --------------------------------------------------------------------------- #


def test_scale_page_prices_only_after_choosing_and_locks_on_confirm(browser, live_server):
    """`AC-047`④：放置**不得**认品类/显示金额；选品后金额随重量**实时**变；确认才锁价出码。"""
    _ensure_rule(live_server)
    stall_no, name, prices = _register(live_server, PRODUCT, PRICE_CENTS)
    price = prices[PRODUCT]
    baseline = _stall_counts(live_server, stall_no)
    page = new_page(browser)
    try:
        page.goto(live_server.base + SCALE_URL)
        _choose_merchant(page, name)

        # ① 放置后、选品前：不得认定品类、不得显示金额、不得有收款码
        page.click("#iconGrid .icon-tile", button="right")
        page.wait_for_function("() => document.querySelectorAll('#basket > *').length > 0", timeout=15000)
        amount, shown_price = page.inner_text("#ledAmount"), page.inner_text("#ledPrice")
        assert not re.search(r"\d", amount), (
            f"放置商品本身不得显示金额（秤只感知重量，AC-047④）：#ledAmount = {amount!r}")
        assert not re.search(r"\d", shown_price), (
            f"放置商品不得认定品类 ⇒ 单价不得出现数字：`#ledPrice` = {shown_price!r}")
        assert page.evaluate("() => document.querySelector('#qrArea').style.display === 'none'"), (
            "确认之前不得出现收款码")
        snap(page, DEMO_SHOTS, "13-秤页-已放商品未选品类")

        # ② ▲▼ 选品后：LED 出现**该商品**单价；金额 = round(重量 × 单价)，单价取自**服务端**价目表
        page.click("#btnDown")
        page.click("#btnUp")
        selected = page.inner_text("#iconGrid .icon-tile.selected .nm").strip()
        assert selected in prices, f"选中的 {selected!r} 必须在服务端价目表里：{sorted(prices)}"
        assert _cents(page.inner_text("#ledPrice")) == prices[selected], (
            f"选品后单价必须等于**服务端 §3.3** 的价：页面={page.inner_text('#ledPrice')!r} "
            f"服务端={prices[selected]} 分"
        )
        assert _cents(page.inner_text("#ledAmount")) == _expected(prices[selected], 1000), (
            f"金额必须 = round(1000g × {prices[selected]} 分/kg)：页面={page.inner_text('#ledAmount')!r}"
        )

        # ③ 调重量：金额**实时**跟着变；两组必须给出不同金额（否则"实时"没被验证）
        pairs = []
        for grams in (1500, 2500):
            _set_weight(page, grams)
            pairs.append((grams, _cents(page.inner_text("#ledAmount"))))
        assert len({amount for _, amount in pairs}) == 2, f"两组重量的金额必须不同：{pairs}"
        for grams, amount in pairs:
            assert amount == _expected(prices[selected], grams), (
                f"{grams}g 的金额必须 = round({grams} × {prices[selected]} 分/kg) = "
                f"{_expected(prices[selected], grams)} 分，页面={amount} 分"
            )

        # ④ 确认**之前**：库里不得有任何交易/流水 —— 不能只看界面
        assert _stall_counts(live_server, stall_no) == baseline, (
            f"点确认之前不得产生任何交易或收款码：基线={baseline} 现在={_stall_counts(live_server, stall_no)}"
        )

        with page.expect_response(lambda r: "/payment" in r.url and r.request.method == "POST") as caught:
            page.click("#btnConfirm")
        payment = caught.value.json()
        page.wait_for_function("() => document.querySelector('#qrArea').style.display !== 'none'",
                               timeout=15000)
        assert page.evaluate("() => document.querySelector('#qrCanvas').width > 0"), "收款码画布必须有内容"
        assert payment.get("qr_payload"), f"服务端收款响应必须带 `qr_payload`：{payment}"
        assert payment["qr_payload"] in page.inner_text("#qrPayload"), "收款码载荷必须逐字来自服务端"
        after = _stall_counts(live_server, stall_no)
        assert after == (baseline[0] + 1, baseline[1] + 1), (
            f"确认后才应产生一笔交易与一笔支付流水：基线={baseline} 现在={after}"
        )
        conn = live_server.connect_db()
        try:
            total = scalar(conn, 'SELECT t.total_amount_cents FROM "transaction" t'
                                 " JOIN stall s ON s.id = t.stall_id WHERE s.stall_no = ?"
                                 " ORDER BY t.id DESC LIMIT 1", (stall_no,))
        finally:
            conn.close()
        assert _cents(page.inner_text("#ledAmount")) == int(total), (
            f"确认后金额必须等于**服务端** `total_amount_cents`：页面={page.inner_text('#ledAmount')!r} "
            f"服务端={total} 分"
        )
        _set_weight(page, 3000, applied=False)  # 锁定：页面按设计忽略重量变化
        assert "已锁定" in page.inner_text("#ledAmountUnit"), (
            f"确认后金额单位行必须标明「已锁定」：{page.inner_text('#ledAmountUnit')!r}")
        assert _cents(page.inner_text("#ledAmount")) == int(total), (
            "确认后价格已锁定，调重量不得再改金额（AC-047④）"
        )
        snap(page, DEMO_SHOTS, "14-秤页-价格与收款码")

        # 模拟扫码 ⇒ 交易成功 ⇒ 按任意键回初始
        page.click("#btnSimulate")
        page.wait_for_function("() => document.querySelector('#successScreen').style.display !== 'none'",
                               timeout=15000)
        assert page.inner_text("#successAmount").strip().startswith("¥"), "成功页必须显示金额"
        match = re.search(r"交易号\s*(\S+)", page.inner_text("#successMeta"))
        assert match, f"成功页必须显示服务端交易号：{page.inner_text('#successMeta')}"
        txn_no = match.group(1)
        snap(page, DEMO_SHOTS, "15-秤页-交易成功")
        page.keyboard.press("a")
        page.wait_for_function("() => document.querySelector('#successScreen').style.display === 'none'",
                               timeout=15000)

        # 中台页实时出现该行 + 应缴逐格对服务端（`AC-047`②）
        page.goto(live_server.base + HUB_URL)
        page.wait_for_function(
            "() => document.querySelector('#eventTable').textContent.includes(%r)" % txn_no, timeout=20000)
        row = _payable_row(live_server, stall_no)
        assert row is not None, f"服务端 §3.36 必须给出摊位 {stall_no} 的应缴行"
        table = page.inner_text("#payableTable")
        assert stall_no in table and name in table, f"应缴表必须出现本商家/摊位：{table}"
        for field in ("paid_txn_count", "received_amount_cents", "commission_cents", "payable_cents"):
            shown = str(row[field]) if field == "paid_txn_count" else f"¥ {row[field] / 100:.2f}"
            assert shown in table, f"应缴表 `{field}` 必须显示服务端值 {shown}（§3.36）：{table}"
        assert row["payable_cents"] > 0, "本笔已确认且已配口径 ⇒ 应缴必须 > 0"
    finally:
        page.close()


def test_scale_page_sends_no_write_request_before_confirm(browser, live_server):
    """`AC-047`④ 的**机械判据**：右键放置 → 选品 → 调重量，这一段**一个写请求都不许有**。"""
    _ensure_rule(live_server)
    stall_no, name, _ = _register(live_server, PRODUCT, PRICE_CENTS)
    page = new_page(browser)
    seen: list[str] = []
    page.on("request", lambda request: seen.append(
        f"{request.method} {request.url.split(live_server.base)[-1]}"))
    try:
        page.goto(live_server.base + SCALE_URL)
        _choose_merchant(page, name)
        seen.clear()  # 只统计"右键放置之后"这一段
        page.click("#iconGrid .icon-tile", button="right")
        page.wait_for_function("() => document.querySelectorAll('#basket > *').length > 0", timeout=15000)
        page.click("#btnDown")
        _set_weight(page, 2200)
        page.wait_for_timeout(300)  # 给异步请求留出发出的机会，否则"没有"可能只是还没发
        before = list(seen)
        assert _stall_counts(live_server, stall_no) == (0, 0), (
            f"确认**之前**库里不得有任何交易/流水：{_stall_counts(live_server, stall_no)}"
        )

        with page.expect_response(lambda r: "/payment" in r.url and r.request.method == "POST"):
            page.click("#btnConfirm")
        # **锚点必须命中**：确认那一下确实被收集到了 ⇒ 监听器在工作，"0 条"不是假象
        assert any(call.startswith("POST ") and "/payment" in call for call in seen), (
            f"监听器没收集到确认请求 ⇒ 本判据无从区分（收集到 {len(seen)} 条）：{seen}"
        )
        writes = [call for call in before if not call.startswith("GET ")]
        assert not writes, f"确认之前不得有任何写请求（AC-047④）：{writes}"
        for forbidden in ("/api/merchant/transactions", "/payment", "/api/mock/payment/callback"):
            assert not any(forbidden in call for call in before), (
                f"确认之前不得出现 {forbidden}：{before}"
            )
    finally:
        page.close()
