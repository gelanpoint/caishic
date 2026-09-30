/* 秤端共享工具：API 调用、离线状态可视标识与显式切换、离线暂存/补传（T-026，REQ-014/REQ-030）。
   纯本地文件，无任何外部依赖（REQ-025）。
   设计要点：
   1. **离线/在线是"显式切换"的可视标识**（REQ-014）：不拔网线也能演示 —— 状态存 localStorage，
      界面上是一个明确的开关；`§3.13` 的 `online` 只表示"服务端可达"，本机开关表示"摊主认为现在断网"，
      二者取或（任一为离线即按离线处理），并在界面上分别显示。
   2. **离线时不得假装记账**（REQ-030 / AC-019）：暂存失败必须把错误摊开给摊主看，
      绝不显示"已记账"；暂存成功才提示"已本地暂存 N 笔"。
   3. 幂等键一律由前端生成一次并**在同一笔上复用**（重发不产生第二笔）。 */
window.MT = (function () {
  "use strict";
  var OFFLINE_KEY = "mt.offline";
  var TOKEN_KEY = "mt.token";
  var STALL_KEY = "mt.stallNo";

  function getIdempotencyKey(prefix) {
    return (prefix || "ui") + "-" + Date.now() + "-" + Math.floor(Math.random() * 100000);
  }

  function request(method, path, options) {
    options = options || {};
    var headers = { "Content-Type": "application/json" };
    var token = sessionStorage.getItem(TOKEN_KEY);
    if (token) { headers["X-Stall-Session"] = token; }
    if (options.idempotencyKey) { headers["Idempotency-Key"] = options.idempotencyKey; }
    return fetch(path, {
      method: method,
      headers: headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (payload) {
        if (!response.ok) {
          var message = payload && payload.error ? payload.error.message : ("HTTP " + response.status);
          var code = payload && payload.error ? payload.error.code : "?";
          var error = new Error(code + ": " + message);
          error.status = response.status;
          error.code = code;
          error.payload = payload;
          throw error;
        }
        return payload;
      });
    });
  }

  var offline = {
    isOn: function () { return localStorage.getItem(OFFLINE_KEY) === "1"; },
    set: function (on) { localStorage.setItem(OFFLINE_KEY, on ? "1" : "0"); },
    toggle: function () { this.set(!this.isOn()); return this.isOn(); }
  };

  return {
    getIdempotencyKey: getIdempotencyKey,
    request: request,
    offline: offline,
    tokenKey: TOKEN_KEY,
    stallKey: STALL_KEY,
    bindStall: function (stallNo) {
      return request("POST", "/api/merchant/session", { body: { stall_no: stallNo } }).then(function (data) {
        sessionStorage.setItem(TOKEN_KEY, data.session_token);
        sessionStorage.setItem(STALL_KEY, stallNo);
        return data;
      });
    },
    /** §3.13 暂存状态：待补传条数 / 阈值 / 是否达阈值 / 最早暂存时刻 / 服务端可达 */
    queueStatus: function () { return request("GET", "/api/merchant/offline/queue"); },
    /** §3.14 断网期间暂存一笔（本地副本）；失败会抛错，**调用方必须把它显示出来** */
    stage: function (items, key) {
      return request("POST", "/api/merchant/offline/queue",
        { body: { items: items, client_idempotency_key: key }, idempotencyKey: key });
    },
    /** §3.15 触发补传：返回 {backfilled, duplicate_discarded, failed, pending_count, purged} */
    sync: function () { return request("POST", "/api/merchant/offline/sync"); },
    toast: function (message, isError) {
      var node = document.getElementById("toast");
      if (!node) { node = document.createElement("div"); node.id = "toast"; document.body.appendChild(node); }
      node.className = "toast" + (isError ? " err" : "");
      node.textContent = message;
      clearTimeout(node._timer);
      node._timer = setTimeout(function () { node.className = "toast hide"; }, isError ? 6000 : 2600);
    },
    yuan: function (cents) { return (cents / 100).toFixed(2); }
  };
})();
