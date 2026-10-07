/* 演示游戏 · 寻路与可通行区域（`REQ-050` / `AC-040`）。
 *
 * 为什么单独一层：顾客原先走的是**写死的几个路径点直连**（`entities.js` 里那五条折线），
 * 于是既没有"目的"、也**不看障碍** —— 实测 15 秒内顾客与摊位/老板/秤的矩形重叠 38 次，
 * 顾客之间重叠 26 次（穿模）。修法不是把折线挪一挪，而是**先有一张"哪里能走"的格子图**，
 * 再让顾客沿格子走。障碍直接**由实体的像素矩形反推**（`GameEntities.rectOf`），
 * 所以美术尺寸一变、摊位一挪，格子图自动跟着变，不需要手工维护第二份坐标表。
 *
 * 可通行判据：**只认 `tile_floor` 与 `tile_path`** —— 草地与水都不是市场地面，
 * 不让顾客踏出去（也就顺带把"绕出地图"堵死了）。
 */

window.GameNav = (function () {
  "use strict";

  var TILE = 16;
  var WALKABLE_TILES = { tile_floor: true, tile_path: true };
  /* A* 四邻（不走斜线）：斜穿两个格子的夹角时，像素上会擦到障碍角。 */
  var DIRS = [[1, 0], [-1, 0], [0, 1], [0, -1]];

  /** 由世界地形 + 实体矩形，算出「1 = 不可走」的格子图。
   *
   *  **三趟**，缺一不可：
   *  1. 地形：只认 `tile_floor` / `tile_path`；
   *  2. 实体自身占的格（柜台、货箱、电脑…）；
   *  3. **顾客身体会压到实体的格** —— 人高 24px 比一格（16px）高，脚下那格可走不等于站得下。
   *     少了这一趟就会出这样的鬼：顾客 `anchor.y = 6.51` 站在柜台里，而
   *     `Math.round(6.51) === 7` 让"脚下那格"判定为可走 —— 实测重叠 254px²。
   *     `body` 传顾客精灵尺寸（`GameSprites.size`），不写死。 */
  function buildGrid(world, solids, body) {
    var grid = [];
    for (var y = 0; y < world.rows; y += 1) {
      var line = [];
      for (var x = 0; x < world.cols; x += 1) {
        line.push(WALKABLE_TILES[world.tiles[y][x]] ? 0 : 1);
      }
      grid.push(line);
    }
    var rects = solids || [];
    rects.forEach(function (rect) {
      markRect(grid, rect);
    });
    var bw = body && body.w ? body.w : TILE;
    var bh = body && body.h ? body.h : TILE * 1.5;
    for (var gy = 0; gy < world.rows; gy += 1) {
      for (var gx = 0; gx < world.cols; gx += 1) {
        if (grid[gy][gx]) { continue; }
        /* 站在 (gx,gy) 时，身体矩形 = 底边中点锚定 */
        var bodyRect = { x: gx * TILE, y: (gy + 1) * TILE - bh, w: bw, h: bh };
        for (var i = 0; i < rects.length; i += 1) {
          if (overlaps(bodyRect, rects[i])) { grid[gy][gx] = 1; break; }
        }
      }
    }
    return grid;
  }

  function overlaps(a, b) {
    return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  }

  /** 把一个像素矩形覆盖到的格子全标成不可走（宁可保守，不可漏）。 */
  function markRect(grid, rect) {
    var x0 = Math.floor(rect.x / TILE), x1 = Math.floor((rect.x + rect.w - 1) / TILE);
    var y0 = Math.floor(rect.y / TILE), y1 = Math.floor((rect.y + rect.h - 1) / TILE);
    for (var y = y0; y <= y1; y += 1) {
      for (var x = x0; x <= x1; x += 1) {
        if (grid[y] && grid[y][x] !== undefined) { grid[y][x] = 1; }
      }
    }
  }

  function walkable(grid, x, y) {
    return !!grid[y] && grid[y][x] === 0;
  }

  /** 找离 (x,y) 最近的可走格（顾客被挤到障碍上时用来自救，避免永久卡死）。 */
  function nearestWalkable(grid, x, y, maxRing) {
    if (walkable(grid, x, y)) { return { x: x, y: y }; }
    var limit = maxRing === undefined ? 6 : maxRing;
    for (var r = 1; r <= limit; r += 1) {
      for (var dy = -r; dy <= r; dy += 1) {
        for (var dx = -r; dx <= r; dx += 1) {
          if (Math.max(Math.abs(dx), Math.abs(dy)) !== r) { continue; }
          if (walkable(grid, x + dx, y + dy)) { return { x: x + dx, y: y + dy }; }
        }
      }
    }
    return null;
  }

  /** A*（四邻、等代价 ⇒ 退化成 BFS 的最短步数，但保留 A* 以便日后加地形代价）。
   *  格子图只有 32×24=768 格，用普通数组当开表足够快，不必上二叉堆。 */
  function findPath(grid, from, to) {
    if (!walkable(grid, from.x, from.y) || !walkable(grid, to.x, to.y)) { return null; }
    var cols = grid[0].length;
    var key = function (x, y) { return y * cols + x; };
    var open = [from], came = {}, cost = {}, seen = {};
    cost[key(from.x, from.y)] = 0;
    var guard = 0;
    while (open.length && guard < 4000) {
      guard += 1;
      var bi = 0;
      for (var i = 1; i < open.length; i += 1) {
        var ci = cost[key(open[i].x, open[i].y)] + Math.abs(open[i].x - to.x) + Math.abs(open[i].y - to.y);
        var cb = cost[key(open[bi].x, open[bi].y)] + Math.abs(open[bi].x - to.x) + Math.abs(open[bi].y - to.y);
        if (ci < cb) { bi = i; }
      }
      var cur = open.splice(bi, 1)[0];
      if (cur.x === to.x && cur.y === to.y) {
        var path = [], node = cur;
        while (node) { path.unshift({ x: node.x, y: node.y }); node = came[key(node.x, node.y)]; }
        return path;
      }
      seen[key(cur.x, cur.y)] = true;
      for (var d = 0; d < DIRS.length; d += 1) {
        var nx = cur.x + DIRS[d][0], ny = cur.y + DIRS[d][1];
        if (!walkable(grid, nx, ny) || seen[key(nx, ny)]) { continue; }
        var nc = cost[key(cur.x, cur.y)] + 1;
        if (cost[key(nx, ny)] === undefined || nc < cost[key(nx, ny)]) {
          cost[key(nx, ny)] = nc;
          came[key(nx, ny)] = cur;
          open.push({ x: nx, y: ny });
        }
      }
    }
    return null;
  }

  /** 摊位前的**服务位**：顾客站在这儿看货 / 买菜。
   *  行 0 的柜台在下沿、人在其**下方**；行 1 镜像，柜台在上沿、人在其**上方**。
   *
   *  为什么行 0 取 `pad.y + 4`（而不是贴着柜台的 `+3`）：人的精灵是 **16×24**，比一格高 ——
   *  站位再贴近一格，头顶那 8px 就会插进柜台下沿（实测重叠 128px²）。这不是"走到摊位里"
   *  （脚下那格仍是可走的），但看起来就是人陷进柜台。隔开一格后两者刚好不重叠。 */
  function serviceTile(world, plot) {
    var pad = plot.pad;
    var x = pad.x + Math.floor(pad.w / 2);
    var y = plot.mirrored ? pad.y + 1 : pad.y + 4;
    return { x: x, y: y };
  }

  /** 顾客间的**主动避让**：矩形真重叠就推开。按**矩形**而不是按中心点距离判 ——
   *  精灵是 16×24，中心距 24px 的两点在对角方向上仍可能重叠，用距离阈值保证不了"不重叠"。
   *
   *  **迎面相遇要往侧向让**（本函数最容易写错的一处）：默认沿"穿透更浅"的轴分开，位移最小。
   *  但两人**相向而行**时这条规则会死锁 —— 沿 x 推开只是把两人推回原处，下一帧又撞上，
   *  实测两个顾客以正常速度的 8% 原地磨了 200 帧、一次都没逛到摊位。故迎面时改沿 **y 侧让**：
   *  人高 24px，错开约 1.5 格即互不重叠，而街道本身有 3 格宽，容得下。
   *  返回推挤次数 —— 让调用方能观察"避让真的发生了"，而不是只看结果碰巧没重叠。 */
  function separate(customers, grid, rectOf, gap, blockedAt) {
    var pushed = 0;
    var pad = gap === undefined ? 1 : gap;
    for (var i = 0; i < customers.length; i += 1) {
      for (var j = i + 1; j < customers.length; j += 1) {
        var a = customers[i], b = customers[j];
        var ra = rectOf(a), rb = rectOf(b);
        var ox = Math.min(ra.x + ra.w, rb.x + rb.w) - Math.max(ra.x, rb.x);
        var oy = Math.min(ra.y + ra.h, rb.y + rb.h) - Math.max(ra.y, rb.y);
        if (ox <= 0 || oy <= 0) { continue; }              /* 本来就没重叠 */
        var dot = (a.dirX || 0) * (b.dirX || 0) + (a.dirY || 0) * (b.dirY || 0);
        var headOn = dot < -0.2;                            /* 朝向大致相反 */
        var order = (headOn || oy < ox) ? ["y", "x"] : ["x", "y"];
        for (var k = 0; k < order.length; k += 1) {
          var axis = order[k];
          if (pushAxis(a, b, ra, rb, axis, axis === "y" ? oy : ox, pad, grid, blockedAt)) { break; }
        }
        pushed += 1;
      }
    }
    return pushed;
  }

  /** 沿一条轴把两个实体推开；两个方向都被挡（墙）就返回 false，交给另一条轴试。 */
  function pushAxis(a, b, ra, rb, axis, overlap, pad, grid, blockedAt) {
    var amount = overlap / 2 + pad / 2;
    var ax = a.anchor.x, ay = a.anchor.y, bx = b.anchor.x, by = b.anchor.y;
    if (axis === "x") {
      var sx = (ra.x < rb.x ? -1 : 1) * amount;
      nudge(a, sx, 0, grid, blockedAt);
      nudge(b, -sx, 0, grid, blockedAt);
    } else {
      var sy = (ra.y < rb.y ? -1 : 1) * amount;
      nudge(a, 0, sy, grid, blockedAt);
      nudge(b, 0, -sy, grid, blockedAt);
    }
    return a.anchor.x !== ax || a.anchor.y !== ay || b.anchor.x !== bx || b.anchor.y !== by;
  }

  /** 挪一步。`blockedAt(entity, x, y)` 给的是**精确**判据（真的按精灵矩形算），
   *  比"脚下那格可走吗"严格得多 —— 后者会因为 `Math.round(13.49) === 13` 放行一个
   *  身体已经压进柜台 8px 的站位（实测重叠 128px²）。没有 `blockedAt` 时退回格子判据。 */
  function nudge(entity, dxPx, dyPx, grid, blockedAt) {
    var nx = entity.anchor.x + dxPx / TILE;
    var ny = entity.anchor.y + dyPx / TILE;
    var free = blockedAt
      ? function (x, y) { return !blockedAt(entity, x, y); }
      : function (x, y) { return walkable(grid, Math.round(x), Math.round(y)); };
    /* 分别试：某条轴被挡不该拖累另一条轴（否则贴墙时整个人都动不了）。 */
    if (free(nx, entity.anchor.y)) { entity.anchor.x = nx; }
    if (free(entity.anchor.x, ny)) { entity.anchor.y = ny; }
  }

  return { buildGrid: buildGrid, walkable: walkable, nearestWalkable: nearestWalkable,
           findPath: findPath, serviceTile: serviceTile, separate: separate,
           overlaps: overlaps, TILE: TILE };
})();
