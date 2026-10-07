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

  /* --- 顾客：**有目的**的行为（`REQ-050` / `AC-040`） -----------------------------
     状态机：`toStall`（沿格子图走向某摊位前的服务位）→ `browsing`（停留看货，结束时
     触发一次购买 `REQ-052`）→ `toStall`（接着逛下一个摊位）。
     **原来的"写死折线直连"已删除** —— 那种走法不看障碍，是穿模的根因（实测 15 秒内
     与摊位/老板/秤重叠 38 次、顾客之间重叠 26 次）。
     路径一律来自 `GameNav`，格子图由地形 + 实体矩形反推，所以摊位挪动/美术改尺寸都不会失配。 */
  var SPAWN_TILES = [[3, 10], [12, 10], [20, 10], [26, 10], [7, 10]];
  var BROWSE_SECONDS = [1.6, 2.4, 1.2, 2.0, 1.8];
  /*: 走这么久还没到就换目标（卡死自救）；1.2 格以内算"已到摊位前"。 */
  var STUCK_SECONDS = 7;
  var ARRIVE_TOLERANCE = 1.2;

  /** 选下一个要逛的摊位：按索引错开起点 ⇒ 顾客自然分散，**不用随机数**（可复现）。
   *  优先挑**没人占用**的摊位 —— 否则几个人会盯着同一个服务位，先到的人站着不动，
   *  后面的人永远挤不进去（实测有顾客 200 帧原地打转、一次都没买成）。 */
  function pickStall(customer, plots, claimed) {
    var n = plots.length;
    if (!n) { return null; }
    var start = (customer.index * 3 + customer.visits) % n;
    var fallback = null;
    for (var k = 0; k < n; k += 1) {
      var plot = plots[(start + k) % n];
      if (plot.stallNo === customer.lastStall) { continue; }
      if (!claimed[plot.stallNo]) { return plot; }
      if (!fallback) { fallback = plot; }
    }
    return fallback || plots[start];
  }

  /** 规划下一段路。返回是否真的拿到了路径 —— 拿不到就不动，**绝不"朝目标直着走过去"**。 */
  function planNext(customer, ctx) {
    var claimed = {};
    (ctx.customers || []).forEach(function (other) {
      if (other !== customer && other.targetStall) { claimed[other.targetStall] = true; }
    });
    var plot = pickStall(customer, ctx.world.plots, claimed);
    if (!plot) { return false; }
    var tile = GameNav.serviceTile(ctx.world, plot);
    customer.lastStall = plot.stallNo;
    return routeTo(customer, tile, ctx.grid, plot.stallNo);
  }

  function routeTo(customer, tile, grid, stallNo) {
    var from = GameNav.nearestWalkable(
      grid, Math.round(customer.anchor.x), Math.round(customer.anchor.y));
    if (!from) { return false; }
    customer.anchor.x = from.x;                 /* 被挤到障碍上时先自救回可走格 */
    customer.anchor.y = from.y;
    var path = GameNav.findPath(grid, from, tile);
    if (!path) { return false; }
    customer.path = path;
    customer.pathIndex = 1;                     /* path[0] 就是当前格 */
    customer.targetStall = stallNo || null;
    return true;
  }

  /** 到达时**吸附到最近的、站得下的格心**。
   *  少了这一步，顾客会停在"离目标 1.2 格以内"的任意连续位置上 —— 身体可能正好压进
   *  柜台几个像素（实测 8.4px² 的薄片重叠），看着像人陷进台子。吸附后站位一定干净。 */
  function settleSpot(customer, ctx, wp) {
    if (!ctx.blockedAt) { return wp; }
    for (var r = 0; r <= 2; r += 1) {
      for (var dy = -r; dy <= r; dy += 1) {
        for (var dx = -r; dx <= r; dx += 1) {
          if (Math.max(Math.abs(dx), Math.abs(dy)) !== r) { continue; }
          var tx = wp.x + dx, ty = wp.y + dy;
          if (!ctx.blockedAt(customer, tx, ty)) { return { x: tx, y: ty }; }
        }
      }
    }
    return { x: customer.anchor.x, y: customer.anchor.y };
  }

  function face(customer, dx, dy) {
    if (Math.abs(dx) > Math.abs(dy)) {
      customer.sprite = dx > 0 ? "person_customer_right" : "person_customer_left";
    } else if (Math.abs(dy) > 0.05) {
      customer.sprite = dy > 0 ? "person_customer_down" : "person_customer_up";
    }
  }

  function stepCustomer(customer, dt, ctx) {
    if (customer.state === "browsing") {
      customer.wait -= dt;
      if (customer.wait <= 0) {
        /* 停留结束 ⇒ 买一次（`REQ-052`），然后继续逛下一个摊位。 */
        if (ctx.onPurchase) { ctx.onPurchase(customer); }
        customer.visits += 1;
        customer.wait = BROWSE_SECONDS[(customer.index + customer.visits) % BROWSE_SECONDS.length];
        customer.state = "toStall";
        customer.path = null;
        customer.stateTime = 0;
      }
      return;
    }
    customer.stateTime = (customer.stateTime || 0) + dt;
    /* 卡死自救：目标被别人占着 / 路被堵住时，不能永远杵在那儿（实测有顾客 200 帧原地打转、
       一次都没买成）。超时就换一个摊位重新规划 —— 换目标**不计入** `visits`，
       否则"购买次数"会被"重新规划次数"污染。 */
    if (customer.stateTime > STUCK_SECONDS) {
      customer.stateTime = 0;
      customer.path = null;
      customer.lastStall = null;
      customer.replans = (customer.replans || 0) + 1;
      if (!planNext(customer, ctx)) { return; }
    }
    if (!customer.path && !planNext(customer, ctx)) { return; }
    if (customer.pathIndex >= customer.path.length) {
      customer.path = null;
      customer.state = "browsing";              /* 起点即终点：直接进入浏览 */
      customer.stateTime = 0;
      return;
    }
    var wp = customer.path[customer.pathIndex];
    var dx = wp.x - customer.anchor.x;
    var dy = wp.y - customer.anchor.y;
    var distance = Math.sqrt(dx * dx + dy * dy);
    var stride = (customer.speed * dt) / TILE;
    /* 最后一格允许"差不多到了"：服务位若被别的顾客占着，硬要踩到那一格就永远到不了。
       1.2 格以内即视作已到摊位前 —— 视觉上就是站在摊位前面，不影响"有目的"这件事。 */
    var last = customer.pathIndex === customer.path.length - 1;
    if (distance <= Math.max(stride, last ? ARRIVE_TOLERANCE : 0.3)) {
      var spot = last ? settleSpot(customer, ctx, wp) : wp;
      customer.anchor.x = spot.x;
      customer.anchor.y = spot.y;
      customer.pathIndex += 1;
      if (customer.pathIndex >= customer.path.length) {
        customer.path = null;
        customer.state = "browsing";
        customer.stateTime = 0;
      }
    } else {
      var nx = customer.anchor.x + (dx / distance) * stride;
      var ny = customer.anchor.y + (dy / distance) * stride;
      /* **移动本身也要过判据**：只让"避让"守规矩是不够的 —— 沿着路径走的中间位置
         照样能把身体挪进柜台几个像素（实测瞬时薄片重叠最大 49px²）。
         整步不行就退一步只走单轴，两轴都不行就原地等下一帧（卡死由超时自救兜底）。 */
      if (!ctx.blockedAt || !ctx.blockedAt(customer, nx, ny)) {
        customer.anchor.x = nx;
        customer.anchor.y = ny;
      } else if (!ctx.blockedAt(customer, nx, customer.anchor.y)) {
        customer.anchor.x = nx;
      } else if (!ctx.blockedAt(customer, customer.anchor.x, ny)) {
        customer.anchor.y = ny;
      }
      /* 记录**朝向**：避让层要靠它区分"迎面相遇"与"同向并行" —— 前者必须侧让，
         否则沿 x 推开只是把两人推回原处、下一帧再撞，形成死锁（实测过）。 */
      customer.dirX = dx / distance;
      customer.dirY = dy / distance;
    }
    face(customer, dx, dy);
    customer.bob += dt * 6;
  }

  /* 摊位主营品类 → 精灵名（`REQ-051` / `AC-041`）。
     品类**由服务端返回的** `iconKey`（品类编码，形如 `V-01`）前缀推导，前端**不硬编码**
     任何摊位的品类：把服务端商品换个品类，摊位外观就跟着变。
     `V` 蔬菜 / `F` 水果 / `M` 肉 / `A` 水产；**无单一主营 ⇒ 杂货**。 */
  var KIND_BY_PREFIX = { V: "stall_veg", F: "stall_fruit", M: "stall_meat", A: "stall_fish" };
  var KIND_PREFIXES = ["A", "F", "M", "V"];   /* 固定顺序 ⇒ 计数并列时结果稳定，不随对象键序漂 */
  /* 占比阈值：纯品类摊位实测 100%（如 A-03 全 `F`），混合摊位最高约 32%（A-09 四类混装）。
     60% 落在这两者中间，两侧都有余量。 */
  var KIND_MAJORITY = 0.6;

  function stallSpriteFor(rows) {
    var count = { V: 0, F: 0, M: 0, A: 0 };
    var total = 0;
    (rows || []).forEach(function (row) {
      var prefix = String(row.iconKey || "").charAt(0).toUpperCase();
      if (count[prefix] !== undefined) { count[prefix] += 1; total += 1; }
    });
    if (!total) { return null; }              /* 没有可判定的数据 ⇒ 不换，保持原样 */
    var best = KIND_PREFIXES[0];
    KIND_PREFIXES.forEach(function (p) { if (count[p] > count[best]) { best = p; } });
    return count[best] / total >= KIND_MAJORITY ? KIND_BY_PREFIX[best] : "stall_grocery";
  }

  function build(world) {
    var list = [];
    var byStall = {};
    var customers = [];

    world.plots.forEach(function (plot) {
      /* 货台：两张 32×32 的摊位图横着摆（manifest 说 stall_* 是 32×32，故占 2×2 格）。
         初始用旧的四款通用图；拿到商品数据后由 `stallSpriteFor` 按**主营品类**换成品类图
         （`REQ-051`）—— 没数据时保持原样，不会因为加载失败而"摊位凭空变样"。 */
      var stallEntities = [];
      [plot.counter.x, plot.counter.x + 2].forEach(function (x, slot) {
        var entity = { kind: "stall", stallNo: plot.stallNo,
          sprite: "stall_" + ((plot.index + slot) % 4),
          label: plot.stallNo + " 摊位", anchor: tileAnchor(x, plot.counter.y), plot: plot };
        stallEntities.push(entity);
        list.push(entity);
      });
      list.push({ kind: "boss", stallNo: plot.stallNo,
        sprite: "person_boss_" + (plot.mirrored ? "up" : "down"),
        label: plot.stallNo + " 老板", anchor: bottomAnchor(plot.boss.x, plot.boss.y), plot: plot });
      list.push({ kind: "scale", stallNo: plot.stallNo, sprite: "scale_idle",
        label: plot.stallNo + " 智能秤", anchor: bottomAnchor(plot.scale.x, plot.scale.y), plot: plot });

      byStall[plot.stallNo] = { plot: plot, items: [], loaded: false, error: null, total: 0,
                                stalls: stallEntities };
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

    /* 顾客初始落在**已保证可走**的格子上；格子图在这一步之后由 `prepare()` 建好，
       故先用 `nearestWalkable` 兜一层 —— 出生点若被道具占了也不会卡在墙里。 */
    for (var ci = 0; ci < CUSTOMER_COUNT; ci += 1) {
      var spawn = SPAWN_TILES[ci % SPAWN_TILES.length];
      var customer = {
        kind: "customer", sprite: "person_customer_down", label: "顾客 " + (ci + 1),
        anchor: { x: spawn[0], y: spawn[1], mode: "bottom" },
        index: ci, state: "toStall", path: null, pathIndex: 0, targetStall: null,
        lastStall: null, visits: 0,
        wait: BROWSE_SECONDS[ci % BROWSE_SECONDS.length],
        speed: 26 + ci * 5, bob: ci * 0.7, demoOnly: true
      };
      customers.push(customer);
      list.push(customer);
    }

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
      /* 按主营品类换摊位外观（`REQ-051`）：服务端数据说了算，前端不写死。 */
      var stallKind = stallSpriteFor(rows);
      if (stallKind) {
        entry.stalls.forEach(function (entity) { entity.sprite = stallKind; });
      }
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

    /* 格子图（`REQ-050`）：由地形 + 实体矩形反推，**只建一次** —— 演示期间实体位置不变，
       不必每帧重建（每帧重建会在 768 格上做 40 多次矩形展开，纯属浪费）。 */
    var grid = null;
    var solidRects = [];
    var SOLID_KINDS = { stall: true, crate: true, computer: true, boss: true, admin: true };

    /** 把实体临时摆到 (x,y) 算一次矩形 —— **精确**判据，比"脚下那格可走吗"严格。 */
    function blockedAt(entity, x, y) {
      var ox = entity.anchor.x, oy = entity.anchor.y;
      entity.anchor.x = x;
      entity.anchor.y = y;
      var rect = rectOf(entity);
      entity.anchor.x = ox;
      entity.anchor.y = oy;
      for (var i = 0; i < solidRects.length; i += 1) {
        if (GameNav.overlaps(rect, solidRects[i])) { return true; }
      }
      return false;
    }

    function prepare() {
      solidRects = list.filter(function (entity) { return SOLID_KINDS[entity.kind]; })
        .map(function (entity) { return rectOf(entity); });
      /* 第 3 个参数是顾客精灵尺寸：人比一格高，站位要按**身体矩形**判（见 `buildGrid` 注释）。 */
      grid = GameNav.buildGrid(world, solidRects, GameSprites.size("person_customer_down"));
      /* 出生点若正好被道具占了，先挪到最近的可走格，避免一出生就卡在墙里。 */
      customers.forEach(function (customer) {
        var at = GameNav.nearestWalkable(grid, Math.round(customer.anchor.x),
                                         Math.round(customer.anchor.y));
        if (at) { customer.anchor.x = at.x; customer.anchor.y = at.y; }
      });
      return grid;
    }

    /** 推进一帧；`ctx.onPurchase(customer)` 由调用方注入（落账见 `REQ-052`）。
     *  返回本帧发生的**避让推挤次数** —— 让"主动避让真的在发生"可被观察到，
     *  而不是只看结果碰巧没重叠（没重叠也可能只是人少）。 */
    function step(dt, ctx) {
      if (!grid) { prepare(); }
      var env = { world: world, grid: grid, customers: customers, blockedAt: blockedAt,
                  onPurchase: ctx && ctx.onPurchase };
      customers.forEach(function (customer) { stepCustomer(customer, dt, env); });
      /* 避让放在移动**之后**：先按路径走、再互相推开。反过来会出现"推开又被路径拉回"的抖动。
         要**迭代到收敛**：一轮只能解开当前这批重叠，而推开一个人可能让他撞上第三个人；
         实测单轮会留下最大 60px² 的残余重叠。四轮足够，且没重叠时第一轮就退出。 */
      var pushed = 0;
      for (var pass = 0; pass < 4; pass += 1) {
        /* pad=2：多留 2px 余量。pad=1 时实测仍会残留 0.4px² 的亚像素薄片
             （推挤被墙挡掉一部分），余量给足才能真正为 0。 */
        var n = GameNav.separate(customers, grid, rectOf, 2, blockedAt);
        pushed += n;
        if (!n) { break; }
      }
      return pushed;
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
      prepare: prepare,
      gridOf: function () { return grid; },
      hitTest: hitTest,
      forStall: function (stallNo) {
        return list.filter(function (entity) { return entity.stallNo === stallNo; });
      }
    };
  }

  return { build: build, rectOf: rectOf, CUSTOMER_COUNT: CUSTOMER_COUNT };
})();
