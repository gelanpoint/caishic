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
    python run.py --mode split   # 【分离形态】只启动中台：不托管 /scale/ 秤端界面，
                                 #   另开一个终端跑 `python scripts/scale_host_runner.py` 当秤端

形态说明（`ADR-0005` §4 第 5 条「分离形态是新增启动方式」，`T-SCALE-17`）：
    - **形态 1 `single`（默认）**：单机 all-in-one，中台顺带托管秤端页面，行为与从前**逐字节一致**；
    - **形态 2 `split`**：中台 × 1 ↔ 秤端 × N。中台只提供 7 个秤端接入端点 + 运营端 + 顾客页，
      **不注册 `/scale/`**（秤端界面归秤端固件）；秤端是独立进程，经**真实 HTTP** 对接。
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


def print_split_banner(host: str, port: int, lan_ip: str) -> None:
    """打印**分离形态**的中台启动信息（`T-SCALE-17`）。

    与形态 1 的横幅刻意不同：这里**不打印 `/scale/` 入口**（分离形态下该路由不存在，
    打印一个 404 的地址是误导），改为打印**秤端接入基址**与**启动秤端的命令**。
    """
    local = f"http://127.0.0.1:{port}"
    remote = f"http://{lan_ip}:{port}"
    print(_LINE)
    print(f" {config.APP_NAME} —— 中台已启动【分离形态 · split】")
    print(_LINE)
    print(" 本进程是**中台**：只提供秤端接入端点 + 运营端 + 顾客页。")
    print(" 秤端是**独立进程**（ESP32-S3 固件；本机可用主机侧运行器模拟），二者经真实 HTTP 对接。")
    print("-" * 68)
    print(f" 秤端接入基址（给秤端用）  {remote}/api/scale/v1/")
    print(f"   本地回环               {local}/api/scale/v1/")
    print("-" * 68)
    print(f" 运营端                    {local}/admin/")
    print(f" 顾客扫码页                {local}/customer/")
    print(f" 入口导航页                {local}/")
    print(" 秤端界面                  **本形态不托管**（`/scale/` 返回 404 —— 归秤端固件）")
    print("-" * 68)
    print(" 启动一个秤端（另开一个终端；中台没起来也能先起秤端）：")
    print(f"   python scripts/scale_host_runner.py --mid {local}")
    print("   想模拟多商家就多开几个终端、用不同的 --device-id（如 SC-000001 / SC-000002 …）")
    print("-" * 68)
    print(f" 数据文件：{config.DB_PATH}")
    print(f" 暂存目录：{config.OFFLINE_STAGING_DIR}")
    print("-" * 68)
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
    parser.add_argument(
        "--mode",
        choices=("single", "split"),
        default="single",
        help="single=形态 1 单机 all-in-one（默认，行为与从前逐字节一致）；split=形态 2 只起中台",
    )
    args = parser.parse_args(argv)

    host, port = args.host, args.port
    split = args.mode == "split"

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

    app = create_app(serve_scale_ui=not split)
    if split:
        print_split_banner(host, port, lan_ip)
    else:
        print_startup_banner(host, port, lan_ip)

    # use_reloader=False：避免调试重载器把进程 fork 成两个，导致"端口莫名被占用"的现场误判。
    app.run(host=host, port=port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
