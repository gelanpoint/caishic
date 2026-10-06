"""`AC-034`~`038` 演示游戏端到端 + `AC-039` ①（端点面）—— `task-18` 独立验证件。

## 验证边界（**不许被绿灯盖掉**）

1. 本机**没有浏览器自动化依赖**（不许新增，`ADR-0004` 白名单）⇒ **"页面在真实浏览器里的渲染效果
   未经自动化验证"**。canvas 绘制、DOM 布局、鼠标悬停事件、像素观感**一律未验**。
2. 本文件做的是**静态 + HTTP 层 + 真实前端代码执行**三件事。其中 `AC-035` 的"清单"**不是读代码猜的**：
   用 Node 把**真的** `app/static/game/js/api.js` 加载起来（只桩 `fetch` / `sessionStorage` / `window`），
   调它**真的** `GameApi.stallData()` 打**真服务**，再拿回来的 `items` 与服务端逐项比对。
   **这仍然不是浏览器渲染验证** —— 只是"数据推导这一段用的是真代码、真数据"。
3. Node 缺失时相关用例**跳过并说明"这不是通过，是没检查"**（与 `test_no_external_assets.py` 同一先例）。
4. 顾客 NPC 是**本地演示逻辑，不代表真实并发**（`spec.md` §6 第 15 条）。
5. 像素画是**程序生成**（`scripts/gen_game_art.py`）不是美术师手绘；风格接近度是**主观判断**，
   本文件**不作**任何"与开罗游戏一致"式断言。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from e2e_support import (
    REPO_ROOT,
    bind_stall,
    session_headers,
    today_iso,
)

STATIC = REPO_ROOT / "app" / "static"
GAME = STATIC / "game"
API_JS = GAME / "js" / "api.js"
PANELS_JS = GAME / "js" / "panels.js"
STALL_NO = "A-01"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    not NODE, reason="环境里没有 node，无法执行前端数据层（**这不是通过，是没检查**）"
)

#: 只桩浏览器 API，**不碰**被测代码：`window` / `sessionStorage` / `fetch`（前缀真服务基址）
HARNESS = """
global.window = global;
const store = {};
global.sessionStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
const [base, apiJs, stallNo] = process.argv.slice(2);
const realFetch = global.fetch;
global.fetch = (path, opts) => realFetch(base + path, opts);
require(apiJs);
(async () => {
  const roster = await global.GameApi.roster();      // 业务日**以服务端返回为准**
  const data = await global.GameApi.stallData(stallNo);
  process.stdout.write(JSON.stringify({ roster, data }));
})().catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""


# --------------------------------------------------------------------------- #
# 助手
# --------------------------------------------------------------------------- #


def _game_api(server, tmp_path, stall_no: str = STALL_NO) -> dict:
    """在 Node 里跑**真的**前端数据层，返回 `{roster, data}`（`data.items` 即悬停清单）。"""
    harness = tmp_path / "game_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(harness), server.base, str(API_JS), stall_no],
        capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
        encoding="utf-8", errors="replace",
    )
    assert done.returncode == 0, f"Node 跑前端数据层失败：\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout)


def _server_day(server) -> str:
    """业务日**只认服务端**（看板响应里的 `business_date`）—— 不用墙钟去猜（会产生假红）。"""
    status, payload = server.api("GET", f"/api/admin/dashboard?business_date={today_iso()}")
    assert status == 200, f"§3.25 看板期望 200，实际 {status}：{payload}"
    return payload["business_date"]


def _expected_items(server, token: str, day: str) -> list[dict]:
    """服务端口径的"悬停清单"：§3.5 在售商品为骨架 × §3.3/§3.4 单价。"""
    status, products = server.api("GET", "/api/merchant/products", headers=session_headers(token))
    assert status == 200, f"§3.5 期望 200，实际 {status}：{products}"
    status, price_list = server.api(
        "GET", f"/api/merchant/price-list?business_date={day}", headers=session_headers(token)
    )
    assert status == 200, f"§3.3 期望 200，实际 {status}：{price_list}"
    cents = {row["product_id"]: row["unit_price_cents"] for row in price_list["items"]}
    return [
        {"productId": p["id"], "name": p["name"], "unitPriceCents": cents.get(p["id"])}
        for p in products
    ]


def _set_price(server, token: str, day: str, product_id: int, cents: int) -> dict:
    status, payload = server.api(
        "POST", "/api/merchant/price-list",
        {"business_date": day, "items": [{"product_id": product_id, "unit_price_cents": cents}]},
        session_headers(token),
    )
    assert status == 200, f"§3.4 调价期望 200，实际 {status}：{payload}"
    return payload


def _set_status(server, token: str, product_id: int, status_value: str):
    return server.api(
        "POST", f"/api/merchant/products/{product_id}/status",
        {"status": status_value}, session_headers(token),
    )


def _scale_catalog(server) -> list[dict]:
    """秤端字典（§3.3）——需先预注册并激活一台设备（数据面要求 `active`）。"""
    from app.domain.device import provision_device

    device_id, device_token = "SC-GAME-01", "game-e2e-device-token-0123456789"
    conn = server.connect_db()
    try:
        provision_device(conn, device_id=device_id, market_code="M-0001",
                         stall_no=STALL_NO, token=device_token)
        conn.commit()
    finally:
        conn.close()
    headers = {"X-Device-Token": device_token, "X-Scale-Proto": "1"}
    status, _ = server.api(
        "POST", "/api/scale/v1/devices/activate",
        {"device_id": device_id, "market_code": "M-0001", "stall_no": STALL_NO}, headers,
    )
    assert status == 200, f"§3.1 激活期望 200，实际 {status}"
    status, payload = server.api("GET", "/api/scale/v1/catalog", headers=headers)
    assert status == 200, f"§3.3 秤端字典期望 200，实际 {status}：{payload}"
    return payload["products"]


# --------------------------------------------------------------------------- #
# `AC-034`：三视角存在且可切换（静态结构 + 页面/资源可加载）
# --------------------------------------------------------------------------- #


def view_keys_from_js(text: str) -> list[str]:
    """从 `panels.js` 的 `VIEWS = {...}` 里取视角键（**纯函数**，便于喂合成负例）。"""
    start = text.index("var VIEWS = {")
    end = text.index("\n  };", start)
    block = text[start:end]
    return re.findall(r"^    (\w+):\s*\{", block, re.MULTILINE)


def test_three_viewpoints_are_defined_with_actions_and_wired_to_tabs(live_server):
    """`AC-034`：商家 / 顾客 / 管理员三视角齐全，各带说明与可执行操作，且点击标签会切换。"""
    text = PANELS_JS.read_text(encoding="utf-8")
    keys = view_keys_from_js(text)
    assert keys == ["merchant", "customer", "admin"], f"三视角定义不符：{keys}"
    block = text[text.index("var VIEWS = {") : text.index("\n  };", text.index("var VIEWS = {"))]
    for key in keys:
        assert re.search(rf"{key}:", block)
    assert block.count("label:") == 3 and block.count("caption:") == 3 and block.count("actions:") == 3
    # 标签按钮必须真的绑定到 setView（否则"可切换"只是文案）
    assert re.search(r"tab\.onclick\s*=\s*function\s*\(\)\s*\{\s*setView\(key\)", text)
    assert "function setView(view)" in text
    # 页面要有承载三视角的容器
    page = (GAME / "index.html").read_text(encoding="utf-8")
    for element_id in ("viewTabs", "viewCaption", "viewActions", "gameCanvas", "tooltipList"):
        assert f'id="{element_id}"' in page, f"页面缺少 `{element_id}`"


def test_sensitivity_view_scan_notices_a_missing_view():
    """**灵敏度负例（纯函数，不落盘）**：视角少一个 ⇒ 扫描结果必须随之变化。"""
    real = view_keys_from_js(PANELS_JS.read_text(encoding="utf-8"))
    assert len(real) == 3
    broken = "  var VIEWS = {\n    merchant: {\n      label: \"商家\",\n    },\n    customer: {\n      label: \"顾客\",\n    }\n  };\n"
    assert view_keys_from_js(broken) == ["merchant", "customer"], "少一个视角必须能被看出来"


def test_game_page_and_its_referenced_assets_are_served(live_server):
    """`AC-034` 前半段 + `AC-037`（HTTP 面）：页面 200，且它引用的资源逐个 200。"""
    status, _ = live_server.api("GET", "/game/")
    assert status == 200, "演示游戏页必须可加载"
    refs = re.findall(r'(?:src|href)="([^"]+)"', (GAME / "index.html").read_text(encoding="utf-8"))
    assert len(refs) >= 9, f"页面只引用 {len(refs)} 个资源，太少（清单可能被改动）"
    for ref in refs:
        assert live_server.api("GET", ref)[0] == 200, f"`/game/` 引用的 `{ref}` 取不到"


# --------------------------------------------------------------------------- #
# `AC-035`：悬停清单**逐项等于服务端**（核心，必须能失败）
# --------------------------------------------------------------------------- #


@needs_node
def test_hover_list_equals_the_server_price_list_item_by_item(live_server, tmp_path):
    """`AC-035`：用**真的前端代码**推导出的清单，与服务端逐项相等（含"下架的不显示"）。"""
    result = _game_api(live_server, tmp_path)
    day = result["roster"]["businessDate"]
    assert result["data"]["businessDate"] == day, "业务日必须来自服务端，不得由前端自己造"
    assert day == _server_day(live_server), "前端拿到的业务日必须等于服务端看板里的业务日"

    token = bind_stall(live_server, STALL_NO)
    expected = _expected_items(live_server, token, day)
    actual = [
        {"productId": i["productId"], "name": i["name"], "unitPriceCents": i["unitPriceCents"]}
        for i in result["data"]["items"]
    ]
    assert actual == expected, (
        f"悬停清单必须**逐项等于**服务端（在售商品 × 单价）：\n前端={actual[:3]}…\n服务端={expected[:3]}…"
    )
    assert len(actual) >= 10, f"清单只有 {len(actual)} 项，判据可能已失效"


@needs_node
def test_hover_list_follows_a_server_side_price_change(live_server, tmp_path):
    """`AC-035` **灵敏度**：经真实端点改价后重新加载，清单随之改变（证明不是前端硬编码）。"""
    token = bind_stall(live_server, STALL_NO)
    day = _server_day(live_server)
    before = _game_api(live_server, tmp_path)["data"]["items"]
    assert before, "前置：清单不得为空"

    target = before[0]["productId"]
    old_cents = before[0]["unitPriceCents"]
    new_cents = old_cents + 7 if old_cents is not None else 1234
    try:
        _set_price(live_server, token, day, target, new_cents)
        after = _game_api(live_server, tmp_path)["data"]["items"]
        changed = {i["productId"]: i["unitPriceCents"] for i in after}
        assert changed[target] == new_cents, (
            f"改价后清单里的单价必须跟着变（前端硬编码就做不到）：期望 {new_cents}，实际 {changed[target]}"
        )
        # 两次**不同数据**得到**两个不同结果** —— 这是"不是静态字符串"的直接证据
        assert before != after, "两次不同服务端数据必须产出两份不同的清单"
        assert len(before) == len(after), "只改价，条目数不该变"
    finally:
        _set_price(live_server, token, day, target, old_cents if old_cents is not None else 1)


# --------------------------------------------------------------------------- #
# `AC-036`：调价 / 上下架经**真实端点**生效（查库，不只看响应）
# --------------------------------------------------------------------------- #


def test_price_change_is_persisted_in_the_database(live_server):
    """`AC-036` ①：调价后**查库**确认 `price_item.unit_price_cents` 真的变了。"""
    token = bind_stall(live_server, STALL_NO)
    day = _server_day(live_server)
    conn = live_server.connect_db()
    try:
        row = conn.execute(
            "SELECT product_id, unit_price_cents FROM price_item WHERE business_date = ?"
            " ORDER BY product_id LIMIT 1", (day,),
        ).fetchone()
        assert row is not None, f"{day} 没有价目表行（种子未导入？）"
        product_id, old_cents = int(row["product_id"]), int(row["unit_price_cents"])
        _set_price(live_server, token, day, product_id, old_cents + 5)
        try:
            stored = conn.execute(
                "SELECT unit_price_cents FROM price_item WHERE business_date = ? AND product_id = ?",
                (day, product_id),
            ).fetchone()["unit_price_cents"]
            assert int(stored) == old_cents + 5, f"库里必须真的改掉：期望 {old_cents + 5}，实际 {stored}"
        finally:
            _set_price(live_server, token, day, product_id, old_cents)
            restored = conn.execute(
                "SELECT unit_price_cents FROM price_item WHERE business_date = ? AND product_id = ?",
                (day, product_id),
            ).fetchone()["unit_price_cents"]
            assert int(restored) == old_cents, "用例必须还原现场"
    finally:
        conn.close()


@needs_node
def test_shelving_removes_the_item_from_products_catalog_and_hover_list(live_server, tmp_path):
    """`AC-036` 后半段 + `AC-039` ①：下架 ⇒ 三处都消失；上架 ⇒ 三处都恢复。"""
    token = bind_stall(live_server, STALL_NO)
    day = _server_day(live_server)
    product_id = _game_api(live_server, tmp_path)["data"]["items"][0]["productId"]

    def snapshot() -> tuple[bool, bool, bool]:
        _, products = live_server.api("GET", "/api/merchant/products", headers=session_headers(token))
        in_products = any(p["id"] == product_id for p in products)
        in_catalog = any(p["product_id"] == product_id for p in _scale_catalog(live_server))
        in_hover = any(i["productId"] == product_id for i in _game_api(live_server, tmp_path)["data"]["items"])
        return in_products, in_catalog, in_hover

    assert snapshot() == (True, True, True), "前置：在售商品应同时出现在三处"
    try:
        status, payload = _set_status(live_server, token, product_id, "inactive")
        assert status == 200, f"下架期望 200，实际 {status}：{payload}"
        assert snapshot() == (False, False, False), "下架后必须从商品列表 / 秤端字典 / 悬停清单**三处**消失"
    finally:
        status, payload = _set_status(live_server, token, product_id, "active")
        assert status == 200, f"重新上架期望 200，实际 {status}：{payload}"
    assert snapshot() == (True, True, True), "重新上架后必须三处都恢复"


def test_invalid_status_over_http_is_a_contract_error(live_server):
    """`AC-039` ④（HTTP 面）：非法 `status` ⇒ `MT-1008`，且**库里 `status` 未变**。"""
    token = bind_stall(live_server, STALL_NO)
    _, products = live_server.api("GET", "/api/merchant/products", headers=session_headers(token))
    product_id = products[0]["id"]
    status, payload = _set_status(live_server, token, product_id, "deleted")
    assert status == 422, f"非法 status 期望 422，实际 {status}：{payload}"
    assert payload["error"]["code"] == "MT-1008", payload
    conn = live_server.connect_db()
    try:
        assert conn.execute("SELECT status FROM product WHERE id = ?", (product_id,)).fetchone()["status"] == "active"
    finally:
        conn.close()


def test_cross_stall_shelving_over_http_is_forbidden_and_writes_nothing(live_server):
    """`AC-039` ③（HTTP 面）：用**别的摊位**的会话改本摊位商品 ⇒ 403 `MT-1004`，且**查库**确认未变。"""
    victim_token = bind_stall(live_server, STALL_NO)
    _, products = live_server.api("GET", "/api/merchant/products", headers=session_headers(victim_token))
    product_id = products[0]["id"]
    intruder_token = bind_stall(live_server, "A-02")

    status, payload = _set_status(live_server, intruder_token, product_id, "inactive")
    assert status == 403, f"越权期望 403，实际 {status}：{payload}"
    assert payload["error"]["code"] == "MT-1004", payload
    conn = live_server.connect_db()
    try:
        row = conn.execute("SELECT status, stall_id FROM product WHERE id = ?", (product_id,)).fetchone()
        assert row["status"] == "active", "越权被拒后目标行**必须原样**（查库，不只看返回值）"
        assert row["stall_id"] != conn.execute(
            "SELECT id FROM stall WHERE stall_no = 'A-02'"
        ).fetchone()["id"], "正对照：目标商品确实属于另一个摊位（否则这条用例测的不是越权）"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# `AC-038`：隔离性（无新增运行期依赖 + 既有页面不变）
# --------------------------------------------------------------------------- #


def test_requirements_txt_is_byte_identical_to_head_and_has_no_new_runtime_dep():
    """`AC-038` ①：`requirements.txt` 与改动前**逐字节**一致，且运行期依赖仍只有白名单里的 `Flask`。"""
    working = (REPO_ROOT / "requirements.txt").read_bytes()
    head = subprocess.run(
        ["git", "show", "HEAD:requirements.txt"], cwd=str(REPO_ROOT),
        capture_output=True, encoding="utf-8", errors="replace",
    )
    assert head.returncode == 0, f"取不到 HEAD 版本：{head.stderr}"
    assert working.decode("utf-8") == head.stdout, "requirements.txt 与改动前不得有任何差异"
    deps = [
        line.strip() for line in working.decode("utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert deps == ["Flask==3.1.3"], f"运行期依赖必须只有白名单里的 Flask：{deps}"


def test_existing_four_pages_and_form1_banner_are_unchanged(live_server):
    """`AC-038` ②：既有 4 个页面仍可用；形态 1 横幅仍打印 IP / 两个入口 / 数据文件路径。"""
    for path in ("/", "/scale/", "/customer/", "/admin/"):
        assert live_server.api("GET", path)[0] == 200, f"既有页面 `{path}` 不得被破坏"
    assert live_server.api("GET", "/game/")[0] == 200, "演示游戏页是**纯新增**入口"

    banner = live_server.log_text()
    for needle in ("操作端", "顾客扫码", "数据文件"):
        assert needle in banner, f"形态 1 横幅缺少 `{needle}`：\n{banner}"
    assert "/scale/" in banner and "/customer/" in banner
    assert "本形态不托管" not in banner, "形态 1 横幅不得出现分离形态的措辞（那是 `--mode split` 的）"
