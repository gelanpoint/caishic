"""端到端走查的**共享夹具**（`T-031` 建立；把 `T-030` 的 `live_server` 从单文件里提出来收敛成一处）。

为什么要有这个文件：`T-030`~`T-034` 五个走查文件都要**真起一个服务**、**真开浏览器**、**真截图**。
每个文件各写一套夹具 = 同一个"服务怎么起、日志往哪写、什么算就绪"的规则写五遍 —— 那就是下次漂移的种子
（`AGENTS.md`：「同一个值或规则在多处各写一遍，就是下次漂移的种子；改的时候合成一处」）。
故夹具只在这里定义一次。

两条**踩过的坑**（不许再犯，见 `docs/PROJECT-STATE.md` 变更记录）：

1. **服务端输出必须落文件，绝不能用 `subprocess.PIPE`**：上一版用 `stdout=subprocess.PIPE` 且没人读，
   werkzeug 的请求日志填满 64KB 管道缓冲后**服务端就阻塞在写日志上** —— 表现是"前几个用例正常、
   后面的用例永远卡住"，极易被误判成"页面点不动"（那是**夹具死锁**，不是产品缺陷）。
   落到文件还有一个好处：启动横幅能读回来做断言（`AGENTS.md` §3 硬要求 1/2/5 的证据就来自它）。
2. **"就绪"必须问服务端**（轮询 `/healthz`），不能 `sleep` 一个猜出来的秒数。

夹具/助手一览：

- `start_live_server(...)`：用仓库的 `run.py` 真起一个进程（独立 `MT_DATA_DIR`，**绝不碰演示库**）；
- `live_server`：模块级服务（走查主线用）；`fault_server`：故障注入专用的**独立**服务（`T-031` 用）；
- `browser`：真实 Chromium（Playwright）；`shots`：截图目录（`.pytest-tmp/e2e-shots/`，已 gitignore）；
- `new_page` / `snap` / `http_json` / `connect_db` / `bind_stall` / `stage_payload`：各走查文件共用的动作。

**Playwright 缺失时的纪律**：`browser` 夹具**跳过并说明"这不是通过，是没检查"**，
绝不静默假装走过查（`AGENTS.md`：「skip 必须标注'这不是通过，是没检查'」）。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

# 证据要能在**任何控制台**下打出来：Windows 默认 GBK，遇到 `¥` 这类字符会直接
# `UnicodeEncodeError` 把用例判红（**检查本身成了故障源** —— `T-030` 实测就是这么挂的）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[1]
SHOTS_DIR = REPO_ROOT / ".pytest-tmp" / "e2e-shots"

#: 演示数据里已知的第一个摊位号（`app/seed_data/seed.json`）
SEED_STALL = "A-01"


# ---------------------------------------------------------------------------
# 真起一个服务（不用 Flask test client：走查要证明的是"真进程 + 真 HTTP"）
# ---------------------------------------------------------------------------


def free_port() -> int:
    """让内核分配一个空闲端口（避免与演示服务/其它用例抢 8000）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveServer:
    """一个真的 `run.py` 服务进程 + 它的日志与数据目录。"""

    def __init__(self, base: str, port: int, proc: subprocess.Popen, log_path: Path,
                 data_dir: Path, handle) -> None:
        self.base = base
        self.port = port
        self.proc = proc
        self.log_path = log_path
        self.data_dir = data_dir
        self._handle = handle

    # -- 观测 ---------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "market_trade.sqlite3"

    def log_text(self) -> str:
        """启动横幅与运行日志（先 flush 再读，避免读到半截）。"""
        self._handle.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    # -- 调用 ---------------------------------------------------------------
    def api(self, method: str, path: str, body: Any = None, headers: dict | None = None):
        """返回 `(状态码, JSON体)`；**4xx/5xx 不抛异常**（错误路径正是要断言的东西）。"""
        return http_json(self.base, method, path, body=body, headers=headers)

    def connect_db(self) -> sqlite3.Connection:
        """直连这台服务的库（用于布置前置条件与"没有产生错误数据"的核对）。"""
        return connect_db(self.db_path)

    # -- 生命周期 -----------------------------------------------------------
    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._handle.close()


def start_live_server(data_dir: Path, *, port: int | None = None,
                      env_extra: dict | None = None, wait: bool = True) -> LiveServer:
    """起一个真服务（`python run.py --port N`），等它 `/healthz` 通了再返回。

    `wait=False` 用于**故意制造启动失败**的场景（如 `T-033` 的端口占用检测），
    此时调用方自己 `proc.wait()` 看退出码与提示。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    port = port or free_port()
    env = {**os.environ, "MT_DATA_DIR": str(data_dir), **(env_extra or {})}
    log_path = data_dir / "server.log"
    handle = log_path.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "run.py", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    server = LiveServer(f"http://127.0.0.1:{port}", port, proc, log_path, data_dir, handle)
    if wait:
        try:
            wait_until_healthy(server)
        except AssertionError:
            server.stop()
            raise
    return server


def wait_until_healthy(server: LiveServer, timeout: float = 60.0) -> None:
    """轮询 `/healthz` 直到 200；进程先退出则把日志一起报出来（不猜原因）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server.proc.poll() is not None:
            raise AssertionError(
                f"服务启动即退出（exit {server.proc.returncode}）：\n{server.log_text()}"
            )
        try:
            with urllib.request.urlopen(server.base + "/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.4)
    server.stop()
    raise AssertionError(f"服务 {timeout:.0f} 秒内未就绪：\n{server.log_text()}")


# ---------------------------------------------------------------------------
# HTTP / 库 助手
# ---------------------------------------------------------------------------


def http_json(base: str, method: str, path: str, body: Any = None,
              headers: dict | None = None, timeout: float = 20.0):
    """发一个 JSON 请求 → `(状态码, JSON体或 None)`。**错误状态不抛异常**（错误路径要断言）。"""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8")
        status = error.code
    payload = None
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"_raw": raw}
    return status, payload


def connect_db(db_path: Path) -> sqlite3.Connection:
    """只读用途的连接（行按列名取用）。"""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """`SELECT COUNT(*)` 的便捷形态（"没有产生错误数据"的断言靠它逐项核对）。"""
    return int(conn.execute(sql, params).fetchone()[0])


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def error_code(payload) -> str | None:
    """从统一错误响应体里取错误码（契约 §1.2）。"""
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return payload["error"].get("code")
    return None


# ---------------------------------------------------------------------------
# 秤端常用动作（各走查文件共用；只依赖契约 §2/§3 定义的字段）
# ---------------------------------------------------------------------------


def bind_stall(server: LiveServer, stall_no: str = SEED_STALL, key: str | None = None) -> str:
    """契约 §3.2：绑定摊位，返回 `session_token`。"""
    status, payload = server.api("POST", "/api/merchant/session", {"stall_no": stall_no},
                                 {"Idempotency-Key": key or f"bind-{stall_no}-{time.time_ns()}"})
    assert status == 201, f"契约 §3.2 期望 201，实际 {status}：{payload}"
    token = payload["session_token"]
    assert isinstance(token, str) and token, f"契约 §3.2 要求 session_token，实际 {token!r}"
    return token


def session_headers(token: str, key: str | None = None) -> dict[str, str]:
    headers = {"X-Stall-Session": token}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def active_products(server: LiveServer, token: str) -> list[dict]:
    """契约 §3.5：本摊位可售商品（仅 `active`）。"""
    status, payload = server.api("GET", "/api/merchant/products", headers=session_headers(token))
    assert status == 200, f"契约 §3.5 期望 200，实际 {status}：{payload}"
    products = [item for item in payload if item.get("status") == "active"]
    assert products, "种子数据应给每摊位若干在售商品，实际为空"
    return products


def create_transaction(server: LiveServer, token: str, items: list[dict], key: str,
                       expect: int = 201):
    """契约 §3.6：创建并计价。返回 `(状态码, 响应体)`。"""
    return server.api(
        "POST", "/api/merchant/transactions",
        {"items": items, "client_idempotency_key": key},
        session_headers(token, key),
    )


def today_iso() -> str:
    from datetime import date

    return date.today().isoformat()


def evidence_key(prefix: str) -> str:
    """每次调用生成一个**新的**幂等键。

    走查里为什么要这样：幂等键复用会返回**首次结果**（契约 §1.3），于是"第二次请求其实没执行"
    与"第二次请求正确幂等"在响应上长得一模一样 —— 用固定键写异常用例会自己把检查废掉。
    """
    return f"e2e-{prefix}-{time.time_ns()}"


def assert_api_error(status: int, payload, code: str, http: int, where: str) -> dict:
    """断言"就是契约 §4 的那条错误"，且响应体符合契约 §1.2 的统一错误格式。"""
    assert status == http, f"{where}：契约期望 HTTP {http}，实际 {status}：{payload}"
    assert error_code(payload) == code, f"{where}：期望错误码 {code}，实际 {error_code(payload)}：{payload}"
    assert isinstance(payload, dict) and set(payload) == {"error"}, f"{where}：顶层键必须恰好为 error：{payload}"
    error = payload["error"]
    assert isinstance(error.get("message"), str) and error["message"].strip(), f"{where}：message 不得为空"
    assert set(error) <= {"code", "message", "detail"}, f"{where}：error 含契约 §1.2 之外的键：{sorted(error)}"
    return error


def stall_id(server: LiveServer, stall_no: str) -> int:
    """摊位号 → 库里的 `stall.id`（用于直连库核对"没有产生错误数据"）。"""
    conn = server.connect_db()
    try:
        value = scalar(conn, "SELECT id FROM stall WHERE stall_no = ?", (stall_no,))
    finally:
        conn.close()
    assert value is not None, f"种子数据里应有摊位 {stall_no}"
    return int(value)


def reconciliation(server: LiveServer, stall_no: str, business_date: str | None = None) -> dict:
    """契约 §3.29 对账等式（默认只算**这一个摊位当天**，避免与其它用例相互干扰）。"""
    day = business_date or today_iso()
    status, payload = server.api(
        "GET", f"/api/admin/reconciliation?business_date={day}&stall_no={stall_no}"
    )
    assert status == 200, f"契约 §3.29 期望 200，实际 {status}：{payload}"
    return payload


def create_priced(server: LiveServer, token: str, product_id: int, key: str, weight_grams: int = 1000) -> dict:
    """契约 §3.6：创建一笔已计价交易（断言 201 + `status = priced`）。"""
    status, payload = create_transaction(
        server, token, [{"product_id": product_id, "weight_grams": weight_grams}], key
    )
    assert status == 201, f"契约 §3.6 期望 201，实际 {status}：{payload}"
    assert payload.get("status") == "priced", f"契约 §3.6 要求 status=priced，实际 {payload.get('status')!r}"
    return payload


def pay_transaction(server: LiveServer, token: str, transaction_no: str, body: dict, key: str):
    """契约 §3.10：确认收款（现金 200 / 收款码 202）。返回 `(状态码, 响应体)`。"""
    return server.api(
        "POST", f"/api/merchant/transactions/{transaction_no}/payment", body, session_headers(token, key)
    )


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


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


def new_page(browser):
    """统一视口与超时：默认 15s 足够（超时即"点不动"的真信号，不要靠调大超时掩盖）。"""
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    page.set_default_timeout(15000)
    return page


def snap(page, shots: Path, name: str) -> str:
    """截图留证（证据落 `.pytest-tmp/e2e-shots/`，gitignore 已忽略）。"""
    path = Path(shots) / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"[截图] {path}")
    return str(path)
