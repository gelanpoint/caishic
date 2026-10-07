"""`REQ-053`~`REQ-057` / `AC-043`~`AC-047` 的契约一致性测试（`task-27`）。

四条最容易写松的断言一律写成**硬的**（都落到查库 / 独立复算 / 库文件字节）：

1. `AC-043`：**查库**证明 `payment_receiver_token` 含 `****`、≤32、**不含明文**，
   并**直接在库文件字节里搜明文收款码**（明文只应存在于那次请求的内存里）。
2. `AC-044` 幂等：同一标识重复触发 ⇒ **笔数与应缴都不翻倍**（断言数字）。
3. `AC-045`（取消族）：**已搬到 `test_demo_cancelled_edges.py`** —— 原版取消的是一笔从未收款的
   `priced` 交易，`after == before` 与"取消怎么计数"无关（`RL-4` 评审 P3-b 判为**不可能失败**）；
   现版先造已收款基线、再取消另一笔，并直接查库证明"不过滤时笔数与金额都不同"。
4. `AC-046`：`payable_cents` **等于我自己按佣金口径逐笔算出的钱**（费率读库、
   佣金用 `demo_console_support.half_up` 算，**不复用 `app/domain/**` 的佣金函数**）。

相关补件：`test_demo_masking.py`（脱敏边界 4/5/6/7/14）、
`test_metrics_cancelled_exclusion.py`（`status <> 'cancelled'` 五处排除的灵敏度）。

日期一律用 `contract_support.today_iso()`（服务端当日口径），**不硬编码营业日**。
助手在 `demo_console_support.py`（只做请求与取数，判据全在本文件）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from contract_support import (
    assert_error_response,
    assert_exact_keys,
    bind_stall_session,
    create_priced_transaction,
    json_of,
    today_iso,
)
from demo_console_support import (
    DEMO_HUB,
    DEMO_MERCHANTS,
    DEMO_SMS,
    MERCHANT_KEYS,
    PAYABLE_KEYS,
    REGISTER_KEYS,
    add_product,
    cancel,
    db_path,
    effective_rate_bp,
    ensure_rule,
    half_up,
    hub,
    pay_cash,
    payable_of,
    register,
    registered,
    suffix,
)
from sensitive_scan import scan_json_for_sensitive

# --- `AC-043` 注册：脱敏存储 + 明文不落库（**查库 + 库文件字节**） ---

def test_register_masks_receiver_code_and_never_stores_plaintext(client, db_conn):
    """`AC-043` ②③：查库证明脱敏（含 `****`、≤32）、明文**不在库里也不在库文件里**。"""
    payload, body = registered(client)
    plaintext, phone = body["receiver_code"], body["phone"]

    assert_exact_keys(payload, REGISTER_KEYS, "§3.34 注册响应")
    assert re.fullmatch(r"D-\d{2}", payload["stall_no"]), (
        f"摊位号应由服务端按 `D-`+两位分配：{payload['stall_no']}"
    )
    assert "****" in payload["receiver_token_masked"], payload["receiver_token_masked"]
    assert len(payload["receiver_token_masked"]) <= 32
    assert "****" in payload["phone_masked"], payload["phone_masked"]
    # 响应里**不得回显明文**（收款码与手机号都只回脱敏值）
    listed_text = client.get(DEMO_MERCHANTS).get_data(as_text=True)
    assert plaintext not in str(payload), "注册响应不得回显收款码明文"
    assert plaintext not in listed_text, "列表不得回显收款码明文"
    assert phone not in str(payload), "注册响应只回脱敏手机号（§3.34）"
    assert phone not in listed_text, "商家列表只回脱敏手机号（§3.34）"

    stored = db_conn.execute(
        "SELECT payment_receiver_token FROM stall WHERE stall_no = ?", (payload["stall_no"],)
    ).fetchone()["payment_receiver_token"]
    assert "****" in stored, f"`stall.payment_receiver_token` 必须是脱敏值，实际 {stored!r}"
    assert len(stored) <= 32, f"脱敏值长度必须 ≤32（表 CHECK），实际 {len(stored)}：{stored!r}"
    assert stored != plaintext, "库里不得是收款码明文"
    assert plaintext not in stored, "库里不得含收款码明文"

    # 最强的一条：**直接在库文件字节里搜收款码明文**（§3.34：明文只存在于那次请求的内存中）
    #
    # ⚠️ 这里**只搜收款码，不搜手机号** —— 契约 §3.34 明文规定手机号"**仅落库供短信使用**"
    #    （`merchant.phone` 的 CHECK 只允许数字与 `-`，掩码根本存不进去）。曾一度断言
    #    "库文件里不得出现手机号原文"，那是**与契约冲突**的断言，且只在 WAL 未 checkpoint 时
    #    侥幸通过（`mid` 实测 + 我复验：`journal_mode=wal`，主库字节里确实有手机号）—— 已删。
    raw = db_path(db_conn).read_bytes()
    wal = Path(str(db_path(db_conn)) + "-wal")
    if wal.is_file():
        raw += wal.read_bytes()  # 未 checkpoint 的行在 WAL 里 —— 一起搜，断言就不再看 checkpoint 时机
    assert plaintext.encode() not in raw, "主库或 WAL 字节里出现了收款码明文（REQ-024 禁止）"

def test_registered_merchant_is_listed_and_selectable_by_the_scale_page(client):
    """`AC-043` ①：注册后该商家出现在秤页可选列表，且其摊位真能建会话（不是只多了一行）。"""
    payload, _ = registered(client)
    listed = json_of(client.get(DEMO_MERCHANTS))
    assert_exact_keys(listed, {"items"}, "§3.33 商家列表")
    ids = [row["merchant_id"] for row in listed["items"]]
    assert ids == sorted(ids), "§3.33 要求按 `merchant_id` 升序（演示可复现）"
    mine = next((row for row in listed["items"] if row["merchant_id"] == payload["merchant_id"]), None)
    assert mine is not None, "刚注册的商家必须出现在列表里"
    assert_exact_keys(mine, MERCHANT_KEYS, "§3.33 列表项")
    assert mine["receiver_token_masked"] == payload["receiver_token_masked"]
    assert mine["stall_no"] == payload["stall_no"]

    assert bind_stall_session(client, payload["stall_no"]), "该摊位必须能建秤端会话（否则秤页选不了它）"

@pytest.mark.parametrize(
    "body",
    [
        {"phone": "13800000000", "receiver_code": "1234"},                            # 缺 merchant_name
        {"merchant_name": "", "phone": "13800000000", "receiver_code": "1234"},        # 空名
        {"merchant_name": "x" * 33, "phone": "13800000000", "receiver_code": "1234"},  # 超长名
        {"merchant_name": "甲", "phone": "138abc", "receiver_code": "1234"},           # 手机号含字母
        {"merchant_name": "甲", "phone": "1" * 21, "receiver_code": "1234"},           # 手机号超长
        {"merchant_name": "甲", "phone": "13800000000", "receiver_code": "123"},       # 收款码过短
        {"merchant_name": "甲", "phone": "13800000000", "receiver_code": "9" * 65},    # 收款码超长
    ],
)
def test_registration_field_validation_is_mt_1008(client, body):
    """契约 §3.34：字段缺失 / 超长 / 过短一律 `MT-1008`(422)，**不得静默接受**。"""
    payload = assert_error_response(client.post(DEMO_MERCHANTS, json=body), "MT-1008")
    assert payload["error"]["code"] == "MT-1008"

# --- `AC-044` 确认即支付 + 幂等 ---

def test_confirm_marks_paid_lands_payment_and_shows_in_hub(client, db_conn):
    """`AC-044` ①②③：确认后转已收款 + 落 `payment` + 中台视图出现该笔 + 计入应缴。"""
    ensure_rule(client, today_iso())  # 无生效口径时佣金为 0，"计入应缴"会退化成 0 == 0 的假绿
    merchant, _ = registered(client)
    token = bind_stall_session(client, merchant["stall_no"])
    add_product(client, token)  # 新摊位是空的：§3.38 先铺一个可计价商品
    txn = create_priced_transaction(client, token, idempotency_key=f"confirm-{suffix()}")

    before = payable_of(hub(client), merchant["merchant_id"])
    response = pay_cash(client, token, txn["transaction_no"], f"pay-{suffix()}")
    assert response.status_code == 200, f"契约 §3.10 现金收款期望 200，实际 {response.status_code}"
    assert json_of(response)["transaction_status"] == "paid"

    row = db_conn.execute(
        'SELECT id, status FROM "transaction" WHERE transaction_no = ?', (txn["transaction_no"],)
    ).fetchone()
    assert row["status"] == "paid", "库里必须真的转成 paid"
    payments = db_conn.execute(
        "SELECT COUNT(*) AS n FROM payment WHERE transaction_id = ? AND status = 'success'",
        (row["id"],),
    ).fetchone()["n"]
    assert payments == 1, f"必须落**恰好一条**成功支付流水，实际 {payments}"

    view = hub(client)
    assert any(event["transaction_no"] == txn["transaction_no"] for event in view["events"]), (
        "中台事件表必须出现该笔（AC-044 ②）"
    )
    after = payable_of(view, merchant["merchant_id"])
    assert after is not None, "中台必须给出该商家的应缴行"
    assert_exact_keys(after, PAYABLE_KEYS, "§3.36 应缴行")
    assert after["paid_txn_count"] == (before["paid_txn_count"] if before else 0) + 1
    assert after["payable_cents"] > 0, "已确认交易必须计入应缴（AC-044 ③）"

def test_confirm_is_idempotent_and_neither_count_nor_payable_doubles(client, db_conn):
    """`AC-044` ④：同一标识重复触发 ⇒ **笔数与应缴都不翻倍**，且回契约声明的 `MT-1001`。

    `§1` 的幂等键总表只把"返回首次结果"赋给 4 个场景，**支付端点（§3.10）不在其中**，
    故这里钉住 §3.10 自己声明的 `MT-1001`(409)，而不是把"回 200"这种**未定义**行为写成期望
    （裁定依据与三情形实测见下方断言处的注释）。
    """
    ensure_rule(client, today_iso())
    merchant, _ = registered(client)
    token = bind_stall_session(client, merchant["stall_no"])
    add_product(client, token)  # 新摊位是空的：§3.38 先铺一个可计价商品
    txn = create_priced_transaction(client, token, idempotency_key=f"idem-{suffix()}")
    key = f"pay-idem-{suffix()}"

    first = pay_cash(client, token, txn["transaction_no"], key)
    assert first.status_code == 200, first.get_data(as_text=True)[:200]
    snapshot = payable_of(hub(client), merchant["merchant_id"])
    txn_id = db_conn.execute('SELECT id FROM "transaction" WHERE transaction_no = ?',
                             (txn["transaction_no"],)).fetchone()["id"]

    again = pay_cash(client, token, txn["transaction_no"], key)
    # 钉住契约**已声明**的那一条：交易已 `paid` ⇒ §3.10 的 `MT-1001`(409 状态不允许收款)。
    # 为什么**不**要求 200（幂等命中）：§1 的幂等键总表只把"返回首次结果"赋给 4 个场景
    # （创建交易/离线暂存、退货冲正、支付回调、补传），**§3.10 支付端点不在其中**；`payment` 表
    # 也没有幂等键列（`0001_init.sql`），故"同键重放回 200"是**契约未定义**行为，不该被写成期望。
    # 实测（`mid` 复现 + 我独立复现）：首次 K1 ⇒ 200/PAY-…；同键 K1 重放 ⇒ 409 MT-1001；
    # **换键 K2 也**是 409 MT-1001 —— 差别不在键而在**交易状态**，正是这条状态机在保护"不重复记账"。
    assert again.status_code == 409, (
        f"对已 `paid` 的交易再收款必须回 409（§3.10 的 MT-1001），实际 {again.status_code}："
        f"{again.get_data(as_text=True)[:200]}"
    )
    assert json_of(again)["error"]["code"] == "MT-1001", json_of(again)

    payments = db_conn.execute("SELECT COUNT(*) AS n FROM payment WHERE transaction_id = ?",
                               (txn_id,)).fetchone()["n"]
    assert payments == 1, f"重复触发不得产生第二笔支付流水，实际 {payments}"
    txns = db_conn.execute('SELECT COUNT(*) AS n FROM "transaction" WHERE transaction_no = ?',
                           (txn["transaction_no"],)).fetchone()["n"]
    assert txns == 1, f"重复触发不得产生第二笔交易，实际 {txns}"
    after = payable_of(hub(client), merchant["merchant_id"])
    assert after["payable_cents"] == snapshot["payable_cents"], (
        f"应缴不得翻倍：{snapshot['payable_cents']} → {after['payable_cents']}"
    )
    assert after["paid_txn_count"] == snapshot["paid_txn_count"], "已确认笔数不得翻倍"

# --- `AC-046` 应缴 = 独立复算的佣金之和；催缴短信 ---

def test_payable_equals_independently_recomputed_commission_sum(client, db_conn):
    """`AC-046` ①：`payable_cents` **等于我自己逐笔算出的佣金之和**，且已取消交易不计入。"""
    day = today_iso()
    ensure_rule(client, day)
    rate_bp = effective_rate_bp(db_conn, day)
    assert rate_bp > 0, f"前置：当日费率必须非 0（否则本条会变成 0 == 0 的假绿），实际 {rate_bp}"

    merchant, _ = registered(client)
    token = bind_stall_session(client, merchant["stall_no"])
    add_product(client, token)  # 新摊位是空的：§3.38 先铺一个可计价商品
    # 三笔不同重量的确认 + 一笔"已计价后取消"（必须被排除）
    for weight in (700, 1300, 2100):
        txn = create_priced_transaction(client, token, idempotency_key=f"w{weight}-{suffix()}",
                                        weights_grams=[weight])
        assert pay_cash(client, token, txn["transaction_no"], f"p{weight}-{suffix()}").status_code == 200
    cancelled = create_priced_transaction(client, token, idempotency_key=f"cx-{suffix()}")
    assert cancel(client, cancelled["transaction_no"], f"cxk-{suffix()}").status_code == 200

    stall_id = db_conn.execute("SELECT id FROM stall WHERE stall_no = ?",
                               (merchant["stall_no"],)).fetchone()["id"]
    rows = db_conn.execute(
        'SELECT status, received_amount_cents FROM "transaction" WHERE stall_id = ? AND business_date = ?',
        (stall_id, day),
    ).fetchall()
    paid = [row for row in rows if row["status"] == "paid"]
    assert len(paid) == 3, f"前置：应有 3 笔已确认，实际 {[(r['status'], r['received_amount_cents']) for r in rows]}"
    assert any(row["status"] == "cancelled" for row in rows), "前置：应有 1 笔已取消"
    my_received = sum(int(row["received_amount_cents"]) for row in paid)
    my_commission = sum(half_up(int(row["received_amount_cents"]) * rate_bp, 10000) for row in paid)

    row = payable_of(hub(client, day), merchant["merchant_id"])
    assert row is not None, "中台必须给出该商家的应缴行"
    assert row["paid_txn_count"] == 3, f"已确认笔数应为 3（已取消不计入），实际 {row['paid_txn_count']}"
    assert row["received_amount_cents"] == my_received, (
        f"实收金额必须等于逐笔之和：期望 {my_received}，实际 {row['received_amount_cents']}"
    )
    assert row["commission_cents"] == my_commission, (
        f"佣金必须等于独立复算之和（费率 {rate_bp}bp）：期望 {my_commission}，实际 {row['commission_cents']}"
    )
    assert row["payable_cents"] == my_commission, (
        f"`payable_cents` 必须等于独立复算的佣金之和：期望 {my_commission}，实际 {row['payable_cents']}"
    )

def test_sms_reminder_is_masked_with_interval_two_and_is_idempotent_by_day(client, db_conn):
    """`AC-046` ②：催缴落表留痕（**脱敏**手机号 + 应缴）、`interval_days = 2`、隔天拦截。"""
    day = today_iso()
    ensure_rule(client, day)
    merchant, body = registered(client)
    token = bind_stall_session(client, merchant["stall_no"])
    add_product(client, token)  # 新摊位是空的：§3.38 先铺一个可计价商品
    txn = create_priced_transaction(client, token, idempotency_key=f"sms-{suffix()}")
    assert pay_cash(client, token, txn["transaction_no"], f"sp-{suffix()}").status_code == 200
    payable = payable_of(hub(client, day), merchant["merchant_id"])["payable_cents"]
    assert payable > 0, "前置：该商家应有应缴"

    response = client.post(DEMO_SMS, json={"business_date": day})
    assert response.status_code == 200, f"契约 §3.37 期望 200，实际 {response.status_code}"
    payload = json_of(response)
    assert_exact_keys(payload, {"business_date", "interval_days", "sent", "skipped"}, "§3.37 响应")
    assert payload["interval_days"] == 2, f"必须每隔一天：应为 2，实际 {payload['interval_days']}"
    sent = next((row for row in payload["sent"] if row["merchant_id"] == merchant["merchant_id"]), None)
    assert sent is not None, f"有应缴的商家必须被发送，实际 sent={payload['sent']}"
    assert_exact_keys(sent, {"merchant_id", "merchant_name", "phone_masked", "payable_cents", "sent_at"},
                      "§3.37 sent 行")
    assert "****" in sent["phone_masked"], sent["phone_masked"]
    assert body["phone"] not in sent["phone_masked"], "催缴不得回显手机号原文"
    assert sent["payable_cents"] == payable, "短信里的应缴金额必须等于中台口径"

    stored = db_conn.execute(
        "SELECT payable_cents, phone_masked, channel FROM payment_reminder WHERE merchant_id = ?"
        " ORDER BY id DESC", (merchant["merchant_id"],),
    ).fetchone()
    assert stored is not None, "催缴必须落 `payment_reminder` 留痕（不是只回个 200）"
    assert int(stored["payable_cents"]) == payable
    assert "****" in stored["phone_masked"] and body["phone"] not in stored["phone_masked"]
    assert stored["channel"] == "sms"

    again = json_of(client.post(DEMO_SMS, json={"business_date": day}))
    skipped = next((row for row in again["skipped"] if row["merchant_id"] == merchant["merchant_id"]), None)
    assert skipped is not None, f"同一营业日重复催缴必须被拦下，实际 skipped={again['skipped']}"
    assert skipped["reason"] == "interval_not_elapsed", skipped
    assert not any(row["merchant_id"] == merchant["merchant_id"] for row in again["sent"]), "被拦下的不得再发"

def test_sms_reminder_skips_merchants_with_nothing_payable(client, db_conn):
    """`AC-046` ② 负例：应缴为 0 的商家**不发**，进 `skipped` 且 `reason = "nothing_payable"`。"""
    day = today_iso()
    merchant, _ = registered(client)  # 全新商家：没有任何已确认交易
    payload = json_of(client.post(DEMO_SMS, json={"business_date": day}))
    skipped = next((row for row in payload["skipped"] if row["merchant_id"] == merchant["merchant_id"]), None)
    assert skipped is not None, f"应缴为 0 的商家必须进 skipped，实际 {payload['skipped']}"
    assert skipped["reason"] == "nothing_payable", skipped
    assert not any(row["merchant_id"] == merchant["merchant_id"] for row in payload["sent"])
    rows = db_conn.execute("SELECT COUNT(*) AS n FROM payment_reminder WHERE merchant_id = ?",
                           (merchant["merchant_id"],)).fetchone()["n"]
    assert rows == 0, "没发就不得留发送痕迹"

# --- `AC-012` 口径：演示端点响应体的敏感字段扫描 + 营业日不硬编码 ---

def test_all_demo_responses_pass_the_sensitive_scan(client):
    """`AC-012` / `REQ-024`：五个演示端点的响应体一律**零敏感字段命中**。"""
    merchant, _ = registered(client)
    responses = [
        client.get(DEMO_MERCHANTS),
        client.get(DEMO_HUB),
        client.post(DEMO_SMS, json={}),
        cancel(client, "NO-SUCH-TXN-0001", f"scan-{suffix()}"),
    ]
    for response in responses:
        payload = response.get_json(silent=True)
        where = f"{response.request.method} {response.request.path}"
        assert scan_json_for_sensitive(payload, where) == [], f"{where} 响应体命中敏感字段"
    assert merchant["merchant_id"]

def test_hub_business_date_defaults_to_the_server_day_and_rejects_bad_dates(client):
    """契约 §3.36：缺省取**服务端当日**（测试不硬编码日期）；非法格式 ⇒ `MT-1008`。"""
    payload = hub(client)
    assert payload["business_date"] == today_iso(), (
        f"缺省营业日应取服务端当日 {today_iso()}，实际 {payload['business_date']}"
    )
    assert_error_response(client.get(DEMO_HUB, query_string={"business_date": "2026/10/08"}), "MT-1008")
    assert_error_response(client.post(DEMO_SMS, json={"business_date": "10-08-2026"}), "MT-1008")
