/* 演示游戏 · 启动、输入与主循环（T-GAME-03）。
 *
 * 输入模型（负责人原话：「鼠标挪动到某个商家那里可以显示这个商家售卖的物品，点击智能秤可以代表
 * 商家老板进行操作」）：
 * - **悬停**：命中某个摊位的铺位 ⇒ 取该摊位服务端数据并显示在售商品与单价（`AC-035`）；
 * - **点击**：命中实体 ⇒ 选中高亮 + 信息面板；在商家视角点智能秤 ⇒ 打开调价 / 上下架操作（`AC-036`）；
 * - **视角**：只改"能做什么、能看什么"，不改任何请求头与鉴权口径（`AC-034` / `REQ-045`）。
 *
 * 页面里没有任何商品 / 价格 / 摊位常量：摊位清单来自 §3.25 看板，商品与单价来自 §3.5/§3.3。
 * 页面加载后**逐个摊位**（限速、不并发轰炸）把真实数据取回来，于是货台上的菜与摊位标牌上的价格
 * 一开始就是服务端的真实值。
 */
(function () {
  "use strict";

  var SCALE = 2;                       /* 像素风：整数倍放大，保持像素锐利 */
  var state = {
    world: null, entities: null, businessDate: null, view: "merchant",
    hover: null, hoverPlot: null, selected: null, operatingStall: null, lastPoint: null
  };
  var canvas = null;
  var ctx = null;
  var lastFrame = 0;
  var listenersBound = false;

  function $(id) { return document.getElementById(id); }

  function hooks() {
    return {
      syncStall: function (stallNo, data) {
        state.entities.syncItems(stallNo, data);
        if (state.hoverPlot && state.hoverPlot.stallNo === stallNo) {
          GamePanels.showHover(state.hoverPlot, data, state.lastPoint);
        }
      }
    };
  }

  /* ---- 加载：精灵（可降级）与真实数据 --------------------------------- */
  function loadArt() {
    GamePanels.setBadge("art", "美术资源加载中…", "");
    GameSprites.load().then(function (art) {
      GamePanels.setBadge("art", art.note, art.mode === "ready" ? "on" : "warn");
      if (art.mode === "placeholder") { GamePanels.log(art.note + "（页面照常可用，美术落地后自动显示真图）"); }
      GameRender.invalidate();
    });
  }

  function loadRoster() {
    return GameApi.roster().then(function (roster) {
      state.businessDate = roster.businessDate;
      state.world = GameMap.build(roster);
      state.entities = GameEntities.build(state.world);
      setupCanvas();
      GamePanels.setBadge("data", "摊位数据加载中（0/" + roster.stalls.length + "）", "warn");
      GamePanels.log("已从运营端看板读到 " + roster.stalls.length + " 个在营摊位：" +
        roster.stalls.join("、") + "（业务日 " + roster.businessDate + "）");
      prefetch(roster.stalls);
      return roster;
    });
  }

  function prefetch(stallNos) {
    var index = 0;
    function next() {
      if (index >= stallNos.length) {
        GamePanels.setBadge("data", "数据就绪（" + stallNos.length + " 个摊位）", "on");
        return;
      }
      var stallNo = stallNos[index];
      index += 1;
      GameApi.stallData(stallNo).then(function (data) {
        state.entities.syncItems(stallNo, data);
        GamePanels.setBadge("data", "摊位数据加载中（" + index + "/" + stallNos.length + "）", "warn");
      }).catch(function (error) {
        state.entities.markError(stallNo, error.message);
        GamePanels.log("摊位 " + stallNo + " 数据读取失败：" + error.message, true);
      }).then(function () { setTimeout(next, 90); });
    }
    next();
  }

  function setupCanvas() {
    canvas = $("gameCanvas");
    if (!canvas) { return; }
    ctx = canvas.getContext("2d");
    /* 画布**后备像素**是地图的整数倍（像素锐利）；**显示尺寸**交给 CSS：宽度自适应容器、
       上限为原生宽度，高度按内在比例（否则窄窗口下地图会被横向截掉）。 */
    canvas.width = state.world.widthPx * SCALE;
    canvas.height = state.world.heightPx * SCALE;
    canvas.style.maxWidth = state.world.widthPx * SCALE + "px";
    canvas.style.width = "100%";
    canvas.style.height = "auto";
    if (ctx && ctx.setTransform) { ctx.setTransform(SCALE, 0, 0, SCALE, 0, 0); }
    if (listenersBound) { return; }        /* 刷新会重建地图，但事件监听只绑一次 */
    listenersBound = true;
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseleave", onLeave);
    canvas.addEventListener("click", onClick);
  }

  /* ---- 输入 ----------------------------------------------------------- */
  function pointToWorld(event) {
    var rect = canvas.getBoundingClientRect();
    var ratioX = canvas.width / (rect.width || canvas.width);
    var ratioY = canvas.height / (rect.height || canvas.height);
    return {
      x: (event.clientX - rect.left) * ratioX / SCALE,
      y: (event.clientY - rect.top) * ratioY / SCALE
    };
  }

  function onMove(event) {
    var point = pointToWorld(event);
    state.lastPoint = event;
    state.hover = state.entities.hitTest(point.x, point.y);
    var plot = GameMap.plotAtPoint(state.world, point.x, point.y);
    state.hoverPlot = plot;
    if (!plot) { GamePanels.hideHover(); return; }
    var stallNo = plot.stallNo;
    GamePanels.showHover(plot, GameApi.cachedStall(stallNo), event);
    GameApi.stallData(stallNo).then(function (data) {
      state.entities.syncItems(stallNo, data);
      if (state.hoverPlot && state.hoverPlot.stallNo === stallNo) {
        GamePanels.showHover(plot, data, state.lastPoint);
      }
    }).catch(function (error) {
      state.entities.markError(stallNo, error.message);
      GamePanels.log("摊位 " + stallNo + " 数据读取失败：" + error.message, true);
    });
  }

  function onLeave() {
    state.hover = null;
    state.hoverPlot = null;
    GamePanels.hideHover();
  }

  function onClick(event) {
    var point = pointToWorld(event);
    var entity = state.entities.hitTest(point.x, point.y);
    if (!entity) {
      var plot = GameMap.plotAtPoint(state.world, point.x, point.y);
      if (!plot) { select(null); return; }
      entity = state.entities.forStall(plot.stallNo).filter(function (row) { return row.kind === "stall"; })[0];
    }
    select(entity);
    dispatch(entity);
  }

  function select(entity) {
    state.selected = entity || null;
    if (!entity) {
      GamePanels.showInfo("未选中", ["点地图上的摊位 / 智能秤 / 顾客 / 管理员，或把鼠标挪到某个商家上看它在售的商品。"]);
      return;
    }
    GamePanels.showInfo(labelOf(entity), describe(entity));
  }

  function labelOf(entity) {
    if (entity.kind === "stall") { return "摊位 " + entity.stallNo + "（货台）"; }
    if (entity.kind === "boss") { return "老板 NPC · " + entity.stallNo; }
    if (entity.kind === "scale") { return "智能秤 · " + entity.stallNo; }
    if (entity.kind === "item") { return "商品 · " + entity.name; }
    if (entity.kind === "customer") { return entity.label; }
    if (entity.kind === "admin") { return "管理员 NPC"; }
    if (entity.kind === "computer") { return "管理端电脑"; }
    return "货箱";
  }

  function describe(entity) {
    var data = entity.stallNo ? GameApi.cachedStall(entity.stallNo) : null;
    if (entity.kind === "item") {
      return [["商品名", entity.name], ["所属摊位", entity.stallNo],
        ["商品 id", entity.productId], ["单价", entity.unitPriceCents === null ? "未设价" :
          "¥" + GameApi.yuan(entity.unitPriceCents)],
        ["快捷键 / 图标键", (entity.hotkey || "—") + " / " + (entity.iconKey || "—")],
        "数据来自服务端 §3.5 商品清单与 §3.3 价目表；下架后本实体与悬停清单都会消失。"];
    }
    if (entity.kind === "stall" || entity.kind === "boss" || entity.kind === "scale") {
      var rows = [["摊位", entity.stallNo],
        ["在售件数", data ? data.items.length : "数据未就绪"],
        ["价目表业务日", data ? data.businessDate : "—"]];
      if (entity.kind === "scale") {
        rows.push(["可执行操作", state.view === "merchant" ? "调价 / 上架 / 下架（点我打开操作面板）" :
          "只读（切到商家视角可操作）"]);
      }
      rows.push("提示：把鼠标挪到该摊位铺位上，可看到它在售商品与单价。");
      return rows;
    }
    if (entity.kind === "customer") {
      return [["身份", "顾客 NPC（本地演示逻辑）"],
        ["行为", "沿固定路径点在街上循环走动"],
        "**验证边界（spec §6 第 15 条）**：顾客 NPC 是本地演示逻辑，不代表真实并发，也不代表真实顾客行为。"];
    }
    if (entity.kind === "admin") {
      return [["身份", "市场管理员 NPC"], ["可执行操作", "只读：点旁边的电脑看市场看板 / 指标 / 留痕"]];
    }
    if (entity.kind === "computer") {
      return [["设备", "管理端电脑"], ["可执行操作", "只读：市场看板 §3.25、使用率指标 §3.30、留痕 §3.31"]];
    }
    return [["物件", "货箱（装饰）"], ["可执行操作", "无"]];
  }

  function dispatch(entity) {
    if (!entity) { return; }
    if (entity.kind === "scale") {
      if (state.view !== "merchant") {
        GamePanels.log("当前是" + GamePanels.viewLabel() + "视角：智能秤只读；切到商家视角才能调价 / 上下架");
        return;
      }
      state.operatingStall = entity.stallNo;
      GamePanels.log("当前操作摊位：" + entity.stallNo + "（会话已绑定该摊位，写操作只作用于本摊位）");
      GameOps.merchantOps(entity.stallNo, GameApi.cachedStall(entity.stallNo), hooks());
      return;
    }
    if (entity.kind === "computer") { GameOps.adminPanel(state.businessDate); return; }
    if (entity.kind === "stall" || entity.kind === "boss") {
      GameOps.customerProfile(entity.stallNo, GameApi.cachedStall(entity.stallNo));
    }
  }

  /* ---- 主循环 --------------------------------------------------------- */
  function frame(now) {
    var dt = lastFrame ? Math.min(0.06, (now - lastFrame) / 1000) : 0;
    lastFrame = now;
    if (state.entities) {
      state.entities.step(dt);
      GameRender.draw(ctx, state.world, state.entities, {
        hover: state.hover, hoverPlot: state.hoverPlot, selected: state.selected,
        view: state.view, viewLabel: GamePanels.viewLabel()
      });
    }
    if (typeof requestAnimationFrame === "function") { requestAnimationFrame(frame); }
  }

  function onView(view) {
    state.view = view;
    GamePanels.log("已切到" + GamePanels.viewLabel() + "视角（只改可执行操作与可见信息，服务端鉴权口径不变）");
    var body = GamePanels.opsPanel("操作面板 · " + GamePanels.viewLabel() + "视角");
    if (body) {
      body.appendChild(GamePanels.node("div", "muted", view === "merchant"
        ? "点地图上的智能秤 → 打开该摊位的调价 / 上下架面板。"
        : view === "admin" ? "点管理端电脑 → 看市场看板 / 使用率指标 / 留痕（只读）。"
          : "点摊位 → 看顾客可见信息；悬停摊位 → 看在售商品与单价（只读）。"));
    }
    if (state.selected) { select(state.selected); }
  }

  function boot() {
    GamePanels.init({ onView: onView, onRefresh: refresh });
    loadArt();
    loadRoster().then(function () {
      GameRender.invalidate();
      if (typeof requestAnimationFrame === "function") { requestAnimationFrame(frame); }
      else { GameRender.draw(ctx, state.world, state.entities, { view: state.view, viewLabel: GamePanels.viewLabel() }); }
    }).catch(function (error) {
      GamePanels.setBadge("data", "摊位清单读取失败：" + error.message, "err");
      GamePanels.log("摊位清单读取失败：" + error.message + "（页面仍在，不会白屏）", true);
    });
  }

  function refresh() {
    GameApi.clearCache();
    GamePanels.log("手动刷新：清空本地缓存，重新取服务端数据");
    loadRoster().catch(function (error) { GamePanels.log("刷新失败：" + error.message, true); });
  }

  window.GameApp = {
    boot: boot,
    refresh: refresh,
    state: state,
    pointToWorld: pointToWorld,
    clickAt: function (worldX, worldY) {
      var entity = state.entities.hitTest(worldX, worldY);
      select(entity);
      dispatch(entity);
      return entity;
    },
    hoverAt: function (worldX, worldY) {
      var plot = GameMap.plotAtPoint(state.world, worldX, worldY);
      state.hover = state.entities.hitTest(worldX, worldY);
      state.hoverPlot = plot;
      if (!plot) { GamePanels.hideHover(); return null; }
      GamePanels.showHover(plot, GameApi.cachedStall(plot.stallNo), null);
      return plot;
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
