"""端到端走查的 **pytest 夹具**（`T-031` 建立；助手已按语义拆到 `tests/e2e_support.py`）。

夹具只有四个，`T-030`~`T-034` 的走查文件共用：

- `live_server`：用仓库的 `run.py` 真起一个进程（独立 `MT_DATA_DIR`，**绝不碰演示库**），模块级；
- `fault_server`：**故障注入**专用服务（故意改库结构，必须与主线隔离）；
- `browser`：真实 Chromium（Playwright）——缺失时**跳过并说明「这不是通过，是没检查」**；
- `shots`：截图目录（`.pytest-tmp/e2e-shots/`，已 gitignore）。

助手的**唯一导入名**是 `tests/e2e_support.py`（不是 `conftest`）：
本文件与 `tests/contract/conftest.py` **同名**，而两边目录都会进 `sys.path`，
用例里写 `from conftest import ...` 时解析到哪一个取决于收集顺序 ——
`python -m pytest -q` 会直接 10 个文件 `ImportError`（`T-035` 收尾时实测，连用例都收不起来）。
故：用例一律 `from e2e_support import ...`（门禁阈值另见 `tests/gates.py`），
本文件**不再**做再导出（再导出就等于给「按裸名导入 conftest」留后门）。
"""

from __future__ import annotations

import pytest

from e2e_support import SHOTS_DIR, LiveServer, start_live_server


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
def shots():
    """截图目录（仓库内但被 `.gitignore` 忽略，避免"为了留证据而污染仓库"）。"""
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    return SHOTS_DIR
