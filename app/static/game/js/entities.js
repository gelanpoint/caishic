/* 演示游戏 · 实体（T-GAME-03；REQ-044 / REQ-047）。
 *
 * 三视角要看到的东西（负责人原话的最小集）都落在这里：
 * - **商家**：老板 NPC + 智能秤 + 货台上的菜（菜与摊位来自服务端数据，见 `syncItems`）；
 * - **顾客**：会走动的人（`person_customer_*` 四向）；
 * - **管理员**：人 + 电脑 + 箱子。
 *
 * 实体的**像素矩形是"现算"的**（`rectOf`）：只存锚点格与锚定方式，尺寸取 `GameSprites.size()`
 * —— manifest 是尺寸的唯一权威（stall 32×32、scale 24×24、person 16×24…）。
 * 于是"美术未就绪（色块 16×16）"与"美术就绪（真实尺寸）"共用同一份代码：manifest 落地后
 * 绘制与命中区域自动变成真尺寸，不需要改代码、也不需要重新布点。
 *
 * **验证边界（spec §6 第 15 条）**：顾客 NPC 的走动是**本地演示逻辑**（写死的几个路径点循环），
 * 不代表真实并发、也不代表真实顾客行为 —— 它不是任何服务端数据的投影。
 * 与之相反，摊位数、商品、单价**全部**来自服务端（`GameApi`），二者不要混为一谈。
 */
window.GameEntities = (function () {
  "use strict";

  var TILE = 16;
  var CUSTOMER_COUNT = 5;

  /* 命中优先级：越"站在前面"的东西越优先被点到 */
  var KIND_RANK = { scale: 6, boss: 5, customer: 5, admin: 4, item: 3, computer: 2, crate: 1, stall: 1 };

  /* 锚定方式：tile = 左上角对齐锚点格；bottom = 底边中点对齐锚点格的下沿（人、秤、菜、道具） */
  function tileAnchor(x, y) { return { x: x, y: y, mode: "tile" }; }
  function bottomAnchor(x, y) { return { x: x, y: y, mode: "bottom" }; }

  function rectOf(entity) {
    var size = GameSprites.size(entity.sprite);
    if (entity.anchor.mode === "tile") {
      return { x: entity.anchor.x * TILE, y: entity.anchor.y * TILE, w: size.w, h: size.h };
    }
    return {
      x: entity.anchor.x * TILE + TILE / 2 - size.w / 2,
      y: (entity.anchor.y + 1) * TILE - size.h,
      w: size.w, h: size.h
    };
  }

  function customerPaths(world) {
    var lane0 = world.street.y0 * TILE;
    var lane1 = (world.street.y0 + 1) * TILE;
    var lane2 = (world.street.y1 + 1) * TILE;
    var top = world.street.detourTop * TILE;
    var bottom = world.street.detourBottom * TILE;
    return [
      [{ x: 3 * TILE, y: lane0 }, { x: 29 * TILE, y: lane0 }],
      [{ x: 29 * TILE, y: lane2 }, { x: 3 * TILE, y: lane2 }],
      [{ x: 8 * TILE, y: lane1 }, { x: 20 * TILE, y: lane1 }, { x: 20 * TILE, y: top },
        { x: 16 * TILE, y: top }, { x: 16 * TILE, y: lane1 }],
      [{ x: 28 * TILE, y: lane1 }, { x: 12 * TILE, y: lane1 }, { x: 12 * TILE, y: bottom },
        { x: 17 * TILE, y: bottom }, { x: 17 * TILE, y: lane1 }],
      [{ x: 10 * TILE, y: lane2 }, { x: 27 * TILE, y: lane2 }, { x: 27 * TILE, y: lane1 }, { x: 10 * TILE, y: lane1 }]
    ].slice(0, CUSTOMER_COUNT);
  }

  function build(world) {
    var list = [];
    var byStall = {};
    var customers = [];

    world.plots.forEach(function (plot) {
      /* 货台：两张 32×32 的摊位图横着摆（manifest 说 stall_* 是 32×32，故占 2×2 格） */
      [plot.counter.x, plot.counter.x + 2].forEach(function (x, slot) {
        list.push({ kind: "stall", stallNo: plot.stallNo, sprite: "stall_" + ((plot.index + slot) % 4),
          label: plot.stallNo + " 摊位", anchor: tileAnchor(x, plot.counter.y), plot: plot });
      });
      list.push({ kind: "boss", stallNo: plot.stallNo,
        sprite: "person_boss_" + (plot.mirrored ? "up" : "down"),
        label: plot.stallNo + " 老板", anchor: bottomAnchor(plot.boss.x, plot.boss.y), plot: plot });
      list.push({ kind: "scale", stallNo: plot.stallNo, sprite: "scale_idle",
        label: plot.stallNo + " 智能秤", anchor: bottomAnchor(plot.scale.x, plot.scale.y), plot: plot });

      byStall[plot.stallNo] = { plot: plot, items: [], loaded: false, error: null, total: 0 };
    });

    var admin = world.admin;
    list.push({ kind: "admin", sprite: "person_admin_down", label: "管理员",
      anchor: bottomAnchor(admin.npc.x, admin.npc.y) });
    list.push({ kind: "computer", sprite: "prop_computer", label: "管理端电脑",
      anchor: bottomAnchor(admin.computer.x, admin.computer.y) });
    admin.crates.forEach(function (crate) {
      list.push({ kind: "crate", sprite: "prop_crate", label: "货箱",
        anchor: bottomAnchor(crate.x, crate.y) });
    });

    customerPaths(world).forEach(function (path, index) {
      var customer = {
        kind: "customer", sprite: "person_customer_down", label: "顾客 " + (index + 1),
        anchor: { x: path[0].x / TILE, y: path[0].y / TILE, mode: "bottom" },
        path: path, seg: 0, speed: 26 + index * 5, bob: index * 0.7, demoOnly: true
      };
      customers.push(customer);
      list.push(customer);
    });

    function syncItems(stallNo, data) {
      var entry = byStall[stallNo];
      if (!entry) { return; }
      /* 先摘掉该摊位**上一批**菜（下架/换价后重画不能留下幽灵实体）。
         注意：必须**原地**删（`splice`），不能 `list = list.filter(...)` ——
         后者会让对外暴露的 `list` 引用指向旧数组，渲染层就再也看不到新菜了。 */
      for (var i = list.length - 1; i >= 0; i -= 1) {
        if (list[i].kind === "item" && list[i].stallNo === stallNo) { list.splice(i, 1); }
      }
      entry.items = [];
      entry.loaded = true;
      entry.error = null;
      var produce = entry.plot.produce;
      var rows = data && data.items ? data.items : [];
      entry.total = rows.length;
      var shown = Math.min(produce.w, rows.length);
      for (var k = 0; k < shown; k += 1) {
        var row = rows[k];
        var item = {
          kind: "item", stallNo: stallNo, productId: row.productId, name: row.name,
          unitPriceCents: row.unitPriceCents, hotkey: row.hotkey, iconKey: row.iconKey,
          sprite: "item_" + (row.productId % 8), label: row.name,
          anchor: bottomAnchor(produce.x + k, produce.y), plot: entry.plot
        };
        entry.items.push(item);
        list.push(item);
      }
    }

    function markError(stallNo, message) {
      var entry = byStall[stallNo];
      if (!entry) { return; }
      entry.loaded = false;
      entry.error = message;
    }

    function step(dt) {
      customers.forEach(function (customer) {
        var target = customer.path[(customer.seg + 1) % customer.path.length];
        var dx = target.x - customer.anchor.x * TILE;
        var dy = target.y - customer.anchor.y * TILE;
        var distance = Math.sqrt(dx * dx + dy * dy);
        var stride = customer.speed * dt;
        if (distance <= stride) {
          customer.anchor.x = target.x / TILE;
          customer.anchor.y = target.y / TILE;
          customer.seg = (customer.seg + 1) % customer.path.length;
        } else {
          customer.anchor.x += (dx / distance) * stride / TILE;
          customer.anchor.y += (dy / distance) * stride / TILE;
        }
        if (Math.abs(dx) > Math.abs(dy)) {
          customer.sprite = dx > 0 ? "person_customer_right" : "person_customer_left";
        } else if (Math.abs(dy) > 0.5) {
          customer.sprite = dy > 0 ? "person_customer_down" : "person_customer_up";
        }
        customer.bob += dt * 6;
      });
    }

    function hitTest(x, y) {
      var best = null;
      var bestKey = -1;
      list.forEach(function (entity) {
        var rect = rectOf(entity);
        if (x < rect.x || x >= rect.x + rect.w || y < rect.y || y >= rect.y + rect.h) { return; }
        var key = (rect.y + rect.h) * 10 + (KIND_RANK[entity.kind] || 0);
        if (key > bestKey) { bestKey = key; best = entity; }
      });
      return best;
    }

    return {
      list: list,
      byStall: byStall,
      customers: customers,
      rectOf: rectOf,
      syncItems: syncItems,
      markError: markError,
      step: step,
      hitTest: hitTest,
      forStall: function (stallNo) {
        return list.filter(function (entity) { return entity.stallNo === stallNo; });
      }
    };
  }

  return { build: build, rectOf: rectOf, CUSTOMER_COUNT: CUSTOMER_COUNT };
})();
