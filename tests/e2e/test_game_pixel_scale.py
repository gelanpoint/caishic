"""像素风缩放不变量（T-GAME-03 回归）。

**为什么要有这条**：像素风的锐利度只靠一条不变量撑着 ——
**画布的"显示尺寸"必须等于它的"后备像素尺寸"**（1:1），且放大倍数是**整数**。
第一版把显示尺寸交给 CSS `width: 100%`，容器比画布窄时浏览器就按**分数比例缩小**：
实测 1280 窗口下显示 882px（倍率 0.8613），画布水平游程 **39.5% 变成奇数** —— 像素被抻坏。
而这条退化**不报错、不白屏、测试也全绿**，只能靠真浏览器量像素才发现。

所以这里用 Node 载入**真的** `main.js`（只桩掉 DOM 与协作者，不碰被测逻辑），
直接对 `GameApp.state.scale` / `canvas.width` / `canvas.style.width` 断言不变量。
真浏览器里的像素级复现见 `docs/game/` 的实玩记录（Chromium headless + 游程统计）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_JS = REPO_ROOT / "app" / "static" / "game" / "js" / "main.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not NODE, reason="环境里没有 node，无法执行前端缩放逻辑（**这不是通过，是没检查**）"
)

#: 只桩 DOM 与协作者；`main.js` 自己的 setupCanvas / applyScale / fitScale 全部**真跑**。
HARNESS = r"""
global.window = global;
global.innerHeight = 900;

const resizeListeners = [];
global.addEventListener = (type, fn) => {
  if (type === "resize") { resizeListeners.push(fn); }
};

function makeEl(id) {
  return {
    id, textContent: "", innerHTML: "", className: "", value: "",
    style: {}, children: [], scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild() {}, addEventListener() {}, setAttribute() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 20,
                                    right: 100, bottom: 20 }),
    querySelector: () => null, querySelectorAll: () => [],
  };
}

const stage = makeEl("stage");
stage.clientWidth = 1024;                       // 由测试改写，模拟不同窗口宽度

const canvas = makeEl("gameCanvas");
canvas.parentNode = stage;
canvas.width = 1152;                            // HTML 里的占位值，JS 应当覆盖它
canvas.height = 704;
canvas.getContext = () => new Proxy({}, { get: () => () => {} });

const elements = { gameCanvas: canvas, stage: stage };
global.document = {
  readyState: "complete",
  addEventListener() {},
  getElementById: (id) => (elements[id] = elements[id] || makeEl(id)),
  documentElement: { clientWidth: 1024, clientHeight: 900 },
};

/* ---- 协作者：全部桩掉，只为让 boot() 跑到 setupCanvas() ---- */
const WORLD = { widthPx: 512, heightPx: 384, plots: [], cols: 32, rows: 24, tile: 16 };
global.GameApi = {
  roster: () => Promise.resolve({ businessDate: "2026-10-07",
                                  stalls: ["A-01", "A-02", "A-03"] }),
  stallData: () => Promise.resolve({ items: [] }),
  cachedStall: () => null, clearCache() {}, yuan: (c) => (c / 100).toFixed(2),
};
global.GameMap = { build: () => WORLD, plotAtPoint: () => null };
global.GameEntities = { build: () => ({ step() {}, syncItems() {}, hitTest: () => null,
                                        forStall: () => [], markError() {} }) };
global.GameRender = { draw() {}, invalidate() {} };
global.GamePanels = {
  init() {}, setBadge() {}, log() {}, viewLabel: () => "商家", node: () => makeEl("d"),
  opsPanel: () => makeEl("b"), showInfo() {}, hideHover() {}, showHover() {},
  toast() {}, button: () => makeEl("btn"), setView() {},
};
global.GameSprites = { load: () => Promise.resolve({ mode: "ready", note: "ok" }) };
global.GameOps = {};

require(process.argv[2]);

function snapshot(label) {
  const scale = global.GameApp.state.scale;
  return {
    label,
    stageWidth: stage.clientWidth,
    scale,
    isInteger: Number.isInteger(scale) && scale >= 1,
    backingWidth: canvas.width,
    backingMatchesScale: canvas.width === WORLD.widthPx * scale,
    styleWidth: canvas.style.width,
    displayIsBacking: canvas.style.width === canvas.width + "px",  // ← 核心不变量
    noPercentWidth: canvas.style.width.indexOf("%") === -1,
  };
}

setTimeout(() => {
  const out = [snapshot("boot")];
  for (const w of [2000, 1500, 1024, 900, 700, 400]) {
    stage.clientWidth = w;
    resizeListeners.forEach((fn) => fn());       // 走真实的 resize 分支
    out.push(snapshot("w=" + w));
  }
  process.stdout.write(JSON.stringify(out));
}, 50);
"""


def _run(harness: Path) -> list[dict]:
    done = subprocess.run(
        [NODE, str(harness), str(MAIN_JS)],
        capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
        encoding="utf-8", errors="replace",
    )
    assert done.returncode == 0, f"Node 跑前端缩放逻辑失败：\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout)


@pytest.fixture(scope="module")
def snaps(tmp_path_factory) -> list[dict]:
    harness = tmp_path_factory.mktemp("scale") / "scale_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    return _run(harness)


def test_scale_is_always_a_positive_integer(snaps):
    """放大倍数必须是正整数 —— 分数倍率就是像素被重采样的根因。"""
    for s in snaps:
        assert s["isInteger"], f"{s['label']}：scale={s['scale']} 不是正整数"


def test_display_size_equals_backing_store(snaps):
    """**核心不变量**：显示尺寸 == 后备像素尺寸，杜绝任何分数比例缩放。"""
    for s in snaps:
        assert s["displayIsBacking"], (
            f"{s['label']}：画布显示尺寸 {s['styleWidth']!r} 与后备像素 "
            f"{s['backingWidth']}px 不一致 ⇒ 浏览器会按分数比例缩放，像素会糊"
        )


def test_never_uses_percentage_width(snaps):
    """第一版的真缺陷：`canvas.style.width = '100%'` 让容器宽度决定缩放比例。"""
    for s in snaps:
        assert s["noPercentWidth"], f"{s['label']}：显示宽度不该是百分比（{s['styleWidth']!r}）"


def test_backing_pixels_are_a_multiple_of_the_world(snaps):
    """后备像素 = 地图原生尺寸 × 整数倍，绘制坐标才不会被错位。"""
    for s in snaps:
        assert s["backingMatchesScale"], (
            f"{s['label']}：后备宽度 {s['backingWidth']} != 512 × {s['scale']}"
        )


def test_narrow_stage_falls_back_to_one_x_not_fractional(snaps):
    """放不下 2 倍时退到 1 倍（宁可地图小、也不缩糊），且绝不出现分数倍率。"""
    narrow = [s for s in snaps if s["stageWidth"] < 1026]
    assert narrow, "样本里应当有窄舞台"
    for s in narrow:
        assert s["scale"] == 1, f"{s['label']}：窄舞台应退到 1 倍，实际 {s['scale']}"


def test_wide_stage_uses_two_x(snaps):
    """够宽时用 2 倍（上限），保证演示时地图够大。"""
    wide = [s for s in snaps if s["stageWidth"] >= 1026]
    assert wide, "样本里应当有宽舞台"
    for s in wide:
        assert s["scale"] == 2, f"{s['label']}：宽舞台应为 2 倍，实际 {s['scale']}"
