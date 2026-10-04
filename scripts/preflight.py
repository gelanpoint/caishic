"""上场前自检：把"跑不起来"的原因当场说清楚，并给出可执行的修复命令。

为什么单独一个脚本：现场演示最贵的失败不是功能不对，而是**评委机器上跑不起来**，
而报错往往发生在最不该出错的地方（Python 版本、Flask 没装、数据目录不可写）。
本脚本的输出就是**照着做就能修**的步骤，不是"建议检查一下"。

退出码：0 = 全部就绪；1 = 有阻塞项。

用法：python scripts/preflight.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

MIN_PY = (3, 11)


def _force_utf8_stdio() -> None:
    """输出编码**不依赖环境变量** —— 与 `app/console.py` 同一条规则。

    ⚠️ 这是本脚本被自己的用例抓出来的真实缺陷（2026-10-04）：
    清掉 `PYTHONUTF8`/`PYTHONIOENCODING` 后，Windows 中文系统 locale 是 `cp936`、
    `sys.stdout.encoding` 是 `gbk` ⇒ 本脚本写出的中文是 **GBK 字节**，
    而捕获方按 UTF-8 解码 ⇒ 满屏 `\ufffd` ⇒ 4 条用例全红。

    **交付前最后一道拦截，自己输出乱码**，现场看到的就是一屏 `\ufffd`。

    为什么在这里**复制**一份而不 `from app.console import ...`：
    `scripts/` 要能被单独拷出来当自检工具用，不该拖着 `app` 整包依赖；
    代价是这十几行重复 —— 已在 `app/console.py` 与此处互相标注。
    规则只有一条：**输出被重定向/管道 ⇒ 强制 UTF-8；接真控制台 ⇒ 沿用控制台编码。**
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if stream.isatty():
                continue
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue


_force_utf8_stdio()


def ok(msg: str) -> None:
    print(f"  [就绪] {msg}")


def bad(msg: str, fix: str = "") -> None:
    print(f"  [阻塞] {msg}")
    if fix:
        for i, line in enumerate(fix.strip().splitlines()):
            print(f"          {'->' if i == 0 else '  '} {line}")


def warn(msg: str, fix: str = "") -> None:
    print(f"  [提醒] {msg}")
    if fix:
        for i, line in enumerate(fix.strip().splitlines()):
            print(f"          {'->' if i == 0 else '  '} {line}")


def check_python() -> bool:
    v = sys.version_info
    if v[:2] >= MIN_PY:
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    bad(f"Python {v.major}.{v.minor} 低于要求的 {MIN_PY[0]}.{MIN_PY[1]}",
        "请安装 Python 3.11 或更高版本后重试")
    return False


def check_flask() -> bool:
    try:
        import flask  # noqa: F401
    except ImportError:
        bad("Flask 未安装（运行期只需要这一个依赖）",
            "有外网：python -m pip install -r requirements.txt\n"
            "无外网：设置 PYTHONPATH 指向交付包内的 offline-deps 目录\n"
            "         Windows： set PYTHONPATH=%CD%\\offline-deps\n"
            "         Linux  ： PYTHONPATH=$PWD/offline-deps")
        return False
    try:
        from importlib.metadata import version
        ver = version("flask")
    except Exception:  # noqa: BLE001
        ver = "未知"
    ok(f"Flask {ver}")
    return True


def data_dir() -> Path:
    raw = os.environ.get("MT_DATA_DIR")
    return Path(raw) if raw else Path.cwd() / "data"


def check_data_dir() -> bool:
    d = data_dir()
    if d.exists() and not d.is_dir():
        bad(f"MT_DATA_DIR 指向的不是目录：{d}", "请把它改成一个可写的目录")
        return False
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = tempfile.NamedTemporaryFile(dir=d, delete=True)
        probe.write(b"x")
        probe.close()
    except OSError as exc:
        bad(f"数据目录不可写：{d}（{exc}）",
            "换到有写权限的目录，例如：\n"
            "  set MT_DATA_DIR=C:\\mt-demo-data")
        return False
    if os.environ.get("MT_DATA_DIR"):
        ok(f"数据目录（由 MT_DATA_DIR 指定）：{d}")
    else:
        warn(f"数据目录未指定，将用仓库内的 {d}",
             "建议显式指定到**快盘**（实测接口耗时受磁盘 fsync 支配，C: 远快于仓库所在盘）：\n"
             "  set MT_DATA_DIR=C:\\mt-demo-data")
    return True


def check_disk_space() -> None:
    try:
        free = shutil.disk_usage(data_dir()).free
        if free < 200 * 1024 * 1024:
            warn(f"数据所在磁盘剩余空间偏小：{free / 1024 / 1024:.0f} MB", "清理后重试")
        else:
            ok(f"数据所在磁盘剩余 {free / 1024 / 1024 / 1024:.1f} GB")
    except OSError:
        pass


def check_repo_files() -> bool:
    root = Path(__file__).resolve().parent.parent
    good = True
    for rel in ("run.py", "app/__init__.py", "app/migrations/0001_init.sql",
                "start.bat", "app/static/scale/index.html"):
        if (root / rel).exists():
            ok(f"关键文件在位：{rel}")
        else:
            bad(f"关键文件缺失：{rel}", "交付包解压不完整，请重新解压")
            good = False
    return good


def main() -> int:
    print("上场前自检 —— 菜市场数字化交易与佣金系统 MVP")
    print(f"  工作目录：{Path.cwd()}")
    print()
    results = [check_python(), check_flask(), check_repo_files(), check_data_dir()]
    check_disk_space()
    print()
    if all(results):
        print("结论：全部就绪，可以直接 python run.py 或双击 start.bat")
        return 0
    print("结论：有阻塞项 —— 请按上面的 '->' 逐条处理后重跑本脚本")
    return 1


if __name__ == "__main__":
    sys.exit(main())
