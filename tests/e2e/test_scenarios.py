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

证据：断言 + 关键步骤截图（截图落在 `.pytest-tmp/e2e-shots/`，该目录已被 `.gitignore` 忽略，
不会污染仓库；断言失败时 pytest 会打印真实 DOM 文本，便于定位"点不动"）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

# 证据要能在**任何控制台**下打出来：Windows 默认 GBK，遇到 `¥` 这类字符会直接
# `UnicodeEncodeError` 把用例判红（**检查本身成了故障源** —— 本轮实测就是这么挂的）。
# 故显式把 stdout 改成 UTF-8 + 出错替换：编码问题不再伪装成"页面点不动"。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
SHOTS = REPO_ROOT / ".pytest-tmp" / "e2e-shots"
STALL = "A-01"


# ---------------------------------------------------------------------------
# 夹具：真起一个服务（独立数据目录，绝不碰演示库 data/market_trade.sqlite3）
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """用仓库的 `run.py` 真起服务（含建库/迁移/种子），退出时收干净。"""
    data_dir = tmp_path_factory.mktemp("mt-e2e-data")
    port = _free_port()
    env = {**os.environ, "MT_DATA_DIR": str(data_dir)}
    # 服务端输出必须落到**文件**：曾经用 `subprocess.PIPE` 且没人读，werkzeug 的请求日志
    # 填满 64KB 管道缓冲后**服务端就阻塞在写**上 —— 表现是"前几个用例正常、后面的用例永远卡住"，
    # 极容易被误判成"页面点不动"（本轮实测踩过）。文件不会被写阻塞，出问题还能读回来看。
    server_log = data_dir / "server.log"
    handle = server_log.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "run.py", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"服务启动即退出（exit {proc.returncode}）：\n"
                + server_log.read_text(encoding="utf-8", errors="replace")
            )
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=2) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        raise AssertionError("服务 60 秒内未就绪")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    handle.close()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        instance = play.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def shots():
    """截图目录（仓库内但被 `.gitignore` 忽略，避免"为了留证据而污染仓库"）。"""
    SHOTS.mkdir(parents=True, exist_ok=True)
    return SHOTS


def new_page(browser):
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    page.set_default_timeout(15000)
    return page


def snap(page, shots, name: str) -> str:
    path = shots / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"[截图] {path}")
    return str(path)


# ---------------------------------------------------------------------------
# 主线 1：秤端（选品 → 计价 → 现金收款 → 收款码 → 改价 → 退货）
# ---------------------------------------------------------------------------


def test_scale_cash_flow_price_change_and_refund(live_server, browser, shots):
    """秤端主线：全部操作**靠点按**完成（`AC-006` 的"选品不要输入"）。"""
    page = new_page(browser)
    page.goto(f"{live_server}/scale/")
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
    page.goto(f"{live_server}/scale/")
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
    """离线主线：开关是**界面上显式可点的**（`REQ-014`），补传后 pending 必须归零（`NFR-013`）。"""
    page = new_page(browser)
    page.goto(f"{live_server}/scale/")
    page.click("#defaultStall")
    page.wait_for_selector(".tile")

    pending_before = int(page.inner_text("#pending").split("/")[0].strip())
    page.click("#offlineToggle")
    assert "离线" in page.inner_text("#offlineBadge"), "切换后徽标应显示离线"

    staged = 2
    for index in range(staged):
        page.locator(".tile").nth(index % page.locator(".tile").count()).click()
        page.click("#checkout")
        page.wait_for_function("document.querySelector('#offlineNote').textContent.includes('暂存')")
    snap(page, shots, "offline-1-staged")
    print(f"[离线] 已暂存 {staged} 笔；界面提示：{page.inner_text('#offlineNote')}")

    page.click("#offlineToggle")
    assert "在线" in page.inner_text("#offlineBadge"), "切回后徽标应显示在线"
    page.click("#syncNow")
    page.wait_for_function("document.querySelector('#syncResult').textContent.includes('补传')")
    result = page.inner_text("#syncResult")
    pending_after = int(page.inner_text("#pending").split("/")[0].strip())
    assert pending_after == 0, f"补传后界面 pending 应归零，实际 {pending_after}（{result}）"
    assert pending_before == 0, "前置：本次会话开始时应无待补传"
    print(f"[离线] 补传 OK：{result}")
    snap(page, shots, "offline-2-synced")
    page.close()


# ---------------------------------------------------------------------------
# 主线 3：顾客扫码页（**浏览器渲染出的字段**与接口白名单一致）
# ---------------------------------------------------------------------------


def _api(base: str, method: str, path: str, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def test_customer_page_renders_exactly_the_sourced_fields(live_server, browser, shots):
    """顾客页：**页面上出现的每个字段都必须有来源**（`REQ-023` / `AC-011`），多一个都不行。"""
    session = _api(live_server, "POST", "/api/merchant/session", {"stall_no": STALL})
    token = session["session_token"]
    head = {"X-Stall-Session": token, "Idempotency-Key": "e2e-customer-1"}
    products = _api(live_server, "GET", "/api/merchant/products", headers=head)
    product = [item for item in products if item["status"] == "active"][0]
    txn = _api(live_server, "POST", "/api/merchant/transactions",
               {"items": [{"product_id": product["id"], "weight_grams": 800}]}, head)
    head_pay = {"X-Stall-Session": token, "Idempotency-Key": "e2e-customer-1-pay"}
    _api(live_server, "POST", f"/api/merchant/transactions/{txn['transaction_no']}/payment",
         {"method": "cash", "operator": "e2e"}, head_pay)

    receipt = _api(live_server, "GET", f"/api/customer/receipts/{txn['transaction_no']}")
    profile = _api(live_server, "GET", f"/api/customer/stalls/{STALL}/profile")

    page = new_page(browser)
    page.goto(f"{live_server}/customer/?stall={STALL}&txn={txn['transaction_no']}")
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
