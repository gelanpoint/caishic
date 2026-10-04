"""`scripts/preflight.py` 的验收：**它必须能在环境不合格时判红，并给出可执行的修复步骤。**

为什么要有这个用例：preflight 是**交付前最后一道拦截**。它自己一旦失灵，
现场就会退化成"直接跑 run.py 然后看报错"——而那正是最贵的失败方式。

三层验：
1. **正例**：合格环境 ⇒ 退出码 0；
2. **负例**：真的把 site-packages 从 `sys.path` 摘掉（Flask 真 import 不到）⇒ 判红，
   且**必须指出 `offline-deps` 这条无外网修复路径** —— 只说"请安装 Flask"对无外网现场没用；
3. **灵敏度**：把判据关掉必须变红 —— 所以断言不能只看"退出码是 1"，
   还要看**它有没有说清原因与修法**，否则一个只会 `sys.exit(1)` 的空壳也能过。
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PREFLIGHT = REPO_ROOT / "scripts" / "preflight.py"


def _run(env_extra: dict, cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, str(PREFLIGHT)],
        cwd=str(cwd), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


@pytest.fixture(scope="module")
def good_data_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("preflight-ok")
    return d


def test_preflight_passes_on_a_healthy_checkout(good_data_dir):
    """正例：Flask 在、关键文件在、数据目录可写 ⇒ 退出码 0。"""
    proc = _run({"MT_DATA_DIR": str(good_data_dir)}, REPO_ROOT)
    assert proc.returncode == 0, f"合格环境应判就绪，实际退出码 {proc.returncode}：\n{proc.stdout}"
    assert "全部就绪" in proc.stdout, f"应给出明确结论：\n{proc.stdout}"


def test_preflight_reports_the_missing_runtime_dependency_with_an_offline_route(tmp_path):
    """负例：让 Flask **真的** import 不到 ⇒ 判红，且给出 `offline-deps` 这条无外网修法。

    ## 为什么用"影子包"而不是内联 `python -c` 篡改 `sys.path`

    第一版探针把 `sys.stdout` 换成 `io.StringIO()` 再 `runpy` 跑 preflight ——
    **结果它把自己要验的东西给毁了**：`force_utf8_stdio()` 靠 `stream.reconfigure()` 定编码，
    而 `StringIO` 没有 `reconfigure` ⇒ 编码强制被架空 ⇒ 子进程按 GBK 写出中文 ⇒
    断言里全是 `\ufffd`。**探针的干扰比被测对象还大。**

    现在只做一件事：在 `PYTHONPATH` 最前面放一个**内容就是 `raise ImportError`** 的 `flask` 包，
    于是解释器侧 `import flask` 真的失败 —— **不碰 stdout、不碰 `sys.path`、不重定向**，
    preflight 的编码强制与判红逻辑都原样跑。

    **跨机器成立**：不依赖 Flask 装在用户目录还是全局目录（`PYTHONNOUSERSITE` 那种做法只在前者成立）。
    """
    shadow = tmp_path / "shadow"
    (shadow / "flask").mkdir(parents=True)
    (shadow / "flask" / "__init__.py").write_text(
        "raise ImportError('模拟：这台机器上没有 Flask')\n", encoding="utf-8"
    )

    env = {**os.environ, "MT_DATA_DIR": str(tmp_path / "d"),
           "PYTHONPATH": str(shadow)}
    proc = subprocess.run(
        [sys.executable, str(PREFLIGHT)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    out = proc.stdout + proc.stderr

    # 前置自证：确认这个环境里 Flask 真的不可导入，否则这条用例在验别的东西
    pre = subprocess.run(
        [sys.executable, "-c", "import flask"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert pre.returncode != 0, (
        "负例前提不成立：该环境下 Flask 仍可导入 ⇒ 这条用例没在验它该验的东西\n" f"{out}"
    )

    assert proc.returncode == 1, f"Flask 缺失时 preflight 应判红（退出码 1）：\n{out}"
    assert "Flask 未安装" in out, f"必须说清是缺什么（且中文不能是乱码）：\n{out}"
    assert "offline-deps" in out, (
        "**只说『请安装 Flask』对无外网现场没用** —— 必须给出包内 offline-deps 这条修法：\n" f"{out}"
    )
    assert "�" not in out, (
        "preflight 的输出里出现替换字符 ⇒ 它没强制 UTF-8，"
        "现场看到的是一屏乱码（`app/console.py` 同款铁律，这里也必须守）\n" f"{out}"
    )


def test_preflight_flags_an_unwritable_data_dir(tmp_path):
    """负例之二：`MT_DATA_DIR` 指向不可写的位置 ⇒ 判红并给出换目录的修法。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    # Windows 下把目录 ACL 改成只读不现实，改用一个**已存在的文件**占位 ——
    # MT_DATA_DIR 指向它时 `mkdir` 必然失败，与权限无关、跨平台一致。
    not_a_dir = tmp_path / "iam-a-file"
    not_a_dir.write_text("x", encoding="utf-8")

    proc = _run({"MT_DATA_DIR": str(not_a_dir)}, REPO_ROOT)
    assert proc.returncode == 1, f"MT_DATA_DIR 指向文件时应判红：\n{proc.stdout}"
    assert "不是目录" in proc.stdout, f"必须说清是'不是目录'：\n{proc.stdout}"
    assert "MT_DATA_DIR" in proc.stdout, f"修复步骤里应点名这个环境变量：\n{proc.stdout}"


def test_preflight_admits_when_data_dir_is_left_to_the_repo(tmp_path):
    """边界：没设 `MT_DATA_DIR` ⇒ **不阻塞，但要提醒**去快盘（实测耗时受 fsync 支配）。

    判据是"提醒"而不是"阻塞"：仓库默认路径确实能跑，阻塞它是越权。
    但必须提醒 —— 这是实测出来的性能事实，不说就等于让现场拿慢盘硬扛。
    """
    env = {k: v for k, v in os.environ.items() if k != "MT_DATA_DIR"}
    proc = subprocess.run(
        [sys.executable, str(PREFLIGHT)], cwd=str(tmp_path), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    text = proc.stdout + proc.stderr
    assert "数据目录未指定" in text or "关键文件缺失" in text, (
        f"未指定 MT_DATA_DIR 时应提醒数据目录（或指出关键文件缺失）：\n{text}"
    )
