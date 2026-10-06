/* 演示游戏 · 精灵加载器（T-GAME-03；REQ-048 的美术接口）。
 *
 * **本文件是"与美术队友真并行"的那道缝**：接口已冻结（见任务书），但 `manifest.json`
 * 与各 PNG 何时落地不由本文件决定。故加载器只有一条铁律：
 *   **manifest 缺失 / 形状不对 / PNG 加载失败 / 单个精灵名不在表里 ⇒ 一律降级为纯色块 + 名字标签，
 *   绝不抛错、绝不白屏。** 美术落地后**不改一行代码**即显示真图（无需任何开关或重新构建）。
 *
 * 降级路径逐条说明：
 * 1. `manifest.json` 取不到（404 / 断网 / 直接双击打开 HTML 文件）⇒ 整层走色块；
 * 2. manifest 能取到但某个 `sheets.*.file` 加载失败 ⇒ **只有那张表**走色块，其余照常显示真图；
 * 3. 精灵名不在 `sprites` 里 ⇒ 该精灵走色块（例如美术只画了最小集的一部分）。
 */
window.GameSprites = (function () {
  "use strict";

  var SPRITE_DIR = "/static/game/sprites/";
  var MANIFEST_URL = SPRITE_DIR + "manifest.json";

  var state = {
    mode: "placeholder",   /* placeholder | ready（ready 也允许个别精灵/表缺失） */
    note: "美术资源未就绪（色块降级）",
    tilePx: 16,
    sheets: {},
    sprites: {},
    images: {}
  };
  var readyCallbacks = [];

  /* ---- 降级配色：由精灵名**确定性**推出，同一名字每次都是同一个颜色（可复算，便于对照） ---- */
  var PREFIX_COLORS = [
    ["tile_floor", "#6f7a72"], ["tile_path", "#a68a5b"], ["tile_grass", "#4f7a3a"],
    ["tile_water", "#2f6f9e"],
    ["stall_", "#8a5a2b"],
    ["scale_", "#3b4a5a"],
    ["person_boss_", "#b5533c"], ["person_customer_", "#3f6fa8"], ["person_admin_", "#7a5aa8"],
    ["item_", "#7d9a3c"],
    ["prop_computer", "#4a4f57"], ["prop_crate", "#9a7233"]
  ];

  function hashOf(text) {
    var hash = 0;
    for (var i = 0; i < text.length; i += 1) { hash = (hash * 31 + text.charCodeAt(i)) % 100000; }
    return hash;
  }

  function colorFor(name) {
    for (var i = 0; i < PREFIX_COLORS.length; i += 1) {
      if (name.indexOf(PREFIX_COLORS[i][0]) === 0) {
        /* item_0~7 之间再拉开一点色相，免得八种菜一个色 */
        var shift = (hashOf(name) % 5) * 12;
        return shade(PREFIX_COLORS[i][1], name.indexOf("item_") === 0 ? shift : 0);
      }
    }
    var hue = hashOf(name) % 360;
    return "hsl(" + hue + ", 38%, 52%)";
  }

  function shade(hex, amount) {
    var value = parseInt(hex.slice(1), 16);
    var r = Math.min(255, ((value >> 16) & 255) + amount);
    var g = Math.min(255, ((value >> 8) & 255) + amount);
    var b = Math.min(255, (value & 255) + amount);
    return "rgb(" + r + "," + g + "," + b + ")";
  }

  function drawPlaceholder(ctx, name, x, y, w, h, showLabel) {
    ctx.fillStyle = colorFor(name);
    ctx.fillRect(x, y, w, h);
    ctx.fillStyle = "rgba(0, 0, 0, 0.35)";
    ctx.fillRect(x, y + h - 1, w, 1);
    ctx.fillRect(x + w - 1, y, 1, h);
    if (showLabel && w >= 12 && h >= 8) {
      ctx.fillStyle = "rgba(255, 255, 255, 0.92)";
      ctx.font = "5px monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(name.slice(0, 8), x + w / 2, y + h / 2, w - 1);
    }
  }

  function sheetReady(sheetName) {
    var sheet = state.sheets[sheetName];
    return !!(sheet && sheet.ok && state.images[sheetName]);
  }

  function loadSheets(sheets) {
    if (typeof Image === "undefined") { return Promise.resolve(); }  /* 无 DOM 的宿主：整层色块 */
    var jobs = Object.keys(sheets).map(function (name) {
      return new Promise(function (resolve) {
        var sheet = sheets[name];
        var image = new Image();
        image.onload = function () { sheet.ok = true; state.images[name] = image; resolve(); };
        image.onerror = function () { sheet.ok = false; resolve(); };  /* 单表失败不牵连其它表 */
        image.src = SPRITE_DIR + sheet.file;
      });
    });
    return Promise.all(jobs);
  }

  function adopt(manifest) {
    if (!manifest || typeof manifest !== "object" || !manifest.sprites || !manifest.sheets) {
      throw new Error("manifest 形状不符（缺少 sheets / sprites）");
    }
    state.tilePx = manifest.tile_px || 16;
    state.sprites = manifest.sprites;
    state.sheets = {};
    Object.keys(manifest.sheets).forEach(function (name) {
      var sheet = manifest.sheets[name] || {};
      state.sheets[name] = { file: sheet.file, w: sheet.w, h: sheet.h, ok: false };
    });
    return loadSheets(state.sheets).then(function () {
      var total = Object.keys(state.sprites).length;
      var usable = Object.keys(state.sprites).filter(function (name) {
        return sheetReady(state.sprites[name].sheet);
      }).length;
      state.mode = usable > 0 ? "ready" : "placeholder";
      state.note = "美术资源就绪（" + usable + "/" + total + " 个精灵有图）";
    });
  }

  function load() {
    return fetch(MANIFEST_URL, { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) { throw new Error("manifest 不可取（HTTP " + response.status + "）"); }
        return response.json();
      })
      .then(adopt)
      .catch(function (error) {
        state.mode = "placeholder";
        state.sheets = {};
        state.sprites = {};
        state.images = {};
        state.note = "美术资源未就绪，已降级为色块（" + (error && error.message ? error.message : "未知原因") + "）";
      })
      .then(function () {
        readyCallbacks.forEach(function (callback) {
          try { callback(state); } catch (err) { /* 回调出错不得拖垮加载器 */ }
        });
        return state;
      });
  }

  /* 画一个精灵：有真图就画真图（按 manifest 的裁剪矩形），否则色块 + 名字标签。
     返回 true 表示用的是真图 —— 便于自检与验收时分辨两条路径。 */
  function draw(ctx, name, x, y, w, h, showLabel) {
    var sprite = state.sprites[name];
    if (sprite && sheetReady(sprite.sheet)) {
      ctx.drawImage(state.images[sprite.sheet], sprite.x, sprite.y, sprite.w, sprite.h,
        x, y, w || sprite.w, h || sprite.h);
      return true;
    }
    drawPlaceholder(ctx, name, x, y, w || state.tilePx, h || state.tilePx, showLabel !== false);
    return false;
  }

  return {
    load: load,
    draw: draw,
    /** 精灵的像素尺寸：**manifest 是唯一权威**（美术尺寸不统一：stall 32×32、scale 24×24、
        person 16×24…）。manifest 未就绪时退回 16×16 —— 调用方（布局/命中）因此永远有值可用。 */
    size: function (name) {
      var sprite = state.sprites[name];
      if (sprite && sprite.w && sprite.h) { return { w: sprite.w, h: sprite.h }; }
      return { w: state.tilePx || 16, h: state.tilePx || 16 };
    },
    onReady: function (callback) { readyCallbacks.push(callback); },
    state: function () { return state; },
    isReady: function () { return state.mode === "ready"; },
    /** 自检用：某个精灵此刻会不会用真图 */
    hasRealArt: function (name) {
      var sprite = state.sprites[name];
      return !!(sprite && sheetReady(sprite.sheet));
    }
  };
})();
