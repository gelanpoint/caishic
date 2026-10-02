"""`T-SIM-00` 时钟接缝的**共用支撑**（助手与常量，不是用例）。

按 `tests/contract/contract_support.py` 的同一先例拆分：**用例文件按语义拆，共用件放支撑模块**。
本模块刻意**不定义 pytest 夹具**（夹具要经 conftest/plugin 才跨文件可见，而多目录同名 `conftest`
正是本项目踩过的坑——`cabe4e1` 的"同名 conftest 撞车"）。各用例文件各自定义 4 行 `injected` 夹具调用
`build_injected()`，路径短、无隐式耦合。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: 允许直读墙钟的唯一文件（**时钟来源的唯一落点**）
CLOCK_MODULE = REPO_ROOT / "app" / "clock.py"

#: 扫描面：运行期会被执行的全部 Python 源（`run.py` + `app/**` + `scripts/**`）
SCAN_FILES = sorted(
    [REPO_ROOT / "run.py"]
    + list((REPO_ROOT / "app").rglob("*.py"))
    + list((REPO_ROOT / "scripts").rglob("*.py"))
)

#: 被禁的直读形态（调用链根名字 + 方法名）。
#: 故意**不做**"字符串里出现 `date.today` 就判红"：docstring 里写这个用法是正常的，
#: 判据必须落在 **AST 调用节点** 上，否则扫描器会变成噪声源（噪声一多就会被顺手放宽）。
FORBIDDEN_CLOCK_CALLS = {
    ("date", "today"),
    ("datetime", "now"),
    ("datetime", "today"),
    ("time", "time"),
}


def find_direct_wall_clock_reads(source: str, where: str) -> list[str]:
    """返回源码里直读墙钟的位置（空 = 干净）。**纯函数**，故合成样例可直接喂给它。

    判据落在**调用链的根名字**上：`date.today()`、`datetime.datetime.now()`、`datetime.date.today()`、
    `time.time()` 都命中；而 `clock.now()`（正确写法）与 `obj.date.today()`（别人的对象方法）不命中 ——
    否则扫描器会变成噪声源，噪声一多就会被顺手放宽。
    """
    import ast

    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        owner = func.value
        while isinstance(owner, ast.Attribute):  # `datetime.datetime.now` → 根 `datetime`
            owner = owner.value
        if isinstance(owner, ast.Name) and (owner.id, func.attr) in FORBIDDEN_CLOCK_CALLS:
            hits.append(f"{where}:{node.lineno} 直读墙钟 `{owner.id}.{func.attr}()`")
    return hits


def wall_clock_timestamp_columns(conn, injected_day: str) -> list[str]:
    """枚举**仍取墙钟**的时间戳列：值以墙钟当天开头、且不以注入业务日开头的 `*_at` 列。

    调用方必须先造出数据 —— **空库上跑这个函数会返回空表，看起来像"一个缺口都没有"**
    （本文件的第一版就犯过这个错，故在此显式写明）。
    """
    wall = date.today().isoformat()
    offenders: set[str] = set()
    tables = [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not row[0].startswith("sqlite_")
    ]
    for table in tables:
        for col in [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]:
            if not col.endswith("_at"):
                continue
            for value in conn.execute(f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL'):
                text = str(value[0])
                if text.startswith(wall) and not text.startswith(injected_day):
                    offenders.add(f"{table}.{col}")
    return sorted(offenders)


def write_clock(tmp_path: Path, text: str) -> Path:
    """把时钟值写进一个临时文件（供 `MT_CLOCK_FILE` 指向）。"""
    path = tmp_path / f"clock-{abs(hash(text))}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def build_injected(tmp_path: Path, monkeypatch, day: str = "2031-03-05"):
    """隔离数据目录 + 注入业务日 `day`；返回 `(Flask 测试客户端, day)`。

    `MT_CLOCK_FILE` 是**每次调用重读**的，故本函数无需重启进程；
    `config` 的路径是导入期读环境变量，故直接 monkeypatch 属性（比设环境变量可靠 ——
    同一次 pytest 会话里 `tests/contract` 可能已经把 `config.DATA_DIR` 固定住了）。
    """
    from app import clock, config, db, seed
    from app import create_app

    monkeypatch.setenv(clock.ENV_CLOCK_FILE, str(write_clock(tmp_path, f"{day} 08:30:00")))

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "market_trade.sqlite3")
    monkeypatch.setattr(config, "OFFLINE_STAGING_DIR", data_dir / "offline_staging")
    monkeypatch.setattr(config, "LOG_DIR", data_dir / "logs")

    db.init_database()
    seed.import_seed()  # 种子价目表按注入业务日落地（site 8 的直接证据）
    application = create_app()
    application.config.update(TESTING=True)
    return application.test_client(), day


def post_tx(client, token: str, key: str):
    """在注入业务日上创建一笔交易（先取商品，否则价目表缺失会 409）。"""
    products = client.get("/api/merchant/products", headers={"X-Stall-Session": token}).get_json()
    assert products, "注入时钟后取不到在营商品 —— 种子未按注入业务日落地"
    return client.post(
        "/api/merchant/transactions",
        json={"items": [{"product_id": products[0]["id"], "weight_grams": 1000}]},
        headers={"X-Stall-Session": token, "Idempotency-Key": key},
    )


def run_full_business_day(client, day: str) -> tuple[str, str]:
    """在注入业务日 `day` 上走完一个营业日，返回 `(session_token, transaction_no)`。

    步骤：佣金口径 → 成交 → 改价（留痕）→ 现金收款 → 日终聚合 → 结算单 → 三指标 → 顾客页。
    **"全链路业务日正确"与"缺口枚举"两条判据必须跑在同一份数据上** ——
    否则缺口枚举会在一张空库上跑，得出"零缺口"的假结论。
    """
    token = client.post("/api/merchant/session", json={"stall_no": "A-01"}).get_json()["session_token"]

    # 日聚合要求"该营业日有生效佣金口径"（`MT-1013`），故先按注入业务日配置口径
    rule = client.put(
        "/api/admin/commission-rules",
        json={"pay_object": "merchant", "rate_bp": 200, "effective_from": day},
    )
    assert rule.status_code == 200, rule.get_json()

    created = post_tx(client, token, "tsim00-txn-1")  # site 1：pricing.py
    assert created.status_code == 201, created.get_json()
    body = created.get_json()
    txn_no = body["transaction_no"]

    # 改价（幅度 20%，不触发 >50% 确认分支）：**留痕是审计时间戳的载体**（`NFR-009`）；
    # 单纯成交与现金收款按设计不写留痕，故"审计时间戳跟随时钟"这条判据必须靠改价事件来验。
    item = body["items"][0]
    changed = client.post(
        f"/api/merchant/transactions/{txn_no}/price-change",
        json={"item_id": item["id"], "final_unit_price_cents": max(1, item["original_unit_price_cents"] * 8 // 10)},
        headers={"X-Stall-Session": token},
    )
    assert changed.status_code == 200, changed.get_json()

    paid = client.post(
        f"/api/merchant/transactions/{txn_no}/payment",
        json={"method": "cash", "operator": "T-SIM-00"},
        headers={"X-Stall-Session": token, "Idempotency-Key": "tsim00-pay-1"},
    )
    assert paid.status_code == 200, paid.get_json()

    assert client.post("/api/admin/daily-aggregate", json={"business_date": day}).status_code == 200
    assert client.get("/api/admin/dashboard").get_json()["business_date"] == day
    assert (
        client.post(
            "/api/admin/settlements", json={"stall_no": "A-01", "period_start": day, "period_end": day}
        ).status_code
        == 201
    )
    assert client.get("/api/admin/metrics/usage", query_string={"business_date": day}).status_code == 200
    assert client.get("/api/customer/stalls/A-01/profile").get_json()["computed_at"].startswith(day)
    return token, txn_no
