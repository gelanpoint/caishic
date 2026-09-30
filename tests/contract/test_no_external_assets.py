"""`T-029` 静态自检：**全部静态资源零外部依赖**（`REQ-025`；`AGENTS.md` §3 硬要求 4；`AC-013`）。

为什么这条检查必须存在（不是洁癖）：演示现场**可能就是没有外网**。
一个 `https://cdn...` 的 `<script>` 在联网的开发机上"看起来没问题"，到了现场会让页面**卡住等超时**
（脚本阻塞解析）或**样式整体缺失**，而演示人只会看到"页面坏了"。故本检查把"零外部资源"
从"我们记得别引"变成"**引了就红**"。

判据（逐条机械、可复算）：

1. **不得出现绝对或协议相对的外链**：`http://` / `https://` / `//host/...`（含 `<script src>`、
   `<link href>`、CSS `url(...)`、`@import`、`fetch("https://...")`）；
2. **不得引用构建产物或包管理器目录**：`node_modules` / `dist/` / `build/` / `webpack` / `vite` /
   `bundle.js`（本期前端是**无构建**的纯静态文件，`plan.md` §4）；
3. **不得引用仓库外的本地绝对路径**：`file://`、`C:\\`、`/home/`；
4. **不得内联远程字体**：`fonts.googleapis.com` / `fonts.gstatic.com` / `@font-face` 指向外部。

**灵敏度要求（本文件的第二根支柱）**：检查必须**能失败**。
除了对真实静态目录扫描，本文件还用**合成样例**回归扫描器本身（`test_scanner_*`）：
故意塞一个 CDN `<script src>` / 协议相对 `//cdn...` / CSS `url(https://...)` / `node_modules` 引用，
**逐个断言必须报红**；再放一个"干净样例"断言**不得误报**（防把本地相对路径也判成外链）。
按 `T-036` 家族的做法：合成负例随每次 `pytest` 一起跑，检查不会悄悄失效。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import REPO_ROOT

STATIC_DIR = REPO_ROOT / "app" / "static"

#: 静态文件后缀（本期只有这四种：无构建、无二进制资源）
STATIC_SUFFIXES = (".html", ".js", ".css", ".json")

#: 外链判据：绝对 URL、协议相对 URL、远程字体域
EXTERNAL_URL_RE = re.compile(r"(?:https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.IGNORECASE)
#: 构建产物 / 包管理器目录引用
BUILD_REF_RE = re.compile(
    r"(?:node_modules|/dist/|/build/|webpack|vite|bundle\.js|\.min\.js)", re.IGNORECASE
)
#: 仓库外本地绝对路径
LOCAL_ABS_RE = re.compile(r"(?:file://|[A-Za-z]:\\\\|/home/|/Users/)")
#: 外部字体（单独列出，便于报错时直说"这是字体"）
REMOTE_FONT_RE = re.compile(r"fonts\.(?:googleapis|gstatic)\.com", re.IGNORECASE)


def scan_static_text(text: str, where: str) -> list[str]:
    """扫描一段静态文本，返回问题清单（空 = 干净）。**纯函数**，故合成样例可直接喂给它。"""
    hits: list[str] = []
    for match in EXTERNAL_URL_RE.finditer(text):
        hits.append(f"{where}: 出现外链 `{match.group(0)}`（REQ-025：不得引用任何外部资源）")
    for match in REMOTE_FONT_RE.finditer(text):
        hits.append(f"{where}: 出现外部字体域 `{match.group(0)}`")
    for match in BUILD_REF_RE.finditer(text):
        hits.append(f"{where}: 引用构建产物/包管理器目录 `{match.group(0)}`（本期前端无构建）")
    for match in LOCAL_ABS_RE.finditer(text):
        hits.append(f"{where}: 引用仓库外绝对路径 `{match.group(0)}`")
    return hits


def scan_static_tree(root: Path = STATIC_DIR) -> list[str]:
    """扫描整个静态目录（含子目录），返回问题清单。"""
    assert root.is_dir(), f"静态目录不存在：{root}"
    hits: list[str] = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in STATIC_SUFFIXES:
            continue
        scanned += 1
        rel = path.relative_to(REPO_ROOT).as_posix()
        hits.extend(scan_static_text(path.read_text(encoding="utf-8", errors="replace"), rel))
    assert scanned > 0, f"{root} 下没有任何静态文件（检查会假绿 —— 那是更坏的情况）"
    return hits


# ---------------------------------------------------------------------------
# 真实静态目录
# ---------------------------------------------------------------------------


def test_no_external_assets_in_static_tree():
    """`REQ-025`：全部静态资源零外部依赖（断网也能完整演示）。"""
    hits = scan_static_tree()
    assert not hits, "静态资源出现外部依赖（现场断网会直接坏页面）：\n" + "\n".join(hits)


def test_static_entry_pages_exist_and_are_local():
    """三个页面入口与入口导航页都必须真的存在（否则演示地址 404，且上面的扫描会假绿）。"""
    for rel in (
        "index.html",
        "scale/index.html",
        "admin/index.html",
        "customer/index.html",
        "css/app.css",
        "js/scale.js",
        "js/offline.js",
        "js/admin.js",
        "js/customer.js",
    ):
        assert (STATIC_DIR / rel).is_file(), f"缺少静态文件：app/static/{rel}"


def test_static_pages_reference_only_local_assets():
    """页面里引用的每个 `src`/`href` 都必须是**站内相对路径**（不得以 `//` 或域名开头）。"""
    for rel in ("index.html", "scale/index.html", "admin/index.html", "customer/index.html"):
        text = (STATIC_DIR / rel).read_text(encoding="utf-8")
        for attr in ("src", "href"):
            for value in re.findall(rf'{attr}="([^"]+)"', text):
                assert not value.startswith(("http://", "https://", "//")), (
                    f"{rel} 的 {attr}=\"{value}\" 指向站外"
                )


def test_static_pages_local_asset_paths_actually_resolve():
    """本地引用必须**真的能解析到文件**。

    "零外链"只保证不引站外，**不保证站内路径没写错**：`../js/scal.js` 这种手误同样是外链检查的盲区，
    而它的现场表现与引 CDN 完全一样（页面打不开/功能缺失）。故这里逐个解析并断言文件存在；
    站内绝对路径（`/static/...`）也一并覆盖。
    """
    checked = 0
    for rel in ("index.html", "scale/index.html", "admin/index.html", "customer/index.html"):
        page = STATIC_DIR / rel
        for attr in ("src", "href"):
            for value in re.findall(rf'{attr}="([^"]+)"', page.read_text(encoding="utf-8")):
                if value.startswith("#") or value.startswith("mailto:"):
                    continue
                if value.startswith("/"):
                    # 站内绝对路径：既可能是资源（`/static/js/x.js`），也可能是页面路由（`/scale/`）
                    inner = value.lstrip("/")
                    if inner.startswith("static/"):
                        inner = inner[len("static/"):]
                    base = (STATIC_DIR / inner).resolve() if inner else STATIC_DIR
                    target = base / "index.html" if base.is_dir() else base
                else:
                    target = (page.parent / value).resolve()
                assert target.is_file(), f"{rel} 引用 `{value}`，但解析不到文件：{target}"
                checked += 1
    assert checked >= 8, f"只解析到 {checked} 个引用，太少（清单可能被改动）"


def test_js_files_parse_as_javascript():
    """四个脚本必须能被解析（无语法错误）—— 页面坏掉的另一半原因就藏在这里。

    判据不靠"读一遍代码"：用 `node --check` 真解析（Node 属于演示机常见的本地工具）。
    若环境里没有 Node，则**跳过并明确说明**（不允许静默通过假装检查过）。
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("环境里没有 node，无法做 JS 语法解析（这不是通过，是没检查）")
    for name in ("offline.js", "scale.js", "admin.js", "customer.js"):
        result = subprocess.run(
            [node, "--check", str(STATIC_DIR / "js" / name)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{name} 语法错误：\n{result.stdout}\n{result.stderr}"



# ---------------------------------------------------------------------------
# 扫描器自身的灵敏度（合成样例，随每次 pytest 一起跑）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sample", "expected_hint"),
    [
        ('<script src="https://cdn.example.com/lib.js"></script>', "外链"),
        ('<link href="//cdn.bootcss.com/x.css" rel="stylesheet">', "外链"),
        ("<style>body{background:url(https://img.example.com/bg.png)}</style>", "外链"),
        ('<style>@import url("https://fonts.googleapis.com/css?family=Roboto");</style>', "外部字体"),
        ('<script src="node_modules/vue/dist/vue.min.js"></script>', "构建产物"),
        ('<img src="file:///C:/data/logo.png">', "仓库外绝对路径"),
    ],
)
def test_scanner_flags_external_dependency_samples(sample: str, expected_hint: str):
    """灵敏度负例：每种外部依赖形态**必须**被判红（否则检查是摆设）。"""
    hits = scan_static_text(sample, "合成样例")
    assert hits, f"该依赖形态未被判红，检查存在盲区：{sample!r}"
    assert any(expected_hint in hit for hit in hits), (
        f"判红了但提示不对（期望含「{expected_hint}」）：{hits}"
    )


def test_scanner_does_not_flag_local_relative_paths():
    """反向负例：**站内相对路径不得误报** —— 否则只能靠"别写检查"来让 CI 变绿。"""
    clean = (
        '<link href="../css/app.css" rel="stylesheet">\n'
        '<script src="../js/scale.js"></script>\n'
        "<style>body{background:url(./dot.png)}</style>\n"
        '<a href="/scale/">操作端</a>\n'
    )
    assert scan_static_text(clean, "干净样例") == []
