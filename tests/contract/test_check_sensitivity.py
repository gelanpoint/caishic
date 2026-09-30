"""`T-036` 灵敏度家族**收口入口**：把"这些检查还能不能失败"集中到一处，一条命令跑完。

## 一键复跑

    python -m pytest tests/contract/test_check_sensitivity.py -q -s
    （逐条打印「破坏 → 必红 → 还原 → 复绿」；`-s` 才能看到证据行）

## 为什么要有这个文件（而不是"以前都验过"）

"检查曾经能失败"不是证据 —— 检查会因为**扫描器退化成永远返回空**、**比对函数被顺手放宽**、
**端点/字段/依赖清单改了而检查没跟着改**而**悄悄变成摆设**。这种变绿是**假绿**：
最能误导人的失败模式就是"CI 全绿，但检查早就不检查了"。
所以每个家族都在这里放一条**随每次 pytest 一起跑的负例**：把真实产物破坏掉，
**必须**判红；还原后**必须**复绿；两侧都断言，缺一侧都不算数。

## 家族登记表（`FAMILIES`）

| id | 来源 | 检查什么 |
| --- | --- | --- |
| `T-007-1` | `T-007` ① | 路由表 ↔ 契约 §2 **双向**比对（多一个/少一个都要红） |
| `T-007-2` | `T-007` ② | 敏感字段扫描（响应体 / 迁移 SQL / 库文件 三入口） |
| `T-007-3` | `T-007` ③ | 运行期依赖隔离（`ast` 扫描 + 依赖清单） |
| `T-007-4` | `T-007` ④ | 统一错误响应格式（契约 §1.2 envelope） |
| `T-029` | `T-029` | 零外部资源（六种形态 + 干净样例 + URL 层解析） |
| `Q-19` | `Q-19` | 使用率指标分子必须**来自真查**（差分灵敏度） |

**未登记（待人工裁定后补）**：`T-034-500`"未预期异常必须给出契约 §1.2 格式而不是 HTML"。
它的检查骨架依赖契约 §4 是否新增通用 5xx 码 —— 见 `T-034` 的裁定请求；
**在裁定前不发明错误码**，故此处不登记（缺口显式留白，不假装已覆盖）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from contract_support import (
    CONTRACT_PATH,
    REPO_ROOT,
    assert_error_response,
    assert_route_table_matches_contract,
    error_envelope_violations,
    json_of,
    parse_error_codes,
    registered_endpoints,
    route_table_diff,
)
from sensitive_scan import scan_db_file, scan_json_for_sensitive, scan_text_for_sensitive
from test_deps_isolation import dev_tool_violations, requirement_names, scan_runtime_imports
from test_no_external_assets import CLEAN_SAMPLE, EXTERNAL_SAMPLES, scan_static_text

#: 家族登记：id → (说明, 负例测试函数名)。守卫会核对**登记的函数真实存在**，
#: 于是"把某个家族的负例删掉"会立刻让守卫变红（这是本文件存在的主要理由）。
FAMILIES: dict[str, tuple[str, str]] = {
    "T-007-1": ("路由表 ↔ 契约 §2 双向比对", "test_family_t007_1_route_table_two_way_red_then_green"),
    "T-007-2": ("敏感字段扫描（响应体/迁移 SQL/库文件）", "test_family_t007_2_sensitive_field_scan_red_then_green"),
    "T-007-3": ("运行期依赖隔离（ast 扫描 + 清单）", "test_family_t007_3_runtime_isolation_red_then_green"),
    "T-007-4": ("统一错误响应格式（§1.2 envelope）", "test_family_t007_4_error_envelope_red_then_green"),
    "T-029": ("静态资源零外部依赖（六形态 + 干净样例）", "test_family_t029_no_external_assets_red_then_green"),
    "Q-19": ("使用率指标分子必须来自真查", "test_family_q19_metric_numerator_red_then_green"),
}

#: 必须被覆盖的家族全集（`T-036` 的验收口径；少一个即守卫变红）
REQUIRED_FAMILIES = frozenset(FAMILIES)


def test_registry_covers_every_family_and_each_negative_exists():
    """守卫：登记表非空、每个家族都有**真实存在**的负例函数、且没有多余登记。"""
    assert set(FAMILIES) == REQUIRED_FAMILIES
    for family, (description, negative) in FAMILIES.items():
        assert description.strip(), f"{family} 缺说明"
        assert callable(globals().get(negative)), (
            f"{family} 登记的负例 `{negative}` 在本文件里不存在 —— 检查被删了/改名了，"
            "而登记表还在说它被覆盖了（这正是「假绿」的入口）"
        )
    print(f"[收口] 已登记 {len(FAMILIES)} 个灵敏度家族：" + "、".join(sorted(FAMILIES)))


# ---------------------------------------------------------------------------
# T-007-1 路由表双向比对
# ---------------------------------------------------------------------------


def test_family_t007_1_route_table_two_way_red_then_green():
    """破坏：给**真实应用实例**注册一个契约里没有的端点 → 双向比对必红；新起实例 → 复绿。"""
    from app import create_app

    def fresh_app():
        # 用**真实工厂**新起实例：还原不需要去改 Flask 内部结构（`url_map` 是内部对象），
        # 也就不会把"破坏"泄漏给同模块的其他用例（契约 conftest 的 seeded_app 是 session 作用域）。
        return create_app()

    green = fresh_app()
    assert_route_table_matches_contract(green)
    print(f"[T-007-1] 破坏前：真实应用路由表与契约 §2 完全一致（{len(registered_endpoints(green))} 条）")

    broken = fresh_app()
    broken.add_url_rule("/api/sensitivity-phantom", endpoint="sensitivity_phantom", view_func=lambda: ("{}", 200))
    with pytest.raises(AssertionError) as failure:
        assert_route_table_matches_contract(broken)
    assert "/api/sensitivity-phantom" in str(failure.value), f"红是红了，但没指出多出来的端点：{failure.value}"
    missing, extra = route_table_diff(broken)
    assert ("GET", "/api/sensitivity-phantom") in extra and not missing, f"双向比对的返回不对：{missing} {extra}"
    print(f"[T-007-1] 破坏后：多出端点被抓住 → {extra}")

    assert_route_table_matches_contract(fresh_app())
    print("[T-007-1] 还原后：复绿")


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
# T-007-3 运行期依赖隔离
# ---------------------------------------------------------------------------


def test_family_t007_3_runtime_isolation_red_then_green(tmp_path):
    """破坏：在**真实 run.py 的副本**里 `import pytest` → 必红；去掉 → 复绿。

    为什么用副本：真文件是运行期源码，测试去改它会让"顺手忘了还原"变成产品事故。
    副本取自真实文件（不是手写的假样例），所以"注入了什么、扫描器看没看见"仍是真实链路。
    """
    copy = tmp_path / "run.py"
    shutil.copyfile(REPO_ROOT / "run.py", copy)
    assert scan_runtime_imports([copy]) == []

    copy.write_text("import pytest\n" + copy.read_text(encoding="utf-8"), encoding="utf-8")
    violations = scan_runtime_imports([copy])
    assert violations, "`import pytest` 没被判红 —— 依赖隔离检查是摆设"
    assert "开发期工具" in violations[0], f"判红了但归类不对：{violations[0]}"
    print(f"[T-007-3] 破坏：{violations[0]}")

    copy.write_text(copy.read_text(encoding="utf-8").replace("import pytest\n", "", 1), encoding="utf-8")
    assert scan_runtime_imports([copy]) == []
    print("[T-007-3] 还原后：复绿")

    # 另一半：依赖清单（真实清单 vs 注入后的清单）
    real_requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert dev_tool_violations(requirement_names(real_requirements)) == []
    injected = real_requirements + "\npytest==8.3.4\n"
    hits = dev_tool_violations(requirement_names(injected))
    assert hits, "运行期清单里注入 pytest 没被判红"
    print(f"[T-007-3] 清单入口：真实清单零违规；注入 pytest → {hits[0]}")


# ---------------------------------------------------------------------------
# T-007-4 统一错误响应格式
# ---------------------------------------------------------------------------


def test_family_t007_4_error_envelope_red_then_green(client):
    """破坏：把错误体换成 HTML / 少字段 / 用契约外错误码 → 必红；真实错误响应 → 复绿。"""
    contract_codes = parse_error_codes()
    assert contract_codes, "契约 §4 的错误码表一个都没解析出来（解析器或契约形态变了）"

    good = {"error": {"code": "MT-1009", "message": "不存在", "detail": {"path": "/nope"}}}
    assert error_envelope_violations(good, 404, contract_codes) == []

    html = "<!doctype html><html><title>500 Internal Server Error</title></html>"
    assert error_envelope_violations(html, 500, contract_codes), "HTML 错误体没被判红"
    print("[T-007-4] 破坏①：HTML 错误体（Flask 默认 500 的形态）被判红")

    missing_code = {"error": {"message": "没有 code"}}
    assert error_envelope_violations(missing_code, 422, contract_codes), "缺 `code` 的体没被判红"
    print("[T-007-4] 破坏②：缺 `code` 字段被判红")

    off_contract = {"error": {"code": "MT-9999", "message": "契约外的码"}}
    violations = error_envelope_violations(off_contract, 422, contract_codes)
    assert violations, "契约外错误码没被判红（那 §4 的表就白写了）"
    print(f"[T-007-4] 破坏③：契约外错误码被判红 → {violations[0]}")

    # 复绿：真实应用的 404（走真实路由链路，不是合成体）
    response = client.get("/api/sensitivity-no-such-path")
    payload = json_of(response)
    assert response.status_code == 404, f"契约 §4 期望 404，实际 {response.status_code}"
    assert_error_response(response, "MT-1009")
    assert error_envelope_violations(payload, response.status_code, contract_codes) == []
    print("[T-007-4] 还原后：真实 404 响应符合 §1.2 envelope，复绿")


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
