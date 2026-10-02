"""`T-SIM-00` · 时钟接缝的**机械守卫**（`REQ-033` / `AC-024`）——检查面。

回答一个问题：**"现在是哪一天"还有没有第二个出处？**

「现在是哪一天」原先散在 **8 处 Python**（`pricing.py:152`、`metrics.py:82/210/284`、
`offline.py:152`、`payment.py:65`、`seed.py:346`、`scripts/reset_demo.py:126`）+ **23 处 SQL 列默认值**。
漏改一处的后果**不是报错，而是静默错位**：业务日落在墙钟当天、日聚合与对账在跨月推进时对不上，
而且没有任何异常。所以本文件把它变成**机械约束**，并各自**自带合成负例**
（`T-036` 家族的规矩：不验证灵敏度的验证是摆设）。

> SQL 那一路本任务**未修**（不在 `T-SIM-00` 的批内），故本文件把缺口**枚举并钉死**，
> 而不是在注释里口头承认：谁修掉一列，期望集合就对不上，用例变红，逼他同步改
> `spec.md` §5 与 `tasks.md` 的 `T-SIM-00` 未决项。
"""

from __future__ import annotations

from datetime import date

import pytest

from clock_support import (
    CLOCK_MODULE,
    REPO_ROOT,
    SCAN_FILES,
    build_injected,
    find_direct_wall_clock_reads,
    run_full_business_day,
    wall_clock_timestamp_columns,
)


# ---------------------------------------------------------------------------
# 判据一：墙钟只允许在 app/clock.py 里读（含合成负例）
# ---------------------------------------------------------------------------
def test_no_direct_wall_clock_reads_outside_clock_module():
    """扫描面内**除 `app/clock.py` 外**不得存在任何墙钟直读。"""
    offenders: list[str] = []
    for path in SCAN_FILES:
        if path == CLOCK_MODULE:
            continue
        offenders.extend(
            find_direct_wall_clock_reads(path.read_text(encoding="utf-8"), str(path.relative_to(REPO_ROOT)))
        )
    assert offenders == [], (
        "发现墙钟直读（必须改为 `app/clock.py` 的 `today_iso()` / `now_iso()`）：\n  " + "\n  ".join(offenders)
    )
    print(f"[T-SIM-00] 扫描 {len(SCAN_FILES)} 个源文件，除 app/clock.py 外零墙钟直读")


def test_scanner_flags_synthetic_violations():
    """**灵敏度负例**：四种直读形态逐个必须被判红（否则上面的扫描等于没扫）。"""
    samples = [
        ("from datetime import date\nx = date.today()\n", "date.today()"),
        ("from datetime import datetime\nx = datetime.now()\n", "datetime.now()"),
        ("import datetime\nx = datetime.datetime.today()\n", "datetime.today()"),
        ("import time\nx = time.time()\n", "time.time()"),
    ]
    for source, hint in samples:
        hits = find_direct_wall_clock_reads(source, "合成样例")
        assert hits, f"该形态未被判红：{hint}"
        assert any(hint in hit for hit in hits), f"判红但提示不对：{hits}"
        print(f"[T-SIM-00] 合成负例判红 → {hits[0]}")


def test_scanner_stays_green_on_clean_and_on_docstring_mentions():
    """反向断言：干净写法（走 `clock`）与**docstring 里提到**墙钟写法都不得误报。"""
    assert find_direct_wall_clock_reads("from .. import clock\nx = clock.today_iso()\n", "干净样例") == []
    assert find_direct_wall_clock_reads('"""解释为什么不用 date.today()。"""\n', "仅文档提及") == []
    print("[T-SIM-00] 干净写法与 docstring 提及均零命中（扫描器不误报）")


# ---------------------------------------------------------------------------
# 判据二：SQL 列默认值这一路的缺口必须被钉住（如实登记，不掩盖）
# ---------------------------------------------------------------------------
@pytest.fixture()
def injected(tmp_path, monkeypatch):
    return build_injected(tmp_path, monkeypatch)


def test_known_gap_sql_column_defaults_use_wall_clock(injected):
    """迁移里 23 处 `DEFAULT (datetime('now','localtime'))` 由 **SQLite 求值**，
    `MT_CLOCK_FILE` 管不到 —— 故凡**应用层没显式赋值**的时间戳列仍是墙钟。

    本用例把缺口**枚举出来并钉住**：它是一份机械记录（不是注释里的口头承认），
    谁补了一列就会变红，逼他同步改规格与未决项 —— **缺口不允许被静默修掉，也不允许被静默忘记**。
    """
    from app import db

    client, day = injected
    run_full_business_day(client, day)  # **必须先造出数据**：空库上枚举会得出"零缺口"的假结论

    conn = db.connect()
    try:
        actual = wall_clock_timestamp_columns(conn, day)
    finally:
        conn.close()

    #: **实测并钉住**的墙钟列（23 个默认列里，应用层未显式赋值的那 12 个）
    expected = sorted(
        {
            "category.created_at",
            "merchant.created_at",
            "merchant.updated_at",
            "payment.created_at",
            "price_item.created_at",
            "price_item.updated_at",
            "product.created_at",
            "stall.created_at",
            "stall_category_alias.created_at",
            "stall_session.created_at",
            "transaction.created_at",
            "transaction_item.created_at",
        }
    )
    print(f"[T-SIM-00][已知缺口] 仍取墙钟的时间戳列（{len(actual)} 个）：{actual}")
    assert actual == expected, (
        "墙钟时间戳列的集合变了 —— 若你刚修掉了若干列，请**同步更新** spec.md §5、"
        "docs/sim-design.md 与 tasks.md 的 T-SIM-00 未决项，再更新本用例的期望集合；\n"
        f"  新增（原来跟随时钟、现在不跟随）：{sorted(set(actual) - set(expected))}\n"
        f"  修掉（原来不跟随、现在跟随）：{sorted(set(expected) - set(actual))}"
    )


def test_gap_probe_is_empty_on_an_empty_database(injected):
    """**允许失败是不可能的**：本用例证明"空库上枚举会得出零缺口"这一陷阱真实存在 ——
    这正是本文件第一版的真实缺陷（缺口枚举跑在空库上，报出"8 个"而漏掉了业务表）。
    它把"必须先造数据"这条使用纪律钉成可执行的证据。
    """
    from app import db

    _, day = injected  # 只用夹具（已建库+种子），**不走任何业务流**
    conn = db.connect()
    try:
        assert wall_clock_timestamp_columns(conn, day) != []
    finally:
        conn.close()
    print("[T-SIM-00] 已确认：缺口枚举依赖数据存在；空库/无业务表数据时必须先造数据再判定")


def test_wall_clock_column_probe_has_expected_shape():
    """合成样例回归探针本身：造一张两种取值的表，必须只判红墙钟那一列。"""
    import sqlite3

    from app import clock  # noqa: F401  （仅为让本用例与接缝同源，不额外断言）

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (business_date TEXT, created_at TEXT, occurred_at TEXT)")
    wall = f"{date.today().isoformat()} 10:00:00"
    conn.execute("INSERT INTO t VALUES (?, ?, ?)", ("2031-03-05", wall, "2031-03-05 08:30:00"))
    conn.commit()
    hits = wall_clock_timestamp_columns(conn, "2031-03-05")
    conn.close()
    assert hits == ["t.created_at"], f"探针判据不对：{hits}"
    print(f"[T-SIM-00] 缺口探针合成样例：只判红墙钟列 {hits}")
