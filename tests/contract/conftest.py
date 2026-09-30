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

import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from flask.testing import FlaskClient

# 必须在 import app.* 之前设置（app/config.py 在导入时读取 MT_DATA_DIR）
REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "specs" / "market-trade-flow" / "contracts" / "rest-api.md"
REQUIREMENTS_RUNTIME = REPO_ROOT / "requirements.txt"
REQUIREMENTS_DEV = REPO_ROOT / "requirements-dev.txt"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="mt-contract-"))
os.environ["MT_DATA_DIR"] = str(TEST_DATA_DIR)

SEED_STALL_NO = "A-01"  #: 演示数据里的已知摊位号（`seed.json` 的首个摊位）

#: **非接口路由豁免表**：契约 §2 只登记接口端点，入口导航页与静态资源由 Flask 托管
#: （`plan.md` §4 / `AGENTS.md` §3 硬要求 4）。表外任何路由都会被判为「契约外端点」并失败，
#: 强制「新增端点必须先进契约再进代码」（`RL-1`）。⚠️ 路径必须是 `normalize_rule` 之后的归一形式
#: （原写 `<path:filename>` 与归一后的路由表永远匹配不上，该缺陷由真实破坏演练暴露并修正）。
NON_API_ROUTES: frozenset[tuple[str, str]] = frozenset({("GET", "/"), ("GET", "/static/{filename}")})

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
    """返回 `(missing, extra)`：契约有而实现没有 / 实现有而契约没有。"""
    expected = CONTRACT_ENDPOINTS if contract is None else contract
    actual = registered_endpoints(app)
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

#: 字段名禁令：与 `data-model.md` §全局约定 逐条对齐（`id_card*` / `id_no*` / `bank_card*` /
#: `card_no*` / `bank_account*`），并额外国产中文列名两种写法。
FORBIDDEN_FIELD_RE = re.compile(r"^(?:id_card|id_no|bank_card|card_no|bank_account)|身份证|银行卡", re.IGNORECASE)
#: 身份证号（18 位，含出生日期段）与银行卡号形态（16~19 位连续数字）。
#: 注：身份证号若以 `X` 结尾，会同时命中银行卡形态（`\d{16,19}` 后的 `X` 不算数字）——
#: 两者都属禁令范围，重复命中不影响判定，但不宣称能区分二者。
ID_CARD_VALUE_RE = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
BANK_CARD_VALUE_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")


def scan_json_for_sensitive(payload, where: str = "响应体") -> list[str]:
    """递归扫描 JSON 的字段名与字符串取值；返回可读命中清单（空 = 零命中）。"""
    hits: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                if isinstance(key, str) and FORBIDDEN_FIELD_RE.search(key):
                    hits.append(f"{where}: 命中禁忌字段名 `{child}`")
                walk(value, child)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            if ID_CARD_VALUE_RE.search(node):
                hits.append(f"{where}: 取值疑似身份证号 → `{path}`")
            if BANK_CARD_VALUE_RE.search(node):
                hits.append(f"{where}: 取值疑似银行卡号 → `{path}`")

    walk(payload, "")
    return hits


def scan_text_for_sensitive(text: str, where: str = "文本") -> list[str]:
    """扫描文本（源码 / SQL / 库文件字节）中的禁忌标识符与取值形态。"""
    hits = [
        f"{where}: 命中禁忌标识符 `{token}`"
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
        if FORBIDDEN_FIELD_RE.search(token)
    ]
    if ID_CARD_VALUE_RE.search(text):
        hits.append(f"{where}: 命中身份证号形态的取值")
    if BANK_CARD_VALUE_RE.search(text):
        hits.append(f"{where}: 命中银行卡号形态的取值")
    return hits


def scan_db_file(db_path: Path | str) -> list[str]:
    """扫描**库文件**：SQLite 模式（对象名 / 列名）+ 全文件字节（16~19 位连续数字即异常）。"""
    path = Path(db_path)
    if not path.is_file():
        return [f"库文件不存在：{path}"]

    hits: list[str] = []
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    finally:
        connection.close()

    for obj_type, name, sql in rows:
        if FORBIDDEN_FIELD_RE.search(name or ""):
            hits.append(f"{path.name}: 模式对象名禁忌 `{name}`（{obj_type}）")
        for identifier in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql or ""):
            if FORBIDDEN_FIELD_RE.search(identifier):
                hits.append(f"{path.name}: DDL（{name}）含禁忌标识符 `{identifier}`")

    # 库文件是二进制，按 latin-1 逐字节解码可无损检索 ASCII 数字串
    hits.extend(scan_text_for_sensitive(path.read_bytes().decode("latin-1"), f"{path.name} 原始字节"))
    return hits


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


def json_of(response) -> dict:
    """取响应 JSON（失败时给出可读的状态码与正文片段）。"""
    payload = response.get_json(silent=True)
    assert isinstance(payload, dict), (
        f"响应不是 JSON 对象：HTTP {response.status_code} {response.get_data(as_text=True)[:300]}"
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


def catalog(client, token: str) -> tuple[list, list]:
    """取回本摊位当前可售商品与当日价目表（契约 §3.5 / §3.3）。"""
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
