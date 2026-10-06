/* 演示游戏 · canvas 渲染（T-GAME-03；REQ-044 / REQ-046）。
 *
 * 三条口径：
 * 1. **像素风**：`ctx.imageSmoothingEnabled = false` + 整数倍缩放（世界坐标 1 格 = 16 美术像素）；
 * 2. **地面层缓存**：地图是静态的，整层画进离屏 canvas 一次，之后每帧只 `drawImage` 一次
 *    （否则每帧要发 700+ 次绘制调用）；精灵加载完成或地图重建时失效重画；
 * 3. **摊位标牌上的价格来自服务端缓存**（`GameApi.cachedStall`），不是本地改的数字 ——
 *    调价成功后界面刷新重新取数，标牌随之变化（AC-036 的"地图上的价格随之更新"）。
 */
window.GameRender = (function () {
  "use strict";

  var TILE = 16;
  var layerCache = { key: null, canvas: null };

  function invalidate() { layerCache.key = null; layerCache.canvas = null; }

  function layerKey(world) {
    var art = GameSprites.state();
    return world.cols + "x" + world.rows + "|" + art.mode + "|" +
      Object.keys(art.sprites).length + "|" + Object.keys(art.images).length;
  }

  function buildLayer(world) {
    if (typeof document === "undefined" || !document.createElement) { return null; }
    var canvas = document.createElement("canvas");
    canvas.width = world.widthPx;
    canvas.height = world.heightPx;
    var ctx = canvas.getContext("2d");
    if (!ctx) { return null; }
    ctx.imageSmoothingEnabled = false;
    for (var y = 0; y < world.tiles.length; y += 1) {
      for (var x = 0; x < world.tiles[y].length; x += 1) {
        GameSprites.draw(ctx, world.tiles[y][x], x * TILE, y * TILE, TILE, TILE, false);
      }
    }
    return canvas;
  }

  function tileLayer(world) {
    var key = layerKey(world);
    if (layerCache.key !== key || !layerCache.canvas) {
      layerCache.canvas = buildLayer(world);
      layerCache.key = key;
    }
    return layerCache.canvas;
  }

  function entityZ(entity) {
    if (entity.kind === "item" && entity.plot) {
      return (entity.plot.pad.y + entity.plot.pad.h) * TILE + 4;   /* 菜压在自己的货台上 */
    }
    var rect = GameEntities.rectOf(entity);
    return rect.y + rect.h;
  }

  function drawEntity(ctx, entity) {
    var rect = GameEntities.rectOf(entity);
    var active = entity.kind === "scale" && entity.highlighted;
    var sprite = active ? "scale_active" : entity.sprite;
    var bob = entity.kind === "customer" ? Math.round(Math.sin(entity.bob) * 1) : 0;
    /* 最后一个 true：色块降级时按**精灵名**打标签（真图模式下该参数不起作用） */
    GameSprites.draw(ctx, sprite, rect.x, rect.y + bob, rect.w, rect.h, true);
  }

  function outline(ctx, rect, color, dashed) {
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    if (dashed) { ctx.setLineDash([3, 2]); }
    ctx.strokeRect(rect.x - 0.5, rect.y - 0.5, rect.w + 1, rect.h + 1);
    ctx.restore();
  }

  function tag(ctx, text, centerX, baselineY, fill) {
    ctx.font = "6px monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    var width = ctx.measureText(text).width + 4;
    ctx.fillStyle = "rgba(12, 16, 14, 0.72)";
    ctx.fillRect(centerX - width / 2, baselineY - 6, width, 8);
    ctx.fillStyle = fill || "#f4f7f4";
    ctx.fillText(text, centerX, baselineY);
  }

  /** 摊位标牌：`摊位号` + 该摊位**服务端**在售件数与最低价（未取到就如实写状态）。 */
  function stallTagText(stallNo) {
    var data = GameApi.cachedStall(stallNo);
    if (!data) { return "数据未就绪"; }
    if (!data.items.length) {
      return data.priceListMissing ? "价目表缺失" : "本摊无在售";
    }
    var lowest = null;
    data.items.forEach(function (item) {
      if (item.unitPriceCents === null) { return; }
      if (lowest === null || item.unitPriceCents < lowest) { lowest = item.unitPriceCents; }
    });
    var price = lowest === null ? "未设价" : "¥" + GameApi.yuan(lowest) + "起";
    return price + " · " + data.items.length + "件";
  }

  function draw(ctx, world, entities, ui) {
    ctx.imageSmoothingEnabled = false;
    var layer = tileLayer(world);
    if (layer) {
      ctx.drawImage(layer, 0, 0);
    } else {
      for (var y = 0; y < world.tiles.length; y += 1) {
        for (var x = 0; x < world.tiles[y].length; x += 1) {
          GameSprites.draw(ctx, world.tiles[y][x], x * TILE, y * TILE, TILE, TILE, false);
        }
      }
    }

    var ordered = entities.list.slice().sort(function (a, b) { return entityZ(a) - entityZ(b); });
    ordered.forEach(function (entity) {
      entity.highlighted = !!(ui.hover && ui.hover === entity);
      drawEntity(ctx, entity);
    });

    world.plots.forEach(function (plot) {
      var stallNo = plot.stallNo;
      tag(ctx, stallNo, (plot.label.x + 0.5) * TILE, (plot.label.y + 1) * TILE, "#ffe9a8");
      tag(ctx, stallTagText(stallNo), (plot.label.x + 0.5) * TILE,
        (plot.label.y + 1) * TILE + (plot.mirrored ? -9 : 9), "#bfe6c8");
    });

    if (ui.hoverPlot && (!ui.hover || ui.hover.kind === "stall")) {
      outline(ctx, { x: ui.hoverPlot.pad.x * TILE, y: ui.hoverPlot.pad.y * TILE,
        w: ui.hoverPlot.pad.w * TILE, h: ui.hoverPlot.pad.h * TILE }, "rgba(255, 214, 102, 0.9)", true);
    }
    if (ui.hover) { outline(ctx, GameEntities.rectOf(ui.hover), "rgba(255, 236, 160, 0.95)", false); }
    if (ui.selected) { outline(ctx, GameEntities.rectOf(ui.selected), "rgba(255, 255, 255, 0.98)", true); }

    if (ui.view) {
      ctx.font = "6px monospace";
      ctx.textAlign = "left";
      ctx.textBaseline = "alphabetic";
      ctx.fillStyle = "rgba(255, 255, 255, 0.72)";
      ctx.fillText("视角：" + ui.viewLabel + "（点空白处取消选中）", 4, world.heightPx - 4);
    }
  }

  return { draw: draw, invalidate: invalidate };
})();
