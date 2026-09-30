/* 秤端收银台逻辑（T-026）。配套页面：../scale/index.html。
   设计原点（AC-006）：**选品不要输入** —— 全部操作是点按钮（图标/快捷键盘 + 预设重量），
   重量也可以直接"读秤"（T-017 的 §3.16 模拟秤读数端点），所以现场演示**全程不用打字**。
   离线时走 §3.14 暂存（REQ-014），回来点"补传"走 §3.15（REQ-015/NFR-013）。 */
(function () {
  "use strict";
  var state = { products: [], cart: [], weight: 1000, transactionNo: null, key: null, items: [] };

  function $(id) { return document.getElementById(id); }
  function show(id, visible) { $(id).classList[visible ? "remove" : "add"]("hide"); }
  function err(id, message) { var node = $(id); node.textContent = message || ""; }

  // ---- 会话与商品 ---------------------------------------------------------
  function stalls() { return ["A-01", "A-02", "A-03", "A-04", "A-05", "A-06"]; }

  function renderStallButtons() {
    var box = $("stalls");
    box.innerHTML = "";
    stalls().forEach(function (no) {
      var button = document.createElement("button");
      button.textContent = no;
      button.onclick = function () { bind(no); };
      box.appendChild(button);
    });
  }

  function bind(stallNo) {
    err("bindErr", "");
    MT.bindStall(stallNo).then(function (data) {
      $("stallName").textContent = data.stall_name || stallNo;
      $("bound").classList.remove("hide");
      $("bindBox").classList.add("hide");
      return MT.request("GET", "/api/merchant/products");
    }).then(function (products) {
      state.products = (products || []).filter(function (p) { return p.status === "active"; });
      renderProducts();
      return refreshOffline();
    }).catch(function (error) { err("bindErr", error.message); });
  }

  function renderProducts() {
    var box = $("products");
    box.innerHTML = "";
    if (!state.products.length) { box.innerHTML = '<div class="empty">本摊位没有在售商品</div>'; return; }
    state.products.forEach(function (product) {
      var tile = document.createElement("div");
      tile.className = "tile";
      tile.innerHTML = '<div class="ico">' + (product.icon_key || "🥬") + '</div>' +
        '<div class="nm">' + product.name + '</div>' +
        '<div class="hk">' + (product.hotkey || "") + '</div>';
      tile.onclick = function () { addToCart(product); };
      box.appendChild(tile);
    });
  }

  // ---- 称重（点按钮或"读秤"）--------------------------------------------
  function renderWeights() {
    var box = $("weights");
    box.innerHTML = "";
    [500, 1000, 1500, 2000, 3000].forEach(function (grams) {
      var button = document.createElement("button");
      button.textContent = (grams / 1000) + " kg";
      button.onclick = function () { setWeight(grams); };
      box.appendChild(button);
    });
    var read = document.createElement("button");
    read.className = "primary";
    read.textContent = "读秤";
    // §3.16 模拟秤读数：演示环境没有真秤，注入一个读数即可（越界值用来演示 MT-1002）
    read.onclick = function () {
      MT.request("POST", "/api/mock/scale/reading", { body: { weight_grams: state.weight } })
        .then(function (data) {
          setWeight(data.current_weight_grams);
          MT.toast("秤读数：" + data.current_weight_grams + " g（注入于 " + data.injected_at + "）");
        })
        .catch(function (error) { MT.toast(error.message, true); });
    };
    box.appendChild(read);
  }

  function setWeight(grams) { state.weight = grams; $("weightNow").textContent = grams + " g"; }

  // ---- 购物车 / 计价 -----------------------------------------------------
  function addToCart(product) {
    state.cart.push({ product_id: product.id, name: product.name, weight_grams: state.weight });
    renderCart();
  }

  function renderCart() {
    var box = $("cart");
    box.innerHTML = "";
    if (!state.cart.length) { box.innerHTML = '<div class="empty">还没选品（点上面的图标）</div>'; return; }
    state.cart.forEach(function (line, index) {
      var row = document.createElement("div");
      row.className = "kv";
      row.innerHTML = "<span>" + line.name + " · " + line.weight_grams + " g</span>";
      var remove = document.createElement("button");
      remove.className = "ghost";
      remove.textContent = "删除";
      remove.onclick = function () { state.cart.splice(index, 1); renderCart(); };
      row.appendChild(remove);
      box.appendChild(row);
    });
  }

  function itemsPayload() {
    return state.cart.map(function (line) {
      return { product_id: line.product_id, weight_grams: line.weight_grams };
    });
  }

  function checkout() {
    err("payErr", "");
    if (!state.cart.length) { err("payErr", "先选品再计价"); return; }
    var key = MT.getIdempotencyKey("scale");
    state.key = key;
    if (MT.offline.isOn()) {
      // 断网：只暂存本地副本（§3.14）。**失败必须摊开报错**，绝不显示"已记账"。
      MT.stage(itemsPayload(), key).then(function (data) {
        state.cart = [];
        renderCart();
        show("payBox", false);
        $("offlineNote").textContent = "已本地暂存（待补传 " + data.pending_count + " 笔）";
        refreshOffline();
      }).catch(function (error) { err("payErr", "暂存失败：" + error.message + "（这笔没有记账，请重试或联系运维）"); });
      return;
    }
    MT.request("POST", "/api/merchant/transactions",
      { body: { items: itemsPayload() }, idempotencyKey: key })
      .then(function (payload) {
        state.transactionNo = payload.transaction_no;
        state.items = payload.items || [];
        state.total = payload.total_amount_cents;
        renderTicket(payload);
        show("payBox", true);
        show("priceOps", true);   // 改价只能在收款前（收款后 API 会回 MT-1001，界面就不该给这个按钮）
        state.cart = [];
        renderCart();
      })
      .catch(function (error) { err("payErr", error.message); });
  }

  function renderTicket(payload) {
    $("txnNo").textContent = payload.transaction_no;
    $("txnTotal").textContent = "¥ " + MT.yuan(payload.total_amount_cents);
    var box = $("txnItems");
    box.innerHTML = "";
    (payload.items || []).forEach(function (item) {
      var row = document.createElement("div");
      row.className = "kv";
      row.innerHTML = "<span>商品 #" + item.product_id + " · " + item.weight_grams + " g</span><b>¥ " +
        MT.yuan(item.amount_cents) + "</b>";
      box.appendChild(row);
    });
  }

  // ---- 收款（现金 / 收款码）---------------------------------------------
  function pay(method) {
    err("payErr", "");
    if (!state.transactionNo) { err("payErr", "先计价"); return; }
    var key = MT.getIdempotencyKey("pay-" + method);
    MT.request("POST", "/api/merchant/transactions/" + state.transactionNo + "/payment",
      { body: { method: method, operator: "操作端" }, idempotencyKey: key })
      .then(function (payload) {
        if (payload.qr_payload) {
          $("qrBox").classList.remove("hide");
          $("qrPayload").textContent = payload.qr_payload;
          $("qrToken").textContent = payload.receiver_token_masked || "";
          MT.toast("已生成收款码，等待顾客扫码（状态 " + payload.status + "）");
        } else {
          $("qrBox").classList.add("hide");
          MT.toast("现金收款成功（" + payload.confirmed_at + "）");
        }
        show("afterSale", true);
      })
      .catch(function (error) { err("payErr", error.message); });
  }

  // ---- 改价（预设幅度，不用打字）与退货 ---------------------------------
  function changePrice(kind) {
    err("payErr", "");
    if (!state.items.length) { err("payErr", "这笔没有明细可改价"); return; }
    var item = state.items[0];
    var body = { item_id: item.id };
    if (kind === "ten-percent-off") {
      body.final_unit_price_cents = Math.round(item.final_unit_price_cents * 0.9);
      body.confirm_over_threshold = true; // 幅度超 50% 才需要，这里显式带上以免弹确认（AC-007）
    } else {
      body.round_off_cents = 50; // 抹零 0.50 元（抹零不计入标价一致率，REQ-008）
    }
    MT.request("POST", "/api/merchant/transactions/" + state.transactionNo + "/price-change",
      { body: body, idempotencyKey: MT.getIdempotencyKey("price") })
      .then(function (payload) {
        state.total = payload.total_amount_cents;
        $("txnTotal").textContent = "¥ " + MT.yuan(payload.total_amount_cents);
        MT.toast("改价完成（留痕 " + payload.audit_event + "）");
      })
      .catch(function (error) { err("payErr", error.message); });
  }

  function refund() {
    err("payErr", "");
    MT.request("POST", "/api/merchant/transactions/" + state.transactionNo + "/refund",
      { body: { amount_cents: state.total }, idempotencyKey: MT.getIdempotencyKey("refund") })
      .then(function (payload) {
        MT.toast(payload.replayed ? "这笔退货先前已冲正（幂等命中，未重复冲减）" : "退货冲正完成");
      })
      .catch(function (error) { err("payErr", error.message); });
  }

  // ---- 离线状态：可视标识 + 显式切换 + 暂存状态 + 补传 -------------------
  function renderOffline() {
    var on = MT.offline.isOn();
    var badge = $("offlineBadge");
    badge.textContent = on ? "离线（本机开关）" : "在线";
    badge.className = "badge " + (on ? "off" : "on");
    $("offlineToggle").textContent = on ? "切回在线" : "切到离线";
  }

  function refreshOffline() {
    return MT.queueStatus().then(function (data) {
      $("serverOnline").textContent = data.online ? "服务端可达" : "服务端不可达";
      $("pending").textContent = data.pending_count + " / 阈值 " + data.threshold +
        (data.threshold_warned ? "（已达告警阈值，仍可继续收银）" : "");
      $("oldest").textContent = data.oldest_staged_at || "—";
      if (data.threshold_warned) { MT.toast("待补传已达阈值 " + data.threshold + "：仍继续接受新交易，请尽快联网补传"); }
      return data;
    }).catch(function () {
      $("serverOnline").textContent = "服务端不可达";
      return null;
    });
  }

  function doSync() {
    err("offlineErr", "");
    MT.sync().then(function (data) {
      $("syncResult").textContent = "补传 " + data.backfilled + " 笔 · 重复丢弃 " + data.duplicate_discarded +
        " 笔 · 失败 " + data.failed + " 笔 · 清除副本 " + data.purged + " 笔 · 剩余待补传 " + data.pending_count;
      MT.toast("补传完成");
      return refreshOffline();
    }).catch(function (error) { err("offlineErr", error.message); });
  }

  // ---- 初始化 -----------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    renderStallButtons();
    renderWeights();
    renderCart();
    renderOffline();
    $("offlineToggle").onclick = function () {
      MT.offline.toggle();
      renderOffline();
      MT.toast(MT.offline.isOn() ? "已切到离线：后续收银只暂存本地副本" : "已切回在线：可正常收银与补传");
    };
    $("checkout").onclick = checkout;
    $("payCash").onclick = function () { pay("cash"); };
    $("payQr").onclick = function () { pay("qr"); };
    $("priceOff").onclick = function () { changePrice("ten-percent-off"); };
    $("priceRound").onclick = function () { changePrice("round-off"); };
    $("refund").onclick = refund;
    $("syncNow").onclick = doSync;
    $("defaultStall").onclick = function () { bind("A-01"); };
    var saved = sessionStorage.getItem(MT.stallKey);
    if (saved) { bind(saved); }
  });
})();
