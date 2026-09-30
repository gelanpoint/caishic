"""`T-014` 契约测试：顾客扫码页（§3.18 摊位信息与信用指标 / §3.19 扫码页数据）—— **字段白名单**。

关联：`REQ-008`、`REQ-023`；`AC-011`（另按 `REQ-024` / `NFR-012` / `AC-012` 断言不返回顾客身份与支付账号）。

===========================================================================
为什么这里是「白名单」而不是「随便列几个字段」—— 设计意图，后来者请勿放宽
===========================================================================
`REQ-023` 要求顾客扫码页**只展示有采集来源的字段**。这不是洁癖，是一条真实教训：
《报告》结论 16、17 记录了绵阳等地「扫码看信息」页面的实况 —— 页面上的
「抽检记录 / 溯源信息」等字段是**空的**（系统根本没有采集流程，字段就是个空壳），
群众的原话是「**扫过好几次空码、无效信息之后，再也不信这个标签了**」。
即：**一个空壳字段会连带毁掉整页真实字段的可信度** —— 顾客无法分辨哪个是空的、哪个是真的，
于是整张标签一起作废。故本项目 `discovery.md` D-08 定下硬约束：
**只展示有真实来源的字段；没有采集流程的字段宁可不做。**

落地到测试就是两条机械判据（本文件的两根支柱）：
① **白名单逐一相等** —— 多一个字段即失败。多出来的那个字段必然没有采集来源，
   正是绵阳那个「溯源信息」空壳的复现路径；
② **每个字段必须有真实取值** —— `None` / 空串 / 空数组一律失败。
   空壳字段哪怕「有名字」，也同样违反 D-08。

白名单**不手抄**：由契约 §3.18 / §3.19 的 `响应(200)` 原文机械抽取
（`_contract_response_fields`）—— 手抄的白名单会在契约修订后悄悄漂移，
而漂移的白名单正是「字段无来源」的入口。

===========================================================================
契约歧义与范围（如实标注）
===========================================================================
- §3.18 的 `可能错误` 只列 `MT-1009`(404)、§3.19 只列 `MT-1009`(404)；
  本文件**不发明**任何契约外错误码（「交易未收款时能否扫码查看」契约未定义，故不断言）。
- 顾客扫码页本期无认证（契约头部 + `NFR-007` 放宽项），故请求不带会话头。
- 时间字段按契约 §1.4「ISO-8601 `YYYY-MM-DD HH:MM:SS`」断言；同时接受 `T` 分隔的
  等价 ISO 写法（两种都解析得通，不据此制造假红）。
"""

from __future__ import annotations

import json
import re

import pytest

from conftest import (
    REPO_ROOT,
    assert_endpoint_implemented,
    assert_error_response,
    assert_exact_keys,
    bind_stall_session,
    create_priced_transaction,
    json_of,
    load_contract_text,
    scan_json_for_sensitive,
    session_headers,
)

STALL = "A-08"  # 本组专用摊位，降低跨组状态干扰（T-008 用 A-01/A-02、T-011 用 A-05）
PROFILE_PATH = "/api/customer/stalls/{stall_no}/profile"
RECEIPT_PATH = "/api/customer/receipts/{transaction_no}"

#: 交易状态机枚举（`data-model.md` §4.1）—— 扫码页 `status` 只允许取这些值
TRANSACTION_STATUSES = {"priced", "payment_failed", "paid", "refunded"}

_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$")

# ---------------------------------------------------------------------------
# 白名单的机械来源：契约 §3.18 / §3.19 的 `响应(200)` 原文
# ---------------------------------------------------------------------------


def _contract_section(heading: str, next_heading: str) -> str:
    text = load_contract_text()
    start = text.index(heading)
    return text[start : text.index(next_heading, start + len(heading))]


def _contract_response_fields(heading: str, next_heading: str) -> dict:
    """抽取该端点 `响应(200):` 后紧跟的 JSON 样例 → 解析成对象（不手抄字段名）。"""
    section = _contract_section(heading, next_heading)
    marker = "响应(200):"
    assert marker in section, f"{heading} 小节里没有 `{marker}` —— 契约改版了？本文件需同步复核"
    brace = section.index("{", section.index(marker))
    payload, _ = json.JSONDecoder().raw_decode(section[brace:])
    assert isinstance(payload, dict), f"{heading} 的响应样例不是 JSON 对象：{payload!r}"
    return payload


#: §3.18 摊位信息白名单（契约原文先列，再机械校验，见 test_contract_examples_drive_the_whitelist）
PROFILE_FIELDS = frozenset({"stall_no", "stall_name", "in_business", "price_consistency_bp", "computed_at"})
#: §3.19 扫码页白名单与其明细元素白名单
RECEIPT_FIELDS = frozenset({"transaction_no", "status", "total_amount_cents", "items", "paid_at"})
RECEIPT_ITEM_FIELDS = frozenset({"name", "weight_grams", "amount_cents"})

#: `AC-011`「每个字段都能指出其采集来源」的**取证表**：字段 → 采集来源（数据模型实体/列或派生口径）。
#: 新增任何展示字段，必须同时在这里写下来源；写不出来就说明它不该出现在页面上（D-08）。
FIELD_SOURCES: dict[str, str] = {
    # §3.18 摊位信息（来源均为运营端建档 + 系统自算）
    "stall_no": "stall.stall_no（运营端摊位档案建档时采集）",
    "stall_name": "stall.name（同上）",
    "in_business": "stall.status 派生（active → true；运营端可停用）",
    "price_consistency_bp": "由 transaction_item 的标价与改价记录派生（REQ-008：改价计入、抹零不计入）",
    "computed_at": "派生指标计算时刻（系统自身产生）",
    # §3.19 扫码页
    "transaction_no": "transaction.transaction_no（计价时生成）",
    "status": "transaction.status（状态机 §4.1）",
    "total_amount_cents": "transaction_item 金额合计（计价端点按单价的整数分×克数计算）",
    "items": "transaction_item 明细行",
    "paid_at": "payment.confirmed_at / 回调到账时刻（仅付款成功后存在）",
    # §3.19 items[] 元素
    "name": "product.name（经 standard_category / 别名归集后的标准名）",
    "weight_grams": "transaction_item.weight_grams（秤端称重采集）",
    "amount_cents": "transaction_item.amount_cents（计价端点计算）",
}


def _empty_shell_violations(payload: dict, where: str) -> list[str]:
    """空壳字段判据（`D-08` 第二根支柱）：字段存在但取值为空。

    `False` / `0` 是**合法真值**，不算空壳；`None` / `""` / `[]` / `{}` 才是空壳。
    """
    shells: list[str] = []
    for key, value in payload.items():
        if value is None or (isinstance(value, (str, list, dict, tuple)) and len(value) == 0):
            shells.append(f"{where}.{key} 为空值（{value!r}）—— 无真实内容的空壳字段违反 REQ-023/D-08")
    return shells


def assert_no_empty_shell_fields(payload: dict, where: str) -> None:
    shells = _empty_shell_violations(payload, where)
    assert not shells, "\n".join(shells)


# ---------------------------------------------------------------------------
# 契约解析自身的核验（**当前应为绿**：这些检查的是契约与我们自己的白名单，不依赖实现）
# ---------------------------------------------------------------------------


def test_contract_examples_drive_the_whitelist():
    """白名单必须与契约 §3.18/§3.19 的 `响应(200)` 样例**逐一相等**（防手抄漂移）。"""
    profile = _contract_response_fields("### 3.18 ", "### 3.19 ")
    receipt = _contract_response_fields("### 3.19 ", "### 3.20 ")
    assert set(profile) == PROFILE_FIELDS, f"§3.18 契约字段与本文件白名单不一致：{sorted(set(profile) ^ PROFILE_FIELDS)}"
    assert set(receipt) == RECEIPT_FIELDS, f"§3.19 契约字段与本文件白名单不一致：{sorted(set(receipt) ^ RECEIPT_FIELDS)}"
    assert set(receipt["items"][0]) == RECEIPT_ITEM_FIELDS, "§3.19 items[] 元素白名单与契约不一致"


def test_every_whitelisted_field_declares_its_collection_source():
    """`AC-011` 的机械化：白名单里的**每一个**字段都要在 `FIELD_SOURCES` 里指出采集来源。

    这条同时是「不得新增无来源字段」的守门人 —— 往白名单里加字段而不写来源，此处即红。
    """
    declared = PROFILE_FIELDS | RECEIPT_FIELDS | RECEIPT_ITEM_FIELDS
    missing = sorted(declared - set(FIELD_SOURCES))
    assert not missing, f"以下展示字段没有采集来源（REQ-023 / AC-011 / D-08）：{missing}"
    assert set(FIELD_SOURCES) == declared, (
        f"`FIELD_SOURCES` 有多余条目（字段已不在白名单，来源表应同步删除）：{sorted(set(FIELD_SOURCES) - declared)}"
    )


def test_whitelist_checker_flags_the_empty_shell_field():
    """灵敏度负例：绵阳那个空壳字段（`traceability_records` 抽检记录/溯源信息）必须被判红。

    `REQUIRED`（D-08 的真实教训）：无来源字段即使"有名字"也必须失败 —— 这是本文件白名单的**存在理由**。
    """
    payload = {"stall_no": "A-01", "stall_name": "A-01 摊位", "traceability_records": []}
    with pytest.raises(AssertionError) as excinfo:
        assert_exact_keys(payload, PROFILE_FIELDS, "§3.18 响应")
    assert "traceability_records" in str(excinfo.value), "契约外字段未被指名"

    assert _empty_shell_violations({"stall_name": ""}, "unit"), "空串字段未被判为空壳"
    assert _empty_shell_violations({"items": []}, "unit"), "空数组未被判为空壳"
    assert _empty_shell_violations({"stall_no": None}, "unit"), "None 未被判为空壳"
    # 反向：合法真值不得误报（False / 0 是真实取值，不是空壳）
    assert _empty_shell_violations({"in_business": False, "total_amount_cents": 0}, "unit") == []


def test_customer_contract_paths_are_registered_in_contract():
    """这两个端点是本组唯一的接口依据，必须真的在契约 §2 里（防"测试跟错了文件"）。"""
    text = load_contract_text()
    assert (REPO_ROOT / "specs" / "market-trade-flow" / "contracts" / "rest-api.md").is_file()
    for path in (PROFILE_PATH, RECEIPT_PATH):
        assert f"`{path}`" in text, f"契约 §2 未登记 {path}"


# ---------------------------------------------------------------------------
# §3.18 摊位信息（`REQ-008`、`REQ-023`、`AC-011`）—— 当前应为红
# ---------------------------------------------------------------------------


def _get_profile(client, stall_no: str = STALL):
    return client.get(PROFILE_PATH.replace("{stall_no}", stall_no))


def test_profile_returns_exact_field_whitelist(client):
    """`AC-011`：字段**逐一相等**，多一个即失败；且每个字段都必须有真实取值（无空壳）。"""
    response = _get_profile(client)
    assert response.status_code == 200, (
        f"契约 §3.18 期望 200，实际 {response.status_code}：{response.get_data(as_text=True)[:300]}"
    )
    payload = json_of(response)
    assert_exact_keys(payload, PROFILE_FIELDS, "§3.18 响应")
    assert PROFILE_FIELDS <= set(payload), f"§3.18 响应缺字段：{sorted(PROFILE_FIELDS - set(payload))}"
    assert_no_empty_shell_fields(payload, "§3.18 响应")

    assert payload["stall_no"] == STALL
    assert isinstance(payload["stall_name"], str) and payload["stall_name"].strip()
    assert isinstance(payload["in_business"], bool), f"in_business 应为布尔，实际 {payload['in_business']!r}"
    bp = payload["price_consistency_bp"]
    assert isinstance(bp, int) and not isinstance(bp, bool) and 0 <= bp <= 10000, (
        f"price_consistency_bp 应为 0~10000 的整数（万分比），实际 {bp!r}"
    )
    assert isinstance(payload["computed_at"], str) and _DATETIME_RE.match(payload["computed_at"]), (
        f"computed_at 应为契约 §1.4 的 ISO-8601 时间，实际 {payload['computed_at']!r}"
    )


def test_profile_unknown_stall_with_mt_1009(client):
    """契约 §3.18：未知摊位号 → 404 `MT-1009`（前置防"通用 404 假绿"）。"""
    assert_endpoint_implemented(client.application, "GET", PROFILE_PATH)
    response = _get_profile(client, "Z-99")
    assert response.status_code == 404, f"契约 §3.18 期望 404，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1009")


def test_profile_exposes_no_customer_identity_and_no_payment_account(client):
    """`REQ-024` / `NFR-012` / `AC-012`：不得出现顾客身份信息、身份证号、银行卡号、完整收款账号。

    白名单已挡住"多出来的字段"，此处再做一次**显式**扫描：白名单若被误放宽，
    这条仍会独立报红（两条判据不互为备份，是各自独立的证据）。
    """
    response = _get_profile(client)
    assert response.status_code == 200, f"契约 §3.18 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    hits = scan_json_for_sensitive(payload, where="§3.18 响应")
    assert not hits, "§3.18 响应命中敏感字段禁令（REQ-024 / NFR-012 / AC-012）：\n" + "\n".join(hits)

    forbidden = {
        "customer_name",
        "customer_phone",
        "customer_id",
        "phone",
        "mobile",
        "id_card",
        "id_no",
        "bank_card",
        "card_no",
        "bank_account",
        "receiver_account",
        "pay_account",
    }
    leaked = sorted(forbidden & set(payload))
    assert not leaked, f"§3.18 响应出现顾客身份/支付账号字段（REQ-023 / REQ-024）：{leaked}"


# ---------------------------------------------------------------------------
# §3.19 扫码页数据（`REQ-023`、`AC-011`）—— 当前应为红
# ---------------------------------------------------------------------------


def _paid_transaction(client, *, tags: str) -> dict:
    """前置：本摊位一笔**已收款**交易（扫码页数据只在付款后存在）。"""
    token = bind_stall_session(client, STALL)
    created = create_priced_transaction(client, token, idempotency_key=f"t014-{tags}-txn")
    paid = client.post(
        f"/api/merchant/transactions/{created['transaction_no']}/payment",
        json={"method": "cash", "operator": "t014-cashier"},
        headers=session_headers(token, f"t014-{tags}-pay"),
    )
    assert paid.status_code == 200, (
        f"前置：契约 §3.10 期望 200，实际 {paid.status_code}：{paid.get_data(as_text=True)[:300]}"
    )
    return created


def _get_receipt(client, transaction_no: str):
    return client.get(RECEIPT_PATH.replace("{transaction_no}", transaction_no))


def test_receipt_returns_exact_field_whitelist(client):
    """`AC-011`：`{transaction_no,status,total_amount_cents,items[{name,weight_grams,amount_cents}],paid_at}`
    逐一相等，`items` 元素同样按白名单校验，且每项都必须有真实取值。"""
    created = _paid_transaction(client, tags="receipt-whitelist")
    response = _get_receipt(client, created["transaction_no"])
    assert response.status_code == 200, (
        f"契约 §3.19 期望 200，实际 {response.status_code}：{response.get_data(as_text=True)[:300]}"
    )
    payload = json_of(response)
    assert_exact_keys(payload, RECEIPT_FIELDS, "§3.19 响应")
    assert RECEIPT_FIELDS <= set(payload), f"§3.19 响应缺字段：{sorted(RECEIPT_FIELDS - set(payload))}"
    assert_no_empty_shell_fields(payload, "§3.19 响应")

    assert payload["transaction_no"] == created["transaction_no"]
    assert payload["status"] in TRANSACTION_STATUSES, (
        f"§3.19 的 status 不在交易状态机枚举内（data-model.md §4.1）：{payload['status']!r}"
    )
    assert payload["status"] == "paid", f"前置已收款，扫码页 status 应为 paid，实际 {payload['status']!r}"
    assert isinstance(payload["total_amount_cents"], int) and not isinstance(payload["total_amount_cents"], bool)
    assert isinstance(payload["paid_at"], str) and _DATETIME_RE.match(payload["paid_at"]), (
        f"paid_at 应为契约 §1.4 的 ISO-8601 时间，实际 {payload['paid_at']!r}"
    )

    items = payload["items"]
    assert isinstance(items, list) and items, "已收款交易的扫码页明细不应为空"
    for index, item in enumerate(items):
        where = f"§3.19 响应 items[{index}]"
        assert_exact_keys(item, RECEIPT_ITEM_FIELDS, where)
        assert RECEIPT_ITEM_FIELDS <= set(item), f"{where} 缺字段：{sorted(RECEIPT_ITEM_FIELDS - set(item))}"
        assert_no_empty_shell_fields(item, where)
        assert isinstance(item["name"], str) and item["name"].strip()
        assert isinstance(item["weight_grams"], int) and item["weight_grams"] > 0, (
            f"{where}.weight_grams 应为正整数（克），实际 {item['weight_grams']!r}"
        )
        assert isinstance(item["amount_cents"], int) and item["amount_cents"] >= 0, (
            f"{where}.amount_cents 应为非负整数（分），实际 {item['amount_cents']!r}"
        )

    # 金额自洽：总额必须等于明细分项之和（顾客照着页面加得出来，页面不能自相矛盾）
    assert payload["total_amount_cents"] == sum(item["amount_cents"] for item in items), (
        f"§3.19 总额 {payload['total_amount_cents']} 与明细分项之和 "
        f"{sum(item['amount_cents'] for item in items)} 不一致"
    )


def test_receipt_exposes_no_customer_identity_and_no_payment_account(client):
    """`REQ-024` / `NFR-012` / `AC-012`：不得返回顾客身份信息、身份证号、银行卡号、完整收款账号。"""
    created = _paid_transaction(client, tags="receipt-sensitive")
    response = _get_receipt(client, created["transaction_no"])
    assert response.status_code == 200, f"契约 §3.19 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    hits = scan_json_for_sensitive(payload, where="§3.19 响应")
    assert not hits, "§3.19 响应命中敏感字段禁令（REQ-024 / NFR-012 / AC-012）：\n" + "\n".join(hits)

    forbidden = {"customer_name", "customer_phone", "customer_id", "id_card", "bank_card", "receiver_account"}
    leaked = sorted(forbidden & set(payload))
    assert not leaked, f"§3.19 响应出现顾客身份/支付账号字段（REQ-023 / REQ-024）：{leaked}"
    leaked_in_items = sorted({key for item in payload.get("items", []) for key in forbidden & set(item)})
    assert not leaked_in_items, f"§3.19 明细元素出现顾客身份/支付账号字段：{leaked_in_items}"


def test_receipt_unknown_transaction_with_mt_1009(client):
    """契约 §3.19：未知交易号 → 404 `MT-1009`（前置防"通用 404 假绿"）。"""
    assert_endpoint_implemented(client.application, "GET", RECEIPT_PATH)
    response = _get_receipt(client, "ZZ-NOT-A-REAL-TXN")
    assert response.status_code == 404, f"契约 §3.19 期望 404，实际 {response.status_code}"
    assert_error_response(response, expected_code="MT-1009")
