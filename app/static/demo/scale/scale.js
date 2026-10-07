/* 智能秤页 · 流程控制（`REQ-054` / `REQ-057`）。
 *
 * 真实调用（**全部经端点，前端不碰库**）：
 *   选商家   → `GET /api/demo/merchants`（列表）
 *   建会话   → `POST /api/merchant/session`（§3.2，摊位来自所选商家）
 *   读商品   → `GET /api/merchant/products`（§3.5）
 *   读价目表 → `GET /api/merchant/price-list?business_date=…`（§3.3，**该参数必填**）
 *   改价/新增/改名 → `POST /api/merchant/products`（§3.38 upsert，`X-Stall-Session` 鉴权）
 *   确认计价 → `POST /api/merchant/transactions`（§3.6，带 `Idempotency-Key`）
 *   出收款码 → `POST /api/merchant/transactions/{no}/payment`（§3.10，`method=qr`）
 *   模拟扫码 → `POST /api/mock/payment/callback`（§3.17）
 *   取消     → `POST /api/demo/transactions/{no}/cancel`（§3.35，复用同一幂等键）
 *
 * **一处诚实标注**：商品的新增/改名过去在既有契约里没有载体（`product` 只能由 seed 建），
 * 现由 §3.38 `POST /api/merchant/products` 补齐（`REQ-058`）。该端点未落地时界面**如实报错**，
 * 绝不假装成功、也绝不跳过商品直接造一笔没有 `product_id` 的交易。
 */
(function () {
  "use strict";

  var state = {
    merchants: [], merchant: null, token: null, day: null,
    products: [], prices: [], entries: [], selected: 0, basket: [],
    weight: 1000, txn: null, payment: null, callbackNo: null, idemKey: null
  };

  function $(id) { return document.getElementById(id); }

  /* ---- 商家与会话 ---- */
  function loadMerchants() {
    return Demo.get("/api/demo/merchants").then(function (data) {
      state.merchants = data.items || [];
      var select = $("merchantSel");
      select.innerHTML = "";
      if (!state.merchants.length) {
        var option = Demo.node("option", null, "（还没有商家，先去注册页注册）");
        option.value = "";
        select.appendChild(option);
        Demo.setBadge("sessionBadge", "无商家可开工", "warn");
        return;
      }
      var placeholder = Demo.node("option", null, "（请选择商家）");
      placeholder.value = "";
      select.appendChild(placeholder);
      state.merchants.forEach(function (row) {
        var item = Demo.node("option", null, row.merchant_name + "（" + row.stall_no + "）");
        item.value = String(row.merchant_id);
        select.appendChild(item);
      });
      var wanted = new URLSearchParams(location.search).get("merchant_id");
      var found = state.merchants.filter(function (row) { return String(row.merchant_id) === String(wanted); })[0];
      if (found) { select.value = String(found.merchant_id); selectMerchant(found); }
      else { Demo.setBadge("sessionBadge", "未选商家", "warn"); }
    }).catch(function (error) {
      Demo.setBadge("sessionBadge", "商家列表读取失败", "err");
      $("merchantHint").textContent = "读取商家列表失败：" + error.message;
      Demo.log("读取商家列表失败：" + error.message, true);
    });
  }

  function selectMerchant(row) {
    state.merchant = row;
    state.token = null;
    state.basket = [];
    state.txn = null;
    state.payment = null;
    ScaleView.hideSuccess();
    ScaleView.hideQr();
    $("btnCancelTxn").disabled = true;
    Demo.setBadge("sessionBadge", "建立会话中…", "warn");
    $("stallBadge").textContent = "摊位：" + row.stall_no;
    Demo.post("/api/merchant/session", { body: { stall_no: row.stall_no } }).then(function (data) {
      state.token = data.session_token;
      Demo.setBadge("sessionBadge", "已选：" + data.stall_name + "（" + data.stall_no + "）", "on");
      Demo.log("已绑定摊位 " + data.stall_no + "（会话令牌已下发，写操作只作用于本摊位）");
      return Demo.businessDay();
    }).then(function (day) {
      state.day = day;
      return refreshStall();
    }).catch(function (error) {
      Demo.setBadge("sessionBadge", "会话建立失败", "err");
      $("merchantHint").textContent = "会话建立失败：" + error.message;
      Demo.log("会话建立失败：" + error.message, true);
    });
  }

  function refreshStall() {
    if (!state.token) { return Promise.resolve(); }
    return Promise.all([
      Demo.get("/api/merchant/products", { token: state.token }),
      Demo.get("/api/merchant/price-list?business_date=" + encodeURIComponent(state.day), { token: state.token })
    ]).then(function (both) {
      state.products = both[0] || [];
      state.prices = both[1].items || [];
      state.entries = ScaleView.merge(state.products, state.prices);
      render();
      Demo.log("本摊位服务端在售商品 " + state.products.length + " 件，价目表 " +
        state.prices.length + " 条（业务日 " + both[1].business_date + "）");
    }).catch(function (error) {
      Demo.log("读取本摊位商品/价目表失败：" + error.message, true);
      Demo.toast("读取本摊位数据失败：" + error.message, true);
    });
  }

  /* ---- 渲染 ---- */
  function render() {
    ScaleView.renderIcons(state.entries, state.selected, { onSelect: selectEntry, onPlace: place });
    var entry = state.entries[state.selected] || null;
    ScaleView.renderLed(entry, state.weight, state.txn);
    ScaleView.renderBasket(state.basket, state.entries, state.txn ? state.txn.total_amount_cents : null);
    ScaleView.setEditTarget(entry);
    if (entry) {
      $("editName").value = entry.name;
      $("editPrice").value = entry.cents === null ? "" : (entry.cents / 100).toFixed(2);
    }
  }

  function selectEntry(index) {
    state.selected = index;
    render();
    var entry = state.entries[index];
    Demo.log("选中商品：" + entry.name + "（" + entry.kind + "，" +
      (entry.productId ? "服务端商品 #" + entry.productId : "未上架") + "）");
  }

  function move(delta) {
    if (!state.entries.length) { return; }
    state.selected = (state.selected + delta + state.entries.length) % state.entries.length;
    render();
    var entry = state.entries[state.selected];
    Demo.log("▲▼ 选品 → " + entry.name + "（第 " + (state.selected + 1) + "/" + state.entries.length + " 个）");
  }

  /* ---- 放上去（右键） ---- */
  function place(index) {
    var entry = state.entries[index];
    if (!entry) { return; }
    var existing = state.basket.filter(function (item) { return item.index === index; })[0];
    if (existing) {
      existing.weightGrams += state.weight;
      Demo.log("再次放上「" + entry.name + "」：重量累加为 " + existing.weightGrams + " g");
    } else {
      state.basket.push({ index: index, weightGrams: state.weight });
      Demo.log("放上「" + entry.name + "」 " + state.weight + " g" +
        (entry.productId ? "（服务端商品 #" + entry.productId + "）" : "（尚未在服务端建立）"));
    }
    state.txn = null;
    ScaleView.hideQr();
    render();
  }

  function setWeight(grams) {
    state.weight = Math.max(100, Math.min(5000, Math.round(grams)));
    var range = $("weightRange");
    if (range) { range.value = String(state.weight); }
    render();
  }

  /* ---- 设置商品名称与价格（`POST /api/merchant/products`，upsert） ----
     鉴权用**当前摊位会话**（`stall_id` 只从会话取，请求体不接受摊位参数 → 前端没有跨摊位改商品的可能）。
     同一个端点既新增（不带 `product_id`）也修改（带 `product_id`），并写当日价目表 ⇒ 立刻可计价。 */
  function upsertProduct(entry, name, cents) {
    return Demo.post("/api/merchant/products", {
      token: state.token,
      body: {
        name: name,
        unit_price_cents: cents,
        category_code: entry.category,
        icon_key: entry.icon,
        hotkey: String((state.entries.indexOf(entry) % 9) + 1),
        product_id: entry.productId || undefined
      }
    });
  }

  function saveProduct() {
    var entry = state.entries[state.selected];
    if (!state.token || !entry) { ScaleView.setSaveResult("先选择商家并选中一个商品。", true); return; }
    var name = String($("editName").value || "").trim();
    var yuan = Number(String($("editPrice").value || "").trim());
    if (name.length < 1 || name.length > 50) {
      ScaleView.setSaveResult("商品名长度必须是 1~50 字符（没有提交任何请求）。", true);
      return;
    }
    if (!isFinite(yuan) || yuan <= 0 || Math.round(yuan * 100) < 1) {
      ScaleView.setSaveResult("单价必须是 ≥ 0.01 的数字（没有提交任何请求）。", true);
      return;
    }
    var cents = Math.round(yuan * 100);
    ScaleView.setSaveResult("提交中…（POST /api/merchant/products）", false);
    upsertProduct(entry, name, cents).then(function (data) {
      entry.productId = data.product_id;
      entry.name = data.name;
      entry.cents = data.unit_price_cents;
      entry.listed = true;
      ScaleView.setSaveResult("已保存：" + data.name + " → ¥ " + Demo.yuan(data.unit_price_cents) +
        " /kg（服务端商品 #" + data.product_id + "，业务日 " + data.business_date +
        "，状态 " + data.status + "）—— 现在即可用它计价。", false);
      Demo.log("商品已保存：#" + data.product_id + " " + data.name + " → ¥ " +
        Demo.yuan(data.unit_price_cents) + "（§3.38 upsert，已写当日价目表）");
      return refreshStall();
    }).catch(function (error) {
      ScaleView.setSaveResult("保存失败：" + error.message +
        "（服务端未新增/未修改；本页不伪造商品）", true);
      Demo.log("保存商品失败：" + error.message, true);
    });
  }

  /* ---- 确认：提交计价 → 生成收款码 ---- */
  function ensureProducts() {
    var jobs = state.basket.map(function (item) {
      var entry = state.entries[item.index];
      if (entry.productId) { return Promise.resolve(entry); }
      return upsertProduct(entry, entry.name, entry.cents === null ? 100 : entry.cents)
        .then(function (data) {
          entry.productId = data.product_id;
          entry.name = data.name;
          entry.cents = data.unit_price_cents;
          return entry;
        });
    });
    return Promise.all(jobs);
  }

  function confirm() {
    if (!state.token) { Demo.toast("先在顶部选择一个已注册商家。", true); return; }
    if (!state.basket.length) {
      place(state.selected);
      if (!state.basket.length) { return; }
    }
    $("btnConfirm").disabled = true;
    Demo.log("确认：先把篮子里的商品在服务端建立/确认（需要时调用 §3.38）…");
    ensureProducts().then(function () {
      state.idemKey = Demo.idem("txn");
      var items = state.basket.map(function (item) {
        return { product_id: state.entries[item.index].productId, weight_grams: item.weightGrams };
      });
      return Demo.post("/api/merchant/transactions", {
        token: state.token,
        idempotencyKey: state.idemKey,
        body: { items: items, client_idempotency_key: state.idemKey }
      });
    }).then(function (txn) {
      state.txn = txn;
      ScaleView.renderTxnResult(txn);
      render();
      $("btnCancelTxn").disabled = false;
      Demo.log("计价成功：" + txn.transaction_no + " 合计 ¥ " + Demo.yuan(txn.total_amount_cents) +
        "（服务端返回，幂等键 " + state.idemKey + "）");
      return Demo.post("/api/merchant/transactions/" + txn.transaction_no + "/payment", {
        token: state.token,
        idempotencyKey: Demo.idem("pay"),
        body: { method: "qr" }
      });
    }).then(function (payment) {
      state.payment = payment;
      state.callbackNo = Demo.idemSave("callback." + payment.payment_no, Demo.idem("cb"));
      ScaleView.showQr(payment.qr_payload, payment.receiver_token_masked, payment.payment_no);
      Demo.log("收款码已生成：支付单号 " + payment.payment_no + "（状态 " + payment.status +
        "，收款标识脱敏 " + payment.receiver_token_masked + "）");
      $("btnConfirm").disabled = false;
    }).catch(function (error) {
      $("btnConfirm").disabled = false;
      Demo.toast("确认失败：" + error.message, true);
      Demo.log("确认失败：" + error.message + "（服务端未生成交易，界面不显示价格）", true);
      var box = $("txnResult");
      box.className = "err-text";
      box.textContent = "确认失败：" + error.message + "（未生成交易）" +
        (error.code === "MT-1009" ? "。提示：商品需先在服务端建立（`POST /api/merchant/products`）。" : "") +
        (error.code === "MT-1006" ? "。提示：该商品当日没有价目表条目 —— 先保存一次单价。" : "");
    });
  }

  /* ---- 模拟扫码：走既有 Mock 回调 ---- */
  function simulateScan() {
    if (!state.payment) { Demo.toast("先点「确认」生成收款码。", true); return; }
    var payment = state.payment;
    $("btnSimulate").disabled = true;
    Demo.post("/api/mock/payment/callback", {
      body: { callback_no: state.callbackNo, payment_no: payment.payment_no, result: "success" }
    }).then(function (data) {
      Demo.log("模拟扫码回调：" + data.payment_no + " → " + data.result +
        "（重复送达幂等：" + data.is_duplicate + "）");
      ScaleView.showSuccess(state.txn, {
        payment_no: data.payment_no,
        receiver_token_masked: payment.receiver_token_masked,
        transaction_status: data.transaction_status
      });
      Demo.toast("交易成功：¥ " + Demo.yuan(state.txn.total_amount_cents) + "（按任意键回初始界面）");
    }).catch(function (error) {
      $("btnSimulate").disabled = false;
      Demo.toast("模拟扫码失败：" + error.message, true);
      Demo.log("模拟扫码失败：" + error.message, true);
    });
  }

  /* ---- 取消（§3.35，复用同一幂等键） ---- */
  function cancelTxn() {
    if (!state.txn) { Demo.toast("还没有已确认的交易可取消。", true); return; }
    var key = state.idemKey || Demo.idemLoad("txn." + state.txn.transaction_no) || Demo.idem("txn");
    Demo.post("/api/demo/transactions/" + state.txn.transaction_no + "/cancel", {
      body: { client_idempotency_key: key }
    }).then(function (data) {
      Demo.log("取消成功：" + data.transaction_no + " → " + data.status +
        "（幂等命中：" + data.is_duplicate + "，取消动作已留痕，中台可见）");
      Demo.toast("已取消：" + data.transaction_no + "（不计入应缴，中台留痕）");
      state.txn = null;
      state.payment = null;
      state.basket = [];
      $("btnCancelTxn").disabled = true;
      ScaleView.hideQr();
      ScaleView.hideSuccess();
      $("txnResult").className = "muted";
      $("txnResult").textContent = "已取消该笔交易（不计入应缴）。可以重新选品。";
      render();
    }).catch(function (error) {
      Demo.log("取消失败：" + error.message + "（服务端状态未改变）", true);
      Demo.toast("取消失败：" + error.message, true);
    });
  }

  function reset() {
    ScaleView.hideSuccess();
    ScaleView.hideQr();
    state.txn = null;
    state.payment = null;
    state.basket = [];
    state.idemKey = null;
    $("btnSimulate").disabled = false;
    $("btnCancelTxn").disabled = true;
    $("txnResult").className = "muted";
    $("txnResult").textContent = "确认后这里显示服务端返回的价格明细。";
    render();
    Demo.log("已回到初始界面（商家会话保留，可继续下一笔）");
  }

  function boot() {
    Demo.nav("scale");
    ScaleView.loadArt().then(render);
    $("merchantSel").onchange = function () {
      var id = $("merchantSel").value;
      var row = state.merchants.filter(function (item) { return String(item.merchant_id) === String(id); })[0];
      if (row) { selectMerchant(row); }
    };
    $("reloadMerchants").onclick = loadMerchants;
    $("btnUp").onclick = function () { move(-1); };
    $("btnDown").onclick = function () { move(1); };
    $("btnClear").onclick = function () {
      state.basket = [];
      state.txn = null;
      ScaleView.hideQr();
      render();
      Demo.log("已清空篮子（本地操作，未调用服务端）");
    };
    $("btnConfirm").onclick = confirm;
    $("btnSave").onclick = saveProduct;
    $("btnSimulate").onclick = simulateScan;
    $("btnCancelTxn").onclick = cancelTxn;
    $("weightRange").oninput = function () { setWeight(Number($("weightRange").value)); };
    Array.prototype.forEach.call(document.querySelectorAll("[data-weight]"), function (button) {
      button.onclick = function () { setWeight(Number(button.getAttribute("data-weight"))); };
    });
    $("successScreen").onclick = reset;
    document.addEventListener("keydown", function (event) {
      if ($("successScreen").style.display === "none") { return; }
      event.preventDefault();
      Demo.log("检测到按键「" + event.key + "」→ 回到初始界面");
      reset();
    });
    loadMerchants();
    setWeight(1000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
