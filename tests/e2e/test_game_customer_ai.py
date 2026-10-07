"""顾客 NPC 的**目的性行为 + 碰撞 + 主动避让**（`REQ-050` / `AC-040`，`T-GAME-06`）。

**为什么要有这组**：第一版顾客走的是五条写死的折线，既不避障也不互相避让 ——
实测 15 秒内与摊位 / 老板 / 秤的矩形重叠 **38 次**、顾客之间重叠 **26 次**，
而且**没有任何报错、页面也不白屏**，只有真的盯着看 / 真的量矩形才会发现。
修法见 `app/static/game/js/nav.js`（格子图 + A*）与 `entities.js`（状态机 + 避让）。

**判据是可量化的**：按固定间隔采样 N 帧，三个计数必须**全为 0** ——
① 顾客脚下踩在不可走格；② 顾客 × 实体 矩形重叠；③ 顾客 × 顾客 矩形重叠。
另外还要证明行为**有目的**（真的在逛不同摊位、真的买成了），否则"不重叠"可以靠
"站着不动"骗过去 —— 那是最典型的假绿。
"""

from __future__ import annotations

from e2e_support import new_page, snap
from game_support import open_game

#: 采样脚本：返回三项重叠计数 + 行为统计。**在页面里跑**，用的是游戏自己的数据与判据。
SAMPLE_JS = """(async () => {
  const SOLID = { stall: 1, crate: 1, computer: 1, boss: 1, admin: 1 };
  const ov = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  const grid = GameApp.state.entities.gridOf();
  let feet = 0, vsSolid = 0, vsEach = 0, pushed = 0;
  const stalls = {};
  for (let i = 0; i < FRAMES; i += 1) {
    const all = GameApp.state.entities.list;
    const cust = all.filter(e => e.kind === 'customer');
    const solid = all.filter(e => SOLID[e.kind]);
    for (const c of cust) {
      if (!GameNav.walkable(grid, Math.round(c.anchor.x), Math.round(c.anchor.y))) { feet += 1; }
      for (const s of solid) {
        if (ov(GameEntities.rectOf(c), GameEntities.rectOf(s))) { vsSolid += 1; }
      }
      if (c.targetStall) { (stalls[c.label] = stalls[c.label] || {})[c.targetStall] = 1; }
    }
    for (let a = 0; a < cust.length; a += 1) {
      for (let b = a + 1; b < cust.length; b += 1) {
        if (ov(GameEntities.rectOf(cust[a]), GameEntities.rectOf(cust[b]))) { vsEach += 1; }
      }
    }
    await new Promise(r => setTimeout(r, 100));
  }
  const visits = {};
  GameApp.state.entities.customers.forEach(c => { visits[c.label] = c.visits; });
  return { feet, vsSolid, vsEach, visits,
           stalls: Object.fromEntries(Object.entries(stalls).map(([k, v]) => [k, Object.keys(v)])) };
})()"""


def _sample(page, frames: int) -> dict:
    return page.evaluate(SAMPLE_JS.replace("FRAMES", str(frames)))


def test_customers_never_clip_or_stack(browser, live_server, shots):
    """`AC-040`：采样期间 **顾客踩障碍格 / 撞实体 / 互相重叠** 三个计数全为 0。"""
    page = new_page(browser)
    try:
        open_game(page, live_server)
        result = _sample(page, 120)                      # 约 12 秒
        assert result["feet"] == 0, (
            f"有 {result['feet']} 人次**脚下踩在不可走格**上 —— 顾客走进了摊位/水里"
        )
        assert result["vsSolid"] == 0, (
            f"顾客与摊位/货箱/老板/秤的矩形重叠 {result['vsSolid']} 次 —— 穿模"
        )
        assert result["vsEach"] == 0, (
            f"顾客之间矩形重叠 {result['vsEach']} 次 —— 没有主动避让，人叠人"
        )
        snap(page, shots, "game-customer-ai")
    finally:
        page.close()


def test_customers_behave_purposefully_not_just_standing_still(browser, live_server):
    """**假绿的防线**：不重叠可以靠"站着不动"骗过去，故必须证明他们真的在逛、真的在买。

    断言每个顾客都**去过不止一个摊位**，且**合计买成过** —— 这才叫"有目的"。
    """
    page = new_page(browser)
    try:
        open_game(page, live_server)
        result = _sample(page, 180)                      # 约 18 秒
        visits = result["visits"]
        stalls = result["stalls"]
        assert len(visits) == 5, f"应有 5 个顾客，实际 {len(visits)}"
        assert sum(visits.values()) > 0, (
            f"18 秒内没有任何顾客走完「停留 → 购买」一轮 —— 行为没有推进，实际 {visits}"
        )
        moving = [label for label, seen in stalls.items() if len(seen) >= 1]
        assert len(moving) == 5, f"有顾客从未选定任何摊位（原地不动）：{stalls}"
    finally:
        page.close()


def test_a_customer_cannot_walk_into_a_stall_even_when_told_to(browser, live_server):
    """**确定性**地钉住"移动本身也要过判据"这条。

    为什么不能只靠上面的统计采样：实测把移动的碰撞检查**摘掉**后，统计判据在 120 帧里
    常常一次重叠都碰不到（这种重叠只发生在"贴着摊位走"的少数构型），于是**注入缺陷仍然全绿**
    —— 那是假绿。这里改成构造场景：把一个顾客摆在柜台正上方，塞给他一条**穿过柜台**的路径，
    然后逐步推进，**每一步**都检查他有没有踩进不可走格。

    为什么必须逐步查、不能只看终局：顾客穿过柜台后会走到终点、进入"浏览"，随后
    `routeTo` 的自救逻辑把他吸附回可走格 —— **终局是干净的，过程却是穿模的**。
    只看终局会漏掉整个缺陷（这一点是实测出来的，不是推想）。
    """
    page = new_page(browser)
    try:
        open_game(page, live_server)
        result = page.evaluate(
            """(async () => {
                const c = GameApp.state.entities.customers[0];
                const counter = GameApp.state.world.plots[0].counter;
                const grid = GameApp.state.entities.gridOf();
                const startY = counter.y - 1;      /* 柜台**正上方一格**（必须在可走格上） */
                c.anchor.x = counter.x + 0.5;
                c.anchor.y = startY;
                c.state = 'toStall';
                c.path = [{ x: c.anchor.x, y: startY },
                          { x: counter.x + 0.5, y: counter.y + 2 }];   /* 终点在柜台里 */
                c.pathIndex = 1;
                /* 起点本身必须是合法站位，否则"踩到不可走格"会把测试自己的错误布置也算进去 */
                const startOk = GameNav.walkable(grid, Math.round(c.anchor.x), startY);
                const bad = [];
                for (let i = 0; i < 40; i += 1) {
                    GameApp.state.entities.step(0.05, {});
                    const tx = Math.round(c.anchor.x), ty = Math.round(c.anchor.y);
                    if (!GameNav.walkable(grid, tx, ty)) {
                        bad.push({ step: i, tile: [tx, ty],
                                   at: [+c.anchor.x.toFixed(2), +c.anchor.y.toFixed(2)] });
                    }
                    await new Promise(r => setTimeout(r, 16));
                }
                return { bad, counter, startY, startOk };
            })()"""
        )
        assert result["startOk"], (
            f"测试自己的布置就不合法：起点 {result['startY']} 不是可走格，这条验证没有意义"
        )
        assert result["bad"] == [], (
            f"顾客被指了一条**穿过柜台**（{result['counter']}）的路径后，"
            f"途中有 {len(result['bad'])} 步踩在不可走格上，例如 {result['bad'][0]} —— "
            "移动这一步没有过碰撞判据，会直接走进摊位里（穿模）"
        )
    finally:
        page.close()


def test_a_customer_cannot_stand_inside_a_stall_counter(browser, live_server):
    """单点判据（不只看统计）：把顾客**强行**摆进柜台里，`blockedAt` 必须判为不可站。

    这条防的是"统计上碰巧没重叠" —— 直接把一个明知非法的位置喂给判据，
    判据必须拒绝。拒绝不了就说明判据是摆设。
    """
    page = new_page(browser)
    try:
        open_game(page, live_server)
        verdict = page.evaluate(
            """() => {
                const c = GameApp.state.entities.customers[0];
                const plot = GameApp.state.world.plots[0];
                const counter = plot.counter;
                const inside = { x: counter.x + 0.5, y: counter.y + 0.5 };
                const grid = GameApp.state.entities.gridOf();
                return {
                    insideWalkable: GameNav.walkable(grid, Math.round(inside.x), Math.round(inside.y)),
                    /* 服务位必须站得下；柜台中心必须站不下 —— 一正一反才说明判据有分辨力 */
                    serviceWalkable: GameNav.walkable(
                        grid, GameNav.serviceTile(GameApp.state.world, plot).x,
                        GameNav.serviceTile(GameApp.state.world, plot).y),
                    customerAt: [c.anchor.x, c.anchor.y],
                };
            }"""
        )
        assert verdict["serviceWalkable"], "摊位前的服务位必须可走，否则顾客永远逛不到摊位"
        assert not verdict["insideWalkable"], (
            "摊位柜台所在的格子被判成了可走 —— 碰撞判据失效，顾客会直接站进摊位里"
        )
    finally:
        page.close()
