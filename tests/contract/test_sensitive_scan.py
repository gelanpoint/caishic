"""`T-007` 能力②：响应体与库文件「零命中身份证 / 银行卡」扫描（`REQ-024` / `NFR-012` / `AC-012`）。

- 契约 §1.5：响应体中一律不得出现身份证号或银行卡号字段；收款标识只以脱敏值出现。
- `data-model.md` 全局约定：字段名禁止匹配 `id_card*` / `id_no*` / `bank_card*` / `card_no*` / `bank_account*`。

本文件同时承载**灵敏度负例**（故意构造含 `id_card` 的响应体 / 故意往库里插一个禁忌列 → 必须命中）。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from sensitive_scan import (
    BANK_CARD_VALUE_RE,
    ID_CARD_VALUE_RE,
    scan_db_file,
    scan_json_for_sensitive,
    scan_text_for_sensitive,
)
from conftest import (
    CONTRACT_ENDPOINTS,
    REPO_ROOT,
    today_iso,
)

# ---------------------------------------------------------------------------
# 探针：契约 §2 的每个端点都要被真实请求一次，响应体一律过扫描
# ---------------------------------------------------------------------------

_NOT_FOUND_ID = "ZZ-NOT-A-REAL-ID"


def _body_for(method: str, path: str) -> dict | None:
    """按契约 §3 给出的必填字段构造**最小合法**请求体（不含任何契约外字段）。"""
    bodies = {
        ("POST", "/api/merchant/session"): {"stall_no": "Z-99"},
        ("POST", "/api/merchant/price-list"): {"business_date": today_iso(), "items": []},
        ("POST", "/api/merchant/transactions"): {"items": [{"product_id": 1, "weight_grams": 1000}]},
        ("POST", "/api/merchant/transactions/{transaction_no}/price-change"): {
            "item_id": 1,
            "final_unit_price_cents": 100,
        },
        ("POST", "/api/merchant/transactions/{transaction_no}/payment"): {
            "method": "cash",
            "operator": "probe",
        },
        ("POST", "/api/merchant/transactions/{transaction_no}/refund"): {"amount_cents": 1},
        ("POST", "/api/merchant/offline/queue"): {"items": [{"product_id": 1, "weight_grams": 1000}]},
        ("POST", "/api/merchant/offline/sync"): {},
        ("POST", "/api/mock/scale/reading"): {"weight_grams": 1000},
        ("POST", "/api/mock/payment/callback"): {
            "callback_no": "CB-PROBE-0001",
            "payment_no": "PAY-PROBE-0001",
            "result": "success",
        },
        ("POST", "/api/admin/categories"): {"code": "Z-99", "name": "probe"},
        ("POST", "/api/admin/aliases"): {"stall_no": "A-01", "alias_name": "probe", "category_id": 1},
        ("PUT", "/api/admin/commission-rules"): {
            "pay_object": "merchant",
            "rate_bp": 100,
            "effective_from": today_iso(),
        },
        ("POST", "/api/admin/daily-aggregate"): {"business_date": today_iso()},
        ("POST", "/api/admin/settlements"): {
            "stall_no": "A-01",
            "period_start": today_iso(),
            "period_end": today_iso(),
        },
    }
    return bodies.get((method, path))


def _query_for(path: str) -> dict:
    """契约 §3 里必填的查询参数。"""
    if path in {"/api/merchant/price-list", "/api/admin/metrics/usage", "/api/admin/reconciliation"}:
        return {"business_date": today_iso()}
    return {}


def _probe(client, method: str, path: str):
    filled = path.replace("{transaction_no}", _NOT_FOUND_ID).replace("{stall_no}", "Z-99")
    kwargs: dict = {
        "method": method,
        "path": filled,
        "headers": {"X-Stall-Session": "probe-invalid-session", "Idempotency-Key": "probe-key-0001"},
    }
    query = _query_for(path)
    if query:
        kwargs["query_string"] = query
    body = _body_for(method, path)
    if body is not None and method != "GET":
        kwargs["json"] = body
    return client.open(**kwargs)


def test_sensitive_scan_covers_every_contract_endpoint(contract):
    """扫描面必须覆盖契约 §2 全部端点 —— 防止"扫是扫了，漏掉一半"。"""
    assert CONTRACT_ENDPOINTS, "契约 §2 未解析到任何端点"
    assert len({p for _, p in CONTRACT_ENDPOINTS}) >= 20, "扫描面明显偏小，解析器可能失灵"


@pytest.mark.parametrize("method,path", sorted(CONTRACT_ENDPOINTS))
def test_response_body_passes_sensitive_scan(client, seeded_app, method, path):
    """逐端点探针：端点必须先**存在**（否则无从扫描），响应体必须零命中。"""
    from conftest import registered_endpoints

    assert (method, path) in registered_endpoints(seeded_app), (
        f"契约 §2 已登记 {method} {path}，但端点未实现 —— 无法扫描其响应体"
        f"（实现批次 `T-015`~`T-025` 未开工）"
    )
    response = _probe(client, method, path)
    assert response.is_json, (
        f"{method} {path} 的响应不是 JSON（契约 §1.1 要求请求与响应均为 JSON）："
        f"HTTP {response.status_code} {response.get_data(as_text=True)[:200]}"
    )
    hits = scan_json_for_sensitive(response.get_json(silent=True), where=f"{method} {path}")
    assert not hits, "响应体命中敏感字段禁令（REQ-024 / NFR-012）：\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# 灵敏度负例①：字段名——故意构造含 id_card 的响应体，必须命中
# ---------------------------------------------------------------------------

FORBIDDEN_FIELD_SAMPLES = [
    "id_card",
    "id_card_no",
    "id_no",
    "bank_card",
    "card_no",
    "bank_account",
    "ID_CARD",
    "身份证号",
    "银行卡号",
]


@pytest.mark.parametrize("field", FORBIDDEN_FIELD_SAMPLES)
def test_sensitivity_forbidden_field_name_is_detected(field):
    """负例：字段名命中禁忌命名形态时，扫描器必须报警（不是静默放过）。"""
    payload = {"transaction_no": "T-1", field: "x"}
    hits = scan_json_for_sensitive(payload, where="unit")
    assert hits, f"禁忌字段名 {field!r} 未被检出"


def test_sensitivity_nested_id_card_field_is_detected():
    """负例（深层嵌套）：藏在数组元素里的 `id_card` 也必须被检出。"""
    payload = {"items": [{"name": "上海青", "id_card": "11010519491231002X"}]}
    hits = scan_json_for_sensitive(payload, where="unit")
    assert len(hits) >= 2, f"嵌套禁忌字段/取值未被完整检出：{hits}"


# ---------------------------------------------------------------------------
# 灵敏度负例②：取值——身份证号与银行卡号形态必须命中
# ---------------------------------------------------------------------------


def test_sensitivity_id_card_value_is_detected():
    """负例：取值是身份证号形态（`ID_CARD_VALUE_RE`）时必须命中。"""
    assert ID_CARD_VALUE_RE.search("11010519491231002X")
    hits = scan_json_for_sensitive({"note": "证件 11010519491231002X"}, where="unit")
    assert hits and "身份证号" in hits[0]


def test_sensitivity_bank_card_value_is_detected():
    """负例：取值是银行卡号形态（16~19 位连续数字）时必须命中。"""
    assert BANK_CARD_VALUE_RE.search("6222021234567890")
    hits = scan_json_for_sensitive({"receiver_token": "6222021234567890"}, where="unit")
    assert hits and "银行卡号" in hits[0]


def test_sensitivity_clean_sample_is_not_flagged():
    """反向负例：契约允许的字段形态（脱敏收款标识、单号、金额）**不得**误报。"""
    clean = {
        "payment_no": "PAY-20260930-0001",
        "method": "qr",
        "status": "pending",
        "qr_payload": "mtpay://pay/PAY-20260930-0001",
        "receiver_token_masked": "6222****1234",
        "callback_no": "CB-20260930-000001",
        "transaction_no": "T-20260930-0001",
        "total_amount_cents": 123456,
        "items": [{"name": "上海青", "weight_grams": 1200, "amount_cents": 576}],
    }
    assert scan_json_for_sensitive(clean, where="unit") == []


# ---------------------------------------------------------------------------
# 灵敏度负例③：库文件（SQLite 模式 + 原始字节）
# ---------------------------------------------------------------------------


def test_sensitivity_db_scan_alarms_on_injected_column(tmp_path):
    """负例：故意在库里建一个 `id_card_no` 列 → 库文件扫描必须命中。"""
    db_file = tmp_path / "injected.sqlite3"
    connection = sqlite3.connect(str(db_file))
    connection.execute("CREATE TABLE customer (id INTEGER PRIMARY KEY, id_card_no TEXT)")
    connection.commit()
    connection.close()

    hits = scan_db_file(db_file)
    assert any("id_card_no" in hit for hit in hits), f"注入的禁忌列未被检出：{hits}"


def test_sensitivity_db_scan_alarms_on_injected_value(tmp_path):
    """负例：故意把银行卡号形态的取值写进库 → 原始字节扫描必须命中。"""
    db_file = tmp_path / "injected_value.sqlite3"
    connection = sqlite3.connect(str(db_file))
    connection.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, note TEXT)")
    connection.execute("INSERT INTO account (note) VALUES ('6222021234567890')")
    connection.commit()
    connection.close()

    hits = scan_db_file(db_file)
    assert any("银行卡号" in hit for hit in hits), f"注入的银行卡号取值未被检出：{hits}"


def test_test_database_file_is_clean():
    """真实测试库文件（建库 + 种子导入后）必须零命中。"""
    from app import config

    hits = scan_db_file(config.DB_PATH)
    assert not hits, "测试库文件命中敏感字段禁令（REQ-024 / NFR-012）：\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# 库文件之外的静态面：迁移 DDL 与种子数据
# ---------------------------------------------------------------------------


def test_migration_ddl_and_seed_data_are_clean():
    """迁移 DDL 与种子数据（含别名 / 商户电话等文本）必须零命中禁忌标识符与取值形态。"""
    hits: list[str] = []
    for sql_file in sorted((REPO_ROOT / "app" / "migrations").glob("*.sql")):
        hits.extend(scan_text_for_sensitive(sql_file.read_text(encoding="utf-8"), sql_file.name))

    seed_file = REPO_ROOT / "app" / "seed_data" / "seed.json"
    seed_text = seed_file.read_text(encoding="utf-8")
    hits.extend(scan_text_for_sensitive(seed_text, seed_file.name))
    seed_keys = set()

    def collect_keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                seed_keys.add(key)
                collect_keys(value)
        elif isinstance(node, list):
            for value in node:
                collect_keys(value)

    collect_keys(json.loads(seed_text))
    for key in sorted(seed_keys):
        hits.extend(scan_text_for_sensitive(key, f"seed.json 键名 {key}"))

    assert not hits, "静态面命中敏感字段禁令：\n" + "\n".join(hits)


def test_sensitivity_text_scan_alarms_on_injected_source(tmp_path):
    """负例：故意写一段含 `bank_card_no` 的源码文本 → 文本扫描必须命中。"""
    source = tmp_path / "bad.py"
    source.write_text("bank_card_no = '6222021234567890'\n", encoding="utf-8")
    hits = scan_text_for_sensitive(source.read_text(encoding="utf-8"), source.name)
    assert hits, "含 bank_card_no 的源码文本未被检出"


def test_no_forbidden_identifier_in_app_sources():
    """`app/**` 与 `run.py` 的源码标识符必须零命中（`NFR-012` 的静态面）。"""
    hits: list[str] = []
    targets = sorted((REPO_ROOT / "app").rglob("*.py")) + [REPO_ROOT / "run.py"]
    for path in targets:
        hits.extend(scan_text_for_sensitive(path.read_text(encoding="utf-8"), path.name))
    assert not hits, "源码命中敏感字段禁令：\n" + "\n".join(hits)
