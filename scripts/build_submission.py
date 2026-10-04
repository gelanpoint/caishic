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
import shutil
import subprocess
import sys
import tarfile
import zipfile
from datetime import date
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
      本包内已带 offline-deps/（Flask 3.1.3 及其硬依赖，约 5 MB），**不需要联网安装**：
          Windows ： set PYTHONPATH=%CD%\\offline-deps  &&  python run.py
          Linux   ： PYTHONPATH=$PWD/offline-deps     python run.py
      这条路只是把该目录加进模块搜索路径，**不修改系统、不写注册表**。

【先确认环境没坑】
  python scripts/preflight.py
  它会检查 Python 版本、Flask 可用性、数据目录可写性，并把可执行的修复命令直接打出来。

【想看验证过程】
  python -m pytest -q          # 531 项测试，约 3~5 分钟
  证据：docs/走查证据/（六步主链真浏览器走查的实测记录与复现命令）

【不要做什么】
  · 不要接外网资源：前端**零 CDN、零外部字体/图标库**，这是硬约束，有机械检查盯着；
  · 不要把 data/ 目录放进版本库：它是运行时数据，种子脚本会重建。

【文档从哪看起】
  README.md                    30 秒跑起来 + 六步演示路径 + 关键设计判断
  提交说明.md                  方案摘要（可直接作为邮件正文）
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
    """把 Flask 及其硬依赖拷进包内。只拷包目录，不拷 dist-info 之外的东西。"""
    import importlib.util

    found: list[tuple[str, Path]] = []
    for name in OFFLINE_PACKAGES:
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.submodule_search_locations:
            found.append((name, Path(spec.origin).parent if spec and spec.origin else Path()))
            continue
        found.append((name, Path(list(spec.submodule_search_locations)[0])))

    target_root = dest / "offline-deps"
    target_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    total = 0
    missing = []
    for name, src in found:
        if not src.is_dir():
            missing.append(name)
            continue
        out = target_root / name
        shutil.copytree(src, out, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        n = len([p for p in out.rglob("*") if p.is_file()])
        copied += n
        total += sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    if missing:
        print(f"  ⚠️ 以下包没找到，已跳过：{missing}")
    (target_root / "README.txt").write_text(
        "本目录是 Flask 3.1.3 及其运行期硬依赖的副本，供**无外网**环境使用。\n"
        "用法：把它加进模块搜索路径即可，不需要 pip install，也不修改系统。\n"
        "  Windows： set PYTHONPATH=%CD%\\offline-deps  &&  python run.py\n"
        "  Linux  ： PYTHONPATH=$PWD/offline-deps     python run.py\n"
        "有外网时直接 python -m pip install -r requirements.txt 即可，不必用本目录。\n",
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--no-offline-deps", action="store_true", help="不带离线兜底包（体积敏感时）")
    ap.add_argument("--prefix", default=None, help="包名前缀，默认 market-trade-mvp-YYYYMMDD")
    args = ap.parse_args()

    prefix = args.prefix or f"market-trade-mvp-{date.today():%Y%m%d}"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"构建交付包 → {out_dir}")
    zip_path = build(out_dir, not args.no_offline_deps, prefix)
    print(f"完成：{zip_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
