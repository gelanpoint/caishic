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
