"""端到端走查的**共享助手**（`T-031` 起从 `tests/conftest.py` 按语义拆出）。

为什么拆：`quality-gates.md` §1.2 规定单文件 ≤400 行。拆法不是对半切，而是按**语义**——
`conftest.py` 只留 pytest 夹具（框架装载的东西），本模块放**普通函数与常量**（服务生命周期、
HTTP/库助手、以及按 `data-model.md` §0/§5.1 口径的**独立复算**公式），任何走查文件都能直接导入。

两条踩过的坑写在本模块最显眼处（详见 `docs/PROJECT-STATE.md` 变更记录）：

1. **服务端输出必须落文件，绝不能用 `subprocess.PIPE`**：没人读的管道会被 werkzeug 的请求日志
   填满 64KB 后**阻塞服务端**，表现是"前几个用例正常、后面的永远卡住"，极易被误判成"页面点不动"
   —— 那是**夹具死锁**，不是产品缺陷。落文件还能把启动横幅读回来做断言（`AGENTS.md` §3 硬要求 1/2/5）。
2. **"就绪"必须问服务端**（轮询 `/healthz`），不能 `sleep` 一个猜出来的秒数。
"""

from __future__ import annotations

import json
import os
import re
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


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


REPO_ROOT = Path(__file__).resolve().parents[1]


SHOTS_DIR = REPO_ROOT / ".pytest-tmp" / "e2e-shots"


SEED_STALL = "A-01"


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
                      env_extra: dict | None = None, wait: bool = True,
                      entry: str = "run.py", env_drop: set[str] | None = None) -> LiveServer:
    """起一个真服务（`python <entry> --port N`），等它 `/healthz` 通了再返回。

    `wait=False` 用于**故意制造启动失败**的场景（如 `T-033` 的端口占用检测），
    此时调用方自己 `proc.wait()` 看退出码与提示。

    `entry` 默认是 `run.py`；`CP-D` 现场验收后新增 `scripts/launch.py` 这条**演示启动路径**
    （`start.bat` / `start.sh` 实际调的就是它），故它也要能被当作真服务起起来测 ——
    否则"双击能起来"这件事又只剩人工走查。

    `env_drop` 用于**先清掉指定环境变量再起服务**（如 `{"PYTHONUTF8", "PYTHONIOENCODING"}`）：
    验收基线必须是"没有任何编码相关变量"的环境，否则又是"靠环境变量才通过"（`CP-D` 教训）。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    port = port or free_port()
    env = {**os.environ, "MT_DATA_DIR": str(data_dir), **(env_extra or {})}
    for name in (env_drop or set()):
        env.pop(name, None)
    log_path = data_dir / "server.log"
    handle = log_path.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, entry, "--port", str(port)],
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


def half_up_div(numerator: int, denominator: int) -> int:
    """整数四舍五入除法 —— **判据来自 `data-model.md` §0 的取整口径**（不是抄实现）。

    走查要能独立复算服务端的数：用**同一份口径**重新算一遍，再比对。
    """
    return (numerator + denominator // 2) // denominator


def ratio_bp(numerator: int, denominator: int) -> int:
    """万分比整数（`data-model.md` §5.1 的显示口径）：分母为 0 → 0（没有分母就没有比率）。"""
    return 0 if denominator <= 0 else half_up_div(numerator * 10_000, denominator)


def item_amount(unit_price_cents: int, weight_grams: int) -> int:
    """明细金额独立复算（`data-model.md` §0 / §2.9：金额 = 四舍五入(单价 × 克 ÷ 1000)）。"""
    return half_up_div(unit_price_cents * weight_grams, 1000)


def ensure_commission_rule(server: LiveServer, rate_bp: int = 250, pay_object: str = "merchant") -> dict:
    """确保"当日有一条生效佣金口径"（契约 §3.24），返回**当前生效**的那条规则。

    为什么要容忍 `MT-1012`：`PUT` 是"新建一条规则"，同一天重复新建会撞上生效期重叠
    （这正是 `§3.24` 声明的行为）。走查要的是"有一条规定了费率"，故撞上就回查既有规则 ——
    这不是放宽检查，而是**不把"测试自己重复建规则"当成产品缺陷**。
    """
    status, payload = server.api(
        "PUT", "/api/admin/commission-rules",
        {"pay_object": pay_object, "rate_bp": rate_bp, "effective_from": today_iso()},
    )
    if status == 200:
        return payload
    assert status == 409 and error_code(payload) == "MT-1012", (
        f"契约 §3.24 期望 200 或 409 MT-1012，实际 {status}：{payload}"
    )
    status, rules = server.api("GET", "/api/admin/commission-rules")
    assert status == 200, f"契约 §3.23 期望 200，实际 {status}：{rules}"
    day = today_iso()
    live = [
        rule for rule in rules
        if rule["effective_from"] <= day and (rule.get("effective_to") or "9999-12-31") >= day
    ]
    assert live, f"当日应有生效佣金口径，实际 {rules}"
    return sorted(live, key=lambda rule: rule["effective_from"])[-1]


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


def pending_count(page) -> int:
    """读秤端界面上的待补传条数（`#pending` 的显示形态：`N / 阈值 …`）——**夹具总定义处只有这一份**。

    ⚠️ **`#pending` 的初始值是 `—`（不是 0）**，要等 `refreshOffline()` 那次 GET 回来才变成
    `N / 阈值 …`。因此**直接调用本函数有竞态**：读早了会拿到 `—`。
    早先的实现是裸 `int(...)`，于是抛 `ValueError: invalid literal for int() with base 10: '—'`
    —— 一个**看起来像随机失败**的报错（`2026-10-02` 全量跑 4 次红 1 次，同批 `p99` 达 3172ms，
    机器过载时 GET 更慢、更容易撞上）。
    现在改成**带上下文的断言失败**，并且调用方应优先用 `wait_pending` / `wait_pending_ready` 先等异步落定。
    """
    raw = page.inner_text("#pending").split("/")[0].strip()
    assert raw.isdigit(), (
        f"`#pending` 还不是数字（读到 {raw!r}）—— 它要等 `refreshOffline()` 的 GET 回来。"
        "请先用 `wait_pending_ready()` / `wait_pending()` 等异步落定，不要直接读。"
    )
    return int(raw)


def wait_pending_ready(page, where: str, timeout: int = 5000) -> int:
    """等 `#pending` **从初始的 `—` 变成真实数字**（即 `refreshOffline()` 已落定），返回该数字。

    这是"自己的异步没等"的**正解**：把"等一个精确值"拆成"先等它变成数字"（本函数）
    与"再等它等于期望值"（`wait_pending`）两步，报错时能区分"没刷新"与"值不对"。
    """
    try:
        page.wait_for_function(
            "() => /^\\d+\\s*\\//.test(document.querySelector('#pending').textContent.trim())",
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - 超时要转成带上下文的断言失败
        raise AssertionError(
            f"{where}：`#pending` 在 {timeout}ms 内仍是 {page.inner_text('#pending')!r}"
            "（未变成 `N / 阈值`，说明 `refreshOffline()` 的 GET 一直没回来）"
        ) from None
    return pending_count(page)


def wait_pending(page, expected: int, where: str) -> int:
    """等 `#pending` **恰好等于** `expected`，并把它作为断言返回。

    为什么必须等（`T-030`/`T-033` 实测都撞到过）：`#pending` 来自 `§3.13` 的一次 GET
    （`refreshOffline()` 的 promise），而 `#offlineNote` / `#syncResult` 是暂存/补传响应回来后
    **同步**写上的 —— 两个数字由**两条不同的异步链**更新，立刻读会撞上"提示已更新、状态还没回来"
    的瞬间（实测：提示写着「待补传 1 笔」、`#pending` 还是 0）。那是**走查自己没等异步**，不是产品缺陷。

    等一个**精确值**是**收紧**而不是放宽：值不对就超时报错，报错里同时给出两个数，
    便于判断是"没暂存"还是"没刷新"。
    """
    try:
        page.wait_for_function(
            "n => parseInt(document.querySelector('#pending').textContent.split('/')[0], 10) === n",
            arg=expected,
            timeout=5000,
        )
    except Exception:  # noqa: BLE001 - 超时是主信号，但要把它转成带上下文的断言失败
        raise AssertionError(
            f"{where}：`#pending` 在 5 秒内未达到 {expected}（实际原文 {page.inner_text('#pending')!r}；"
            f"界面提示 {page.inner_text('#offlineNote')!r}）"
        ) from None
    observed = pending_count(page)
    assert observed == expected, f"{where}：`#pending` 应为 {expected}，实际 {observed}"
    return observed
