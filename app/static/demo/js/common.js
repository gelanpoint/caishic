/* 演示三页共用工具（`REQ-057`）。零外部依赖：只用浏览器原生 `fetch`。
 *
 * 两条纪律：
 * 1. **所有业务数字都来自服务端响应** —— 这里只做"取回来 + 格式化"，不做任何估算/补零/默认值；
 * 2. **失败必须如实摊开** —— 统一按契约 §1.2 的错误信封解析，把 `MT-xxxx` 与 message 一起抛出，
 *    调用方必须显示出来（不得把失败画成成功）。
 */
window.Demo = (function () {
  "use strict";

  var DAY_KEY = "demo.businessDate";
  var IDEM_PREFIX = "demo.idem.";
  var cachedDay = null;

  function api(method, path, options) {
    options = options || {};
    var headers = {};
    if (options.body !== undefined) { headers["Content-Type"] = "application/json"; }
    if (options.token) { headers["X-Stall-Session"] = options.token; }
    if (options.idempotencyKey) { headers["Idempotency-Key"] = options.idempotencyKey; }
    return fetch(path, {
      method: method,
      headers: headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (payload) {
        if (!response.ok) {
          var envelope = payload && payload.error ? payload.error : {};
          var error = new Error((envelope.code || "?") + ": " +
            (envelope.message || ("HTTP " + response.status)));
          error.code = envelope.code || "?";
          error.status = response.status;
          error.payload = payload;
          throw error;
        }
        return payload;
      });
    });
  }

  function get(path, options) { return api("GET", path, options); }
  function post(path, options) { return api("POST", path, options); }

  /* 本地"今天"（按本地时区，不用 `toISOString()` —— 那是 UTC，会差一天） */
  function todayLocal() {
    var now = new Date();
    return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  }

  /* 营业日以**服务端**为准：`GET /api/demo/hub` 不带参数时回服务端当日。取不到再退回本地日期。 */
  function businessDay() {
    if (cachedDay) { return Promise.resolve(cachedDay); }
    return get("/api/demo/hub").then(function (data) {
      cachedDay = data.business_date;
      try { sessionStorage.setItem(DAY_KEY, cachedDay); } catch (error) { /* 无痕模式忽略 */ }
      return cachedDay;
    }).catch(function () {
      var saved = null;
      try { saved = sessionStorage.getItem(DAY_KEY); } catch (error) { saved = null; }
      cachedDay = saved || todayLocal();
      return cachedDay;
    });
  }

  function yuan(cents) {
    if (cents === null || cents === undefined) { return "—"; }
    return (cents / 100).toFixed(2);
  }
  function money(cents) { return cents === null || cents === undefined ? "—" : "¥ " + yuan(cents); }

  /* 幂等键：同一笔交易**生成一次、缓存复用**（重复取消/重复提交才走幂等分支） */
  function idem(prefix) {
    var key = (prefix || "demo") + "-" + Date.now() + "-" + Math.floor(Math.random() * 100000);
    return key;
  }
  function idemSave(slot, key) {
    try { sessionStorage.setItem(IDEM_PREFIX + slot, key); } catch (error) { /* 忽略 */ }
    return key;
  }
  function idemLoad(slot) {
    try { return sessionStorage.getItem(IDEM_PREFIX + slot); } catch (error) { return null; }
  }

  function node(tag, className, text) {
    var element = document.createElement(tag);
    if (className) { element.className = className; }
    if (text !== undefined && text !== null) { element.textContent = String(text); }
    return element;
  }
  function $(id) { return document.getElementById(id); }

  function toast(message, isError) {
    var box = $("toast");
    if (!box) { return; }
    box.textContent = message;
    box.className = "toast" + (isError ? " err" : "");
    clearTimeout(box._timer);
    box._timer = setTimeout(function () { box.className = "toast hide"; }, isError ? 7000 : 3000);
  }

  var PAGES = [
    { key: "register", label: "商家注册", href: "/demo/register/" },
    { key: "scale", label: "智能秤", href: "/demo/scale/" },
    { key: "hub", label: "中台", href: "/demo/hub/" }
  ];

  /* 三页互跳 + 当前营业日（营业日来自服务端） */
  function nav(current) {
    var box = $("nav");
    if (box) {
      box.innerHTML = "";
      PAGES.forEach(function (page) {
        var link = node("a", page.key === current ? "active" : "", page.label);
        link.href = page.href;
        box.appendChild(link);
      });
    }
    var dayBadge = $("dayBadge");
    if (dayBadge) { dayBadge.textContent = "营业日：读取中…"; }
    businessDay().then(function (day) {
      if (dayBadge) { dayBadge.textContent = "营业日：" + day; }
    });
  }

  function setBadge(id, text, kind) {
    var element = $(id);
    if (!element) { return; }
    element.textContent = text;
    element.className = "badge" + (kind ? " " + kind : "");
  }

  /* 收款码：**本地渲染**。优先用 Lead 的 `window.MTQR`（`/static/demo/js/qr.js`）；
     它不在时画**明确标注**的占位图 —— 绝不画一个没标注的假二维码。 */
  function renderQr(canvas, payload) {
    if (!canvas) { return "none"; }
    var ctx = canvas.getContext("2d");
    var note = $("qrNote");
    if (window.MTQR && typeof window.MTQR.render === "function") {
      try {
        var info = window.MTQR.render(canvas, payload) || {};
        if (note) {
          note.className = "muted";
          note.textContent = "本地渲染的二维码（由服务端 qr_payload 生成，未使用任何外部库/CDN）" +
            (info.version ? "｜版本 " + info.version + " / " + info.size + "×" + info.size + " 模块" : "");
        }
        return "real";
      } catch (error) {
        drawPlaceholder(ctx, canvas, payload, "二维码渲染失败：" + error.message +
          "。演示占位图，非可扫二维码；扫码由「模拟扫码」按钮代替");
        return "placeholder";
      }
    }
    drawPlaceholder(ctx, canvas, payload, "演示占位图，非可扫二维码；扫码由「模拟扫码」按钮代替");
    return "placeholder";
  }

  function drawPlaceholder(ctx, canvas, payload, note) {
    var size = canvas.width || 220;
    var cell = 10;
    var cols = Math.floor((size - 20) / cell);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, size, size);
    ctx.fillStyle = "#0b0f12";
    var hash = 0;
    var text = String(payload || "no-payload");
    for (var i = 0; i < text.length; i += 1) { hash = (hash * 31 + text.charCodeAt(i)) % 1000003; }
    for (var y = 0; y < cols; y += 1) {
      for (var x = 0; x < cols; x += 1) {
        hash = (hash * 1103515245 + 12345) % 2147483647;
        if (hash % 100 < 45) { ctx.fillRect(10 + x * cell, 10 + y * cell, cell - 1, cell - 1); }
      }
    }
    ctx.strokeStyle = "#c0392b";
    ctx.lineWidth = 3;
    ctx.strokeRect(1.5, 1.5, size - 3, size - 3);
    ctx.fillStyle = "#c0392b";
    ctx.font = "13px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("占位图", size / 2, size / 2 + 5);
    var noteBox = $("qrNote");
    if (noteBox) { noteBox.textContent = note; }
  }

  function log(message, isError) {
    var box = $("logList");
    if (!box) { return; }
    var line = node("div", "log-line" + (isError ? " err" : ""));
    var now = new Date();
    line.textContent = "[" + String(now.getHours()).padStart(2, "0") + ":" +
      String(now.getMinutes()).padStart(2, "0") + ":" + String(now.getSeconds()).padStart(2, "0") + "] " + message;
    box.insertBefore(line, box.firstChild);
    while (box.childNodes.length > 60) { box.removeChild(box.lastChild); }
  }

  function clear(element) { if (element) { element.innerHTML = ""; } }

  function table(columns, rows, builder) {
    var wrap = node("div", "table-wrap");
    var element = node("table");
    var head = node("tr");
    columns.forEach(function (column) {
      var th = node("th", column.num ? "num" : "", column.label);
      head.appendChild(th);
    });
    element.appendChild(head);
    rows.forEach(function (row) {
      var tr = node("tr");
      builder(row).forEach(function (cell, index) {
        var td = node("td", columns[index] && columns[index].num ? "num" : "");
        if (cell && cell.nodeType) { td.appendChild(cell); }        /* 单元格可以直接给 DOM 节点（如链接） */
        else { td.textContent = cell === null || cell === undefined ? "—" : String(cell); }
        tr.appendChild(td);
      });
      element.appendChild(tr);
    });
    wrap.appendChild(element);
    return wrap;
  }

  return {
    api: api, get: get, post: post,
    todayLocal: todayLocal, businessDay: businessDay,
    yuan: yuan, money: money,
    idem: idem, idemSave: idemSave, idemLoad: idemLoad,
    node: node, $: $, toast: toast, nav: nav, setBadge: setBadge,
    renderQr: renderQr, log: log, clear: clear, table: table
  };
})();
