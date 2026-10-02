"""演示启动路径的回归检查（`start.bat` / `start.sh` → `scripts/launch.py`）。

**为什么有这份文件**：`CP-D` 现场验收发现"按现场方式启动"这条路**没有任何自动化检查** ——
`pytest -q` 全绿、`run.py` 直跑正常，都**证明不了双击 `start.bat` 能起来**。
于是这里把三件当场要命的事钉成用例：

1. `start.bat` **必须保持纯 ASCII**（并保持 CRLF）：
   `cmd.exe` 是按**控制台代码页**解析 `.bat` 的**字节**的，中文一旦被拆断，
   现场看到的是 `'…' 不是内部或外部命令` / `" was unexpected at this time.`，
   而在 `if ... ( ... )` 块里出现时，**真正该看的提示（如"端口已被占用"）会被顶掉**。
   所以中文一律交给 `scripts/launch.py`（Python 自己管编码）。这条用例就是防止它被改回去。
2. 演示启动路径**真的能起来**（`scripts/launch.py` 起真服务 + `/healthz` 200 + 横幅四要素）。
3. **端口被占用时给出清晰中文提示**，且输出里**不得出现 cmd 的解析错误特征串** ——
   这正是现场那次失败的形态（提示被解析错误顶掉）。
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

from e2e_support import REPO_ROOT, start_live_server

START_BAT = REPO_ROOT / "start.bat"
START_SH = REPO_ROOT / "start.sh"
LAUNCHER = REPO_ROOT / "scripts" / "launch.py"

#: cmd.exe 解析被破坏时的特征串（现场那次失败就是这些，而不是"端口被占用"）
CMD_PARSE_ERRORS = re.compile(
    r"不是内部或外部命令|is not recognized as an internal or external command"
    r"|was unexpected at this time|命令语法不正确"
)


def test_start_bat_stays_pure_ascii_and_crlf():
    """`start.bat` 一个非 ASCII 字节都不许有；行尾必须 CRLF（两者都是 cmd 的硬约束）。

    **这条能真的失败**：往里加一个中文字符（或让编辑器改成 LF）它就红 —— 见本文件 docstring。
    """
    raw = START_BAT.read_bytes()
    offenders = [(index, byte) for index, byte in enumerate(raw) if byte > 127]
    assert not offenders, (
        f"start.bat 出现了 {len(offenders)} 个非 ASCII 字节（前几个：{offenders[:5]}）。"
        "cmd.exe 按控制台代码页解析 .bat 的字节，中文会被拆断并变成"
        "「不是内部或外部命令」这类解析错误 —— 中文请加到 scripts/launch.py 里。"
    )
    bare_lf = raw.count(b"\n") - raw.count(b"\r\n")
    assert bare_lf == 0, f"start.bat 有 {bare_lf} 个裸 LF 行尾：cmd.exe 在多行 if 块里会解析失败"
    assert raw.count(b"\r\n") > 20, f"start.bat 只有 {raw.count(b'\r\n')} 行？文件是不是被截断了"


def test_demo_launcher_starts_a_real_service(tmp_path):
    """`scripts/launch.py`（双击 `start.bat` 真正执行的东西）必须能起服务并打印横幅四要素。"""
    server = start_live_server(tmp_path / "launcher-data", entry="scripts/launch.py")
    try:
        status, payload = server.api("GET", "/healthz")
        assert status == 200 and payload["status"] == "ok", f"/healthz 异常：{status} {payload}"
        log = server.log_path.read_text(encoding="utf-8", errors="replace")
        for expected in ("已启动", "入口 1 · 操作端", "入口 2 · 顾客扫码页", "本机 IP", "数据文件"):
            assert expected in log, f"启动横幅缺少 `{expected}`：\n{log[:1200]}"
        assert not CMD_PARSE_ERRORS.search(log), f"启动输出里出现 cmd 解析错误：\n{log[:1200]}"
    finally:
        server.stop()


def test_port_conflict_shows_chinese_hint_not_parse_errors(tmp_path):
    """端口被占用时必须看到**清晰中文提示**，而不是一串"不是内部或外部命令"。

    判据（现场验收原话）：把端口占住后跑启动脚本，看到的是提示，不是解析错误。
    """
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(4)
    busy_port = blocker.getsockname()[1]
    try:
        proc = subprocess.run(
            [sys.executable, str(LAUNCHER), "--port", str(busy_port)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            env={**os.environ, "PYTHONUTF8": "1", "MT_DATA_DIR": str(tmp_path / "busy")},
        )
    finally:
        blocker.close()

    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0, f"端口被占用时不应返回 0：\n{output}"
    assert f"端口 {busy_port} 已被占用" in output, f"缺少明确的占用提示：\n{output}"
    assert f"--port {busy_port + 1}" in output, f"缺少「换哪个端口」的具体建议：\n{output}"
    assert not CMD_PARSE_ERRORS.search(output), f"出现 cmd 解析错误（提示被顶掉了）：\n{output}"
