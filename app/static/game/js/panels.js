/* 演示游戏 · 面板与视角（T-GAME-03；REQ-045 / REQ-046）。
 *
 * `AC-034` 的"三视角"落在这里：顶部按钮组 + 当前视角高亮 + 一句**当前可执行操作**说明。
 * 视角只改**能做什么、能看什么**（本地演示层），**不改**任何请求头或鉴权口径 ——
 * 授权仍然只来自摊位会话令牌与既有运营端端点（`REQ-045` 原文）。
 *
 * 本文件只管 DOM 与文案；写操作的实现（调价 / 上下架）在 ops.js，数据在 api.js。
 */
window.GamePanels = (function () {
  "use strict";

  var VIEWS = {
    merchant: {
      label: "商家",
      caption: "商家视角：点**智能秤**即以该摊位老板身份操作（调价、上架 / 下架），悬停看该摊在售商品与单价。",
      actions: ["悬停摊位 → 看在售商品与单价", "点智能秤 → 调价 / 上架 / 下架（真实端点）",
        "点老板 / 菜 / 货台 → 看该摊位信息"]
    },
    customer: {
      label: "顾客",
      caption: "顾客视角：**只读**。悬停看摊位在售商品与单价，点摊位看摊位信用指标；没有任何写入口。",
      actions: ["悬停摊位 → 看在售商品与单价", "点摊位 → 看摊位营业状态与标价一致率（§3.18）",
        "点顾客 → 看该顾客的说明（本地演示逻辑）"]
    },
    admin: {
      label: "管理员",
      caption: "管理员视角：**只读**。点管理端电脑看市场看板 / 使用率指标 / 留痕；没有任何写入口。",
      actions: ["点电脑 → 市场看板与使用率指标（§3.25 / §3.30）", "点电脑 → 最近留痕（§3.31，只读）",
        "点管理员 / 货箱 → 看说明"]
    }
  };

  var el = {};
  var handlers = {};
  var current = "merchant";

  function $(id) { return document.getElementById(id); }

  function node(tag, className, text) {
    var element = document.createElement(tag);
    if (className) { element.className = className; }
    if (text !== undefined && text !== null) { element.textContent = String(text); }
    return element;
  }

  function row(label, value, className) {
    var line = node("div", "kv" + (className ? " " + className : ""));
    line.appendChild(node("span", null, label));
    line.appendChild(node("b", null, value));
    return line;
  }

  function button(text, className, onClick) {
    var element = node("button", className || "ghost", text);
    element.onclick = onClick;
    return element;
  }

  function renderTabs() {
    var box = el.viewTabs;
    if (!box) { return; }
    box.innerHTML = "";
    Object.keys(VIEWS).forEach(function (key) {
      var tab = node("button", "tab" + (key === current ? " active" : ""), VIEWS[key].label);
      tab.id = "tab-" + key;
      tab.setAttribute("aria-pressed", key === current ? "true" : "false");
      tab.onclick = function () { setView(key); };
      box.appendChild(tab);
    });
  }

  function setView(view) {
    if (!VIEWS[view]) { return; }
    current = view;
    renderTabs();
    if (el.viewCaption) { el.viewCaption.textContent = VIEWS[view].caption; }
    if (el.viewActions) {
      el.viewActions.innerHTML = "";
      VIEWS[view].actions.forEach(function (text) {
        el.viewActions.appendChild(node("li", null, text));
      });
    }
    hideHover();
    if (handlers.onView) { handlers.onView(view); }
  }

  function init(nextHandlers) {
    handlers = nextHandlers || {};
    el.viewTabs = $("viewTabs");
    el.viewCaption = $("viewCaption");
    el.viewActions = $("viewActions");
    el.tooltip = $("tooltip");
    el.tooltipTitle = $("tooltipTitle");
    el.tooltipList = $("tooltipList");
    el.infoTitle = $("infoTitle");
    el.infoBody = $("infoBody");
    el.opsTitle = $("opsTitle");
    el.opsBody = $("opsBody");
    el.logList = $("logList");
    el.toast = $("toast");
    el.stage = $("stage");
    el.artBadge = $("artBadge");
    el.dataBadge = $("dataBadge");
    el.refreshBtn = $("refreshBtn");
    if (el.refreshBtn && handlers.onRefresh) { el.refreshBtn.onclick = handlers.onRefresh; }
    setView("merchant");
  }

  /* ---- 悬停清单（AC-035）：**逐项来自服务端**，下架的不在这里出现 ---- */
  function showHover(plot, data, point) {
    if (!el.tooltip || !plot) { return; }
    el.tooltipTitle.textContent = "摊位 " + plot.stallNo + " · " +
      (data ? "在售 " + data.items.length + " 件" : "数据未就绪");
    var list = el.tooltipList;
    list.innerHTML = "";
    if (!data) {
      list.appendChild(node("div", "muted", "该摊位数据尚未取到（服务端未响应或仍在加载）"));
    } else if (!data.items.length) {
      list.appendChild(node("div", "muted", data.priceListMissing ? "该摊位当日价目表缺失" : "该摊位当前无在售商品"));
    } else {
      data.items.forEach(function (item) {
        var line = node("div", "tip-row");
        line.appendChild(node("span", "tip-name", item.name));
        line.appendChild(node("b", "tip-price",
          item.unitPriceCents === null ? "未设价" : "¥" + GameApi.yuan(item.unitPriceCents)));
        list.appendChild(line);
      });
    }
    /* 长清单**多列铺开**：件数越多列越多。25 件商品单列要 480px，而 `.tip-list` 上限 240px，
       用户又滚不动（`.tooltip` 是 pointer-events:none）⇒ 原先有一半商品根本看不到。 */
    var count = data ? data.items.length : 0;
    var cols = Math.max(1, Math.min(3, Math.ceil(count / 14)));
    setListMode(cols);
    el.tooltip.classList.toggle("wide", cols > 1);
    el.tooltip.classList.remove("hide");
    var stage = el.stage ? el.stage.getBoundingClientRect() : null;
    var vh = (typeof window !== "undefined" && window.innerHeight) || 0;
    var stageTop = stage ? stage.top : 0;
    var width = el.tooltip.offsetWidth || 200;
    var height = el.tooltip.offsetHeight || 120;
    var left = point && stage ? point.clientX - stage.left + 14 : 12;
    var topBelow = point && stage ? point.clientY - stageTop + 12 : 12;
    var top = topBelow;
    /* 地图比视口高（舞台底部在屏幕外），所以**不能只按舞台裁剪**：光标在下面一排摊位时，
       清单会整块掉到屏幕外。下方放不下就**翻到光标上方**。 */
    if (stage && vh && (stageTop + topBelow + height) > vh - 8) {
      var topAbove = point.clientY - stageTop - height - 12;
      if (topAbove >= 4) { top = topAbove; }
    }
    if (stage) {
      left = Math.max(4, Math.min(left, stage.width - width - 4));
      top = Math.max(4, Math.min(top, stage.height - height - 4));
    }
    /* 最后兜底：翻上去仍放不下（清单比视口还高）⇒ 退回**单列 + 内部滚动**。
       ⚠️ 绝不能给多列容器设 max-height：多列会把放不下的内容溢出到**新列**，
       而新列在水平方向被裁掉（实测 scrollWidth 785 > clientWidth 213 ⇒ 商品直接丢）。 */
    if (stage && vh && (stageTop + top + height) > vh - 8) {
      setListMode(1);
      el.tooltip.classList.remove("wide");
      height = el.tooltip.offsetHeight || height;
      top = Math.max(4, Math.min(top, stage.height - height - 4));
    }
    el.tooltip.style.left = left + "px";
    el.tooltip.style.top = top + "px";
  }

  /* 清单的列模式（1/2/3 列）。切模式时必须清掉内联 max-height —— 见上面的警告。 */
  function setListMode(cols) {
    el.tooltipList.className = "tip-list" +
      (cols === 2 ? " tip-col2" : cols === 3 ? " tip-col3" : "");
    el.tooltipList.style.maxHeight = "";
    el.tooltipList.scrollTop = 0;
  }

  function hideHover() {
    if (el.tooltip) { el.tooltip.classList.add("hide"); }
  }

  function showInfo(title, rows) {
    if (!el.infoTitle || !el.infoBody) { return; }
    el.infoTitle.textContent = title;
    el.infoBody.innerHTML = "";
    (rows || []).forEach(function (entry) {
      if (typeof entry === "string") { el.infoBody.appendChild(node("div", "muted", entry)); }
      else { el.infoBody.appendChild(row(entry[0], entry[1])); }
    });
  }

  function opsPanel(title) {
    if (!el.opsTitle || !el.opsBody) { return null; }
    el.opsTitle.textContent = title;
    el.opsBody.innerHTML = "";
    return el.opsBody;
  }

  /* 把面板带进视野。1280 这类窄屏下布局是「地图在上、面板在下」，而地图本身就占满一屏
     （原生 1024×768）⇒ 不滚动的话，用户点了智能秤**看不到操作面板**，像是没反应。
     只在面板确实不在视口内时才滚，避免无谓跳动。
     用**瞬时**滚动（不加 `behavior:"smooth"`）：平滑滚动会让页面在用户光标底下继续滑动几百毫秒，
     紧接着的操作就会点空 —— 面板是"点完马上要操作"的东西，确定性优先于动效。 */
  function reveal(target) {
    if (!target || !target.getBoundingClientRect || typeof window === "undefined") { return; }
    var vh = window.innerHeight || 0;
    if (!vh) { return; }
    var rect = target.getBoundingClientRect();
    if (rect.bottom <= 0 || rect.top >= vh) {
      if (target.scrollIntoView) { target.scrollIntoView({ block: "center" }); }
    }
  }

  function log(message, isError) {
    if (!el.logList) { return; }
    var line = node("div", "log-line" + (isError ? " err" : ""));
    var time = new Date();
    line.textContent = "[" + String(time.getHours()).padStart(2, "0") + ":" +
      String(time.getMinutes()).padStart(2, "0") + ":" + String(time.getSeconds()).padStart(2, "0") + "] " + message;
    el.logList.insertBefore(line, el.logList.firstChild);
    while (el.logList.childNodes.length > 40) { el.logList.removeChild(el.logList.lastChild); }
  }

  function toast(message, isError) {
    if (!el.toast) { return; }
    el.toast.textContent = message;
    el.toast.className = "toast" + (isError ? " err" : "");
    clearTimeout(el.toast._timer);
    el.toast._timer = setTimeout(function () { el.toast.className = "toast hide"; }, isError ? 6000 : 2600);
  }

  function setBadge(which, text, kind) {
    var target = which === "art" ? el.artBadge : el.dataBadge;
    if (!target) { return; }
    target.textContent = text;
    target.className = "badge " + (kind || "");
  }

  return {
    init: init,
    setView: setView,
    view: function () { return current; },
    viewLabel: function () { return VIEWS[current].label; },
    showHover: showHover,
    hideHover: hideHover,
    showInfo: showInfo,
    opsPanel: opsPanel,
    reveal: reveal,
    log: log,
    toast: toast,
    setBadge: setBadge,
    node: node,
    row: row,
    button: button
  };
})();
