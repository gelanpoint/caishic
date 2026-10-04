"""`scripts/build_submission.py` 的验收：**离线兜底包必须带包元数据**。

## 为什么这条门禁独立存在（2026-10-04 的真实事故）

第一版打包只拷包目录、不拷 `<name>-<ver>.dist-info`。
我当时的验收是 `python -c "import flask"` 成功 ⇒ 宣布"离线兜底成立"。

**那是假绿**：Werkzeug 初始化服务器时会调
`importlib.metadata.version("werkzeug")` 拼 HTTP 响应头，缺元数据直接抛
`PackageNotFoundError`，`run.py` 秒退、端口起不来 ——
**评委双击 `start.bat` 窗口一闪就没了**。

所以门禁不是"包里有没有 flask 目录"，而是三条一起看：

1. 每个依赖的**包目录**在；
2. 每个依赖的 **`.dist-info` 元数据**在（缺它就是上面那个事故）；
3. 打包脚本**自报缺失清单**为空 —— 不许静默跳过。

端到端那道（真解压、真起服务、真打四个页面）在 `scripts/judge_sim.py`，
**这里只守"包里有没有该有的东西"这个静态事实**，跑得快、能进日常全量。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # tests/contract/ → 仓库根
BUILD = REPO_ROOT / "scripts" / "build_submission.py"
OFFLINE_PACKAGES = ("flask", "blinker", "click", "itsdangerous", "jinja2", "markupsafe", "werkzeug")


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("submission")
    proc = subprocess.run(
        [sys.executable, str(BUILD), "--out", str(out)],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    assert proc.returncode == 0, f"打包失败：\n{proc.stdout}\n{proc.stderr}"
    root = out / next(p.name for p in out.iterdir() if p.is_dir())
    return root


def test_tracked_files_are_present_and_data_dir_is_absent(package):
    """包 = 已跟踪文件 + 额外两样；`data/` 与临时产物**必须不在里面**。"""
    assert (package / "run.py").is_file()
    assert (package / "start.bat").is_file()
    assert (package / "requirements.txt").is_file()
    assert (package / "交付说明.txt").is_file()
    assert (package / "README.md").is_file()
    # 种子数据会在首次启动时重建，包里带一份旧的只会让人以为"数据也在包里"
    assert not (package / "data").exists(), "data/ 是运行时数据，不该进交付包"
    for junk in (".pytest_cache", ".pytest-tmp"):
        assert not (package / junk).exists(), f"{junk} 是临时产物，不该进交付包"


def test_requirements_stays_single_dependency(package):
    """『运行期依赖只有 Flask』是明确卖点，交付包里也必须保持这句话。"""
    text = (package / "requirements.txt").read_text(encoding="utf-8")
    names = [
        ln.split("==")[0].split(">=")[0].split("<=")[0].strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert names == ["Flask"], f"requirements.txt 只应有 Flask，实际：{names}"


def test_offline_deps_contains_package_and_metadata(package):
    """核心门禁：包目录 **和** `.dist-info` 元数据都要在。

    ⚠️ 只查包目录会漏掉 2026-10-04 那个事故 —— 缺元数据时 `import` 照样成功，
    但 werkzeug 启动即崩。这里显式查两样。
    """
    od = package / "offline-deps"
    assert od.is_dir(), "offline-deps 目录应存在（无外网兜底）"
    entries = {p.name for p in od.iterdir()}

    missing_pkgs = [n for n in OFFLINE_PACKAGES if n not in entries]
    assert not missing_pkgs, f"offline-deps 缺包目录：{missing_pkgs}"

    meta = {e for e in entries if e.endswith(".dist-info") or e.endswith(".egg-info")}
    missing_meta = [n for n in OFFLINE_PACKAGES
                    if not any(m.lower().startswith(n.lower()) for m in meta)]
    assert not missing_meta, (
        f"offline-deps 缺包元数据：{missing_meta}\n"
        "**这不是多余的** —— werkzeug 初始化时会 importlib.metadata.version('werkzeug')，"
        "缺元数据 ⇒ PackageNotFoundError ⇒ run.py 秒退、端口起不来。"
    )


def test_build_reports_nothing_missing(package, tmp_path):
    """打包脚本必须**自报缺失**，不许静默跳过（静默跳过 = 又一次假绿）。"""
    out = tmp_path / "rebuild"
    proc = subprocess.run(
        [sys.executable, str(BUILD), "--out", str(out)],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "以下包没找到" not in proc.stdout, (
        f"打包脚本报了缺包却仍然成功，这是不允许的静默失败：\n{proc.stdout}"
    )


def test_shipped_metadata_can_answer_importlib_metadata(package, tmp_path):
    """**元数据真能被 `importlib.metadata` 读出来** —— 这才是事故的真实触发点。

    这是本文件最强的判据：不是查目录名对不对，而是**用事故里报错的那个调用去问**。
    """
    od = package / "offline-deps"
    probe = (
        "import importlib.metadata as m, sys;"
        "names=['werkzeug','flask','jinja2','click','markupsafe','blinker','itsdangerous'];"
        "out={};"
        "\nfor n in names:\n"
        "    try:\n"
        "        out[n]=m.version(n)\n"
        "    except Exception as e:\n"
        "        out[n]='ERR:'+type(e).__name__\n"
        "print(out)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(tmp_path), env={"PYTHONPATH": str(od), "PATH": "/usr/bin:/bin",
                                "SYSTEMROOT": str(Path("C:/Windows")) if sys.platform == "win32" else "/",
                                "PYTHONNOUSERSITE": "1"},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    try:
        versions = json.loads(line.replace("'", '"'))
    except Exception:  # noqa: BLE001
        raise AssertionError(f"探针输出不可解析：{proc.stdout!r} / {proc.stderr!r}")
    bad = {k: v for k, v in versions.items() if str(v).startswith("ERR:")}
    assert not bad, (
        f"这些包的元数据读不出来：{bad}\n"
        "这正是事故里 `importlib.metadata.version('werkzeug')` 抛错的同一个原因。"
    )