"""§3.5 交易上报（在线与补传共用）的契约测试（`T-SCALE-03`）。

覆盖：`REQ-038`、`REQ-039`、`REQ-041`；`AC-027`（前半段）、`AC-030`、`AC-031`；
错误码 `MT-2001` / `MT-2002` / `MT-2003` / `MT-2004` / `MT-1006` / `MT-1012`。

本文件的**核心是 `AC-030`**：契约 §5 要求"金额不一致"时**三件事齐全**（入账取中台重算值 /
写 `audit_log` 留痕 / 响应如实返回 `amount_mismatch` + `mismatch_detail`）。
故此处**真造出不一致状态**（`delta_cents=1`，即秤端多报一分），并逐条断言这三件事，
另配一条 `delta_cents=0` 的对照用例证明该判定**能区分**（不是"永远 true"）。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

from scale_provision import (
    DEMO_DEVICE_ID,
    EMPTY_PRICE_DATE,
    OTHER_STALL_NO,
    active_device_token,
)
from scale_support import (
    INGEST_KEYS,
    SEED_STALL_NO,
    assert_narrow_body,
    assert_scale_error_response,
    audit_rows,
    heartbeat,
    json_of,
    price_list_version,
    report_transaction,
    sample_line,
    sample_product_id,
    stall_id_of,
    today_iso,
    transaction_count,
    transaction_row,
)


def _token(client, db_conn) -> str:
    """数据面要求设备**已激活**（未激活 → `MT-2001`），故前置走一次 §3.1 激活。"""
    return active_device_token(client, db_conn)


def _key() -> str:
    """每次调用一个新幂等键（`data-model.md` §2.8 要求长度 ≤64）。"""
    return f"SC-{DEMO_DEVICE_ID}-{uuid.uuid4().hex[:12]}"


def _all_values(node) -> set:
    """递归收集 JSON 里的全部标量值（用于结构无关地核对"两个金额都留在留痕里"）。"""
    values: set = set()
    if isinstance(node, dict):
        for key, value in node.items():
            values.add(key)
            values |= _all_values(value)
    elif isinstance(node, list):
        for value in node:
            values |= _all_values(value)
    elif isinstance(node, str):
        values.add(node)
    elif isinstance(node, int) and not isinstance(node, bool):
        values.add(node)
    return values


def _line_diffs(node) -> list[dict]:
    """递归找出 `mismatch_detail` 里的**逐行差异条目**（含 `product_id` 的对象）。"""
    found: list[dict] = []
    if isinstance(node, dict):
        if "product_id" in node:
            found.append(node)
        for value in node.values():
            found += _line_diffs(value)
    elif isinstance(node, list):
        for value in node:
            found += _line_diffs(value)
    return found


# ---------------------------------------------------------------------------
# 正常入账
# ---------------------------------------------------------------------------


def test_report_creates_priced_transaction_with_authoritative_amount(client, db_conn):
    """§3.5：一致时 `amount_mismatch=false`，入账金额 = 中台重算值，交易绑定到注册摊位（`AC-027`）。"""
    response, authoritative = report_transaction(client, _token(client, db_conn), db_conn, idempotency_key=_key())
    assert response.status_code == 201, (
        f"§3.5 新建应回 201，实际 {response.status_code}：{response.get_data(as_text=True)[:300]}"
    )
    payload = json_of(response)
    assert_narrow_body(payload, INGEST_KEYS, "§3.5 上报响应")
    assert payload["status"] == "priced"
    assert payload["amount_mismatch"] is False, payload["mismatch_detail"]
    assert payload["mismatch_detail"] is None
    assert payload["replayed"] is False
    assert int(payload["authoritative_amount_cents"]) == authoritative
    assert int(payload["reported_amount_cents"]) == authoritative
    assert payload["business_date"] == today_iso()
    assert payload["transaction_no"] in payload["receipt_url"]
    assert "/customer/" in payload["receipt_url"], "凭证地址必须是中台的顾客页"
    row = transaction_row(db_conn, payload["transaction_no"])
    assert row is not None, "交易未落库"
    assert int(row["total_amount_cents"]) == authoritative
    assert int(row["stall_id"]) == stall_id_of(db_conn, SEED_STALL_NO)


def test_pending_count_does_not_block_ingest(client, db_conn):
    """§3.2 + `NFR-014`：暂存积压（`pending_count` 很大）**不得**成为拒绝交易的理由。"""
    token = _token(client, db_conn)
    assert heartbeat(client, token, device_id=DEMO_DEVICE_ID, pending_count=999_999).status_code == 200
    response, _ = report_transaction(client, token, db_conn, idempotency_key=_key())
    assert response.status_code in (200, 201), (
        f"暂存积压只告警、仍须继续接受交易（NFR-014）：{response.status_code} "
        f"{response.get_data(as_text=True)[:300]}"
    )


def test_backfilled_transaction_keeps_the_staged_business_date(client, db_conn):
    """§3.5 + `REQ-041`：补传按**暂存时**的营业日入账，不是按到达日的墙钟。

    `origin=offline_backfill` 落库为 `data-model.md` §2.8 的 `backfilled`。
    """
    staged = (date.today() - timedelta(days=1)).isoformat()
    response, authoritative = report_transaction(
        client, _token(client, db_conn), db_conn, idempotency_key=_key(),
        business_date=staged, origin="offline_backfill",
    )
    assert response.status_code in (200, 201), response.get_data(as_text=True)[:300]
    payload = json_of(response)
    assert payload["business_date"] == staged, "补传不得被记成到达日"
    row = transaction_row(db_conn, payload["transaction_no"])
    assert row["business_date"] == staged
    assert row["origin"] == "backfilled"
    assert int(row["total_amount_cents"]) == authoritative


# ---------------------------------------------------------------------------
# 幂等（§1.1 / §1.3；`REQ-039`、`AC-031`）
# ---------------------------------------------------------------------------


def test_replay_returns_first_result_and_does_not_create_a_second_transaction(client, db_conn):
    """`AC-031`：同键同体重发 ⇒ 200 + `replayed=true` + **首次结果**，交易总数不增加。"""
    token = _token(client, db_conn)
    key = _key()
    before = transaction_count(db_conn)
    first, _ = report_transaction(client, token, db_conn, idempotency_key=key)
    assert first.status_code == 201, first.get_data(as_text=True)[:300]
    second, _ = report_transaction(client, token, db_conn, idempotency_key=key)
    assert second.status_code == 200, (
        f"§3.5 幂等命中应回 200，实际 {second.status_code}：{second.get_data(as_text=True)[:300]}"
    )
    assert json_of(second)["replayed"] is True
    assert json_of(second)["transaction_no"] == json_of(first)["transaction_no"]
    assert transaction_count(db_conn) == before + 1, "幂等命中不得新建交易"


def test_replay_after_a_mismatch_returns_the_first_conclusion_not_a_recheck(client, db_conn):
    """§3.5 末条：幂等命中返回**首次的比对结论**（不是重新比对），否则同一笔前后两个说法。"""
    token = _token(client, db_conn)
    key = _key()
    first, authoritative = report_transaction(client, token, db_conn, idempotency_key=key, delta_cents=1)
    assert first.status_code == 201, first.get_data(as_text=True)[:300]
    assert json_of(first)["amount_mismatch"] is True
    second, _ = report_transaction(client, token, db_conn, idempotency_key=key, delta_cents=1)
    assert second.status_code == 200, second.get_data(as_text=True)[:300]
    payload = json_of(second)
    assert payload["replayed"] is True
    assert payload["amount_mismatch"] is True, "命中必须回首次结论"
    assert int(payload["authoritative_amount_cents"]) == authoritative


def test_same_key_with_a_different_body_returns_mt1012(client, db_conn):
    """`AC-031` 后半段 / §4 `MT-1012`：同键**不同体** ⇒ 409 明确拒绝，且不新建交易。"""
    token = _token(client, db_conn)
    key = _key()
    first, _ = report_transaction(client, token, db_conn, idempotency_key=key, weight_grams=780)
    assert first.status_code == 201, first.get_data(as_text=True)[:300]
    before = transaction_count(db_conn)
    second, _ = report_transaction(client, token, db_conn, idempotency_key=key, weight_grams=781)
    assert second.status_code == 409, second.get_data(as_text=True)[:300]
    assert_scale_error_response(second, "MT-1012")
    assert transaction_count(db_conn) == before, "同键不同体不得新建交易"


# ---------------------------------------------------------------------------
# 越权与非法载荷
# ---------------------------------------------------------------------------


def test_report_without_token_returns_mt2001_and_creates_no_transaction(client, db_conn):
    """`AC-027` 前半段 / §4 `MT-2001`：未注册设备上报 ⇒ 拒绝且**不产生任何交易行**。"""
    before = transaction_count(db_conn)
    # 请求体里的 price_list_version 用**有效令牌**取真值（只为让载荷合规）：
    # 若这里也用 token=None 去 GET /price-list，那次 GET 会先被 401 拒掉，
    # 用例就永远走不到"上报本身被 401 拒"这一步（本缺陷由 mid 评审指出）。
    version = price_list_version(client, _token(client, db_conn), today_iso())
    response, _ = report_transaction(
        client, None, db_conn, idempotency_key=_key(), price_version=version
    )
    assert response.status_code == 401, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2001")
    assert transaction_count(db_conn) == before, "被拒的上报不得留下交易行"


def test_unauthorized_stall_id_in_body_returns_mt2002_and_creates_no_transaction(client, db_conn):
    """§1.6 / §4 `MT-2002`：请求体里的 `stall_id` 与绑定不一致 ⇒ 403，**不采信、不入账**（`REQ-036`）。

    "不采信"的判据不是"响应里没有它"，而是**没有产生任何交易行**、且没有按请求里的摊位入账。
    """
    other_stall_id = stall_id_of(db_conn, OTHER_STALL_NO)
    before = transaction_count(db_conn)
    response, _ = report_transaction(
        client, _token(client, db_conn), db_conn, idempotency_key=_key(),
        extra_fields={"stall_id": other_stall_id},
    )
    assert response.status_code == 403, (
        f"§4 MT-2002 应为 403，实际 {response.status_code}：{response.get_data(as_text=True)[:300]}"
    )
    assert_scale_error_response(response, "MT-2002")
    assert transaction_count(db_conn) == before, "越权请求不得留下交易行"
    leaked = db_conn.execute(
        'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?', (other_stall_id,)
    ).fetchone()
    assert int(leaked["n"]) == 0, "不得按请求体里的 stall_id 入账"


def test_invalid_payload_returns_mt2004_and_creates_no_transaction(client, db_conn):
    """§4 `MT-2004`：`weight_grams` 越界（>50 公斤）⇒ 422，且**该笔不得被标记为成功**（`REQ-027`）。"""
    before = transaction_count(db_conn)
    response, _ = report_transaction(
        client, _token(client, db_conn), db_conn, idempotency_key=_key(), weight_grams=50_001
    )
    assert response.status_code == 422, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2004")
    assert transaction_count(db_conn) == before, "非法载荷不得落库"


def test_report_with_unsupported_proto_returns_mt2003(client, db_conn):
    """§4 `MT-2003`：协议版本不兼容 ⇒ 409，**不得按最新版猜测解析**（猜错就是静默错算）。"""
    response, _ = report_transaction(
        client, _token(client, db_conn), db_conn, idempotency_key=_key(), proto="2"
    )
    assert response.status_code == 409, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2003")


def test_report_without_a_price_list_returns_mt1006_and_never_records_zero(client, db_conn):
    """§3.4/§3.5 + 主契约 `MT-1006`：该营业日无价目表 ⇒ 409，**绝不静默按 0 元成交**。

    真造"无价目表"态：用 `EMPTY_PRICE_DATE`（库里没有任何价目表行），上报金额填 0 ——
    中台若照单入账，就是"缺价时静默按 0 元成交"，那正是契约明文禁止的。
    """
    before = transaction_count(db_conn)
    response, _ = report_transaction(
        client, _token(client, db_conn), db_conn, idempotency_key=_key(),
        business_date=EMPTY_PRICE_DATE, product_id=sample_product_id(db_conn),
    )
    assert response.status_code == 409, (
        f"无价目表应回 409 MT-1006，实际 {response.status_code}：{response.get_data(as_text=True)[:300]}"
    )
    assert_scale_error_response(response, "MT-1006")
    assert transaction_count(db_conn) == before, "缺价不得以 0 元成交"


# ---------------------------------------------------------------------------
# AC-030：金额不一致 ⇒ 三件事齐全（本文件的核心）
# ---------------------------------------------------------------------------


def test_amount_mismatch_takes_all_three_actions(client, db_conn):
    """`AC-030` / 契约 §5.4：真造"秤端上报 ≠ 中台重算"（多报一分），断言**三件事一件不少**。

    (a) 入账金额 = **中台重算值**（库里的 `total_amount_cents` 也要对得上，不只看响应字段）；
    (b) 写了 `audit_log` 留痕（`event_type = scale_amount_mismatch`），且**原始上报值留在留痕里**；
    (c) 响应如实返回 `amount_mismatch = true` 与逐行 `mismatch_detail`。

    构造方式：`delta_cents=1` —— 顶层 `amount_cents` 与 `local_lines[].amount_cents`
    **同时**比权威值多一分（契约 §5.2 的逐行比对里两处都要能被看出来）。
    """
    token = _token(client, db_conn)
    key = _key()
    line = sample_line(db_conn)
    response, authoritative = report_transaction(
        client, token, db_conn, idempotency_key=key, delta_cents=1
    )
    assert response.status_code in (200, 201), (
        f"不一致**不是错误响应**：该笔交易必须成立（RL-9），实际 {response.status_code} "
        f"{response.get_data(as_text=True)[:300]}"
    )
    payload = json_of(response)
    assert_narrow_body(payload, INGEST_KEYS, "§3.5 上报响应")
    transaction_no = payload["transaction_no"]
    reported = authoritative + 1

    # (a) 入账金额取中台重算值（响应 + 库里两处都要对）
    assert int(payload["authoritative_amount_cents"]) == authoritative
    assert int(payload["reported_amount_cents"]) == reported, "上报原值必须如实回显"
    row = transaction_row(db_conn, transaction_no)
    assert int(row["total_amount_cents"]) == authoritative, (
        f"入账金额必须是中台重算值 {authoritative}，实际 {row['total_amount_cents']}（静默采信了秤端）"
    )

    # (b) audit_log 留痕，且含设备号 / 幂等键 / 两个金额（原始上报值不得被改写）
    records = [
        record for record in audit_rows(db_conn, "scale_amount_mismatch")
        if key in record["payload_json"]
    ]
    assert len(records) == 1, f"应恰好留一条 scale_amount_mismatch 留痕，实际 {len(records)} 条"
    recorded = _all_values(json.loads(records[0]["payload_json"]))
    assert DEMO_DEVICE_ID in recorded, "留痕必须含设备号"
    assert key in recorded, "留痕必须含幂等键"
    assert authoritative in recorded, "留痕必须含中台重算值"
    assert reported in recorded, "留痕必须含**秤端原始上报值**（§5.5：不得静默改写）"

    # (c) 响应如实返回不一致与逐行差异
    assert payload["amount_mismatch"] is True
    detail = payload["mismatch_detail"]
    assert detail is not None, "不一致时必须给出 mismatch_detail"
    diffs = _line_diffs(detail)
    assert diffs, f"mismatch_detail 必须逐行给出差异（至少 product_id 与两个金额）：{detail!r}"
    matched = [diff for diff in diffs if int(diff["product_id"]) == line["product_id"]]
    assert matched, f"差异行必须指向上报的那个商品 {line['product_id']}：{diffs!r}"
    values = _all_values(matched[0])
    assert authoritative in values and reported in values, (
        f"差异行必须同时给出中台重算值与秤端上报值，实际 {matched[0]!r}"
    )


def test_matched_amount_stays_false_and_writes_no_mismatch_audit(client, db_conn):
    """`AC-030` 的**对照臂（灵敏度）**：金额一致时不得报不一致、不得留 mismatch 痕迹。

    没有这一条，上面的用例无法证明该判定能区分 —— "永远返回 true" 的实现在只跑负例时会全绿。
    """
    key = _key()
    response, authoritative = report_transaction(
        client, _token(client, db_conn), db_conn, idempotency_key=key, delta_cents=0
    )
    assert response.status_code in (200, 201), response.get_data(as_text=True)[:300]
    payload = json_of(response)
    assert payload["amount_mismatch"] is False, f"一致时不得报不一致：{payload['mismatch_detail']!r}"
    assert payload["mismatch_detail"] is None
    assert int(transaction_row(db_conn, payload["transaction_no"])["total_amount_cents"]) == authoritative
    leftovers = [
        record for record in audit_rows(db_conn, "scale_amount_mismatch")
        if key in record["payload_json"]
    ]
    assert not leftovers, "一致时不得写 scale_amount_mismatch 留痕"
