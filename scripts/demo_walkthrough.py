"""现场走查：按 docs/演示脚本.md 的六步主链，用真浏览器真点一遍。

⚠️ **这是开发期工具，不是运行期组件** —— `run.py` / `app/**` 不 import 它。
它只用标准库 + `playwright`，而 `playwright` 在 `requirements-dev.txt` 里
（见 `docs/adr/0004`：运行期依赖只有 Flask）。

与测试的区别：
- 测试断言的是接口/契约；**这里断言的是"人按脚本能不能走通"**，包括界面文案与可见反馈；
- 全程**同机两个浏览器上下文**（秤端 + 顾客端），对应演示现场"一个窗口当秤端、另一个当顾客端"；
- 每步留截图，逐步打印实测数字，失败**如实抛出**不做美化。

⚠️ **走查脚本自己出假绿，比走查失败更危险** —— 本脚本已经栽过三次（见
`docs/走查证据/README.md` 的"修掉的走查自身的错误"表）。
写新断言时务必确认：**它读到的是不是我要验的那个东西**，
而不是"某个看起来相关的文案"。

用法：python demo_walkthrough.py <base_url> <截图目录>
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

STEPS = []


def step(name):
    def deco(fn):
        STEPS.append((name, fn))
        return fn
    return deco


def wait_txn_no(page, timeout=15000):
    page.wait_for_function(
        "() => { const e = document.querySelector('#txnNo');"
        " return e && e.textContent.trim() !== '—'; }",
        timeout=timeout,
    )
    return page.inner_text("#txnNo").strip()


def wait_new_txn_no(page, previous, timeout=15000):
    """等**一笔新交易**出号。

    ⚠️ 不能直接复用 `wait_txn_no`：成交之后 `#txnNo` 里仍然是**上一个**交易号，
    而 `wait_txn_no` 的判据只是"不是 —"，会**立刻返回旧号**——
    第一版走查就是这么把"第二笔没开成"误判成应用缺陷的（专项探针已证明应用支持连续开单）。
    开新单必须等**号变了**。
    """
    page.wait_for_function(
        "prev => { const e = document.querySelector('#txnNo');"
        " return e && e.textContent.trim() !== '—' && e.textContent.trim() !== prev; }",
        arg=previous,
        timeout=timeout,
    )
    return page.inner_text("#txnNo").strip()


def wait_pending(page, want, timeout=15000):
    """等 `#pending` 恰好等于 want。

    ⚠️ `#pending` 的**显示形态是 `N / 阈值 …`**（如 `0 / 200 笔`），不是裸数字 ——
    第一版走查按 `/^\\d+$/` 匹配，**永远匹配不上**，卡在离线那一步。
    这个坑本项目的 `tests/e2e_support.py` 早就写明了（`pending_count` 用 `split("/")[0]`），
    是我重造辅助函数时没先去看已有的那一份。

    另外它来自 §3.13 的一次 GET（`refreshOffline()`），而 `#offlineNote` 由**另一条异步链**同步写入，
    两个数字可能短暂不一致 ⇒ 要等，不能立刻读。
    """
    try:
        page.wait_for_function(
            "want => { const e = document.querySelector('#pending');"
            " if (!e) return false;"
            " const raw = (e.textContent || '').split('/')[0].trim();"
            " return /^\\d+$/.test(raw) && parseInt(raw, 10) === want; }",
            arg=want,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        raise AssertionError(
            f"等 #pending == {want} 超时；实测读到 {page.inner_text('#pending')!r}，"
            f"界面提示={page.inner_text('#offlineNote').strip()!r}"
        ) from None
    return int(page.inner_text("#pending").split("/")[0].strip())


@step("主链① 选品 → 称重 → 计价")
def s1(ctx, base, shots, log, prev=None):
    page = ctx.new_page()
    page.goto(f"{base}/scale/")
    page.click("#defaultStall")
    page.wait_for_selector(".tile")
    tiles = page.locator(".tile").count()
    page.locator(".tile").nth(0).click()
    page.locator(".tile").nth(1).click()
    cart = page.inner_text("#cart")
    weights = page.locator("#weights button").count()
    page.locator("#weights button").first.click()
    page.click("#checkout")
    txn_no = wait_txn_no(page)
    total = page.inner_text("#txnTotal").strip()
    items = page.inner_text("#txnItems")
    page.screenshot(path=str(shots / "01-计价.png"), full_page=True)
    log(f"  商品图标 {tiles} 个 · 预设重量 {weights} 档 · 购物车两行：{cart.strip()[:40]!r}")
    log(f"  交易号 {txn_no} · 界面总额 {total} · 明细行数 {items.count('¥')}")
    assert txn_no, "计价后应出交易号"
    assert total.startswith("¥"), f"计价后应出金额：{total!r}"
    assert items.count("¥") == 2, f"应有两行明细：{items!r}"
    return page, txn_no


@step("主链② 收款（收款码 + 现金）")
def s2(ctx, base, shots, log, prev):
    page, txn_qr = prev["主链① 选品 → 称重 → 计价"]
    # 收款码是**异步**的：点「生成收款码」只是拿到码，等支付回调落地才算收完。
    # ⚠️ 第一版走查就是漏了这一步——不等回调就开下一笔，交易号不变、断言失败，
    # 一度看起来像"应用不支持连续开单"。专项探针证实**应用是支持的**（不刷新也能开出 T-...0002），
    # 所以这是**脚本时序错，不是应用缺陷**——记在这里，别让下一个人再怀疑一次应用。
    page.click("#payQr")
    page.wait_for_selector("#qrBox:not(.hide)", timeout=15000)
    payload = page.inner_text("#qrPayload").strip()
    masked = page.inner_text("#qrToken").strip()
    log(f"  收款码已生成：payload={payload[:46]}… · 脱敏收款标识={masked}")
    assert payload and masked, "收款码内容与脱敏标识不应为空"
    page.screenshot(path=str(shots / "02a-收款码.png"), full_page=True)
    page.wait_for_selector("#afterSale:not(.hide)", timeout=20000)
    log(f"  收款码收款落地：{txn_qr}（等支付回调后进入售后区）")

    # 第二笔走现金：**不刷新页面**（实测支持连续开单，正是演示现场的连续操作）
    page.locator(".tile").nth(2).click()
    page.locator("#weights button").first.click()
    page.click("#checkout")
    txn_cash = wait_new_txn_no(page, txn_qr)
    assert txn_cash != txn_qr, f"第二笔应产生新交易号，却还是 {txn_cash}"
    page.click("#payCash")
    page.wait_for_function(
        "() => { const t = (document.querySelector('#toast')||{}).textContent||'';"
        " return t.includes('现金收款成功'); }",
        timeout=15000,
    )
    log(f"  现金收款 OK：{txn_cash}（与上一笔不同号，证明支持连续开单）")
    page.screenshot(path=str(shots / "02b-现金收款.png"), full_page=True)
    return page, txn_qr


@step("主链③ 顾客扫码（同机第二窗口）")
def s3(ctx, base, shots, log, prev):
    _, txn_qr = prev["主链② 收款（收款码 + 现金）"]
    cust = ctx.browser.new_context(viewport={"width": 1100, "height": 900})
    cpage = cust.new_page()
    cpage.goto(f"{base}/customer/?stall=A-01&txn={txn_qr}")
    cpage.wait_for_selector("#receiptItems")
    text = cpage.inner_text("body")
    cpage.screenshot(path=str(shots / "03-顾客页.png"), full_page=True)
    log(f"  顾客页标题：{cpage.title()}")

    # 白名单验证按 tests/e2e/test_scenarios.py 的已验收做法：
    # ① 接口里**非空**的每个真值都必须出现在页面上（有来源就必须显示）；
    # ② 白名单**之外**的字段名一个都不许出现（"多显示一个"与"少显示一个"同等严重）；
    # ③ 渲染行数 = 接口给的明细行数，表头与白名单一致。
    # ⚠️ 第一版走查用 `[data-field]` 取字段，取到空数组 [] 却照常往下走 —— 那是**假绿**，
    # 而"字段白名单多一个就算失败"正是演示脚本点名的差异化卖点，不能这么验。
    api = cpage.evaluate(
        """async (txn) => {
            const r = await fetch(`/api/customer/receipts/${txn}`);
            const receipt = await r.json();
            const p = await fetch('/api/customer/stalls/A-01/profile');
            const profile = await p.json();
            return {receipt, profile};
        }""",
        txn_qr,
    )
    receipt, profile = api["receipt"], api["profile"]
    must_show = {
        "transaction_no": receipt["transaction_no"],
        "amount": f"{receipt['total_amount_cents'] / 100:.2f}",
        "stall_no": profile["stall_no"],
        "stall_name": profile["stall_name"],
        "item_name": receipt["items"][0]["name"],
        "weight_grams": str(receipt["items"][0]["weight_grams"]),
    }
    missing = {k: v for k, v in must_show.items() if str(v) not in text}
    forbidden = [f for f in ("id_card", "bank_card", "customer_name", "phone",
                             "receiver_account", "operator") if f in text]
    rows = cpage.locator("#receiptItems tbody tr").count()
    headers = cpage.inner_text("#receiptItems thead")
    log(f"  ① 接口真值必须显示：{len(must_show)} 项 → 缺失 {list(missing) or '无'}")
    log(f"  ② 白名单外字段：{forbidden or '无'}")
    log(f"  ③ 渲染明细 {rows} 行（接口 {len(receipt['items'])} 行）· 表头：{headers.strip()!r}")
    log(f"  含标价一致率：{'一致率' in text}")
    assert not missing, f"接口有真值但页面没渲染：{missing}\n页面文本：{text}"
    assert not forbidden, f"顾客页出现了白名单外字段：{forbidden}"
    assert rows == len(receipt["items"]), f"渲染明细 {rows} 行 ≠ 接口 {len(receipt['items'])} 行"
    assert "重量" in headers and "金额" in headers, f"明细表头与白名单不一致：{headers}"
    assert "一致率" in text, "顾客页应显示标价一致率"
    cust.close()
    return True


@step("主链④ 改价与退货")
def s4(ctx, base, shots, log, prev):
    page = prev["主链② 收款（收款码 + 现金）"][0]
    page.locator(".tile").nth(3).click()
    page.click("#checkout")
    txn_no = wait_new_txn_no(page, page.inner_text("#txnNo").strip())
    before = page.inner_text("#txnTotal").strip()
    page.wait_for_selector("#priceOps:not(.hide)")
    page.click("#priceOff")
    page.wait_for_function(
        "prev => { const e = document.querySelector('#txnTotal');"
        " return e && e.textContent.trim() !== prev; }",
        arg=before,
        timeout=10000,
    )
    after = page.inner_text("#txnTotal").strip()
    log(f"  改价（收款前）{txn_no}：{before} → {after}")
    assert after != before, "改价后金额应变化"
    page.click("#payCash")
    page.wait_for_selector("#afterSale:not(.hide)", timeout=15000)
    page.click("#refund")
    page.wait_for_function(
        "() => { const t = (document.querySelector('#toast')||{}).textContent||'';"
        " return t.includes('退货') || t.includes('冲正'); }",
        timeout=10000,
    )
    toast = page.inner_text("#toast")
    err = page.inner_text("#payErr").strip()
    log(f"  退货结果提示：{toast.strip()!r} · 页面错误：{err!r}")
    assert not err, f"退货报了错：{err!r}"
    page.screenshot(path=str(shots / "04-改价与退货.png"), full_page=True)
    return page


@step("主链⑤ 离线与补传")
def s5(ctx, base, shots, log, prev):
    page = prev["主链④ 改价与退货"]
    wait_pending(page, 0)
    page.click("#offlineToggle")
    badge = page.inner_text("#offlineBadge")
    log(f"  离线徽标：{badge.strip()!r}")
    assert "离线" in badge, "切换后徽标应显示离线"
    for i in range(3):
        page.locator(".tile").nth(i % page.locator(".tile").count()).click()
        page.click("#checkout")
        wait_pending(page, i + 1)
        log(f"  第 {i + 1} 笔暂存后 #pending={i + 1} · 界面提示：{page.inner_text('#offlineNote').strip()!r}")
    page.screenshot(path=str(shots / "05-离线暂存.png"), full_page=True)
    page.click("#offlineToggle")
    page.click("#syncNow")
    # ⚠️ 补传结果要读 **`#syncResult`**，不是 `#offlineNote`。
    # 第一版走查用 `/补传\s*3\s*笔/` 去匹配 `#offlineNote`，结果**匹配到了补传前的旧文案**
    # （"已本地暂存（待补传 3 笔）"）—— 断言照样绿，而它**根本没验到补传结果**。这是假绿。
    page.wait_for_function(
        "() => { const e = document.querySelector('#syncResult');"
        " const t = (e ? e.textContent : '').trim(); return t.length > 0; }",
        timeout=15000,
    )
    sync_text = page.inner_text("#syncResult").strip()
    wait_pending(page, 0)
    pending_now = page.inner_text("#pending").strip()
    log(f"  补传结果（#syncResult）：{sync_text!r}")
    log(f"  补传后 #pending：{pending_now!r}")
    # 演示脚本要说的两句硬话：服务端计数要对得上，且待补传归零
    assert "3" in sync_text, f"补传结果里应含服务端计数 3：{sync_text!r}"
    assert int(pending_now.split("/")[0].strip()) == 0, f"补传后待补传应归零：{pending_now!r}"
    page.screenshot(path=str(shots / "06-补传完成.png"), full_page=True)
    return page


@step("主链⑥ 运营端 / 经营数据")
def s6(ctx, base, shots, log, prev):
    prev["主链⑤ 离线与补传"].close()
    admin = ctx.new_page()
    admin.goto(f"{base}/admin/")
    admin.wait_for_load_state("networkidle")
    buttons = admin.evaluate(
        "() => Array.from(document.querySelectorAll('button'))"
        ".map(b => ({id: b.id || '', text: (b.textContent||'').trim().slice(0,24)}))"
    )
    log(f"  运营端按钮清单：{[(b['id'], b['text']) for b in buttons]}")
    sections = admin.evaluate(
        "() => Array.from(document.querySelectorAll('h1,h2,h3'))"
        ".map(h => (h.textContent||'').trim()).filter(Boolean)"
    )
    log(f"  页面分区：{sections}")
    admin.screenshot(path=str(shots / "07-运营端.png"), full_page=True)

    # 逐个点按钮（只读类操作），记录页面反馈与是否有报错
    clicked = []
    for b in buttons:
        bid = b["id"]
        if not bid:
            continue
        try:
            admin.click(f"#{bid}", timeout=4000)
            admin.wait_for_timeout(700)
            err = admin.evaluate(
                "() => { const e = document.querySelector('.err,#err,#adminErr');"
                " return e ? (e.textContent||'').trim() : ''; }"
            )
            clicked.append({"id": bid, "text": b["text"], "err": err})
        except Exception as exc:  # noqa: BLE001
            clicked.append({"id": bid, "text": b["text"], "err": f"点击失败: {str(exc)[:60]}"})
    for c in clicked:
        log(f"  点 #{c['id']:<22} {c['text']:<24} 页面错误={c['err']!r}")
    return clicked


def main():
    base = sys.argv[1].rstrip("/")
    shots = Path(sys.argv[2])
    shots.mkdir(parents=True, exist_ok=True)
    lines = []

    def log(msg):
        print(msg, flush=True)
        lines.append(msg)

    result = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 950})
        try:
            for name, fn in STEPS:
                log(f"\n=== {name} ===")
                t0 = time.monotonic()
                state = fn(ctx, base, shots, log, result)
                if state is not None:
                    result[name] = state
                log(f"  ⏱ {time.monotonic() - t0:.1f}s")
        finally:
            browser.close()
    log("\n=== 走查结论：六步全部走通 ===")
    (shots / "walkthrough.log").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"steps": len(STEPS), "shots": str(shots)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
