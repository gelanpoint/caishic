/* 演示游戏 · 地图与摊位布点（T-GAME-03；REQ-044）。
 *
 * 地图**不含任何摊位常量**：摊位数量、摊位号、先后顺序全部来自 `GameApi.roster()`
 * （§3.25 市场方看板的"全部在营摊位"）。本文件只决定"第 i 个摊位画在哪一格"——
 * 那是**布局**，不是数据。
 *
 * 尺寸口径（重要）：**精灵尺寸的唯一权威是 manifest**（美术已冻结：tile_* 16×16、
 * stall_* 32×32、scale_* 24×24、person_* 16×24、prop_computer 32×32、prop_crate 16×16），
 * 故本文件只给**锚点格**（锚点 + 锚定方式），真实像素矩形由 `GameEntities.rectOf()` 在**绘制/命中时**
 * 按 manifest 现算。这样 manifest 缺失（色块 16×16）与就绪（真实尺寸）走同一份布局代码，
 * 美术落地后不改一行代码、也不需要重新布点。
 *
 * 布局（单位 = 16px 美术格）：一条横街 `y = 9..11`，摊位成排挂在街道两侧（奇数排镜像朝上），
 * 每排 5 个摊位；每个摊位：老板 → 货台（两个 32×32 摊位图）→ 货台上的菜 → 智能秤（最靠街）；
 * 管理员区在最下方：电脑 + 货箱 + 管理员 NPC。摊位多于两排时自动加排。
 */
window.GameMap = (function () {
  "use strict";

  var TILE = 16;
  var COLS = 32;
  var PER_ROW = 5;
  var PLOT_PITCH = 6;
  var FIRST_BAND_Y = 4;
  var BAND_PITCH = 8;
  var PAD_H = 5;
  var STREET_Y0 = 9;
  var STREET_Y1 = 11;

  function fill(tiles, x0, y0, x1, y1, name) {
    for (var y = y0; y <= y1; y += 1) {
      for (var x = x0; x <= x1; x += 1) {
        if (tiles[y] && tiles[y][x] !== undefined) { tiles[y][x] = name; }
      }
    }
  }

  function build(roster) {
    var stallNos = (roster && roster.stalls ? roster.stalls : []).slice();
    var rowCount = Math.max(2, Math.ceil(stallNos.length / PER_ROW));
    var lastBandY = FIRST_BAND_Y + (rowCount - 1) * BAND_PITCH;
    var rows = lastBandY + 12;              /* 标签行 + 步道 + 管理员区 5 格 */

    var tiles = [];
    for (var y = 0; y < rows; y += 1) {
      var line = [];
      for (var x = 0; x < COLS; x += 1) { line.push("tile_grass"); }
      tiles.push(line);
    }

    fill(tiles, 1, 0, 4, 2, "tile_water");
    fill(tiles, 1, rows - 4, 4, rows - 2, "tile_water");
    fill(tiles, 0, STREET_Y0, COLS - 1, STREET_Y1, "tile_path");

    var walkwayY = lastBandY + 6;
    fill(tiles, 16, walkwayY, COLS - 1, walkwayY, "tile_path");
    fill(tiles, 17, walkwayY + 1, COLS - 1, walkwayY + 5, "tile_floor");

    var plots = [];
    stallNos.forEach(function (stallNo, index) {
      var rowIndex = Math.floor(index / PER_ROW);
      var col = index % PER_ROW;
      var px = 2 + col * PLOT_PITCH;
      var baseY = FIRST_BAND_Y + rowIndex * BAND_PITCH;
      var mirrored = rowIndex % 2 === 1;

      var pad = { x: px - 1, y: baseY, w: PLOT_PITCH, h: PAD_H };
      fill(tiles, pad.x, pad.y, pad.x + pad.w - 1, pad.y + pad.h - 1, "tile_floor");

      plots.push({
        stallNo: stallNo,
        index: index,
        rowIndex: rowIndex,
        mirrored: mirrored,
        pad: pad,
        /* 锚点格：counter 是 32×32 的摊位图，横向摆两张（4 格宽）；其余按"底边中点"锚定 */
        counter: { x: px, y: mirrored ? baseY + 2 : baseY + 1, w: 4 },
        produce: { x: px, y: baseY + 2, w: 4 },
        boss: { x: px + 4, y: mirrored ? baseY + 3 : baseY + 1 },
        scale: { x: px + 4, y: mirrored ? baseY + 1 : baseY + 3 },
        label: { x: px + 2, y: mirrored ? baseY + PAD_H : baseY - 1 }
      });
    });

    var admin = {
      floor: { x: 17, y: walkwayY + 1, w: 15, h: 5 },
      computer: { x: 20, y: walkwayY + 2 },
      crates: [{ x: 18, y: walkwayY + 2 }, { x: 24, y: walkwayY + 2 }, { x: 29, y: walkwayY + 4 }],
      npc: { x: 21, y: walkwayY + 3 }
    };

    return {
      cols: COLS, rows: rows, tile: TILE,
      tiles: tiles, plots: plots, admin: admin,
      street: { y0: STREET_Y0, y1: STREET_Y1, detourTop: 8, detourBottom: lastBandY + 3 },
      widthPx: COLS * TILE, heightPx: rows * TILE
    };
  }

  /** 世界坐标（美术像素）落在哪个摊位的铺位内 —— 悬停清单的命中判据。 */
  function plotAtPoint(world, x, y) {
    for (var i = 0; i < world.plots.length; i += 1) {
      var pad = world.plots[i].pad;
      if (x >= pad.x * TILE && x < (pad.x + pad.w) * TILE &&
          y >= pad.y * TILE && y < (pad.y + pad.h) * TILE) {
        return world.plots[i];
      }
    }
    return null;
  }

  return { build: build, plotAtPoint: plotAtPoint, TILE: TILE, COLS: COLS, PER_ROW: PER_ROW };
})();
