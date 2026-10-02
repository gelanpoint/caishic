"""`T-SIM-00` · 时钟接缝的**行为面**（`REQ-033` / `AC-024`）。

与 `test_clock_guard.py`（**检查面**：墙钟直读的机械守卫 + SQL 默认值缺口枚举）是一套东西的两半：
本文件问的是"**接缝真的生效吗**"，守卫文件问的是"**有没有第二个出处**"。
按"失效方式不同"切（沿用 `T-036` 家族 `test_check_sensitivity.py` / `test_check_sensitivity_content.py`
的同一分法）：本文件被"注入没生效却没人发现"废掉，守卫文件被"扫描器退化成永远返回空"废掉。

`AC-024` 的前后两半各有一条判据：
- **未注入** → 业务日与时间戳等于本机墙钟（`test_default_clock_is_wall_clock`）；
- **注入 `D`** → 交易、日聚合、结算单、三指标、审计留痕的业务日与**应用层写入的时间戳**全部落在 `D`
  （`test_injected_clock_drives_full_business_day`）。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from clock_support import build_injected, run_full_business_day, write_clock


@pytest.fixture()
def injected(tmp_path, monkeypatch):
    return build_injected(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# AC-024 前半：未注入 = 本机墙钟（"逐字段一致"）
# ---------------------------------------------------------------------------
def test_default_clock_is_wall_clock(monkeypatch):
    """未设 `MT_CLOCK_FILE` 时，业务日与时间戳等于本机墙钟。"""
    from app import clock

    monkeypatch.delenv(clock.ENV_CLOCK_FILE, raising=False)
    assert clock.today() == date.today()
    assert clock.today_iso() == date.today().isoformat()

    before = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp = clock.now_iso()
    after = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    assert before <= stamp <= after, f"未注入时时间戳不属于墙钟区间：{stamp!r} 不在 [{before}, {after}]"
    print(f"[T-SIM-00] 未注入 → 墙钟（{stamp}）")


def test_db_timestamp_forwards_to_the_clock_seam(monkeypatch, tmp_path):
    """`app/db.py::now_iso` 必须转发到时钟接缝 —— 审计留痕的时间戳由此一次覆盖。"""
    from app import clock, db

    monkeypatch.setenv(clock.ENV_CLOCK_FILE, str(write_clock(tmp_path, "2031-03-04 05:06:07")))
    assert db.now_iso() == "2031-03-04 05:06:07"


# ---------------------------------------------------------------------------
# AC-024 后半：注入后全链路落在 D（走真实 HTTP 端点）
# ---------------------------------------------------------------------------
def test_injected_clock_drives_seed_prices(injected):
    """site 8：种子价目表必须落在**注入业务日**（否则秤端一条都计不了价 → 全链路 409）。"""
    from app import db

    _, day = injected
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT business_date, COUNT(*) AS n FROM price_item GROUP BY business_date"
        ).fetchall()
        dates = {row["business_date"] for row in rows}
        assert day in dates, f"注入业务日 {day} 没有价目表，实际落在：{sorted(dates)}"
        assert date.today().isoformat() not in dates, "墙钟当天出现了价目表 —— 种子没走注入时钟"
    finally:
        conn.close()
    print(f"[T-SIM-00] 种子价目表落在注入业务日 {day}（墙钟当天零行）")


def test_injected_clock_drives_full_business_day(injected):
    """一个完整营业日的每一步，业务日与**应用层写入的**时间戳都必须是 `D`。"""
    from app import db

    client, day = injected
    token, txn_no = run_full_business_day(client, day)

    # 「这笔落在 D 而不落在墙钟当天」——用列表查询**双向**证明（单向会被"两边都空"骗过）
    under_d = client.get(
        "/api/merchant/transactions", query_string={"business_date": day}, headers={"X-Stall-Session": token}
    ).get_json()
    under_wall = client.get(
        "/api/merchant/transactions",
        query_string={"business_date": date.today().isoformat()},
        headers={"X-Stall-Session": token},
    ).get_json()
    assert under_d["total"] == 1, f"注入业务日 {day} 下应有 1 笔，实际 {under_d['total']}"
    assert under_wall["total"] == 0, f"墙钟当天不应有交易，实际 {under_wall['total']}"

    # 看板默认营业日 = D（"默认营业日"这一路取样点的判据）
    mdash = client.get("/api/merchant/dashboard", headers={"X-Stall-Session": token}).get_json()
    assert mdash["business_date"] == day, f"商户端看板默认营业日 = {mdash['business_date']}，应为 {day}"
    assert mdash["txn_count"] == 1 and mdash["gross_amount_cents"] > 0

    # 结算单口径与日聚合一致（`AC-023`）
    settle = client.get("/api/admin/settlements", query_string={"stall_no": "A-01"}).get_json()
    assert settle and settle[0]["gross_amount_cents"] == mdash["gross_amount_cents"]
    assert settle[0]["period_end"] == day, f"结算单期末 = {settle[0]['period_end']}，应为 {day}"

    # 三个使用率指标（`AC-005` 口径）在注入业务日上可用
    usage = client.get("/api/admin/metrics/usage", query_string={"business_date": day}).get_json()
    assert usage["business_date"] == day
    assert usage["stall_usage_numerator"] == 1 and usage["stall_usage_denominator"] == 10

    # 审计留痕的时间戳（`db.now_iso` → 时钟）
    logs = client.get("/api/admin/audit-logs", query_string={"stall_no": "A-01"}).get_json()
    events = logs.get("items", logs if isinstance(logs, list) else [])
    assert events, "审计留痕为空，无法核对时间戳"
    for row in events:
        assert str(row.get("occurred_at", "")).startswith(day), f"留痕时间戳未跟随注入时钟：{row}"

    conn = db.connect()
    try:
        txn = conn.execute(
            'SELECT business_date FROM "transaction" WHERE transaction_no = ?', (txn_no,)
        ).fetchone()
        assert txn["business_date"] == day
    finally:
        conn.close()
    print(f"[T-SIM-00] 全链路业务日与留痕时间戳均落在注入日 {day}（墙钟当天零交易）")


# ---------------------------------------------------------------------------
# 判据三：读不到 / 读不懂 → 明确报错（绝不静默退回墙钟）
# ---------------------------------------------------------------------------
def test_missing_clock_file_fails_loud(monkeypatch, tmp_path):
    """时钟文件不存在 → 抛 `ClockSourceError`；**不得**静默退回墙钟。"""
    from app import clock

    monkeypatch.setenv(clock.ENV_CLOCK_FILE, str(tmp_path / "不存在.txt"))
    with pytest.raises(clock.ClockSourceError) as exc:
        clock.today_iso()
    assert "读不到" in str(exc.value)
    print(f"[T-SIM-00] 时钟文件缺失 → 明确报错：{exc.value}")


def test_malformed_clock_file_fails_loud(monkeypatch, tmp_path):
    """时钟文件内容非法 → 抛 `ClockSourceError`，且**不继承 `ValueError`**（防止被顺手接住）。"""
    from app import clock

    monkeypatch.setenv(clock.ENV_CLOCK_FILE, str(write_clock(tmp_path, "不是日期")))
    with pytest.raises(clock.ClockSourceError):
        clock.now()
    assert not issubclass(clock.ClockSourceError, ValueError)
    print("[T-SIM-00] 时钟内容非法 → 明确报错，且异常类型独立于 ValueError")


def test_clock_file_is_reread_on_every_call(monkeypatch, tmp_path):
    """**每次调用重读** —— 这是仿真"不重启服务、不新增端点即可推进业务日"的前提。"""
    from app import clock

    path = tmp_path / "advancing-clock.txt"
    monkeypatch.setenv(clock.ENV_CLOCK_FILE, str(path))
    for day in ("2031-12-30", "2031-12-31", "2032-01-01"):
        path.write_text(f"{day} 06:00:00", encoding="utf-8")
        assert clock.today_iso() == day
    print("[T-SIM-00] 同一进程内连续推进三个业务日（跨年），无需重启、无需新增端点")
