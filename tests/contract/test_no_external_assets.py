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

import json
import re
from pathlib import Path

import pytest

from contract_support import REPO_ROOT

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


def test_static_pages_local_asset_paths_resolve_as_served_urls():
    """本地引用必须按**浏览器的方式**解析后仍指向真实文件。

    这条检查是被真实缺陷逼出来的（`2026-09-30`，Playwright 浏览器走查发现）：
    页面 `/scale/` 里写 `../js/scale.js`，**磁盘上**能解析到 `app/static/js/scale.js`
    （旧检查据此判绿），但**浏览器**按 URL 解析 → 请求 `/js/scale.js` → 404
    ⇒ 页面无样式、无脚本、**整页点不动**。旧检查只证明"文件在磁盘上存在"，
    证明不了"浏览器取得到"—— 两件事在"页面 URL 与文件位置不同构"时就会分叉。

    故现在按 URL 解析（`urljoin(页面 URL, 引用)`），再按服务端真实规则映射回文件：
    `/static/<p>` → `app/static/<p>`；目录式路由（`/scale/` 等）→ 其 `index.html`；
    其余 `/<p>` → `app/static/<p>`。映射不到文件即失败。
    """
    from urllib.parse import urljoin

    served = {
        "index.html": "/",
        "scale/index.html": "/scale/",
        "admin/index.html": "/admin/",
        "customer/index.html": "/customer/",
        "game/index.html": "/game/",
    }
    checked = 0
    for rel, page_url in served.items():
        page = STATIC_DIR / rel
        for value in re.findall(r'(?:src|href)="([^"]+)"', page.read_text(encoding="utf-8")):
            if value.startswith(("#", "mailto:")):
                continue
            path = urljoin(page_url, value).split("?", 1)[0]
            # 服务端的真实规则：`/`、四个目录式页面路由、`/static/<p>`（其余一律 404）。
            # ⚠️ **不能**用"映射不到就退回 `app/static/<path>`"这种宽松兜底 —— 第一版正是这么写的，
            # 于是 `/js/scale.js` 又能"在磁盘上找到文件"、缺陷照样漏过（本轮负例把它抓了出来）。
            if path == "/":
                target = STATIC_DIR / "index.html"
            elif path in ("/scale/", "/admin/", "/customer/", "/game/"):
                target = STATIC_DIR / path.strip("/") / "index.html"
            elif path.startswith("/static/"):
                target = STATIC_DIR / path[len("/static/"):]
            else:
                raise AssertionError(
                    f"{rel} 引用 `{value}`：浏览器会请求 `{path}`，但**服务端没有这个路由**"
                    "（只有 `/`、三个页面路由与 `/static/<路径>`）—— 现场表现为无样式、无脚本、整页点不动"
                )
            assert target.is_file(), (
                f"{rel} 引用 `{value}`：浏览器会请求 `{path}`，但服务端取不到文件（映射到 {target}）"
            )
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
            # 显式编码：**不要依赖 locale 或 PYTHONUTF8**（`CP-D` 现场教训：靠环境变量的
            # 捕获在一台机器上绿、在另一台上变 `\ufffd`）。本文件里 node 的输出是 ASCII，
            # 但规则要一致 —— 捕获子进程输出一律显式 UTF-8。
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, f"{name} 语法错误：\n{result.stdout}\n{result.stderr}"



# ---------------------------------------------------------------------------
# 扫描器自身的灵敏度（合成样例，随每次 pytest 一起跑）
# ---------------------------------------------------------------------------


#: 扫描器灵敏度用的**六种外部依赖形态**（`T-036` 的收口入口也引用这一份，不另抄一遍）
EXTERNAL_SAMPLES: tuple[tuple[str, str], ...] = (
    ('<script src="https://cdn.example.com/lib.js"></script>', "外链"),
    ('<link href="//cdn.bootcss.com/x.css" rel="stylesheet">', "外链"),
    ("<style>body{background:url(https://img.example.com/bg.png)}</style>", "外链"),
    ('<style>@import url("https://fonts.googleapis.com/css?family=Roboto");</style>', "外部字体"),
    ('<script src="node_modules/vue/dist/vue.min.js"></script>', "构建产物"),
    ('<img src="file:///C:/data/logo.png">', "仓库外绝对路径"),
)

#: 反向负例：站内相对路径**不得误报**
CLEAN_SAMPLE = (
    '<link href="../css/app.css" rel="stylesheet">\n'
    '<script src="../js/scale.js"></script>\n'
    "<style>body{background:url(./dot.png)}</style>\n"
    '<a href="/scale/">操作端</a>\n'
)


@pytest.mark.parametrize(("sample", "expected_hint"), EXTERNAL_SAMPLES)
def test_scanner_flags_external_dependency_samples(sample: str, expected_hint: str):
    """灵敏度负例：每种外部依赖形态**必须**被判红（否则检查是摆设）。"""
    hits = scan_static_text(sample, "合成样例")
    assert hits, f"该依赖形态未被判红，检查存在盲区：{sample!r}"
    assert any(expected_hint in hit for hit in hits), (
        f"判红了但提示不对（期望含「{expected_hint}」）：{hits}"
    )


def test_scanner_does_not_flag_local_relative_paths():
    """反向负例：**站内相对路径不得误报** —— 否则只能靠"别写检查"来让 CI 变绿。"""
    assert scan_static_text(CLEAN_SAMPLE, "干净样例") == []


# ---------------------------------------------------------------------------
# `/game/` 的**经 JS 引用**的资源（`task-18` 追加）
#
# 原用例 `test_static_pages_local_asset_paths_resolve_as_served_urls` 只扫 HTML 的 `src`/`href`。
# 而演示游戏的**精灵资源不经 HTML**：`sprites.js` 里 `SPRITE_DIR + "manifest.json"` 与
# `SPRITE_DIR + sheet.file` 是**用 JS 拼出来的**。⇒ 只扫 HTML 的检查对它们**完全无覆盖**：
# 谁把 `SPRITE_DIR` 从 `/static/game/sprites/` 改成相对路径 `sprites/`，浏览器就会去请求
# `/game/sprites/manifest.json`（服务端没有这个路由 ⇒ 404），而按 `sprites.js` 的设计
# **失败会静默降级成色块** —— 页面不报错、检查也不红，正是"加了个页面却没人扫它的资源"。
# ---------------------------------------------------------------------------

GAME_DIR = STATIC_DIR / "game"
SPRITE_DIR = GAME_DIR / "sprites"
GAME_JS_DIR = GAME_DIR / "js"
PROBE_PREFIX = "__sensitivity_probe"

#: 服务端**真实**路由规则（与上面那条用例同一份口径，不另立一套）
PAGE_ROUTES = ("/", "/scale/", "/admin/", "/customer/", "/game/")
PAGE_URL = "/game/"


def resolve_served_path(page_url: str, reference: str) -> str | None:
    """按**浏览器的方式**把页面里的引用解析成服务端路径；`None` = 服务端没有这个路由。

    故意**不**做"映射不到就退回 `app/static/<path>`"的宽松兜底（第一版正是这么写的，
    于是 `/js/scale.js` 又能"在磁盘上找到文件"、缺陷照样漏过）。
    """
    from urllib.parse import urljoin

    path = urljoin(page_url, reference).split("?", 1)[0]
    return path if path in PAGE_ROUTES or path.startswith("/static/") else None


def sprite_base_from_js() -> str:
    """从 `sprites.js` 读精灵取图基址（判据的一部分，不靠猜）。"""
    text = (GAME_JS_DIR / "sprites.js").read_text(encoding="utf-8")
    match = re.search(r'var\s+SPRITE_DIR\s*=\s*"([^"]+)"', text)
    assert match, "`sprites.js` 里找不到 `SPRITE_DIR` —— 取图基址必须可机械读取"
    return match.group(1)


def game_referenced_urls() -> list[str]:
    """`/game/` 页真正会去取的资源：HTML 的 `src`/`href` **+ JS 拼出来的精灵资源**。"""
    page = (GAME_DIR / "index.html").read_text(encoding="utf-8")
    urls = list(re.findall(r'(?:src|href)="([^"]+)"', page))
    base = sprite_base_from_js()
    manifest = json.loads((SPRITE_DIR / "manifest.json").read_text(encoding="utf-8"))
    urls.append(base + "manifest.json")
    urls.extend(base + sheet["file"] for sheet in manifest["sheets"].values())
    return urls


def test_game_page_and_js_referenced_assets_are_served(client):
    """`AC-037` / `AC-034`：`/game/` 引用的**每一个**资源（含 JS 拼的）都要真能取到。"""
    urls = game_referenced_urls()
    assert len(urls) >= 15, f"只解析到 {len(urls)} 个引用，太少（清单可能被改动）：{urls}"
    for value in urls:
        path = resolve_served_path(PAGE_URL, value)
        assert path is not None, (
            f"`/game/` 引用 `{value}`：浏览器会请求站外/不存在的路由（服务端只有 `/`、页面路由与 `/static/<路径>`）"
        )
        assert client.get(path).status_code == 200, f"`{value}` 解析成 `{path}`，服务端取不到（非 200）"


def test_game_sprite_base_is_an_absolute_static_path():
    """取图基址**必须**是 `/static/...` 绝对路径 —— 相对路径会被浏览器解析到 `/game/...`（404）。"""
    base = sprite_base_from_js()
    assert base.startswith("/static/"), f"`SPRITE_DIR` 必须是 `/static/...` 绝对路径，实际 `{base}`"
    assert resolve_served_path(PAGE_URL, base) == base


def test_game_manifest_sheets_exist_on_disk_and_inside_their_sheets():
    """清单自洽：每个 sheet 文件存在，且每个精灵矩形落在其 sheet 内（尺寸口径的唯一权威）。"""
    manifest = json.loads((SPRITE_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sprites"], "manifest 里没有任何精灵"
    for name, sheet in manifest["sheets"].items():
        assert (SPRITE_DIR / sheet["file"]).is_file(), f"sheet `{name}` 的文件不存在：{sheet['file']}"
    for name, sprite in manifest["sprites"].items():
        sheet = manifest["sheets"][sprite["sheet"]]
        assert sprite["x"] + sprite["w"] <= sheet["w"], f"精灵 `{name}` 横向越出 sheet"
        assert sprite["y"] + sprite["h"] <= sheet["h"], f"精灵 `{name}` 纵向越出 sheet"


def test_sensitivity_relative_sprite_base_would_404_in_a_browser():
    """**灵敏度负例（纯函数，不落盘）**：把基址换成相对路径 ⇒ 必须被判成"服务端没有这个路由"。

    这条证明上面的判据**能失败**：`sprites/` 从 `/game/` 解析出 `/game/sprites/...`，
    而服务端只有 `/static/...` 与页面路由 ⇒ 必须返回 `None`（判红），不得被宽松兜底救活。
    """
    assert resolve_served_path(PAGE_URL, "sprites/") is None, "相对基址必须判红（否则判据是摆设）"
    assert resolve_served_path(PAGE_URL, "sprites/manifest.json") is None
    assert resolve_served_path(PAGE_URL, "/static/game/sprites/manifest.json") is not None
    assert resolve_served_path(PAGE_URL, "./css/game.css") is None, "相对 CSS 引用同样会 404"


def test_sensitivity_probe_cdn_script_in_game_dir_turns_the_scan_red():
    """负例：往 `app/static/game/js/` 塞一个带 CDN 的脚本 ⇒ `AC-037` 扫描**必红**（用完即删）。

    这条同时证明**扫描面真的覆盖了 `/game/`**（不是只扫老目录）。
    """
    probe = GAME_JS_DIR / f"{PROBE_PREFIX}_cdn.js"
    probe.write_text('var s = document.createElement("script");\n'
                     's.src = "https://cdn.example.com/game-lib.js";\n', encoding="utf-8")
    try:
        hits = scan_static_tree()
        assert hits, "往 `/game/js/` 塞 CDN 脚本后扫描必须变红 —— 否则 `/game/` 不在扫描面内"
        assert any("__sensitivity_probe_cdn.js" in hit for hit in hits), hits
    finally:
        probe.unlink()
    assert scan_static_tree() == [], "负例用完必须还原（删除探针后扫描必须复绿）"


def test_no_sensitivity_probe_left_behind():
    """守卫：探针一个都不许留在仓库里（负例崩溃/中断时由这条兜住）。"""
    leftovers = [p.relative_to(REPO_ROOT).as_posix() for p in STATIC_DIR.rglob(f"{PROBE_PREFIX}*")]
    assert leftovers == [], f"灵敏度探针没清干净：{leftovers}"
