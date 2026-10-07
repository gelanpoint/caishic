"""`REQ-057` / `AC-047`：三个联动演示页的**真浏览器**端到端验证（`task-27`）。

## 这份文件真的开浏览器

用既有 `browser` 夹具（Playwright + Chromium）**真跑**三个页面：真点击、真右键、真按键。
夹具缺失时 pytest **跳过并说明"这不是通过，是没检查"**（见 `tests/conftest.py`）。

## 判据要点

- `AC-047` ①：秤页走完「选商家 → 右键放商品 → 上下选品 → 确认 → 出价格与收款码 → 模拟扫码
  → 交易成功 → 任意键回初始」，且**全程经服务端真实端点**（用请求日志证明，不是看代码猜）。
- `AC-047` ②：中台页**实时**出现该行（轮询等它出现，不等就直接判红）。
- `AC-047` ③：三个页面加载时**没有任何非本机 origin 的请求**；并且**每个本地资源都必须 <400**
  （这一条会抓出"页面引了一个不存在的本地文件"—— 那同样是把页面打瘸了）。
- 截图落 `/home/gelanpoint/caishic-实玩截图/demo/`（负责人要真截图）。

## 未覆盖（如实）

- 视觉观感（配色、排版、像不像一台秤）**未作断言** —— 那是主观判断，截图留证供人看。
- 触屏手势 / 手机真机扫码未验（本机只跑桌面 Chromium 视口）。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from e2e_support import (REPO_ROOT, active_products, bind_stall, create_transaction,
                         evidence_key, http_json, new_page, session_headers, snap, today_iso)

DEMO_SHOTS = Path("/home/gelanpoint/caishic-实玩截图/demo")
REGISTER_URL = "/demo/register/"
SCALE_URL = "/demo/scale/"
HUB_URL = "/demo/hub/"
NAV_HREFS = (REGISTER_URL, SCALE_URL, HUB_URL)


@pytest.fixture(scope="module", autouse=True)
def demo_shots_dir():
    DEMO_SHOTS.mkdir(parents=True, exist_ok=True)
    return DEMO_SHOTS


def _new_merchant_fields() -> tuple[str, str, str]:
    tag = f"{time.time_ns() % 10**9:09d}"
    # 名字以非数字收尾：避免与紧随其后的手机号在库文件里连成 16~19 位连续数字（见 demo_console_support）
    return f"演示商户{tag[:6]}号", f"138{tag}", f"99{tag}12345"


def _register_via_ui(page, base: str) -> tuple[str, str, str]:
    """走**注册页真实表单**注册一个商家（`AC-047` 的页面 ① 必须真能用）。"""
    name, phone, code = _new_merchant_fields()
    page.goto(base + REGISTER_URL)
    page.fill("#name", name)
    page.fill("#phone", phone)
    page.fill("#code", code)
    page.click("#submitBtn")
    page.wait_for_function(
        "() => document.querySelector('#formResult').textContent.includes('已注册')", timeout=15000
    )
    body = page.inner_text("#formResult")
    assert "****" in body, f"注册结果必须显示**脱敏值**（明文不得回显）：{body}"
    assert code not in body, "注册页不得回显收款码明文"
    return name, phone, code


def _choose_merchant(page, name: str) -> None:
    """在秤页选择刚注册的商家（未选商家不能开工 —— 页面自己的门禁）。"""
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


def _ensure_rule(live_server, rate_bp: int = 250) -> None:
    """确保当日有生效佣金口径。

    没有口径时 §3.36 的佣金与应缴**合法地为 0**（页面自己也会提示"口径还没配"），
    那样"应缴表逐格对服务端"就退化成 0 == 0，区分不出对错 —— 故这是判据的前置，不是放宽。
    `MT-1012`(409) = 已有一条，容忍（§3.24 的声明行为）。
    """
    status, payload = live_server.api(
        "PUT", "/api/admin/commission-rules",
        {"pay_object": "merchant", "rate_bp": rate_bp, "effective_from": today_iso()},
    )
    assert status in (200, 409), f"§3.24 期望 200 或 409，实际 {status}：{payload}"


def _merchant_of(live_server, name: str) -> dict:
    """按商家名从 §3.33 取那一行（页面断言要**锚在服务端真值**上，不锚在页面上）。"""
    status, payload = live_server.api("GET", "/api/demo/merchants")
    assert status == 200, f"§3.33 期望 200，实际 {status}：{payload}"
    row = next((item for item in payload["items"] if item["merchant_name"] == name), None)
    assert row is not None, f"注册的商家 {name!r} 必须出现在 §3.33 列表里"
    return row


def _payable_row(live_server, stall_no: str) -> dict | None:
    """按摊位号从 §3.36 取应缴行（`None` = 服务端没给该摊位应缴）。"""
    status, payload = live_server.api("GET", "/api/demo/hub")
    assert status == 200, f"§3.36 期望 200，实际 {status}：{payload}"
    return next((row for row in payload["payables"] if row["stall_no"] == stall_no), None)


# --------------------------------------------------------------------------- #
# `AC-047` ①②③：三页可开、可互跳；秤页全流程；中台实时；零外部资源
# --------------------------------------------------------------------------- #


def test_three_pages_open_and_cross_navigate(browser, live_server):
    """`AC-047`：三个页面都能打开、都有指向另外两页的导航，且真能互相跳过去。"""
    page = new_page(browser)
    try:
        for key, url in (("register", REGISTER_URL), ("scale", SCALE_URL), ("hub", HUB_URL)):
            response = page.goto(live_server.base + url)
            assert response is not None and response.status < 400, f"`{url}` 必须可加载，实际 {response}"
            hrefs = page.eval_on_selector_all("#nav a", "els => els.map(a => a.getAttribute('href'))")
            assert set(hrefs) >= set(NAV_HREFS), f"`{url}` 的导航必须含三页互跳链接，实际 {hrefs}"
            snap(page, DEMO_SHOTS, f"10-{key}-页")

        # 真点导航跳一圈（注册 → 秤 → 中台 → 注册）
        page.goto(live_server.base + REGISTER_URL)
        for url in (SCALE_URL, HUB_URL, REGISTER_URL):
            page.click(f"#nav a[href='{url}']")
            page.wait_for_url(re.escape(live_server.base + url), timeout=15000)
            assert page.url.endswith(url), f"点导航后应停在 `{url}`，实际 {page.url}"
    finally:
        page.close()


def test_scale_page_walks_the_full_flow_and_hub_shows_the_row(browser, live_server):
    """`AC-047` ① ②：秤页走完全流程，中台页**实时**出现该行；收款码载荷来自服务端。"""
    page = new_page(browser)
    api_calls: list[str] = []

    def on_request(request):
        if "/api/" in request.url:
            api_calls.append(f"{request.method} {request.url.split(live_server.base)[-1]}")

    page.on("request", on_request)
    try:
        _ensure_rule(live_server)  # 先有佣金口径，应缴才非 0（否则下面逐格对不上服务端）
        name, _, _ = _register_via_ui(page, live_server.base)
        snap(page, DEMO_SHOTS, "11-注册成功")

        page.goto(live_server.base + SCALE_URL)
        _choose_merchant(page, name)
        snap(page, DEMO_SHOTS, "12-秤页-已选商家")

        # 右键预置图标 ⇒ 放上去（加入本笔交易）
        page.click("#iconGrid .icon-tile", button="right")
        page.wait_for_function("() => document.querySelectorAll('#basket > *').length > 0", timeout=15000)
        # 按钮上下选品
        page.click("#btnDown")
        page.click("#btnUp")
        snap(page, DEMO_SHOTS, "13-秤页-已放商品")

        # 确认 ⇒ 服务端计价 + 生成收款码（用 expect_response 精确拿到那一次响应，不靠事件回调抢时间）
        with page.expect_response(
            lambda response: "/payment" in response.url and response.request.method == "POST"
        ) as caught:
            page.click("#btnConfirm")
        payment = caught.value.json()
        page.wait_for_function(
            "() => document.querySelector('#qrArea').style.display !== 'none'", timeout=15000
        )
        assert payment.get("qr_payload"), f"服务端收款响应必须带 `qr_payload`：{payment}"
        qr_text = page.inner_text("#qrPayload")
        assert payment["qr_payload"] in qr_text, (
            f"页面显示的收款码载荷必须**逐字来自服务端**：页面={qr_text[:60]!r} "
            f"服务端={payment['qr_payload'][:60]!r}"
        )
        assert payment["payment_no"] in qr_text, "页面必须显示服务端支付单号"
        assert payment["receiver_token_masked"] in qr_text, "页面必须显示服务端下发的脱敏收款标识"
        assert "本地渲染的二维码" in page.inner_text("#qrNote"), (
            "收款码必须是**本地真渲染**的二维码；`qr.js` 缺失时页面会退回占位图"
            f"（REQ-057 要求页面内本地方式渲染，占位图不算）：{page.inner_text('#qrNote')!r}"
        )
        snap(page, DEMO_SHOTS, "14-秤页-价格与收款码")

        # 模拟扫码 ⇒ 交易成功
        page.click("#btnSimulate")
        page.wait_for_function(
            "() => document.querySelector('#successScreen').style.display !== 'none'", timeout=15000
        )
        meta = page.inner_text("#successMeta")
        match = re.search(r"交易号\s*(\S+)", meta)
        assert match, f"成功页必须显示服务端交易号：{meta}"
        txn_no = match.group(1)
        assert page.inner_text("#successAmount").strip().startswith("¥"), "成功页必须显示金额"
        snap(page, DEMO_SHOTS, "15-秤页-交易成功")

        # 按任意键 ⇒ 回到初始界面
        page.keyboard.press("a")
        page.wait_for_function(
            "() => document.querySelector('#successScreen').style.display === 'none'", timeout=15000
        )
        assert "确认后" in page.inner_text("#txnResult"), "回到初始后价格区应复位"
        snap(page, DEMO_SHOTS, "16-秤页-回到初始")

        # 全程只经服务端真实能力（不是前端伪造数据）
        joined = "\n".join(api_calls)
        assert "POST /api/merchant/transactions" in joined, f"计价必须走 §3.6：\n{joined}"
        assert "POST /api/mock/payment/callback" in joined, f"模拟扫码必须走 §3.17 回调：\n{joined}"

        # 中台页**实时**出现该行（轮询等它出现；不等就是假绿）
        conn = live_server.connect_db()
        try:
            row = conn.execute(
                'SELECT status, total_amount_cents FROM "transaction" WHERE transaction_no = ?', (txn_no,)
            ).fetchone()
            assert row is not None, f"服务端库里必须有这笔 {txn_no}（页面显示的交易号得是真的）"
            assert row["status"] == "paid", f"模拟扫码后库里应为 paid，实际 {row['status']}"
        finally:
            conn.close()

        page.goto(live_server.base + HUB_URL)
        page.wait_for_function(
            "() => document.querySelector('#eventTable').textContent.includes(%r)" % txn_no, timeout=20000
        )
        # 应缴表**逐格**对服务端：原来那条 `txn_no in #payableTable or "应缴" in body` 是**恒真**的
        # （应缴表按 §3.36 只渲染商家/摊位/笔数/金额，永远不含交易号；而页面静态标题里本就有"应缴"），
        # 等于什么都没检查 —— 换成"拿服务端 §3.36 的那一行，去页面里逐格找"。
        merchant_row = _merchant_of(live_server, name)
        payable_row = _payable_row(live_server, merchant_row["stall_no"])
        table = page.inner_text("#payableTable")
        assert payable_row is not None, f"服务端 §3.36 必须给出摊位 {merchant_row['stall_no']} 的应缴行"
        assert merchant_row["stall_no"] in table, f"应缴表必须出现本摊位的摊位号：{table}"
        assert merchant_row["merchant_name"] in table, f"应缴表必须出现商家名：{table}"
        assert str(payable_row["paid_txn_count"]) in table, f"已确认笔数必须与服务端一致：{table}"
        for field in ("received_amount_cents", "commission_cents", "payable_cents"):
            shown = f"¥ {payable_row[field] / 100:.2f}"
            assert shown in table, f"应缴表 `{field}` 必须显示服务端值 {shown}（§3.36）：{table}"
        assert payable_row["payable_cents"] > 0, "本笔已确认 ⇒ 该商家应缴必须 > 0（否则上面几格无从区分）"
        snap(page, DEMO_SHOTS, "17-中台页-实时出现该行")
    finally:
        page.close()


def test_demo_pages_request_zero_external_resources(browser, live_server):
    """`AC-047` ③：三页加载**没有任何非本机 origin 的请求**，且每个本地资源都 <400。"""
    page = new_page(browser)
    requests: list[str] = []
    bad_status: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.on("response", lambda response: bad_status.append(f"{response.status} {response.url}")
            if response.status >= 400 else None)
    try:
        for url in NAV_HREFS:
            page.goto(live_server.base + url)
            page.wait_for_timeout(700)  # 让页面把该拉的资源拉完（含轮询/图标）
    finally:
        page.close()

    external = [url for url in requests if not url.startswith(live_server.base)]
    assert external == [], f"演示页不得请求任何站外资源（AGENTS.md §3 硬要求 4）：{external}"
    assert len(requests) >= 8, f"只观察到 {len(requests)} 个请求，太少了（可能没真的加载）：{requests}"
    assert bad_status == [], (
        "演示页引用的**本地**资源必须都取得到（404 = 页面引了一个不存在的文件）：\n"
        + "\n".join(f"    - {item}" for item in bad_status)
    )


def test_sensitivity_injected_external_script_turns_the_origin_check_red(browser, live_server):
    """**灵敏度负例**：在途给秤页塞一个外链脚本（**不改仓库里任何文件**）⇒ 非本机请求判据必须变红。

    没有这一条，"零外部资源"就只是个永远绿的摆设：判据能不能失败，得亲手弄坏一次才算数。
    """
    page = new_page(browser)
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    html = (REPO_ROOT / "app" / "static" / "demo" / "scale" / "index.html").read_text(encoding="utf-8")
    poisoned = html.replace(
        "</head>", '<script src="https://cdn.example.com/probe.js"></script>\n</head>', 1
    )
    assert poisoned != html, "前置：页面必须含 `</head>` 才能注入探针"
    page.route("**/demo/scale/",
               lambda route: route.fulfill(status=200, content_type="text/html", body=poisoned))
    try:
        page.goto(live_server.base + SCALE_URL)
        page.wait_for_timeout(600)
    finally:
        page.close()

    external = [url for url in requests if not url.startswith(live_server.base)]
    assert any("cdn.example.com" in url for url in external), (
        f"塞了外链脚本却观察不到非本机请求 —— 说明判据是摆设：{requests}"
    )


def test_register_page_shows_the_masked_value_stored_in_the_database(browser, live_server):
    """`AC-043` 的**页面侧**：注册页显示的脱敏值必须等于库里真正存的那一个。"""
    page = new_page(browser)
    try:
        _, _, code = _register_via_ui(page, live_server.base)
        shown = page.inner_text("#formResult")
        conn = live_server.connect_db()
        try:
            rows = conn.execute(
                "SELECT stall_no, payment_receiver_token FROM stall WHERE payment_receiver_token LIKE '%****%'"
                " ORDER BY id DESC"
            ).fetchall()
            assert rows, "库里必须有脱敏收款标识"
            newest = rows[0]["payment_receiver_token"]
            assert newest in shown, f"注册页显示的脱敏值必须来自服务端：库里={newest!r}，页面={shown!r}"
            assert code not in shown and code not in newest, "明文收款码不得出现在页面或库里"
        finally:
            conn.close()
    finally:
        page.close()


def test_hub_page_sends_reminders_through_the_real_endpoint(browser, live_server):
    """`REQ-057` 页面 ③：中台页的「发送催缴短信」必须真调 §3.37，并把结果摊在页面上。"""
    page = new_page(browser)
    calls: list[str] = []
    page.on("request", lambda request: calls.append(request.url) if "/api/" in request.url else None)
    try:
        page.goto(live_server.base + HUB_URL)
        page.wait_for_function(
            "() => !document.querySelector('#payableTable').textContent.includes('读取中')", timeout=15000
        )
        # 用 expect_response 精确拿到 §3.37 的响应体，再把页面渲染**逐项**对上去。
        # 原来那条 `"****" in result or "已发送" in result or …` 是**恒真**的：`hub.js` 无条件输出
        # "已发送 N 条"，任何一次点击都能满足它 —— 等于没检查。
        with page.expect_response(lambda response: "/api/demo/sms-reminders" in response.url) as caught:
            page.click("#smsBtn")
        sms = caught.value.json()
        page.wait_for_function(
            "() => document.querySelector('#smsResult').textContent.trim().length > 0", timeout=15000
        )
        snap(page, DEMO_SHOTS, "18-中台页-催缴结果")

        assert any("/api/demo/sms-reminders" in url for url in calls), f"必须真调 §3.37：{calls}"
        assert sms["interval_days"] == 2, f"§3.37 要求每隔一天：{sms['interval_days']}"
        result = page.inner_text("#smsResult")
        # ① 条数行必须与服务端响应**逐数**一致（不是"页面里出现了某个词"）
        assert f"已发送 {len(sms['sent'])} 条，跳过 {len(sms['skipped'])} 条" in result, (
            f"页面条数必须等于服务端响应：sent={len(sms['sent'])} skipped={len(sms['skipped'])}\n{result}"
        )
        assert f"每隔 {sms['interval_days']} 天" in result, f"页面必须显示服务端的间隔天数：{result}"
        # ② 每一条 sent 都要在页面上**逐项**找到（商家名 + 脱敏手机号 + 应缴金额）
        for row in sms["sent"]:
            assert row["phone_masked"] in result, f"sent 行的脱敏手机号必须出现在页面上：{row}"
            assert f"¥ {row['payable_cents'] / 100:.2f}" in result, f"sent 行的应缴必须逐字一致：{row}"
        # ③ 每一条 skipped 也要对得上（商家 #id + 该原因的页面文案）
        reason_text = {"interval_not_elapsed": "距上次发送不足间隔", "nothing_payable": "应缴为 0"}
        for row in sms["skipped"]:
            assert f"商家 #{row['merchant_id']}" in result, f"skipped 行必须出现在页面上：{row}"
            assert reason_text[row["reason"]] in result, f"skipped 原因文案必须对得上：{row}"
        # ④ 至少有一条 sent 或 skipped 里有脱敏手机号/原因 —— 否则上面两个循环都空转
        assert sms["sent"] or sms["skipped"], f"§3.37 不得两边都空：{sms}"
    finally:
        page.close()


def test_hub_polling_preserves_the_payable_table_scroll(browser, live_server):
    """轮询重建表格**不得**把 `.table-wrap` 的 `scrollTop` 打回顶部（Lead 真机复核发现的真缺陷）：
    `renderPayables` 每 1.5s `Demo.clear()` + 重建 ⇒ 溢出时（`max-height:360px`）滚动位置被打回顶部，
    折叠线以下的商家**永远够不着**；修复 = `hub.js` 的 `saveScroll()`/`restoreScroll()`。
    种子只给 10 行（实测 348px < 360px，**不溢出**），故这里再注册 10 家把表撑到必然溢出。"""
    for index in range(10):
        status, _ = live_server.api("POST", "/api/demo/merchants", {
            "merchant_name": f"滚动商户{index}号", "phone": f"1380000{index:04d}",
            "receiver_code": f"RC{index:08d}"})
        assert status == 201, f"§3.34 期望 201，实际 {status}"
    page, wrap = new_page(browser), "#payableTable .table-wrap"
    try:
        page.goto(live_server.base + HUB_URL)
        page.wait_for_selector("#payableTable .table-wrap tr", timeout=15000)
        target = page.evaluate("(s) => { const w = document.querySelector(s);"
                               " w.scrollTop = w.scrollHeight; return w.scrollTop; }", wrap)
        assert target > 0, "前置：应缴表必须真的溢出（否则本判据无从区分）"
        last = page.evaluate("(s) => { const r = document.querySelectorAll(s + ' tr');"
                             " return r[r.length - 1].innerText; }", wrap)
        page.wait_for_timeout(4000)  # ≥2 个轮询周期（`POLL_MS = 1500`）
        after = page.evaluate("(s) => document.querySelector(s).scrollTop", wrap)
        assert after > 0 and after >= target - 2, (
            f"轮询重建把 scrollTop 打回顶部（{target} → {after}）⇒ 折叠线以下的应缴行够不着")
        assert re.search(r"[A-Z]-\d{2}", last) and "¥" in last, (
            f"末行（只有滚下去才看得见）必须仍有摊位号与金额：{last!r}")
    finally:
        page.close()
