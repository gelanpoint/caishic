"""仿真侧的标准输出编码：**不依赖环境变量**地把输出编码定下来。

## 为什么 `sim/` 要自己有一份（而不是复用 `app/console.py`）

规则完全相同（"被重定向 → 强制 UTF-8；接真控制台 → 沿用控制台编码"，理由见
`app/console.py` 的模块 docstring 与 `docs/PROJECT-STATE.md`「交付铁律 1」）：
Python 决定 stdout 用什么编码，靠的是 `PYTHONUTF8` / `PYTHONIOENCODING` **或系统 locale** ——
在中文 Windows 上没设这两个变量时是 `cp936`，于是仿真打印的 `⇒`、`①` 这类字符会直接
`UnicodeEncodeError`（本实现第一版就实测踩到：`⇒` 在 GBK 下不可编码）。

**为什么不 import `app.console`**：`sim/**` 不得 `import app`（唯一耦合面是 HTTP 契约的 38 个端点，
`tests/sim/test_sim_no_app_import.py` 机械钉死）。反过来 `app` 引用 `sim` 也被同一条闸门禁止
（"仿真不是启动路径"）。所以这 20 行规则在两侧各有一份 —— 这是隔离闸门的代价，
不是可以顺手消除的重复；能消除的是"靠环境变量的通过"，那一条已经消除。
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """把 `stdout` / `stderr` 的编码定下来（幂等；流不可重配时不崩，让调用方继续跑）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # 已被替换成非文本流（例如测试里注入的对象）
            continue
        try:
            if stream.isatty():
                continue  # 真控制台：交给控制台自己的编码，中文才显示得对
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue
