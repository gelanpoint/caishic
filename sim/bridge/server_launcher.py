"""子进程拉起被测系统 `run.py` + 隔离数据目录 + 就绪探测（`T-SIM-07`）。

## 为什么这样切

`--mode=live` 的全部说服力在于"**这套设计真的能跑**"（`docs/sim-design.md` §2.1）。
因此被测系统必须以**真实服务进程**的形态起来 —— 端口占用检测、建库迁移、种子导入、
`business_date` 取时钟文件，全部走它自己的启动路径；本模块只负责**怎么把它安全地拉起来**：

1. **隔离数据目录**：`MT_DATA_DIR` 指向 `data/sim/<run_id>/` 的子目录，
   **绝不碰演示库**（§2.4 的"交叉污染检查"）；
2. **落在 `C:` 卷**：`docs/PROJECT-STATE.md` 的实测记录是「数据目录在 `D:` ⇒ 请求 p50 297ms /
   p95 770ms，根因是磁盘 fsync」。本模块**默认**在 `C:` 建目录，并在 `C:` 不可用时**如实退回并
   记录实际盘符**（判据⑤不许假装）。
3. **独立端口**：绑定 `127.0.0.1:0` 向内核要一个空闲端口再立刻释放 —— 与开发中正在跑的服务
   撞端口会让"live 跑不通"变成一个和仿真无关的环境问题。
4. **就绪探测**：轮询 `/healthz` 到 200 为止，**带超时上限**；进程早退（最常见的是端口占用，
   `run.py` 自己会返回 exit 1 并打印中文提示）时**立刻抛错并附日志尾部** ——
   否则调用方只会看到"启动超时"，看不出真因。

## 业务日推进：不另造一套

仿真**不修改**被测进程的环境变量（进程内改不了），而是按 `T-SIM-00` 已确立的接缝
写 `MT_CLOCK_FILE` 指向的文件（`app/clock.py` 每次调用重读 ⇒ 改文件即推进时间，
不重启服务、不新增端点）。`ClockFile` 就是这个接缝在仿真侧的唯一实现。

## 被测系统只读

`system_tree_digest()` 对 `app/**` 与 `specs/**` 取树指纹（路径 + 文件内容），
供运行前后比对 —— 判据⑥「live 模式只读系统」的机械证据。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

#: 仓库根（`sim/bridge/server_launcher.py` → `sim/bridge/` → `sim/` → 仓库根）
REPO_ROOT = Path(__file__).resolve().parents[2]

#: 启动脚本（被测系统入口）
RUN_PY = REPO_ROOT / "run.py"

#: 仿真数据目录的**默认盘根**：必须是 `C:`（见模块 docstring 第 2 条）
DEFAULT_DATA_ROOT = Path("C:/mt-sim")

#: `/healthz` 就绪探测：轮询间隔与超时上限（秒）
READY_POLL_INTERVAL_S = 0.25
READY_TIMEOUT_S = 180.0


class ServerLaunchError(RuntimeError):
    """拉起被测系统失败（进程早退 / 就绪超时 / 端口不可用）。

    **不复用 `OSError` / `TimeoutError`**：这两类在业务代码里常被"重试或忽略"顺手接住，
    而"被测系统没起来"必须让调用方当场知道。
    """


# ---------------------------------------------------------------------------
# 端口与数据目录
# ---------------------------------------------------------------------------
def free_port(host: str = "127.0.0.1") -> int:
    """向内核要一个当前空闲的端口（绑定 0 后立刻释放）。

    **不做端口探测后再重试的循环**：一次分配 + 一次就绪探测足够；真撞上了
    `run.py` 会自己拒绝并给出提示，本模块把那个提示原样带出去。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def resolve_data_dir(run_id: str, explicit: Path | str | None = None) -> tuple[Path, str]:
    """返回 `(数据目录, 选取理由)`。

    * `explicit` 给了就用它（**如实记录它的盘符**，判据⑤据此判定是否降级）；
    * 否则优先 `C:/mt-sim/<run_id>`；`C:` 不可写时退回系统临时目录并**在理由里说清**。

    目录名只允许 `[A-Za-z0-9._-]`，其余字符会让"多个 run 并存"时目录互相串。
    """
    if explicit is not None:
        target = Path(explicit)
        target.mkdir(parents=True, exist_ok=True)
        return target, "显式指定"

    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in run_id)
    candidate = DEFAULT_DATA_ROOT / safe
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".writable"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        import tempfile

        fallback = Path(tempfile.gettempdir()) / "mt-sim" / safe
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback, f"`C:` 不可写（{exc}）⇒ 退回系统临时目录（**判据⑤按此盘符判定**）"
    return candidate, "`C:` 卷（性能预算要求；见 `docs/sim-design.md` §2.6）"


# ---------------------------------------------------------------------------
# 业务日时钟（`MT_CLOCK_FILE` 接缝，`REQ-033`）
# ---------------------------------------------------------------------------
class ClockFile:
    """写 `MT_CLOCK_FILE` 指向的文件以推进业务日。

    **原子替换**（先写 `.tmp` 再 `os.replace`）：被测进程**每次调用都重读**该文件
    （`app/clock.py::_read_injected`），写到一半被读到会抛 `ClockSourceError` ——
    那是对的（宁可响亮报错也不要静默落到墙钟），但仿真自己不该制造这种噪声。
    """

    ENV_NAME = "MT_CLOCK_FILE"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def set(self, business_date: str, moment: str = "07:30:00") -> str:
        """把业务日设为 `business_date`（时刻固定在早市开始前，便于时间戳可读）。"""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(f"{business_date} {moment}\n", encoding="utf-8", newline="\n")
        os.replace(tmp, self.path)
        return f"{business_date} {moment}"


# ---------------------------------------------------------------------------
# 服务句柄
# ---------------------------------------------------------------------------
@dataclass
class ServerHandle:
    """已就绪的被测服务进程；`stop()` 幂等。"""

    process: subprocess.Popen
    host: str
    port: int
    data_dir: Path
    clock: ClockFile
    log_path: Path
    ready_elapsed_s: float
    data_dir_reason: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def tail_log(self, lines: int = 40) -> str:
        """日志尾部 —— 启动失败时**必须**带出去，否则调用方只看到"超时"。"""
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:  # pragma: no cover - 日志文件不该读不到
            return f"（日志读不到：{self.log_path}）"
        return "\n".join(content[-lines:])

    def stop(self, timeout_s: float = 15.0) -> None:
        """停服务；先 `terminate`，超时再 `kill`（演示机/测试机上不能留孤儿进程）。"""
        if self.process.poll() is not None:
            return
        self.process.terminate()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return
            time.sleep(0.1)
        self.process.kill()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - kill 之后极少再超时
            pass


def _probe_healthz(host: str, port: int, deadline: float, process: subprocess.Popen,
                   log_path: Path) -> float | None:
    """轮询 `/healthz` 到 200 为止；返回耗时秒。进程早退则返回 `None`（由调用方报错）。"""
    started = time.monotonic()
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return None
        connection = http.client.HTTPConnection(host, port, timeout=3.0)
        try:
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            body = response.read()
            if response.status == 200:
                payload = json.loads(body.decode("utf-8"))
                if payload.get("status") == "ok":
                    return time.monotonic() - started
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(READY_POLL_INTERVAL_S)
    return None


def launch_server(
    *,
    run_id: str,
    out_dir: Path | str,
    data_dir: Path | str | None = None,
    business_date: str,
    host: str = "127.0.0.1",
    port: int | None = None,
    timeout_s: float = READY_TIMEOUT_S,
    clock_file: Path | str | None = None,
) -> ServerHandle:
    """拉起 `run.py` 并等到 `/healthz` 就绪；失败时抛 `ServerLaunchError`（附日志尾部）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    resolved_dir, reason = resolve_data_dir(run_id, data_dir)
    clock_path = Path(clock_file) if clock_file is not None else (out / "clock.txt")
    clock = ClockFile(clock_path)
    #: **必须在起进程之前写**：种子导入按 `app/seed.py::_resolve_business_dates` 取时钟，
    #: 于是种子价目表落在 [start-1, start]，第 1 个营业日的「复制上一营业日」才有数据可复制。
    clock.set(business_date)

    listen_port = int(port) if port else free_port(host)
    log_path = out / "server.log"
    env = dict(os.environ)
    env.update({
        "MT_DATA_DIR": str(resolved_dir),
        "MT_HOST": host,
        "MT_PORT": str(listen_port),
        ClockFile.ENV_NAME: str(clock_path),
    })

    with log_path.open("w", encoding="utf-8", newline="\n") as log_file:
        process = subprocess.Popen(
            [sys.executable, str(RUN_PY), "--host", host, "--port", str(listen_port)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    handle = ServerHandle(
        process=process, host=host, port=listen_port, data_dir=resolved_dir, clock=clock,
        log_path=log_path, ready_elapsed_s=0.0, data_dir_reason=reason,
    )
    elapsed = _probe_healthz(host, listen_port, time.monotonic() + timeout_s, process, log_path)
    if elapsed is None:
        exit_code = process.poll()
        handle.stop()
        detail = (
            f"被测进程已退出（exit={exit_code}）—— 端口占用是 `run.py` 自己拒绝启动的最常见原因"
            if exit_code is not None
            else f"`/healthz` 在 {timeout_s:.0f}s 内没有就绪"
        )
        raise ServerLaunchError(
            f"{detail}（data_dir={resolved_dir}，port={listen_port}）。\n---- 服务日志尾部 ----\n{handle.tail_log()}"
        )
    handle.ready_elapsed_s = elapsed
    return handle


# ---------------------------------------------------------------------------
# 被测系统树指纹（判据⑥：live 模式只读系统）
# ---------------------------------------------------------------------------
def system_tree_digest(repo_root: Path | str = REPO_ROOT,
                       subtrees: tuple[str, ...] = ("app", "specs")) -> dict:
    """对 `app/**`、`specs/**` 取树指纹：`{子树: sha256}`（路径 + 内容一起进哈希）。

    **为什么不逐文件记哈希**：指纹要能回答"运行前后有没有变"，一个汇总值就够，
    逐文件清单会让报告体膨胀却没有额外判别力。
    """
    root = Path(repo_root)
    digests: dict[str, str] = {}
    for name in subtrees:
        base = root / name
        hasher = hashlib.sha256()
        files = sorted(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        for path in files:
            hasher.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(hashlib.sha256(path.read_bytes()).digest())
        digests[name] = hasher.hexdigest()
        digests[f"{name}_files"] = len(files)
    return digests
