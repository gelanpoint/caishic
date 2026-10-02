"""仓库根 `conftest.py`：**给每次 pytest 会话一个唯一的 basetemp**（父代理 `2026-10-02` 裁定）。

## 为什么需要它

原先 `pytest.ini` 写死 `--basetemp=.pytest-tmp`。这个选择本身是对的（当初是为了绕开
Windows 上 `cleanup_numbered_dir()` 对失效目录符号链接调 `unlink()` 返回
`WinError 5 拒绝访问` 导致整个进程以退出码 1 结束的问题，见 `pytest.ini` 的注解）。

但**写死一个共享目录**带来第二个问题：pytest 在**会话开始时整体重建** basetemp，
而多会话/并行 Agent 同时跑时，上一个人（或刚退出的进程）的文件句柄还没释放，
重建就会撞上：

```
PermissionError: [WinError 32] 另一个程序正在使用此文件，进程无法访问。
  '...\\.pytest-tmp\\mt-e2e-data0\\market_trade.sqlite3'
```

实测表现是 `ERROR at setup`（用例连跑都没跑起来），**看起来像"偶发红"** ——
与 `Q-19` 的"退化状态"同一类问题：**一个偶尔红的用例会让整套测试失去可信度**。

## 做法

**仍在仓库内**（保持原修复的意图），只是每次会话拿到独立子目录
`.pytest-tmp/session-<pid>-<8位随机>`。于是：

* 会话开始时的重建只动**自己的**空目录 ⇒ 不会撞上别人的句柄；
* 因为显式指定了 basetemp，pytest 走 `given_basetemp` 分支，**不创建也不清理
  `pytest-current` 目录符号链接** ⇒ 原来的 `WinError 5` 不会回来（有实测证据）。

调用方若**显式**指定了 `--basetemp`（例如临时排查），这里一律尊重、不覆盖。

`e2e-shots/` 是**证据目录**（多份文档与报告引用 `.pytest-tmp/e2e-shots/` 这个路径），
故**不随会话变化**，也**绝不被清理**。
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

#: 会话目录的父目录（`.gitignore` 已忽略 `.pytest-tmp/`）
BASETEMP_ROOT = REPO_ROOT / ".pytest-tmp"

#: 证据目录：**不随会话变化、不清理**（文档与报告引用这个固定路径）
EVIDENCE_DIR_NAME = "e2e-shots"

#: 会话目录前缀（用于区分"我们创建的"与"别人的"）
SESSION_PREFIX = "session-"

#: 超过这个秒数的会话目录视为陈旧、可best-effort 清理（1 天）
STALE_AFTER_SECONDS = 24 * 3600


def _prune_stale_sessions(now: float | None = None) -> list[str]:
    """best-effort 清理陈旧的 `session-*` 目录；**任何失败都忽略**。

    刻意忽略失败：清理是"省磁盘"，不是判据。若为此抛错，就等于**用一个偶发换另一个偶发**。
    只动 `session-*`，`e2e-shots/` 与其它目录一律不碰。
    """
    current = time.time() if now is None else now
    removed: list[str] = []
    if not BASETEMP_ROOT.is_dir():
        return removed
    for entry in BASETEMP_ROOT.iterdir():
        if not entry.is_dir() or not entry.name.startswith(SESSION_PREFIX):
            continue
        try:
            if current - entry.stat().st_mtime > STALE_AFTER_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
                removed.append(entry.name)
        except OSError:
            continue
    return removed


def pytest_configure(config) -> None:
    """把 basetemp 指到本次会话独有的目录（调用方显式指定时不覆盖）。"""
    if config.option.basetemp:
        return
    unique = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    target = BASETEMP_ROOT / f"{SESSION_PREFIX}{unique}"
    target.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(target)
    config._mt_basetemp_unique = str(target)  # 供终端摘要显示（pytest_terminal_summary）
    _prune_stale_sessions()


def pytest_report_header(config) -> str | None:
    """在报告头上打印本次会话的 basetemp —— 便于"两次并发跑"时一眼看出各自独立。"""
    target = getattr(config, "_mt_basetemp_unique", None)
    if not target:
        return None
    return f"basetemp（本会话独有）= {Path(target).relative_to(REPO_ROOT)}"
