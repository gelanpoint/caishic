"""现场演示启动器：**所有面向人的中文都在这里打印**，`start.bat` / `start.sh` 只做 ASCII 管道。

## 为什么需要它（`CP-D` 现场验收发现的缺陷）

`cmd.exe` 是按**控制台代码页**去解析 `.bat` 的**字节**的。`.bat` 里一旦出现非 ASCII
（中文注释、中文提示），多字节序列就可能被拆断，现场看到的是
`'免安装打包，见' 不是内部或外部命令` / `" was unexpected at this time.` 这类**解析错误**；
而当它发生在 `if ... ( ... )` **括号块内**时，真正该看到的提示（例如"端口已被占用，换个端口"）
**根本打不出来** —— 失败信息被"解析错误"顶掉了，这就是本次要修的东西。

**修法不是"把那一行调好"，而是把整类风险消掉**：`start.bat` 保持**纯 ASCII**（可逐字节验证），
中文一律交给 Python 打印 —— Python 自己管编码（`PYTHONUTF8=1` + `chcp 65001`），
与 `cmd` 的解析路径无关。于是"控制台代码页是什么"不再影响脚本能否跑起来。

> 诚实记录：本机在 `65001/936/437/850/932/950/1252` 八个代码页下**未能复现**现场那串解析错误
> （见交接记录）。所以这里不是"修好了我复现到的那一行"，而是**按机制把不可靠的东西移出 .bat**。

## 职责（保持薄，三件事）

1. **演示数据目录默认落在用户数据目录**（快盘）：`MT_DATA_DIR` 未设置时设上，
   **已显式设置则一律不动**（现场要换盘/换目录就设它）；取不到用户数据目录就什么都不做，
   此时行为与从前完全一致（仓库内 `data/`）。`python run.py` 的默认值**不变**。
2. 打印**将要使用的数据目录**（中文，来自 Python）。
3. 调用 `run.py` 的 `main()`；**非 0 退出时补一段中文提示**（含端口占用时的换端口办法）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.console import force_utf8_stdio  # noqa: E402  （轻量、无副作用：先把输出编码定下来）

_LINE = "-" * 68


def default_demo_data_dir() -> Path | None:
    """演示数据目录的默认落点（用户数据目录，通常是快盘）；取不到返回 `None`（即什么都不设）。

    与 `start.bat` 的第一版行为一致，但**只在这一处实现**（`start.sh` 复用同一份逻辑，
    避免"两个启动脚本各写一遍默认值"这种下次必然漂移的重复）。
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "MarketTradeDemo" / "data" if base else None


def apply_demo_data_dir() -> Path | None:
    """未显式设置 `MT_DATA_DIR` 时把它设到用户数据目录；返回实际生效的演示目录（未设置则 `None`）。

    ⚠️ 必须在 `import app.config` **之前**调用：`app.config` 在导入时读取环境变量。
    """
    if os.environ.get("MT_DATA_DIR"):
        return None
    candidate = default_demo_data_dir()
    if candidate is None:
        return None
    os.environ["MT_DATA_DIR"] = str(candidate)
    return candidate


def suggested_port(argv: list[str]) -> int | None:
    """从命令行 `--port N`（或 `MT_PORT`）推出"换一个端口"的建议值；推不出返回 `None`。

    为什么不在提示里写死一个端口：写死会在"那个端口恰好也占着"时给出**没用的**建议，
    等于把确定性反馈又变成猜。`run.py` 自己会打印它算出的 `port + 1`，这里保持一致。
    """
    for index, item in enumerate(argv):
        if item == "--port" and index + 1 < len(argv):
            try:
                return int(argv[index + 1]) + 1
            except ValueError:
                return None
        if item.startswith("--port="):
            try:
                return int(item.split("=", 1)[1]) + 1
            except ValueError:
                return None
    raw = os.environ.get("MT_PORT")
    try:
        return int(raw) + 1 if raw else None
    except ValueError:
        return None


def print_failure_hint(code: int, argv: list[str]) -> None:
    """非 0 退出时的中文提示（`run.py` 已打印具体原因，这里只补"怎么办"）。"""
    next_port = suggested_port(argv)
    print(_LINE)
    print(f" [启动失败] 退出码 {code}。上面若有具体原因，以那条为准。最常见的两种：")
    if next_port is None:
        print("   1) 端口被占用 → 换端口再启动：start.bat --port 8010")
    else:
        print(f"   1) 端口被占用 → 换端口再启动：start.bat --port {next_port}")
    print("   2) 依赖没装 → python -m pip install -r requirements.txt")
    print(_LINE)


def main(argv: list[str] | None = None) -> int:
    # 输出编码自己定，**不依赖 PYTHONUTF8 / PYTHONIOENCODING / locale**：
    # 被重定向时强制 UTF-8（捕获方按 UTF-8 读），接真控制台时沿用控制台编码（中文才显示得对）。
    force_utf8_stdio()

    argv = list(sys.argv[1:] if argv is None else argv)
    data_dir = apply_demo_data_dir()

    if data_dir is not None:
        print(f"[演示] 数据目录：{data_dir}（如需改回仓库内 data/，先清空环境变量 MT_DATA_DIR）")
    print("正在启动服务（首次启动会自动建库并导入种子数据，请稍候）...")

    # 注释与提示都放在 import 之后由 Python 打印：`run` 会拉起整个应用（含 `app.config`）。
    import run  # noqa: E402  （必须在 apply_demo_data_dir() 之后导入）

    code = run.main(argv)
    if code != 0:
        print_failure_hint(code, argv)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
