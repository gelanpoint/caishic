"""`T-036` 灵敏度家族（**内容与数据面**）：敏感字段扫描 / 零外部资源 / 指标分子差分。

与 `test_check_sensitivity.py`（**结构面**：路由表、依赖隔离、错误格式、兜底 500）是一套东西的两半，
**一键复跑入口在 `test_check_sensitivity.py`**（它持有登记表 `FAMILIES` 与守卫，
并且会 import 本模块核对"每个家族登记的负例函数真实存在"）：

    python -m pytest tests/contract/test_check_sensitivity.py tests/contract/test_check_sensitivity_content.py -q -s

为什么按"结构 vs 内容"切：两类的**失效方式不同** —— 结构面是被"比对函数被顺手放宽"废掉，
内容面是被"扫描器退化成永远返回空"废掉；放在同一个文件里只会一起涨破 400 行门禁。
"""

from __future__ import annotations

import pytest

from contract_support import REPO_ROOT, TEST_DATA_DIR, json_of
from sensitive_scan import scan_db_file, scan_json_for_sensitive, scan_text_for_sensitive
from test_no_external_assets import CLEAN_SAMPLE, EXTERNAL_SAMPLES, scan_static_text



# ---------------------------------------------------------------------------
# T-007-2 敏感字段扫描
# ---------------------------------------------------------------------------
def test_family_t007_2_sensitive_field_scan_red_then_green(tmp_path):
    """破坏：响应体塞一个身份证号 / 迁移 SQL 加一列银行卡 / 库文件建一张身份证表 → 三入口必红。"""
    # 入口 A：响应体
    clean_payload = {"transaction_no": "T-20260930-0001", "total_amount_cents": 1234}
    assert scan_json_for_sensitive(clean_payload) == []
    hits = scan_json_for_sensitive({"stall": {"id_card": "110101199003071234"}}, "合成响应体")
    assert hits, "响应体里的身份证号没被判红 —— 扫描器有盲区"
    print(f"[T-007-2] 响应体入口：干净体零命中；注入 id_card → {hits[0]}")

    # 入口 B：迁移 SQL（用**真实迁移文件**做底本 + 注入一列）
    real_sql = (REPO_ROOT / "app" / "migrations" / "0001_init.sql").read_text(encoding="utf-8")
    assert scan_text_for_sensitive(real_sql, "真实迁移") == []
    anchor = "CREATE TABLE IF NOT EXISTS stall (\n"
    mutated = real_sql.replace(anchor, anchor + "    id_card                TEXT,\n", 1)
    assert mutated != real_sql, "注入点没找到（迁移文件形态变了？）—— 负例必须重写，不能默认通过"
    sql_hits = scan_text_for_sensitive(mutated, "注入后的迁移")
    assert sql_hits, "迁移 SQL 里的 id_card 列没被判红"
    print(f"[T-007-2] 迁移入口：真实迁移零命中；注入 id_card → {sql_hits[0]}")

    # 入口 C：库文件（真实库 vs 合成库）
    import sqlite3

    synthetic = tmp_path / "synthetic.sqlite3"
    conn = sqlite3.connect(synthetic)
    conn.execute("CREATE TABLE customer (id INTEGER PRIMARY KEY, id_card TEXT)")
    conn.commit()
    conn.close()
    db_hits = scan_db_file(synthetic)
    assert db_hits, "库文件里的 id_card 列没被判红"
    print(f"[T-007-2] 库文件入口：合成库命中 → {db_hits[0]}")

    from contract_support import TEST_DATA_DIR

    real_db = TEST_DATA_DIR / "market_trade_sqlite3"
    if real_db.exists():
        assert scan_db_file(real_db) == [], f"真实演示库被判红：{scan_db_file(real_db)}"
        print("[T-007-2] 库文件入口：真实演示库零命中（还原态复绿）")



# ---------------------------------------------------------------------------
# T-029 零外部资源
# ---------------------------------------------------------------------------
def test_family_t029_no_external_assets_red_then_green():
    """六种外部依赖形态逐个必红；干净样例必须零命中（双向都断言，避免"一律判红"也能过）。"""
    assert len(EXTERNAL_SAMPLES) == 6, f"形态数变了：{len(EXTERNAL_SAMPLES)}（六种都要在）"
    for sample, hint in EXTERNAL_SAMPLES:
        hits = scan_static_text(sample, "合成样例")
        assert hits and any(hint in hit for hit in hits), f"该形态未被判红/提示不对：{sample!r} → {hits}"
    print(f"[T-029] 六种外部依赖形态全部判红：{[hint for _s, hint in EXTERNAL_SAMPLES]}")

    assert scan_static_text(CLEAN_SAMPLE, "干净样例") == []
    print("[T-029] 干净样例（站内相对路径）零命中，复绿")



# ---------------------------------------------------------------------------
# Q-19 使用率指标分子必须来自真查
# ---------------------------------------------------------------------------
def test_family_q19_metric_numerator_red_then_green(client, db_conn):
    """差分灵敏度：**改变数据 → 分子必须跟着变**；写死/查错表的实现过不了这条。

    `Q-19` 的原始缺陷是"分子分母各查各的、分母还能退化" —— 它的负例形态就是这条差分：
    造出数据、看分子动不动。

    ⚠️ **为什么用一个种子数据之外的营业日（`2026-01-15`）**：
    契约测试共用**会话级**的演示库，前面模块会改价目表、停用商品（第一版跑"今天"，
    在全量跑时前面的用例已经把 A-01 的价目表动过，`create_priced_transaction` 直接 409 `MT-1006`）。
    选一个没人碰的日期，基线必然为 0，于是"数据 → 数字"这条因果不依赖任何前置状态；
    破坏物也全部在同一个日期上，删干净即可还原。
    """
    probe_date = "2026-01-15"
    metrics_path = f"/api/admin/metrics/usage?business_date={probe_date}"
    cash_key, price_key, stall_key = "cash_txn_numerator", "price_list_numerator", "stall_usage_numerator"

    def usage() -> dict:
        response = client.get(metrics_path)
        assert response.status_code == 200, f"契约 §3.28 期望 200，实际 {response.status_code}"
        return json_of(response)

    base = usage()
    assert {cash_key, price_key, stall_key} <= set(base), f"指标字段名变了：{sorted(base)}"
    assert base[cash_key] == 0 and base[price_key] == 0, (
        f"{probe_date} 上居然已有数据（这个日期被别的用例用了？换个日期）：{base}"
    )

    # 破坏一：写入 1 笔该日的交易 + 1 笔现金成功收款 → 现金分子与交易摊位分子必须各 +1
    stall_row = db_conn.execute("SELECT id FROM stall WHERE status = 'active' ORDER BY id LIMIT 1").fetchone()
    db_conn.execute(
        'INSERT INTO "transaction" (transaction_no, stall_id, business_date, status, total_amount_cents,'
        " round_off_cents, origin, client_idempotency_key) VALUES (?, ?, ?, 'paid', 1000, 0, 'online', 'q19-probe')",
        ("T-20260115-0001", stall_row["id"], probe_date),
    )
    transaction_id = db_conn.execute(
        'SELECT id FROM "transaction" WHERE transaction_no = ?', ("T-20260115-0001",)
    ).fetchone()["id"]
    db_conn.execute(
        "INSERT INTO payment (payment_no, transaction_id, method, amount_cents, status, confirmed_at, operator)"
        " VALUES ('PAY-20260115-000001', ?, 'cash', 1000, 'success', '2026-01-15 08:00:00', 'q19')",
        (transaction_id,),
    )
    db_conn.commit()
    after_cash = usage()
    assert after_cash[cash_key] == 1, (
        f"现金分子没有跟着数据走（写死了？查错表了？）：{base[cash_key]} → {after_cash[cash_key]}"
    )
    assert after_cash[stall_key] == 1, f"交易摊位分子没跟着走：{after_cash}"
    print(f"[Q-19] 差分①：+1 笔现金收款 → {cash_key} {base[cash_key]} → {after_cash[cash_key]}、"
          f"{stall_key} {base[stall_key]} → {after_cash[stall_key]}")

    # 破坏二：把某个摊位的**全部在售商品**在该日补上价目 → 该摊位算"价目表完整" → 分子 +1
    product_ids = [
        row["id"]
        for row in db_conn.execute(
            "SELECT id FROM product WHERE stall_id = ? AND status = 'active'", (stall_row["id"],)
        )
    ]
    assert product_ids, "该摊位没有在售商品 —— 负例的前提没造出来"
    db_conn.executemany(
        "INSERT INTO price_item (stall_id, product_id, business_date, unit_price_cents, source)"
        " VALUES (?, ?, ?, 500, 'manual')",
        [(stall_row["id"], pid, probe_date) for pid in product_ids],
    )
    db_conn.commit()
    after_price = usage()
    assert after_price[price_key] == 1, (
        f"价目表分子没跟着数据走（少查了在售商品？NOT EXISTS 写反了？）："
        f"{after_cash[price_key]} → {after_price[price_key]}（补了 {len(product_ids)} 行价目）"
    )
    print(f"[Q-19] 差分②：给 {len(product_ids)} 个在售商品补当日价目 → {price_key} "
          f"{after_cash[price_key]} → {after_price[price_key]}")

    # 还原：破坏物全在这个日期上，删干净 → 指标必须回到基线（这既是复绿证据，
    # 也是"残留物没清干净就会被抓住"的守卫）
    db_conn.execute("DELETE FROM price_item WHERE business_date = ?", (probe_date,))
    db_conn.execute("DELETE FROM payment WHERE transaction_id = ?", (transaction_id,))
    db_conn.execute('DELETE FROM "transaction" WHERE business_date = ?', (probe_date,))
    db_conn.commit()
    restored = usage()
    assert restored == base, f"残留物没清干净（后续用例的绝对计数会被污染）：{base} → {restored}"
    print(f"[Q-19] 还原后：{cash_key}={restored[cash_key]}、{price_key}={restored[price_key]}，与破坏前一致（复绿）")
    print("[Q-19] 结论：两个分子都随真实数据变化 ⇒ 它们是**真查**出来的（写死的实现必红）")
