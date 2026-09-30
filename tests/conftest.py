"""端到端走查的 **pytest 夹具**（`T-031` 建立；助手已按语义拆到 `tests/e2e_support.py`）。

夹具只有四个，`T-030`~`T-034` 的走查文件共用：

- `live_server`：用仓库的 `run.py` 真起一个进程（独立 `MT_DATA_DIR`，**绝不碰演示库**），模块级；
- `fault_server`：**故障注入**专用服务（故意改库结构，必须与主线隔离）；
- `browser`：真实 Chromium（Playwright）——缺失时**跳过并说明"这不是通过，是没检查"**；
- `shots`：截图目录（`.pytest-tmp/e2e-shots/`，已 gitignore）。

助手（`start_live_server` / `bind_stall` / `assert_api_error` / `reconciliation` / `half_up_div` …）
在 `tests/e2e_support.py`；本文件把它们**再导出**一次，故 `from conftest import ...` 依然可用。
"""

from __future__ import annotations

import pytest

from e2e_support import (  # noqa: F401  （再导出，保持既有 `from conftest import ...` 可用）
    LiveServer,
    REPO_ROOT,
    SEED_STALL,
    SHOTS_DIR,
    active_products,
    assert_api_error,
    bind_stall,
    connect_db,
    count,
    create_priced,
    create_transaction,
    ensure_commission_rule,
    error_code,
    evidence_key,
    free_port,
    half_up_div,
    http_json,
    item_amount,
    new_page,
    pay_transaction,
    ratio_bp,
    reconciliation,
    scalar,
    session_headers,
    snap,
    stall_id,
    start_live_server,
    today_iso,
    wait_until_healthy,
)


@pytest.fixture(scope="module")
def live_server(tmp_path_factory) -> LiveServer:
    """走查主线用的真服务（模块级；独立数据目录，退出时收干净）。"""
    server = start_live_server(tmp_path_factory.mktemp("mt-e2e-data"))
    yield server
    server.stop()


@pytest.fixture(scope="module")
def fault_server(tmp_path_factory) -> LiveServer:
    """**故障注入**专用服务（`T-031`）：故意把库写坏/改表结构，故必须与主线服务隔离 ——
    否则"上一次故障注入"会变成"下一次主线失败"的假凶手。"""
    server = start_live_server(tmp_path_factory.mktemp("mt-e2e-fault"))
    yield server
    server.stop()


@pytest.fixture(scope="module")
def browser():
    """真实 Chromium。缺失 Playwright 时**跳过并说明这不是通过，是没检查**。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - 取决于开发机环境
        pytest.skip(
            "环境里没有 Playwright，无法做真实浏览器走查 —— **这不是通过，是没检查**"
            "（安装：python -m pip install playwright && python -m playwright install chromium；"
            "注意它属开发期工具，不进演示机）"
        )
    with sync_playwright() as play:
        try:
            instance = play.chromium.launch()
        except Exception as error:  # pragma: no cover - 取决于开发机环境
            pytest.skip(
                f"Chromium 起不来（{error}）—— **这不是通过，是没检查**"
                "（安装：python -m playwright install chromium）"
            )
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def shots() -> Path:
    """截图目录（仓库内但被 `.gitignore` 忽略，避免"为了留证据而污染仓库"）。"""
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    return SHOTS_DIR
