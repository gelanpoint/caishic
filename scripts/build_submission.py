"""构建交付包：评委拿到就能跑。

**为什么要有这个脚本**：交付物必须能在**评委的机器**上跑起来，而评委机器
**可能连不上 PyPI** —— 那样连第一步 `pip install Flask` 都做不到，演示直接归零。
所以包里带一份 `offline-deps/`（从本机 site-packages 拷出的 Flask 及其硬依赖），
用法是设 `PYTHONPATH`，**不改仓库、不改运行期依赖声明**（`requirements.txt` 仍然只有 Flask）。

打包内容 = `git archive HEAD` 的全部内容（**只含已跟踪文件**，天然不含 data/ 与临时产物）
              + `offline-deps/`
              + `交付说明.txt`（评委视角的上手说明）

用法：
    python scripts/build_submission.py --out D:\\release
    python scripts/build_submission.py --out D:\\release --no-offline-deps   # 体积敏感时
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Flask 3.1.3 的运行期硬依赖（来自 importlib.metadata.requires 实测，不靠记忆列）
OFFLINE_PACKAGES = ("flask", "blinker", "click", "itsdangerous", "jinja2", "markupsafe", "werkzeug")

DELIVERY_README = """\
菜市场数字化交易与佣金系统 MVP —— 交付包
================================================================

【这是什么】
一套断网也能跑通的菜市场交易与经营数据系统，外加一套多智能体仿真。
不是 PPT、不是截图、不是录屏 —— **它是一个能真的跑起来的东西**。

【30 秒跑起来】
  Windows ：双击 start.bat
  Linux/mac：./start.sh
  或直接  ：python run.py

启动后终端会打印局域网访问地址与三个入口：
  操作端（秤端）  /scale/
  顾客扫码页      /customer/
  运营端          /admin/
首次启动自动建库 + 导入种子数据（幂等，重复启动不重复写）。

【运行前唯一的前置：Python 3.11+ 与 Flask】

  情况 A · 机器能上 PyPI（最常见）
      python -m pip install -r requirements.txt

  情况 B · 机器没有外网（现场常见）
      本包内已带 offline-deps/（Flask 3.1.3 及其硬依赖，约 2 MB），**不需要联网安装**：
          Windows ： set PYTHONPATH=%CD%\\offline-deps  &&  python run.py
          Linux   ： PYTHONPATH=$PWD/offline-deps     python run.py
      这条路只是把该目录加进模块搜索路径，**不修改系统、不写注册表**。

【先确认环境没坑】
  python scripts/preflight.py
  它会检查 Python 版本、Flask 可用性、数据目录可写性，并把可执行的修复命令直接打出来。

【想看验证过程】
  python -m pytest -q          # 753 项测试，约 3~5 分钟
  证据：docs/走查证据/（六步主链真浏览器走查的实测记录与复现命令）
  评委环境仿真：python scripts/judge_sim.py <本包解压出来的目录>
  上场前自检：python scripts/preflight.py

【不要做什么】
  · 不要接外网资源：前端**零 CDN、零外部字体/图标库**，这是硬约束，有机械检查盯着；
  · 不要把 data/ 目录放进版本库：它是运行时数据，种子脚本会重建。

【文档从哪看起】

  ── 赛题交付物：四份文档（**先看这四份**）──
  docs/方案说明/01-问题梳理.md          这道题难在哪（抽佣为什么失败、钱该从哪来）
  docs/方案说明/02-总体方案.md          我们打算怎么做（含架构图与自研成本账）
  docs/方案说明/03-附录A-智能秤设计.md  秤端：硬件/成本/对接/断网，验证到哪一步
  docs/方案说明/04-附录B-中台设计.md    中台：数据/接口/归属/佣金/不重复记账，验证到哪一步
  建议阅读顺序 1 → 2 → 3 → 4。**代码与这个包是这四份的支撑证据。**

  ── 其余导航 ──
  README.md                    30 秒跑起来 + 六步演示路径 + 完整文档索引
  docs/提交说明.md             方案摘要（**可直接作为邮件正文**）
  docs/要求与对应.md           比赛要求逐条 + 我们的对应与自证位置
  docs/走查证据/README.md      演示到底能不能跑通的实测记录
  docs/调研报告-现实情况.md    26 条实地结论 + 30 处来源链接
"""


def repo_files() -> list[Path]:
    """用 `git archive HEAD` 取已跟踪文件 —— 天然不含 data/ 与临时产物。"""
    raw = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout
    files: list[Path] = []
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                files.append(member)
    return files  # type: ignore[return-value]


def extract_tracked(dest: Path) -> int:
    raw = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        tar.extractall(dest)
    return len([p for p in dest.rglob("*") if p.is_file()])


def copy_offline_deps(dest: Path) -> tuple[int, int]:
    """把 Flask 及其硬依赖拷进包内。

    ⚠️⚠️ **必须连 `<pkg>-<ver>.dist-info` 一起拷**，否则离线兜底是**假绿**：
    Werkzeug 的服务器初始化里有 `importlib.metadata.version("werkzeug")`
    （用来拼 HTTP 响应头），缺了元数据就抛 `PackageNotFoundError`，
    **`python run.py` 直接退出、端口永远起不来**。

    代价是我自己踩过的：第一次打包只拷了包目录，用 `import flask` 一测能导入就宣布兜底成立 ——
    **导入成功不等于跑得起来**。真去起服务才发现进程秒退、端口连不上。
    现在验收标准是**真起服务并打三个入口页**。
    """
    import importlib.util

    target_root = dest / "offline-deps"
    target_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    total = 0
    missing = []

    for name in OFFLINE_PACKAGES:
        spec = importlib.util.find_spec(name)
        if spec is None:
            missing.append(name)
            continue

        # ⚠️ `submodule_search_locations` 对**包**给出的是**包目录本身**
        # （flask ⇒ ...\site-packages\flask），**不是父目录**；
        # 对**单文件模块**给的是空列表，得退回 `origin` 的目录。
        # 搞错这一处 ⇒ 拼出 `site-packages/flask/flask` ⇒ 七个包全判"没找到"。
        if spec.submodule_search_locations:
            search_roots = [Path(spec.submodule_search_locations[0]).parent]
        elif spec.origin:
            search_roots = [Path(spec.origin).parent]
        else:
            missing.append(name)
            continue

        found_any = False
        for root in search_roots:
            pkg_dir = root / name
            if pkg_dir.is_dir():
                shutil.copytree(pkg_dir, target_root / name, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                found_any = True
            # 元数据：<Name>-<Version>.dist-info / .egg-info —— 运行期 importlib.metadata 要读它
            for pattern in (f"{name}-*.dist-info", f"{name}-*.egg-info"):
                for meta in root.glob(pattern):
                    shutil.copytree(meta, target_root / meta.name, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                    found_any = True
        if not found_any:
            missing.append(name)

    copied = len([p for p in target_root.rglob("*") if p.is_file()])
    total = sum(p.stat().st_size for p in target_root.rglob("*") if p.is_file())

    if missing:
        print(f"  [!] 以下包没找到，已跳过：{missing}")
    (target_root / "README.txt").write_text(
        "本目录是 Flask 3.1.3 及其运行期硬依赖的副本，供**无外网**环境使用。\n"
        "用法：把它加进模块搜索路径即可，不需要 pip install，也不修改系统。\n"
        "  Windows： set PYTHONPATH=%CD%\\offline-deps  &&  python run.py\n"
        "  Linux  ： PYTHONPATH=$PWD/offline-deps     python run.py\n"
        "有外网时直接 python -m pip install -r requirements.txt 即可，不必用本目录。\n"
        "\n"
        "注意：这里**同时包含各包的 <name>-<ver>.dist-info 元数据**，不是多余的。\n"
        "Werkzeug 在服务器启动时会调 importlib.metadata.version('werkzeug') 来拼 HTTP 响应头，\n"
        "缺了元数据就会 PackageNotFoundError，**服务直接起不来**。\n",
        encoding="utf-8",
    )
    return copied, total


def build(out_dir: Path, offline: bool, prefix: str) -> Path:
    stage = out_dir / prefix
    if stage.exists():
        shutil.rmtree(stage)
    n = extract_tracked(stage)
    print(f"  已跟踪文件 {n} 个 → {stage}")

    (stage / "交付说明.txt").write_text(DELIVERY_README, encoding="utf-8")
    print("  已写入 交付说明.txt")

    if offline:
        copied, size = copy_offline_deps(stage)
        print(f"  offline-deps：{copied} 个文件 / {size / 1024 / 1024:.1f} MB")

    zip_path = out_dir / f"{prefix}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(out_dir))
    print(f"  压缩包：{zip_path}  {zip_path.stat().st_size / 1024 / 1024:.1f} MB")
    return zip_path


def head_commit_date() -> str:
    """包名里的日期取 **HEAD 提交的日期**，不是"今天"。

    为什么不是 `date.today()`：
    ① **可复现** —— 同一个 commit 反复打包得到**同一个包名**，而不是"哪天打的就算哪天"；
    ② **可追溯** —— 包名能指回具体提交，收件人一眼知道这是哪一版；
    ③ **不违反项目时钟纪律** —— 墙钟直读只允许出现在 `app/clock.py`
       （`tests/unit/test_clock_guard.py` 机械守卫，`scripts/` 也在扫描面内）。

    取不到（不在 git 仓库里、或 `git` 不在 PATH）时退回调用方给的 `--prefix`，
    **不静默换一个日期蒙混**。
    """
    proc = subprocess.run(
        ["git", "log", "-1", "--format=%cd", "--date=format:%Y%m%d"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    value = (proc.stdout or "").strip()
    if proc.returncode != 0 or not value.isdigit() or len(value) != 8:
        print("  [!] 取不到 HEAD 提交日期，请显式传 --prefix <包名>")
        return "HEAD"
    return value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--no-offline-deps", action="store_true", help="不带离线兜底包（体积敏感时）")
    ap.add_argument("--prefix", default=None, help="包名前缀，默认 market-trade-mvp-YYYYMMDD")
    args = ap.parse_args()

    prefix = args.prefix or f"market-trade-mvp-{head_commit_date()}"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"构建交付包 → {out_dir}")
    zip_path = build(out_dir, not args.no_offline_deps, prefix)
    print(f"完成：{zip_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
