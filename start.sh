#!/usr/bin/env sh
# ===================================================================
#  start.sh —— macOS / Linux 启动脚本
#  行尾必须是 LF（.gitattributes 强制 *.sh eol=lf）
#  依赖：Python 3.11+（优先 python3，回退 python）
#  不依赖外网、不使用 Docker（ADR-0003）
# ===================================================================
set -eu

cd "$(dirname "$0")"

# 让 Python 的 stdio 一律使用 UTF-8：
#   最小化发行版的 POSIX/C locale 下 Python 默认按 ASCII 编码，
#   打印中文会直接 UnicodeEncodeError（启动即崩，且报错与真实原因不符）
export PYTHONUTF8=1

if command -v python3 >/dev/null 2>&1; then
    PY_CMD=python3
elif command -v python >/dev/null 2>&1; then
    PY_CMD=python
else
    echo "====================================================================" >&2
    echo " [启动失败] 未找到 Python。请安装 Python 3.11 或更高版本。" >&2
    echo "====================================================================" >&2
    exit 1
fi

if ! "$PY_CMD" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "====================================================================" >&2
    echo " [启动失败] Python 版本过低，本项目需要 3.11 或更高版本。" >&2
    "$PY_CMD" -c 'import sys; print("  当前版本: " + sys.version)' >&2
    echo "====================================================================" >&2
    exit 1
fi

# 演示数据目录默认落在**用户数据目录**（避开可能很慢的工作目录所在卷），
# 具体规则与全部中文提示都在 `scripts/launch.py`（与 `start.bat` **共用同一份实现**，
# 避免两个启动脚本各写一遍默认值 —— 那种重复下次必然漂移）。
# 依据（实测）：接口响应时间受**磁盘 fsync 成本**支配，同一代码同一负载在不同卷上差一个数量级；
# 详见 docs/standards/quality-gates.md §1.1 的「响应时间阈值必须连口径一起读」注。
exec "$PY_CMD" scripts/launch.py "$@"
