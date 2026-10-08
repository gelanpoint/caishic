/* 智能秤页 · 流程控制（`REQ-057` / `AC-047`，2026-10-08 按负责人现场纠正的顺序改写）。
 *
 * 顺序：① 选商家建会话（只读）→ ② **右键放一件上秤盘**（秤只感知重量，**不认定品类、不显示金额、
 * 一个请求都不发**）→ ③ **▲▼/左键由人选品类** ⇒ LED 出单价，金额按「重量 × 单价」**本地预览** →
 * ④ **调重量**金额实时变（仍只读）→ ⑤ **点确认**才提交：§3.38 建商品（如需）→ §3.6 计价 →
 * §3.10 `method=qr` 出收款码，金额以**服务端**为准并**锁定** → ⑥ **点模拟**走 §3.17 回调 → 成功 →
 * 任意键回初始。
 *
 * 三条硬纪律：确认之前**不产生任何交易/收款码**（空盘或未选品类点确认只提示）；**本地预览必须标注**，
 * 与服务端不一致时显示服务端值并说明；**只走真实端点**，`stall_id` 只从会话取。
 * 商品 upsert（§3.38）实现在 `scale-product.js`，视图在 `scale-view.js`。
 */
(function () {
  "use strict";

  var state = {
    merchants: [], merchant: null, token: null, day: null,
    products: [], prices: [], entries: [],
    cursor: 0,                                   /* ▲▼ 高亮的光标（不是"品类认定"） */
    pan: { placed: false, entryIndex: null, weightGrams: 1000 },   /* 秤盘：**至多一件** */
    txn: null, payment: null, callbackNo: null, idemKey: null
  };

  function $(id) { return document.getElementById(id); }
  function locked() { return Boolean(state.txn); }
  function panEntry() {
    return state.pan.entryIndex === null ? null : state.entries[state.pan.entryIndex];
  }
  /* 本地预览金额：与 §3.6 同口径（`(单价 × 克 + 500) / 1000` 整数四舍五入），**但它只是预览** */
  function previewCents() {
    var entry = panEntry();
    if (!state.pan.placed || !entry || entry.cents === null) { return null; }
    return Math.floor((entry.cents * state.pan.weightGrams + 500) / 1000);
  }

  /* ---- 商家与会话（只读） ---- */
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
    state.txn = null;
    state.payment = null;
    state.pan = { placed: false, entryIndex: null, weightGrams: state.pan.weightGrams };
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

  /* ---- 渲染与锁定 ---- */
  function setLocked(on) {
    $("weightRange").disabled = on;
    Array.prototype.forEach.call(document.querySelectorAll("[data-weight]"), function (button) {
      button.disabled = on;
    });
    $("btnUp").disabled = on;
    $("btnDown").disabled = on;
    $("btnClear").disabled = on;
    $("btnConfirm").disabled = on;
    $("btnCancelTxn").disabled = !on;
    $("weightHint").textContent = on
      ? "价格已锁定（服务端计价）：调重量 / 换品类都不再改变这一笔金额。要改就先「取消交易」或按任意键回初始。"
      : "确认之前：重量与金额都是本地预览，随便调；点「确认」才把这一件提交服务端计价并锁定价格。";
  }

  function render() {
    var entry = panEntry();
    ScaleView.renderIcons(state.entries, state.cursor, { onSelect: selectEntry, onPlace: place });
    ScaleView.renderLed({
      entry: entry,
      placed: state.pan.placed,
      weightGrams: state.pan.weightGrams,
      previewCents: locked() ? null : previewCents(),
      lockedCents: locked() ? state.txn.total_amount_cents : null
    });
    ScaleView.renderPan(state.pan, entry, locked() ? null : previewCents(),
      locked() ? state.txn.total_amount_cents : null);
    ScaleView.setEditTarget(state.entries[state.cursor] || null);
    if (state.entries[state.cursor]) {
      $("editName").value = state.entries[state.cursor].name;
      $("editPrice").value = state.entries[state.cursor].cents === null ? "" :
        (state.entries[state.cursor].cents / 100).toFixed(2);
    }
    setLocked(locked());
  }

  /* ---- 第 2 步：右键 ⇒ 放一件上秤盘（**不认定品类、不发请求**） ---- */
  function place(index) {
    if (locked()) { Demo.toast("价格已锁定：先「取消交易」或按任意键回初始。", true); return; }
    var entry = state.entries[index];
    if (!entry) { return; }
    var replaced = state.pan.placed;
    state.pan.placed = true;
    state.pan.entryIndex = null;     /* ★ 秤不替人认定品类 */
    state.cursor = index;
    state.txn = null;
    state.payment = null;
    ScaleView.hideQr();
    ScaleView.hideSuccess();
    render();
    Demo.log((replaced ? "已替换秤盘上的商品" : "放上秤盘") + "：1 件 " + state.pan.weightGrams +
      " g —— 秤只感知重量，**未认定品类**，请用 ▲▼ 选择（本步不发任何请求）");
    Demo.toast(replaced ? "已替换秤盘上的商品 —— 请重新选择品类" :
      "已放上秤盘 —— 请用 ▲▼ 选择这件是什么品类");
  }

  /* ---- 第 3 步：人工选品类（左键或 ▲▼，纯本地） ---- */
  function chooseCategory(index, how) {
    if (locked()) { Demo.toast("价格已锁定：先「取消交易」或按任意键回初始。", true); return; }
    state.cursor = index;
    if (state.pan.placed) { state.pan.entryIndex = index; }
    render();
    var entry = state.entries[index];
    Demo.log(how + " → " + entry.name + "（" + entry.kind + "，" +
      (entry.productId ? "服务端商品 #" + entry.productId : "未上架") + "）" +
      (state.pan.placed ? "；金额为本地预览" : "（秤盘空着，先右键放一件）"));
  }

  function selectEntry(index) { chooseCategory(index, "左键指定品类"); }

  function move(delta) {
    if (locked()) { Demo.toast("价格已锁定：先「取消交易」或按任意键回初始。", true); return; }
    if (!state.entries.length) { return; }
    chooseCategory((state.cursor + delta + state.entries.length) % state.entries.length,
      delta < 0 ? "▲ 上移选择" : "▼ 下移选择");
  }

  function takeOff() {
    if (locked()) { Demo.toast("价格已锁定：先「取消交易」或按任意键回初始。", true); return; }
    state.pan = { placed: false, entryIndex: null, weightGrams: state.pan.weightGrams };
    state.txn = null;
    ScaleView.hideQr();
    render();
    Demo.log("已取下秤盘商品（本地操作，未调用服务端）");
  }

  /* ---- 第 4 步：调重量（纯本地，金额实时变） ---- */
  function setWeight(grams) {
    if (locked()) { Demo.toast("价格已锁定，调重量不再改变金额。", true); return; }
    state.pan.weightGrams = Math.max(100, Math.min(5000, Math.round(grams)));
    var range = $("weightRange");
    if (range) { range.value = String(state.pan.weightGrams); }
    render();
  }

  /* ---- 商品新增/改名（`POST /api/merchant/products`，upsert）—— 实现在 `scale-product.js` ---- */
  function ctx() { return { token: state.token, entries: state.entries, cursor: state.cursor }; }

  function ensureProduct(entry) {
    if (entry.productId) { return Promise.resolve(entry.productId); }
    return ScaleProduct.upsert(ctx(), entry, entry.name, entry.cents).then(function (data) {
      entry.productId = data.product_id;
      entry.name = data.name;
      entry.cents = data.unit_price_cents;
      Demo.log("确认时先在服务端建立商品 #" + data.product_id + "（§3.38，否则 §3.6 会 MT-1006）");
      return data.product_id;
    });
  }

  /* ---- 第 5 步：确认 ⇒ 服务端计价并**锁定**价格 + 出收款码 ---- */
  function confirm() {
    var box = $("txnResult");
    if (!state.token) { Demo.toast("先在顶部选择一个已注册商家。", true); return; }
    if (locked()) { Demo.toast("这一笔已锁定价格：先「取消交易」或按任意键回初始。", true); return; }
    if (!state.pan.placed) {                       /* ★ 不再"偷偷先放一件"兜底 */
      box.className = "err-text";
      box.textContent = "请先把商品放上秤盘（右键预置图标）—— 秤盘空着不会产生任何交易。";
      Demo.toast("秤盘是空的：先右键放一件上去。", true);
      return;
    }
    var entry = panEntry();
    if (!entry) {
      box.className = "err-text";
      box.textContent = "请先用 ▲▼（或左键点图标）选择秤盘上这件是什么品类 —— 未选品类不会计价。";
      Demo.toast("还没选品类：用 ▲▼ 选一件。", true);
      return;
    }
    if (entry.cents === null) {
      box.className = "err-text";
      box.textContent = "该商品还没有单价：先在右侧「设置商品名称与价格」里保存一次。";
      return;
    }
    var preview = previewCents();
    $("btnConfirm").disabled = true;
    Demo.log("确认：把秤盘这一件提交服务端计价（本地预览 ¥ " + Demo.yuan(preview) + "，以服务端为准）…");
    ensureProduct(entry).then(function (productId) {
      state.idemKey = Demo.idem("txn");
      return Demo.post("/api/merchant/transactions", {
        token: state.token,
        idempotencyKey: state.idemKey,
        body: {
          items: [{ product_id: productId, weight_grams: state.pan.weightGrams }],
          client_idempotency_key: state.idemKey
        }
      });
    }).then(function (txn) {
      state.txn = txn;                            /* ⇒ 锁定 */
      ScaleView.renderTxnResult(txn);
      render();
      Demo.log("计价成功并锁定：" + txn.transaction_no + " 合计 ¥ " + Demo.yuan(txn.total_amount_cents) +
        "（服务端返回，幂等键 " + state.idemKey + "）");
      if (preview !== null && preview !== txn.total_amount_cents) {
        var note = Demo.node("div", "warn-text", "注意：本地预览 ¥ " + Demo.yuan(preview) +
          " 与服务端计价 ¥ " + Demo.yuan(txn.total_amount_cents) +
          " 不一致 —— 以服务端为准（预览只是「重量 × 单价」的本地估算）。");
        $("txnResult").appendChild(note);
        Demo.log("预览与服务端不一致：预览 ¥ " + Demo.yuan(preview) + " ≠ 服务端 ¥ " +
          Demo.yuan(txn.total_amount_cents) + "（已按服务端显示）", true);
      }
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
    }).catch(function (error) {
      $("btnConfirm").disabled = false;
      Demo.toast("确认失败：" + error.message, true);
      Demo.log("确认失败：" + error.message + "（服务端未生成交易，界面不显示价格）", true);
      box.className = "err-text";
      box.textContent = "确认失败：" + error.message + "（未生成交易）" +
        (error.code === "MT-1009" ? "。提示：商品需先在服务端建立（`POST /api/merchant/products`）。" : "") +
        (error.code === "MT-1006" ? "。提示：该商品当日没有价目表条目 —— 先保存一次单价。" : "");
    });
  }

  /* ---- 第 6 步：模拟扫码（§3.17） ---- */
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

  /* ---- 取消（§3.35，复用同一幂等键）⇒ 解锁 ---- */
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
      state.pan = { placed: false, entryIndex: null, weightGrams: state.pan.weightGrams };
      ScaleView.hideQr();
      ScaleView.hideSuccess();
      $("txnResult").className = "muted";
      $("txnResult").textContent = "已取消该笔交易（不计入应缴）。秤盘已清空，价格已解锁，可以重新放一件。";
      render();
    }).catch(function (error) {
      Demo.log("取消失败：" + error.message + "（服务端状态未改变）", true);
      Demo.toast("取消失败：" + error.message, true);
    });
  }

  /* ---- 任意键回初始 ---- */
  function reset() {
    ScaleView.hideSuccess();
    ScaleView.hideQr();
    state.txn = null;
    state.payment = null;
    state.pan = { placed: false, entryIndex: null, weightGrams: state.pan.weightGrams };
    state.idemKey = null;
    $("btnSimulate").disabled = false;
    $("txnResult").className = "muted";
    $("txnResult").textContent = "确认后这里显示服务端返回的价格明细。";
    render();
    Demo.log("已回到初始界面（秤盘已清空、价格已解锁；商家会话保留，可继续下一笔）");
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
    $("btnClear").onclick = takeOff;
    $("btnConfirm").onclick = confirm;
    $("btnSave").onclick = function () { ScaleProduct.save(ctx(), refreshStall); };
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
