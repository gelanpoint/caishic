"""控制台/标准输出编码：**不依赖环境变量**地把输出编码定下来。

## 为什么需要它（`CP-D` 现场验收的第二个缺陷，与 `.bat` 那次同类）

`start.bat` 那次教训是"不可靠的东西不要留给环境"；中文提示从 `cmd echo` 搬到 Python 之后，
**同样的依赖换了个位置又出现了一次**：Python 决定 stdout 用什么编码，靠的是
`PYTHONUTF8` / `PYTHONIOENCODING` **或系统 locale**。

- 开发机上若恰好设了 `PYTHONUTF8=1`（或 `PYTHONIOENCODING=utf-8`）：输出是 UTF-8，测试全绿；
- 现场/干净环境上两个变量都没有：Windows 中文系统 locale 是 `cp936`，于是输出是 **GBK 字节**，
  而捕获方（测试、CI、把输出重定向到文件的人）按 UTF-8 解码 ⇒ 满屏 `\ufffd` ⇒ 断言失败。

实测（本机，清掉两个变量）：`locale.getpreferredencoding(False) = cp936`、`sys.stdout.encoding = gbk`。
父代理那台机器上就是这么红的（`assert '已启动' in '...\ufffd\ufffd...'`），而我这台"绿"只是因为我
每条命令都带了 `PYTHONIOENCODING=utf-8` —— **靠环境变量的通过不算通过**。

## 规则（两条，一眼能判）

- **输出被重定向/管道**（不是真控制台）：**强制 UTF-8** —— 写给机器看的东西必须与 locale 无关；
- **输出接在真控制台上**（`isatty()`）：**沿用控制台编码** —— 中文 Windows 控制台用 `cp936` 显示中文
  本来就正常，硬改成 UTF-8 反而会变成乱码（除非先 `chcp 65001`，`start.bat` 已经这么做了）。

于是"文件/管道里的字节"与"人能看到的字"各自都是对的，且都**不取决于调用者设了什么变量**。
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """按上面两条规则把 `stdout` / `stderr` 的编码定下来（幂等，可重复调用）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # 已被替换成非文本流（如测试里注入的对象）
            continue
        try:
            if stream.isatty():
                continue  # 真控制台：交给控制台自己的编码，中文才显示得对
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # 流已关闭/不可重配（例如被 pytest 的捕获层包装）：不做保证也不崩 —— 让调用方继续跑。
            continue
