#!/usr/bin/env python
"""启动入口：检测端口占用 → 建库/迁移 → 导入种子 → 启动 HTTP 服务 → 打印访问信息。

对应 `AC-013`（一条命令启动）与 `AGENTS.md` §3 的现场演示硬要求：
1. 打印**局域网 IP**（便于手机真机扫码）；
2. 同时打印**两个入口地址**：操作端（秤端/运营端）与顾客扫码页；
3. 支持**同一台机器开两个浏览器窗口**分别扮演秤端与顾客端（现场 WiFi 不可用时的降级方案）；
5. **检测端口占用并给出明确提示**，并打印**数据文件路径**（便于重置演示数据）。

用法：
    python run.py                # 默认 0.0.0.0:8000（可用环境变量 MT_HOST / MT_PORT 覆盖）
    python run.py --port 8010    # 换端口启动（演示机上被占用时的第一选择）
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

# 允许在任意工作目录下执行 `python run.py`（把仓库根加入模块搜索路径）
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app import config  # noqa: E402  （必须在 sys.path 处理之后导入）
from app import create_app  # noqa: E402
from app.console import force_utf8_stdio  # noqa: E402
from app.db import init_database  # noqa: E402
from app.seed import import_seed, summary_line  # noqa: E402

_LINE = "=" * 68


def detect_lan_ip() -> str:
    """探测本机在局域网中的 IP。

    做法：对一个**不可路由**的地址做 UDP `connect` —— UDP 不握手、**不发任何包**，
    仅让内核按路由表选出出口网卡地址。因此该函数**不依赖外网**（`REQ-025`：零外部依赖）。
    失败时回退到主机名解析结果（可能是 127.0.0.1，此时如实打印，不假装是局域网地址）。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def port_is_available(host: str, port: int) -> bool:
    """探测端口能否绑定。

    **故意不设置 `SO_REUSEADDR`**：在 Windows 上它允许两个 socket 绑定同一端口，
    会把"端口已被占用"探测成"可用"，从而让本检查失效（检查必须能真的失败）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def print_startup_banner(host: str, port: int, lan_ip: str) -> None:
    """打印启动信息（现场演示硬要求 1 / 2 / 5）。"""
    local = f"http://127.0.0.1:{port}"
    remote = f"http://{lan_ip}:{port}"
    print(_LINE)
    print(f" {config.APP_NAME} —— 已启动")
    print(_LINE)
    print(" 【同机演示】同一台机器开两个浏览器窗口即可跑完整流程（现场无 WiFi 时的降级方案）")
    print(f"   入口 1 · 操作端（秤端）   {local}/scale/")
    print(f"   入口 2 · 顾客扫码页       {local}/customer/")
    print(f"   运营端                    {local}/admin/")
    print(f"   入口导航页                {local}/")
    print("-" * 68)
    print(f" 【局域网访问】本机 IP：{lan_ip}  （手机真机扫码用；需与演示机同一局域网）")
    print(f"   入口 1 · 操作端（秤端）   {remote}/scale/")
    print(f"   入口 2 · 顾客扫码页       {remote}/customer/")
    print(f"   运营端                    {remote}/admin/")
    print(f"   入口导航页                {remote}/")
    print("-" * 68)
    print(f" 数据文件：{config.DB_PATH}")
    print(f" 暂存目录：{config.OFFLINE_STAGING_DIR}")
    print(f" 重置演示数据：python scripts/reset_demo.py")
    print("-" * 68)
    print(" 种子数据只含档案 / 品类字典 / 别名 / 商品 / 价目表（REQ-025）；")
    print(" 【佣金口径由运营端现场配置】—— 不是遗漏，是演示动线的一部分（REQ-017）。")
    print(_LINE)
    print(" 停止服务：Ctrl+C")
    print(_LINE)


def main(argv: list[str] | None = None) -> int:
    # 输出编码自己定：被重定向时强制 UTF-8，接真控制台时沿用控制台编码（见 app/console.py）。
    # **不依赖 PYTHONUTF8 / PYTHONIOENCODING / locale** —— 靠环境变量的"通过"在别人机器上会变成乱码。
    force_utf8_stdio()

    parser = argparse.ArgumentParser(
        description="菜市场数字化交易与佣金系统 MVP —— 启动入口",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=config.HOST, help="监听地址（0.0.0.0 表示所有网卡）")
    parser.add_argument("--port", type=int, default=config.PORT, help="监听端口")
    args = parser.parse_args(argv)

    host, port = args.host, args.port

    # ---- 端口占用检测（硬要求 5：明确提示，而不是抛一个看不懂的异常栈） ----
    # 注意：0.0.0.0 可能与"仅绑定 127.0.0.1 的进程"共存，故两处都探测，避免漏报。
    bind_hosts = [host] if host != "0.0.0.0" else ["0.0.0.0", "127.0.0.1"]
    for bind_host in bind_hosts:
        if not port_is_available(bind_host, port):
            print(_LINE, file=sys.stderr)
            print(f" [启动失败] 端口 {port} 已被占用（无法绑定 {bind_host}:{port}）。", file=sys.stderr)
            print(" 可能原因：本系统已有一个实例在运行，或该端口被其他程序占用。", file=sys.stderr)
            print(" 处理办法：", file=sys.stderr)
            print(f"   1) 关掉已占用该端口的程序；或", file=sys.stderr)
            print(f"   2) 换一个端口启动：python run.py --port {port + 1}", file=sys.stderr)
            print(f"      （也可用环境变量：MT_PORT={port + 1}）", file=sys.stderr)
            print(_LINE, file=sys.stderr)
            return 1

    lan_ip = detect_lan_ip()

    # ---- 自动建库 / 按序补齐迁移（AC-013「一条启动命令」，data-model.md §7） ----
    applied = init_database()
    if applied:
        print(f"[建库] 本次执行迁移：{', '.join(applied)}")
    else:
        print("[建库] 数据库已存在且已是最新版本（未重复执行迁移）")

    # ---- 导入种子数据（T-005；REQ-025 只读本地文件、不依赖外网） ----
    # 幂等：重复启动只补缺，不产生重复数据，也不覆盖摊主已调整过的价目表（见 app/seed.py 模块 docstring）。
    seed_summary = import_seed()
    print(f"[种子] {summary_line(seed_summary)}")

    app = create_app()
    print_startup_banner(host, port, lan_ip)

    # use_reloader=False：避免调试重载器把进程 fork 成两个，导致"端口莫名被占用"的现场误判。
    app.run(host=host, port=port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
