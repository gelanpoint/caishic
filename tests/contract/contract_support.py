"""契约测试基础设施（`T-007`）—— 夹具与「检查原语」。

关联：`REQ-024`；`NFR-009`、`NFR-010`；`AC-012`。产出文件授权：`plan.md` §4（`tests/` 目录级）+ `tasks.md` §3 `T-007`。

四类东西：①契约解析（§2 端点表 / §4 错误码表，机械抽取不手抄）；②路由表 ↔ §2 双向一致
（`assert_route_table_matches_contract`，多一个或少一个都必须红）；③统一错误格式
（`assert_error_response`，对已知坏样例必须报错）；④敏感字段扫描（`scan_json_for_sensitive` 等，
并由 `SensitiveScanClient` 对**每一个契约测试的每一份 JSON 响应**自动扫描，不是只扫一次）。

纪律：只校验契约 §2/§3（§6 的 OpenAPI 骨架是派生视图，不作依据）；不引入契约与 spec 之外的
端点/字段/错误码（`RL-1`）；每条检查都配灵敏度负例（`AGENTS.md`："验证要能失败"）。
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from flask.testing import FlaskClient

# 敏感字段扫描原语已按语义拆到 `sensitive_scan.py`（`Q-16`）；此处只引入 `SensitiveScanClient` 需要的那一个
from sensitive_scan import scan_json_for_sensitive  # noqa: E402  (conftest 内的兄弟模块导入)

# 必须在 import app.* 之前设置（app/config.py 在导入时读取 MT_DATA_DIR）
REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "specs" / "market-trade-flow" / "contracts" / "rest-api.md"
REQUIREMENTS_RUNTIME = REPO_ROOT / "requirements.txt"
REQUIREMENTS_DEV = REPO_ROOT / "requirements-dev.txt"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: 契约测试专用数据目录（**进程退出时删除**）。
#: 这里有两个**互相独立**的问题，不要混为一谈（实测根因见 `docs/PROJECT-STATE.md` 变更记录）：
#: ① 本目录原先只建不删 → 每次 pytest 都在系统 Temp 泄漏一个 `mt-contract-*`；现由 `atexit` 兜底清除；
#: ② pytest 自身的 basetemp 清理报 `PermissionError` 与之**无关**（那条路径是 `pytest-of-<user>/pytest-current`，
#:   与 `mt-contract-*` 无关），修复在仓库根 `pytest.ini` 的 `--basetemp=.pytest-tmp`。
TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="mt-contract-"))
atexit.register(shutil.rmtree, TEST_DATA_DIR, ignore_errors=True)
os.environ["MT_DATA_DIR"] = str(TEST_DATA_DIR)

SEED_STALL_NO = "A-01"  #: 演示数据里的已知摊位号（`seed.json` 的首个摊位）

#: **非接口路由豁免表**：契约 §2 只登记接口端点，入口导航页与静态资源由 Flask 托管
#: （`plan.md` §4 / `AGENTS.md` §3 硬要求 4）。表外任何路由都会被判为「契约外端点」并失败，
#: 强制「新增端点必须先进契约再进代码」（`RL-1`）。⚠️ 路径必须是 `normalize_rule` 之后的归一形式
#: （原写 `<path:filename>` 与归一后的路由表永远匹配不上，该缺陷由真实破坏演练暴露并修正）。
NON_API_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/"),
        ("GET", "/static/{filename}"),
        # 三个页面入口（T-026 秤端 / T-027 运营端 / T-028 顾客扫码页）—— 页面不是接口，
        # 但 `run.py` 的启动横幅正是按这三个路径打印演示入口，故它们必须有路由
        # （否则现场照着横幅打开会 404）。改路径必须同时改这里，否则路由表比对会红。
        ("GET", "/scale/"),
        ("GET", "/admin/"),
        ("GET", "/customer/"),
        # 演示游戏页面（`REQ-044`，2026-10-06）—— 与上面三个页面同类：**页面不是接口**，
        # 故不进契约端点表；但它必须有路由，否则负责人照着导航点进去会 404。
        # 它**两种形态都注册**（纯展示层，不依赖秤端界面归谁托管）。
        ("GET", "/game/"),
        # 演示控制台三个页面（`REQ-057`，2026-10-08）—— 与 `/game/` 同类：**页面不是接口**，
        # 故不进契约端点表；但必须有路由，否则照着导航点进去会 404。
        ("GET", "/demo/register/"),
        ("GET", "/demo/scale/"),
        ("GET", "/demo/hub/"),
    }
)

# 1. 契约解析


def load_contract_text() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8")


def _section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    return text[start : text.index(next_heading, start + len(heading))]


#: §2 行：`| 1 | \`/healthz\` | GET | 说明 | 关联需求 |`；§4 行：`| \`MT-1001\` | 409 | ... |`
_ENDPOINT_ROW_RE = re.compile(r"^\|\s*\d+\s*\|\s*`([^`]+)`\s*\|\s*([A-Z]+)\s*\|", re.M)
_ERROR_ROW_RE = re.compile(r"^\|\s*`(MT-\d{4})`\s*\|\s*(\d{3})\s*\|", re.M)


def parse_contract_endpoints(text: str | None = None) -> frozenset[tuple[str, str]]:
    """机械抽取契约 §2 → `{(方法, 路径)}`（路径保留 `{param}` 占位形式）。"""
    section = _section(text or load_contract_text(), "## 2. 端点总表", "\n## 3. ")
    return frozenset((m.group(2), m.group(1)) for m in _ENDPOINT_ROW_RE.finditer(section))


def parse_error_codes(text: str | None = None) -> dict[str, int]:
    """机械抽取契约 §4 → `{错误码: HTTP 状态}`。"""
    section = _section(text or load_contract_text(), "## 4. 统一错误码表", "\n## 5. ")
    return {m.group(1): int(m.group(2)) for m in _ERROR_ROW_RE.finditer(section)}


CONTRACT_ENDPOINTS: frozenset[tuple[str, str]] = parse_contract_endpoints()
CONTRACT_ERROR_CODES: dict[str, int] = parse_error_codes()

#: 形态 2（秤端 ↔ 中台）契约：**独立文件、独立解析，刻意不并入上面两个集合**。
#: 为什么不合并：`CONTRACT_ENDPOINTS` / `CONTRACT_ERROR_CODES` 的条数语义（31 端点 / 14 错误码）
#: 是既有用例的**判据本身**（`test_contract_surface.py` 写死了条数），合并会让那套判据漂移，
#: 而漂移掉的正是"有人改契约时必须来看一眼"的提示。
#: 两个契约各自做一遍「路由表 ↔ 契约 §2」的双向核验（形态 2 在 `test_scale_contract_surface.py`），
#: **谁少登记谁变红，互不掩盖**。
SCALE_CONTRACT_PATH = REPO_ROOT / "specs" / "market-trade-flow" / "contracts" / "scale-midplatform.md"


def load_scale_contract_text() -> str:
    return SCALE_CONTRACT_PATH.read_text(encoding="utf-8")


def parse_scale_contract_endpoints(text: str | None = None) -> frozenset[tuple[str, str]]:
    """机械抽取形态 2 契约 §2 → `{(方法, 路径)}`（路径保留 `{param}` 占位形式）。"""
    section = _section(text or load_scale_contract_text(), "## 2. 端点总表", "\n## 3. ")
    return frozenset((m.group(2), m.group(1)) for m in _ENDPOINT_ROW_RE.finditer(section))


def parse_scale_error_codes(text: str | None = None) -> dict[str, int]:
    """机械抽取形态 2 契约 §4 → `{错误码: HTTP 状态}`（`MT-2xxx` 秤端接入段）。"""
    section = _section(text or load_scale_contract_text(), "## 4. 统一错误码表", "\n## 5. ")
    return {m.group(1): int(m.group(2)) for m in _ERROR_ROW_RE.finditer(section)}


SCALE_CONTRACT_ENDPOINTS: frozenset[tuple[str, str]] = parse_scale_contract_endpoints()
SCALE_CONTRACT_ERROR_CODES: dict[str, int] = parse_scale_error_codes()

# 2. Flask 路由表 ↔ 契约 §2 双向一致

_FLASK_PARAM_RE = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")


def normalize_rule(rule: str) -> str:
    """把 Flask 路由规则（`/a/<int:x>`）归一成契约写法（`/a/{x}`）。"""
    return _FLASK_PARAM_RE.sub(r"{\1}", rule)


def registered_endpoints(app) -> set[tuple[str, str]]:
    """已注册的接口端点（剔除 HEAD/OPTIONS 与豁免的非接口路由）。"""
    found: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        for method in {m for m in rule.methods if m not in {"HEAD", "OPTIONS"}}:
            pair = (method, normalize_rule(str(rule)))
            if pair not in NON_API_ROUTES:
                found.add(pair)
    return found


def route_table_diff(app, contract: frozenset[tuple[str, str]] | None = None):
    """返回 `(missing, extra)`：契约有而实现没有 / 实现有而契约没有。

    `extra` 一侧**先减去形态 2 的端点**：那些路由由 `scale-midplatform.md` 登记、
    并在 `test_scale_contract_surface.py` 里做自己的双向核验。不减的话，形态 2 一注册路由，
    本函数就会把它们报成「契约未登记」，而那是**误报** —— 两个契约各自负责自己的端点，
    谁少登记谁在自己的检查里变红。
    """
    expected = CONTRACT_ENDPOINTS if contract is None else contract
    actual = registered_endpoints(app) - SCALE_CONTRACT_ENDPOINTS
    return sorted(expected - actual), sorted(actual - expected)


def assert_route_table_matches_contract(app) -> None:
    """双向一致断言：missing 与 extra 任一非空即失败（`tasks.md` `T-007` 能力①）。"""
    missing, extra = route_table_diff(app)
    problems: list[str] = []
    if missing:
        lines = "\n".join(f"    - {m} {p}" for m, p in missing)
        problems.append(f"契约 §2 已登记但实现未注册（{len(missing)} 个）：\n{lines}")
    if extra:
        lines = "\n".join(f"    - {m} {p}" for m, p in extra)
        problems.append(f"实现已注册但契约 §2 未登记（{len(extra)} 个，违反 RL-1「规格先于代码」）：\n{lines}")
    if problems:
        raise AssertionError("路由表 ↔ 契约 §2 端点总表不一致：\n" + "\n".join(problems))


def assert_endpoint_implemented(app, method: str, path: str) -> None:
    """**前置**：端点必须已实现 —— 否则"期望 404 / `MT-1009`"会被通用 404 处理器**假绿**（T-008 首轮实测暴露）。"""
    assert (method, path) in registered_endpoints(app), f"前置失败：{method} {path} 尚未实现（契约 §2 已登记）"

# 3. 统一错误响应格式（契约 §1.2 + §4）


def error_envelope_violations(payload, http_status: int | None = None, codes: dict | None = None) -> list[str]:
    """校验响应体是否符合契约 §1.2（`{"error": {"code","message","detail"}}`）；返回违规清单。"""
    table = CONTRACT_ERROR_CODES if codes is None else codes
    if not isinstance(payload, dict):
        return [f"顶层不是 JSON 对象，实际为 {type(payload).__name__}"]

    violations: list[str] = []
    if set(payload) != {"error"}:
        violations.append(f"顶层键必须恰好为 ['error']，实际为 {sorted(payload)}")

    error = payload.get("error")
    if not isinstance(error, dict):
        violations.append(f"'error' 必须是对象，实际为 {type(error).__name__}")
        return violations

    unexpected = set(error) - {"code", "message", "detail"}
    if unexpected:
        violations.append(f"'error' 含契约 §1.2 之外的键：{sorted(unexpected)}")

    code = error.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"MT-\d{4}", code):
        violations.append(f"'error.code' 必须是形如 MT-#### 的字符串，实际为 {code!r}")
    elif code not in table:
        violations.append(f"错误码 {code} 不在契约 §4 错误码表内")
    elif http_status is not None and http_status != table[code]:
        violations.append(f"HTTP 状态 {http_status} 与契约 §4 为该错误码规定的 {table[code]} 不一致")

    message = error.get("message")
    if not isinstance(message, str) or not message.strip():
        violations.append(f"'error.message' 必须是非空字符串，实际为 {message!r}")

    if "detail" in error and not isinstance(error["detail"], dict):
        violations.append(f"'error.detail' 若出现必须是对象，实际为 {type(error['detail']).__name__}")

    return violations


def assert_error_response(response, expected_code: str | None = None) -> dict:
    """断言响应是合规的统一错误响应，并（可选）断言具体错误码。返回其 JSON 体。"""
    payload = response.get_json(silent=True)
    violations = error_envelope_violations(payload, http_status=response.status_code)
    if expected_code is not None:
        actual = (payload or {}).get("error", {}).get("code") if isinstance(payload, dict) else None
        if actual != expected_code:
            violations.append(f"期望错误码 {expected_code}，实际为 {actual!r}")
    if violations:
        body = response.get_data(as_text=True)[:400]
        raise AssertionError(
            "统一错误响应格式不合规（契约 §1.2 / §4）：\n"
            + "\n".join(f"    - {v}" for v in violations)
            + f"\n    实际响应：HTTP {response.status_code} {body}"
        )
    return payload

# 4. 敏感字段扫描（`REQ-024` / `NFR-012` / `AC-012`）


class SensitiveScanClient(FlaskClient):
    """每次请求后自动扫描 JSON 响应体的测试客户端 —— `T-008`~`T-014` 的每份响应都必经此处。"""

    def open(self, *args, **kwargs):
        response = super().open(*args, **kwargs)
        if response.is_json:
            where = f"{response.request.method} {response.request.path}"
            hits = scan_json_for_sensitive(response.get_json(silent=True), where=where)
            if hits:
                raise AssertionError(
                    "响应体命中敏感字段禁令（REQ-024 / NFR-012 / AC-012）：\n"
                    + "\n".join(f"    - {h}" for h in hits)
                )
        return response

# 5. 夹具


@pytest.fixture(scope="session")
def seeded_app():
    """建库 + 导入种子 + 创建 Flask 应用（会话级，指向测试专用数据目录）。"""
    from app import create_app
    from app import db as app_db
    from app import seed as app_seed

    app_db.init_database()
    app_seed.import_seed()
    application = create_app()
    application.config.update(TESTING=True)
    application.test_client_class = SensitiveScanClient
    return application


@pytest.fixture()
def client(seeded_app):
    """Flask 测试客户端（带响应体敏感字段自动扫描）。"""
    return seeded_app.test_client()


@pytest.fixture()
def contract():
    """契约的机械视图：端点总表 + 错误码表。"""
    return {"endpoints": CONTRACT_ENDPOINTS, "error_codes": CONTRACT_ERROR_CODES}


@pytest.fixture()
def db_conn():
    """直连测试库（供**布置前置条件**用：契约测试要能构造边界状态）。"""
    from app import db as app_db

    connection = app_db.connect()
    try:
        yield connection
    finally:
        connection.close()

# 6. 契约调用助手（供各契约测试组复用；只依赖契约 §2/§3 定义的字段）


def today_iso() -> str:
    """营业日（契约 §1.4：`YYYY-MM-DD`）。"""
    from datetime import date

    return date.today().isoformat()


def json_of(response) -> dict | list:
    """取响应 JSON（失败时给出可读的状态码与正文片段）。

    **接受对象或数组** —— 契约里两种响应形态都有：§3.2 / §3.3 / §3.4 回对象，
    §3.5 商品列表回数组。本助手原先只接受 `dict`，与契约 §3.5「响应(200): 数组」
    **直接矛盾**：任何符合契约的实现都不可能通过，属**测试自身的缺陷**，故按契约修正
    （契约是唯一事实来源，不是测试 —— 判据与本次变更原因见提交信息）。

    仍拒绝标量与 null —— 保留本助手作为「响应结构校验」的价值；
    放宽到"什么都接受"就等于把这个检查废掉。
    """
    payload = response.get_json(silent=True)
    assert isinstance(payload, (dict, list)), (
        f"响应既不是 JSON 对象也不是数组：HTTP {response.status_code} {response.get_data(as_text=True)[:300]}"
    )
    return payload


def session_headers(token: str, idempotency_key: str | None = None) -> dict[str, str]:
    """构造秤端请求头（契约 §1.3 / §1.6）。"""
    headers = {"X-Stall-Session": token}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def bind_stall_session(client, stall_no: str = SEED_STALL_NO) -> str:
    """按契约 §3.2 绑定摊位并取回会话 Token（失败即断言失败，并给出可读原因）。"""
    response = client.post("/api/merchant/session", json={"stall_no": stall_no})
    assert response.status_code == 201, (
        f"契约 §3.2 期望 201，实际 {response.status_code}：{response.get_data(as_text=True)[:300]}"
    )
    token = json_of(response).get("session_token")
    assert isinstance(token, str) and 16 <= len(token) <= 64, (
        f"契约 §3.2 要求响应含 16~64 位的 session_token，实际 {token!r}"
    )
    return token


def catalog(client, token: str) -> tuple[list, dict | list]:
    """取回本摊位当前可售商品（§3.5：**数组**）与当日价目表（§3.3：对象）—— 形态不同，故并集标注。"""
    products = client.get("/api/merchant/products", headers=session_headers(token))
    assert products.status_code == 200, (
        f"契约 §3.5 期望 200，实际 {products.status_code}：{products.get_data(as_text=True)[:300]}"
    )
    price_list = client.get(
        "/api/merchant/price-list",
        query_string={"business_date": today_iso()},
        headers=session_headers(token),
    )
    assert price_list.status_code == 200, (
        f"契约 §3.3 期望 200，实际 {price_list.status_code}：{price_list.get_data(as_text=True)[:300]}"
    )
    return json_of(products), json_of(price_list)


def create_priced_transaction(
    client, token: str, *, idempotency_key: str, weights_grams: list[int] | None = None
) -> dict:
    """按契约 §3.6 创建一笔已计价交易（供收款 / 退货 / 对账用例复用）。"""
    products = catalog(client, token)[0]
    assert products, "契约 §3.5 未返回任何可售商品（种子数据应含每摊位 25 个商品）"
    weights = weights_grams or [1000]
    product_ids = [item["id"] for item in products[: len(weights)]]
    response = client.post(
        "/api/merchant/transactions",
        json={
            "items": [
                {"product_id": pid, "weight_grams": weight}
                for pid, weight in zip(product_ids, weights)
            ]
        },
        headers=session_headers(token, idempotency_key),
    )
    assert response.status_code == 201, (
        f"契约 §3.6 期望 201，实际 {response.status_code}：{response.get_data(as_text=True)[:300]}"
    )
    payload = json_of(response)
    assert payload.get("status") == "priced", f"契约 §3.6 要求 status=priced，实际 {payload.get('status')!r}"
    return payload


def assert_exact_keys(payload: dict, allowed: set[str], where: str) -> None:
    """字段白名单断言：**不得**出现白名单之外的字段（`AC-011` 的落地形式）。"""
    extra = sorted(set(payload) - allowed)
    assert not extra, f"{where} 出现白名单之外的字段（契约要求字段白名单）：{extra}"
