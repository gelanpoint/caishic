/* 顾客扫码页逻辑（T-028）。配套页面：../customer/index.html。
   **本页的第一约束是字段白名单**（REQ-023 / AC-011 / discovery D-08）：
   只渲染契约 §3.18 / §3.19 列出的字段，且**每个字段必须有真值才渲染** ——
   `null` / 空串 / 空数组一律不显示（绵阳"扫过好几次空码"的教训：一个空壳字段会连带毁掉整页的可信度）。
   故本文件把白名单写成常量，并在渲染前用 `pickWhitelisted()` **再过滤一次**：
   即使接口将来多返回一个字段，页面也不会显示它（多出来的那个字段必然没有采集来源）。 */
(function () {
  "use strict";
  //: §3.18 白名单（契约原文）
  var PROFILE_FIELDS = ["stall_no", "stall_name", "in_business", "price_consistency_bp", "computed_at"];
  //: §3.19 白名单与明细元素白名单（契约原文）
  var RECEIPT_FIELDS = ["transaction_no", "status", "total_amount_cents", "items", "paid_at"];
  var ITEM_FIELDS = ["name", "weight_grams", "amount_cents"];
  var STATUS_TEXT = { priced: "已计价（未收款）", paid: "已收款", refunded: "已退货冲正", payment_failed: "收款失败" };

  function $(id) { return document.getElementById(id); }
  function hasValue(value) {
    if (value === null || value === undefined) { return false; }
    if (typeof value === "string") { return value.trim() !== ""; }
    if (Array.isArray(value)) { return value.length > 0; }
    return true; // 数字与布尔（含 0 / false）都是真值，不是空壳
  }
  function pickWhitelisted(payload, allowed) {
    var out = {};
    allowed.forEach(function (key) {
      if (payload && hasValue(payload[key])) { out[key] = payload[key]; }
    });
    return out;
  }
  function yuan(cents) { return "¥ " + (cents / 100).toFixed(2); }
  function percent(bp) { return (bp / 100).toFixed(2) + "%"; }

  function rows(box, pairs) {
    box.innerHTML = "";
    pairs.forEach(function (pair) {
      if (!hasValue(pair[1])) { return; } // 空壳字段不渲染
      var node = document.createElement("div");
      node.className = "kv";
      node.innerHTML = "<span>" + pair[0] + "</span><b></b>";
      node.querySelector("b").textContent = String(pair[1]);
      box.appendChild(node);
    });
  }

  function renderProfile(payload) {
    var data = pickWhitelisted(payload, PROFILE_FIELDS);
    $("profileFor").textContent = data.stall_no || "—";
    rows($("profile"), [
      ["摊位号", data.stall_no],
      ["摊位名称", data.stall_name],
      ["当前是否在营", data.in_business === true ? "在营" : (data.in_business === false ? "未营业" : "")],
      ["标价一致率", hasValue(data.price_consistency_bp) ? percent(data.price_consistency_bp) : ""],
      ["指标计算时刻", data.computed_at]
    ]);
  }

  function renderReceipt(payload) {
    var data = pickWhitelisted(payload, RECEIPT_FIELDS);
    $("receiptNo").textContent = data.transaction_no || "—";
    rows($("receipt"), [
      ["交易状态", data.status ? (STATUS_TEXT[data.status] || data.status) : ""],
      ["金额", hasValue(data.total_amount_cents) ? yuan(data.total_amount_cents) : ""],
      ["收款时间", data.paid_at],
      ["交易号", data.transaction_no]
    ]);
    var box = $("receiptItems");
    box.innerHTML = "";
    var items = (data.items || []).map(function (item) { return pickWhitelisted(item, ITEM_FIELDS); });
    if (!items.length) {
      box.innerHTML = '<div class="empty">这笔交易没有可展示的明细（页面不显示无来源的空壳字段）</div>';
      return;
    }
    var table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>商品</th><th class='num'>重量(克)</th><th class='num'>金额</th></tr></thead>";
    var body = document.createElement("tbody");
    items.forEach(function (item) {
      var tr = document.createElement("tr");
      var cells = [item.name || "—", String(item.weight_grams), yuan(item.amount_cents)];
      cells.forEach(function (text, index) {
        var td = document.createElement("td");
        if (index > 0) { td.className = "num"; }
        td.textContent = text;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    table.appendChild(body);
    box.appendChild(table);
  }

  function load() {
    var params = new URLSearchParams(location.search);
    var stallNo = params.get("stall") || "A-01";
    var txnNo = params.get("txn");
    $("err").textContent = "";

    fetch("/api/customer/stalls/" + encodeURIComponent(stallNo) + "/profile")
      .then(function (response) { return response.json(); })
      .then(renderProfile)
      .catch(function () { $("err").textContent = "摊位信息读取失败"; });

    if (txnNo) {
      $("receiptCard").classList.remove("hide");
      fetch("/api/customer/receipts/" + encodeURIComponent(txnNo))
        .then(function (response) { return response.json(); })
        .then(renderReceipt)
        .catch(function () { $("err").textContent = "交易凭证读取失败（可能交易号不存在）"; });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("reload").onclick = load;
    $("stallPick").onchange = function () {
      var url = new URL(location.href);
      url.searchParams.set("stall", this.value);
      location.href = url.toString();
    };
    load();
  });
})();
