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

echo "正在启动服务（首次启动会自动建库并导入种子数据）..."
exec "$PY_CMD" run.py "$@"
