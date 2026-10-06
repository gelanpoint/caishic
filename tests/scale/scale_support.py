"""秤端接入契约测试的**助手**（`T-SCALE-03`；唯一接口依据 `contracts/scale-midplatform.md`）。

有：契约面的机械视图（`SCALE_CONTRACT_ENDPOINTS` / `SCALE_CONTRACT_ERROR_CODES`，**从
`tests/contract/contract_support.py` 复用**，不在这里重抄 —— 同一个规则写两遍就是下次漂移的种子）、
统一错误响应断言（`MT-2xxx` 并入错误码表后复用同一套 envelope 规则）、窄响应体白名单与
「不得出现的字段」扫描器（各带灵敏度负例）、7 个端点的调用助手、直连库的核对助手。

**没有**：① 任何 `@pytest.fixture`（共用夹具在 `tests/scale/conftest.py`，是契约层 `contract_support`
的**原件**，形态 2 的每份 JSON 响应因此也自动过 `SensitiveScanClient` 的敏感字段扫描 —— 契约 §1.5
明文要求本契约响应体同受 `AC-012` 覆盖）；② 造设备/摊位的场景助手（在 `scale_provision.py`，
依赖方向单向：`scale_provision` → 本文件）。

纪律：不发明契约之外的端点/字段/错误码（`RL-1`）；每条检查都配灵敏度负例（`AGENTS.md`
「不验证灵敏度的验证是摆设」）；只**声明**实现必须提供的口子，绝不写实现代码。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_TESTS_DIR = REPO_ROOT / "tests" / "contract"
if str(CONTRACT_TESTS_DIR) not in sys.path:
    # `contract_support` 依赖同目录的 `sensitive_scan`（裸名导入），故必须先把它的目录放进 `sys.path`，
    # 否则"只跑 tests/scale"时导入失败。这样 `pytest tests/scale` 与全量 `pytest -q` 行为一致。
    sys.path.insert(0, str(CONTRACT_TESTS_DIR))

from contract_support import (  # noqa: E402  (上面的 sys.path 垫片必须先行)
    CONTRACT_ERROR_CODES,
    SCALE_CONTRACT_ENDPOINTS,
    SCALE_CONTRACT_ERROR_CODES,
    SEED_STALL_NO,
    assert_exact_keys,
    error_envelope_violations,
    json_of,
    today_iso,
)

#: 形态 2 契约 §2 的 7 个端点 / §4 的 5 个错误码（机械抽取，不手抄）
SCALE_ENDPOINTS = SCALE_CONTRACT_ENDPOINTS
SCALE_ERROR_CODES = SCALE_CONTRACT_ERROR_CODES
#: 统一错误响应格式的唯一规则在 `rest-api.md` §1.2（`error_envelope_violations`），形态 2 只是
#: **新增错误码**（契约 §1.2 原文）—— 故这里是**并表**，不是另写一套格式校验。
MERGED_ERROR_CODES: dict[str, int] = {**CONTRACT_ERROR_CODES, **SCALE_ERROR_CODES}

BASE_PATH = "/api/scale/v1"
DEVICE_TOKEN_HEADER = "X-Device-Token"
PROTO_HEADER = "X-Scale-Proto"
SUPPORTED_PROTO = "1"

# ---------------------------------------------------------------------------
# 请求头 / 统一错误响应
# ---------------------------------------------------------------------------


def scale_headers(
    token: str | None = None, *, idempotency_key: str | None = None, proto: str | None = SUPPORTED_PROTO
) -> dict[str, str]:
    """秤端请求头（契约 §1.3 / §1.6 / §1.9）。`token=None` 用于构造 `MT-2001` 的"缺令牌"态。"""
    headers: dict[str, str] = {}
    if token is not None:
        headers[DEVICE_TOKEN_HEADER] = token
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    if proto is not None:
        headers[PROTO_HEADER] = proto
    return headers


def assert_scale_error_response(response, expected_code: str | None = None) -> dict:
    """断言响应是契约 §1.2 的统一错误响应，并（可选）断言具体错误码与 §4 的 HTTP 状态。

    与契约层 `assert_error_response` 的唯一差别：**错误码表并入 `MT-2xxx`**（否则形态 2 的码
    会被判成"不在契约 §4 错误码表内"，那是把两个契约段混为一谈）。
    """
    payload = response.get_json(silent=True)
    violations = error_envelope_violations(
        payload, http_status=response.status_code, codes=MERGED_ERROR_CODES
    )
    if expected_code is not None:
        actual = (payload or {}).get("error", {}).get("code") if isinstance(payload, dict) else None
        if actual != expected_code:
            violations.append(f"期望错误码 {expected_code}，实际为 {actual!r}")
    if violations:
        raise AssertionError(
            "统一错误响应格式不合规（契约 §1.2 / §4）：\n"
            + "\n".join(f"    - {v}" for v in violations)
            + f"\n    实际响应：HTTP {response.status_code} {response.get_data(as_text=True)[:400]}"
        )
    return payload


# ---------------------------------------------------------------------------
# 窄响应体：字段白名单 + 「不得出现的字段」
# ---------------------------------------------------------------------------

#: 窄响应体**不允许**出现的字段名片段（**逐字取自契约 §1.1 / §3.3 的"不含成本、不含佣金相关字段"**；
#: `REQ-036`、`NFR-015`）。只放契约点名的四类，不凭感觉加词 —— 多一个词就会误伤合规响应。
#: ⚠️ 本扫描器**只适用于 §3.3 字典 / §3.4 价目表**那类窄体响应；§3.7 退货响应**契约明文要求**
#: `commission_delta_cents`，对它只能用字段白名单（`assert_exact_keys`），用本扫描器必假红。
FORBIDDEN_NARROW_KEY_PARTS = ("cost", "commission", "margin", "profit", "purchase")

#: 各端点响应的**顶层**字段白名单（逐字取自契约 §3 的响应示例；多一个字段即失败）
ACTIVATE_KEYS = {"market", "stall", "business_date", "server_time", "config"}
ACTIVATE_CONFIG_KEYS = {
    "offline_warn_threshold",
    "customer_base_url",
    "catalog_version",
    "price_list_business_date",
    "proto",
}
HEARTBEAT_KEYS = {"business_date", "server_time", "config_changed", "catalog_version"}
CATALOG_KEYS = {"catalog_version", "products", "categories"}
CATALOG_PRODUCT_KEYS = {"product_id", "name", "category_id", "status"}
CATALOG_CATEGORY_KEYS = {"category_id", "name"}
PRICE_LIST_KEYS = {"business_date", "items", "price_list_version"}
PRICE_LIST_ITEM_KEYS = {"product_id", "unit_price_cents"}
INGEST_KEYS = {
    "transaction_no",
    "business_date",
    "status",
    "authoritative_amount_cents",
    "reported_amount_cents",
    "amount_mismatch",
    "mismatch_detail",
    "replayed",
    "receipt_url",
}
SETTLE_QR_KEYS = {"transaction_no", "status", "qr_payload"}
SETTLE_CASH_KEYS = {"transaction_no", "status", "paid_at"}
REFUND_KEYS = {"transaction_no", "status", "refund_amount_cents", "commission_delta_cents"}


def forbidden_narrow_keys(payload, forbidden: tuple[str, ...] = FORBIDDEN_NARROW_KEY_PARTS) -> list[str]:
    """递归找出窄响应体里**不允许出现**的字段名，返回带路径的命中清单（空 = 合规）。"""
    hits: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if any(part in str(key).lower() for part in forbidden):
                    hits.append(f"{path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(payload, "$")
    return hits


def assert_narrow_body(payload: dict, allowed: set[str], where: str) -> None:
    """窄响应体断言：顶层字段**恰好**是白名单，且不含成本/佣金类字段（多一个即失败）。"""
    assert_exact_keys(payload, allowed, where)
    hits = forbidden_narrow_keys(payload)
    assert not hits, f"{where} 出现窄响应体禁止的字段（契约 §1.1 / §3.3）：{hits}"


# ---------------------------------------------------------------------------
# 数据助手（用库里的事实算期望值，而不是把实现回显一遍）
# ---------------------------------------------------------------------------


def sample_product_id(conn, *, stall_no: str = SEED_STALL_NO) -> int:
    """取该摊位一个在售商品号（**不要求**它在该营业日有价目表 —— 构造 `MT-1006` 态用）。"""
    row = conn.execute(
        "SELECT p.id AS product_id FROM product p JOIN stall s ON s.id = p.stall_id "
        "WHERE s.stall_no = ? AND p.status = 'active' ORDER BY p.id LIMIT 1",
        (stall_no,),
    ).fetchone()
    assert row is not None, f"种子数据缺少摊位 {stall_no} 的在售商品"
    return int(row["product_id"])


def sample_line(conn, *, business_date: str | None = None, stall_no: str = SEED_STALL_NO) -> dict:
    """取该摊位在该营业日**有价目表**的第一个商品 → `{"product_id", "unit_price_cents"}`。"""
    bd = business_date or today_iso()
    row = conn.execute(
        "SELECT p.id AS product_id, pi.unit_price_cents AS unit_price_cents "
        "FROM product p JOIN price_item pi ON pi.product_id = p.id "
        "JOIN stall s ON s.id = p.stall_id "
        "WHERE s.stall_no = ? AND pi.business_date = ? AND p.status = 'active' "
        "ORDER BY p.id LIMIT 1",
        (stall_no, bd),
    ).fetchone()
    assert row is not None, f"种子数据缺少摊位 {stall_no} 在 {bd} 的有价目表商品"
    return {"product_id": int(row["product_id"]), "unit_price_cents": int(row["unit_price_cents"])}


def authoritative_amount_cents(conn, weight_grams: int, **kwargs) -> int:
    """**独立算出**中台权威重算值（复用 `app.domain.pricing`，契约 §5.1 指定的同一实现）。"""
    from app.domain.pricing import price_amount

    return price_amount(sample_line(conn, **kwargs)["unit_price_cents"], weight_grams)


# ---------------------------------------------------------------------------
# 契约调用助手（只依赖契约 §3 定义的字段）
# ---------------------------------------------------------------------------


def activate(
    client,
    token,
    *,
    device_id: str,
    firmware_version="0.1.0",
    hardware_rev="esp32s3-n8r8",
    stall_no: str | None = None,
    proto=SUPPORTED_PROTO,
    extra=None,
):
    """§3.1 设备激活。`stall_no` 是**可选声明**（带上且与既有绑定不一致 ⇒ `MT-2005`）；
    `token=None` → 缺 `X-Device-Token`（`MT-2001` 的触发条件之一）。
    """
    body = {"device_id": device_id, "firmware_version": firmware_version, "hardware_rev": hardware_rev}
    if stall_no is not None:
        body["stall_no"] = stall_no
    if extra:
        body.update(extra)
    return client.post(f"{BASE_PATH}/devices/activate", json=body, headers=scale_headers(token, proto=proto))


def heartbeat(
    client,
    token,
    *,
    device_id: str,
    pending_count=0,
    firmware_version="0.1.0",
    proto=SUPPORTED_PROTO,
    extra=None,
):
    """§3.2 心跳。`pending_count` 由秤端自报，**中台不得据此拒绝任何交易**（`NFR-014`）。"""
    body = {"device_id": device_id, "pending_count": pending_count, "firmware_version": firmware_version}
    if extra:
        body.update(extra)
    return client.post(f"{BASE_PATH}/devices/heartbeat", json=body, headers=scale_headers(token, proto=proto))


def get_catalog(client, token, *, since_version=None, proto=SUPPORTED_PROTO):
    """§3.3 商品字典与品类（窄响应体）。"""
    query = {} if since_version is None else {"since_version": since_version}
    return client.get(f"{BASE_PATH}/catalog", query_string=query, headers=scale_headers(token, proto=proto))


def get_price_list(client, token, *, business_date=None, proto=SUPPORTED_PROTO):
    """§3.4 价目表（离线计价的唯一依据）。"""
    query = {} if business_date is None else {"business_date": business_date}
    return client.get(f"{BASE_PATH}/price-list", query_string=query, headers=scale_headers(token, proto=proto))


def price_list_version(client, token, business_date: str) -> int:
    """取当前价目表版本（上报体要带 `price_list_version`，用真值而不是编一个）。"""
    response = get_price_list(client, token, business_date=business_date)
    assert response.status_code == 200, (
        f"§3.4 期望 200，实际 {response.status_code}：{response.get_data(as_text=True)[:300]}"
    )
    return int(json_of(response)["price_list_version"])


def report_transaction(
    client,
    token,
    conn,
    *,
    idempotency_key: str,
    weight_grams: int = 780,
    business_date: str | None = None,
    delta_cents: int = 0,
    origin: str = "online",
    proto=SUPPORTED_PROTO,
    extra_fields: dict | None = None,
    product_id: int | None = None,
    price_version: int | None = None,
):
    """§3.5 交易上报；返回 `(响应, 中台权威重算值或 None)`。

    `delta_cents` 是**刻意制造**的"秤端本地计价 ≠ 中台重算"的差（`AC-030` 用它改一个分位）：
    `0` 是正常态；`+1` 即"秤端多报一分"。**同时**改顶层 `amount_cents` 与
    `local_lines[].amount_cents` —— 契约 §5.2 的逐行比对里两处都要能被看出来。

    `product_id` 显式给出时**不查价目表**（用于构造"该营业日无价目表 ⇒ `MT-1006`"的态），
    此时第二个返回值为 `None`。

    `price_version` 显式给出时**跳过取版本的那次 GET**：`token=None` 的用例（断言 `MT-2001`）
    不能先发一次带令牌的 GET —— 那会让"无令牌上报"根本走不到被拒那一步。
    """
    bd = business_date or today_iso()
    if product_id is None:
        line = sample_line(conn, business_date=bd)
        product_id = line["product_id"]
        unit_price_cents = line["unit_price_cents"]
        authoritative: int | None = authoritative_amount_cents(conn, weight_grams, business_date=bd)
    else:
        unit_price_cents = 0
        authoritative = None
    reported = (authoritative or 0) + delta_cents
    body = {
        "business_date": bd,
        "captured_at": f"{bd} 08:20:11",
        "origin": origin,
        "price_list_version": price_version if price_version is not None else price_list_version(client, token, bd),
        "items": [{"product_id": product_id, "weight_grams": weight_grams}],
        "amount_cents": reported,
        "local_lines": [
            {
                "product_id": product_id,
                "unit_price_cents": unit_price_cents,
                "weight_grams": weight_grams,
                "amount_cents": reported,
            }
        ],
        "round_off_cents": 0,
        "client_idempotency_key": idempotency_key,
    }
    if extra_fields:
        body.update(extra_fields)
    response = client.post(
        f"{BASE_PATH}/transactions",
        json=body,
        headers=scale_headers(token, idempotency_key=idempotency_key, proto=proto),
    )
    return response, authoritative


def settle(client, token, transaction_no: str, method: str, *, idempotency_key: str, extra=None):
    """§3.6 收款确认（`cash` / `qr`）。请求体严格按契约：`{"method": ...}`。"""
    body = {"method": method}
    if extra:
        body.update(extra)
    return client.post(
        f"{BASE_PATH}/transactions/{transaction_no}/settle",
        json=body,
        headers=scale_headers(token, idempotency_key=idempotency_key),
    )


def refund(client, token, transaction_no: str, amount_cents: int, *, idempotency_key: str,
           reason: str = "顾客退货"):
    """§3.7 退货**申请**（冲正与佣金扣减由中台执行）。"""
    return client.post(
        f"{BASE_PATH}/transactions/{transaction_no}/refund",
        json={"amount_cents": amount_cents, "reason": reason},
        headers=scale_headers(token, idempotency_key=idempotency_key),
    )


# ---------------------------------------------------------------------------
# 直连测试库的核对助手（契约测试要能核对"库里的状态"，而不只看响应字段）
# ---------------------------------------------------------------------------


def transaction_row(conn, transaction_no: str):
    return conn.execute('SELECT * FROM "transaction" WHERE transaction_no = ?', (transaction_no,)).fetchone()


def transaction_count(conn) -> int:
    return int(conn.execute('SELECT COUNT(*) AS n FROM "transaction"').fetchone()["n"])


def payment_rows(conn, transaction_no: str) -> list[dict]:
    rows = conn.execute(
        'SELECT p.* FROM payment p JOIN "transaction" t ON t.id = p.transaction_id '
        "WHERE t.transaction_no = ? ORDER BY p.id",
        (transaction_no,),
    ).fetchall()
    return [dict(row) for row in rows]


def refund_rows(conn, transaction_no: str) -> list[dict]:
    rows = conn.execute(
        'SELECT r.* FROM refund r JOIN "transaction" t ON t.id = r.transaction_id '
        "WHERE t.transaction_no = ? ORDER BY r.id",
        (transaction_no,),
    ).fetchall()
    return [dict(row) for row in rows]


def audit_rows(conn, event_type: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE event_type = ? ORDER BY id", (event_type,)
    ).fetchall()
    return [dict(row) for row in rows]


def sqlite_tables(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def stall_id_of(conn, stall_no: str) -> int:
    row = conn.execute("SELECT id FROM stall WHERE stall_no = ?", (stall_no,)).fetchone()
    assert row is not None, f"种子数据缺少摊位 {stall_no}"
    return int(row["id"])
