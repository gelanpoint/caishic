/* 运营端页面逻辑（T-027）。配套页面：../admin/index.html。
   原则（父代理本轮强调）：**运营端是"给别人看数"的地方，最容易被做成好看的假数** ——
   故本页所有数字都**直接来自接口响应**，不做任何本地估算、补零或兜底默认值；
   指标页**把分子与分母一起显示**（AC-005「第三方可逐一核对」的页面形态）；
   留痕页**只读**（NFR-009：接口不提供任何写入口，页面也不给编辑控件）。
   金额一律按"分"接收、仅显示时除以 100（不在前端做取整或求和口径）。 */
(function () {
  "use strict";
  var today = new Date().toISOString().slice(0, 10);

  function $(id) { return document.getElementById(id); }
  function yuan(cents) { return "¥ " + (cents / 100).toFixed(2); }
  function percent(bp) { return (bp / 100).toFixed(2) + "%"; }
  function text(node, value) { node.textContent = value === null || value === undefined ? "—" : String(value); }

  function api(method, path) {
    return fetch(path, { method: method }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok) {
          var error = payload && payload.error ? payload.error : { code: "?", message: "HTTP " + response.status };
          throw new Error(error.code + ": " + error.message);
        }
        return payload;
      });
    });
  }
  function post(path, body) {
    return fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok) {
          var error = payload && payload.error ? payload.error : { code: "?", message: "HTTP " + response.status };
          throw new Error(error.code + ": " + error.message);
        }
        return payload;
      });
    });
  }
  function put(path, body) {
    return fetch(path, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok) {
          var error = payload && payload.error ? payload.error : { code: "?", message: "HTTP " + response.status };
          throw new Error(error.code + ": " + error.message);
        }
        return payload;
      });
    });
  }
  function fail(error) { $("err").textContent = String(error.message || error); }
  function clearErr() { $("err").textContent = ""; }

  function table(columns, rowsData, rowBuilder) {
    var table_ = document.createElement("table");
    var head = document.createElement("tr");
    columns.forEach(function (column) { var th = document.createElement("th"); th.textContent = column; head.appendChild(th); });
    table_.appendChild(head);
    rowsData.forEach(function (row) {
      var tr = document.createElement("tr");
      rowBuilder(row).forEach(function (cell, index) {
        var td = document.createElement("td");
        if (index > 0 && typeof cell === "number") { td.className = "num"; td.textContent = String(cell); }
        else { td.textContent = cell === null || cell === undefined ? "—" : String(cell); }
        tr.appendChild(td);
      });
      table_.appendChild(tr);
    });
    return table_;
  }

  // ---- §3.20/§3.21/§3.22 字典与别名 --------------------------------------
  function loadCategories() {
    api("GET", "/api/admin/categories").then(function (data) {
      $("catCount").textContent = data.categories.length + " 个标准品类 / " + data.aliases.length + " 条别名映射";
      var catBox = $("categories");
      catBox.innerHTML = "";
      catBox.appendChild(table(["id", "编码", "名称", "状态"], data.categories,
        function (row) { return [row.id, row.code, row.name, row.status]; }));
      var aliasBox = $("aliases");
      aliasBox.innerHTML = "";
      aliasBox.appendChild(table(["id", "摊位", "别名", "标准品类 id"], data.aliases.slice(0, 40),
        function (row) { return [row.id, row.stall_no, row.alias_name, row.category_id]; }));
    }).catch(fail);
  }

  // ---- §3.23/§3.24 佣金口径 ---------------------------------------------
  function loadRules() {
    api("GET", "/api/admin/commission-rules").then(function (rules) {
      var box = $("rules");
      box.innerHTML = "";
      if (!rules.length) { box.innerHTML = '<div class="empty">还没有佣金口径（日聚合会被 MT-1013 拒绝，这是设计好的前置）</div>'; return; }
      box.appendChild(table(["id", "收费对象", "费率", "档位", "生效起", "生效止"],
        rules, function (row) {
          return [row.id, row.pay_object, percent(row.rate_bp), row.category_tier, row.effective_from, row.effective_to];
        }));
    }).catch(fail);
  }

  // ---- §3.25 市场方看板 --------------------------------------------------
  function loadDashboard() {
    api("GET", "/api/admin/dashboard?business_date=" + today).then(function (data) {
      rows($("market"), [
        ["走秤笔数", data.market.txn_count],
        ["交易总额", yuan(data.market.gross_amount_cents)],
        ["佣金合计", yuan(data.market.commission_amount_cents)]
      ]);
      var box = $("stalls");
      box.innerHTML = "";
      box.appendChild(table(["摊位", "笔数", "交易总额(分)", "退货额(分)", "佣金(分)"], data.stalls,
        function (row) {
          return [row.stall_no, row.txn_count, row.gross_amount_cents, row.refund_amount_cents, row.commission_amount_cents];
        }));
    }).catch(fail);
  }

  // ---- §3.26 聚合 / §3.27/§3.28 结算 / §3.29 对账 ------------------------
  function loadSettlements() {
    api("GET", "/api/admin/settlements").then(function (rowsData) {
      var box = $("settlements");
      box.innerHTML = "";
      if (!rowsData.length) { box.innerHTML = '<div class="empty">还没有结算单</div>'; return; }
      box.appendChild(table(["单号", "摊位", "期起", "期止", "交易总额(分)", "佣金(分)", "版本"],
        rowsData, function (row) {
          return [row.settlement_no, row.stall_no, row.period_start, row.period_end,
            row.gross_amount_cents, row.commission_amount_cents, row.version];
        }));
    }).catch(fail);
  }

  function loadReconciliation() {
    api("GET", "/api/admin/reconciliation?business_date=" + today).then(function (data) {
      rows($("recon"), [
        ["营业日", data.business_date],
        ["订单总额", yuan(data.order_total_cents) + "（" + data.order_total_cents + " 分）"],
        ["支付流水", yuan(data.payment_total_cents) + "（" + data.payment_total_cents + " 分）"],
        ["分账明细", yuan(data.split_total_cents) + "（" + data.split_total_cents + " 分）"],
        ["是否平衡", data.balanced ? "平衡（差 " + data.diff_cents + " 分）" : "不平衡（差 " + data.diff_cents + " 分）"]
      ]);
      $("recon").className = data.balanced ? "" : "err";
    }).catch(fail);
  }

  // ---- §3.30 三个使用率指标（**分子/分母一起显示**）---------------------
  function loadMetrics() {
    api("GET", "/api/admin/metrics/usage?business_date=" + today).then(function (data) {
      var rowsData = [
        ["摊位使用率", data.stall_usage_bp, data.stall_usage_numerator, data.stall_usage_denominator, "当日有走秤交易的摊位数 ÷ 在营摊位数"],
        ["现金交易占比", data.cash_txn_share_bp, data.cash_txn_numerator, data.cash_txn_denominator, "当日现金成功笔数 ÷ 当日全部走秤笔数"],
        ["价目表维护率", data.price_list_maintenance_bp, data.price_list_numerator, data.price_list_denominator, "当日价目表完整的在营摊位数 ÷ 在营摊位数"]
      ];
      var box = $("metrics");
      box.innerHTML = "";
      box.appendChild(table(["指标", "数值", "分子", "分母", "口径"],
        rowsData, function (row) { return [row[0], percent(row[1]), row[2], row[3], row[4]]; }));
      $("metricsNote").textContent = "口径来源：data-model.md §5.1（分子与分母来自系统自有数据，页面不做任何估算）。"
        + " 已废弃的「日均智能秤交易占比」分母是外部基准，本期不提供（Q-15）。";
    }).catch(fail);
  }

  // ---- §3.31 留痕（只读）------------------------------------------------
  function loadAudit() {
    api("GET", "/api/admin/audit-logs?limit=50").then(function (data) {
      $("auditTotal").textContent = "共 " + data.total + " 条（按时间倒序，最多显示 50 条）";
      var box = $("audits");
      box.innerHTML = "";
      if (!data.items.length) { box.innerHTML = '<div class="empty">暂无留痕</div>'; return; }
      box.appendChild(table(["id", "事件", "表", "引用", "操作方", "时间"],
        data.items, function (row) {
          return [row.id, row.event_type, row.ref_table, row.ref_id, row.actor, row.occurred_at];
        }));
    }).catch(fail);
  }

  function rows(box, pairs) {
    box.innerHTML = "";
    pairs.forEach(function (pair) {
      var node = document.createElement("div");
      node.className = "kv";
      node.innerHTML = "<span></span><b></b>";
      node.querySelector("span").textContent = pair[0];
      node.querySelector("b").textContent = String(pair[1]);
      box.appendChild(node);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("reload").onclick = function () {
      clearErr();
      loadCategories(); loadRules(); loadDashboard(); loadSettlements(); loadReconciliation(); loadMetrics(); loadAudit();
    };
    $("aggregate").onclick = function () {
      clearErr();
      post("/api/admin/daily-aggregate", { business_date: today }).then(function (data) {
        $("aggResult").textContent = "聚合完成：摊位 " + data.stalls_aggregated + " 个，版本 revision=" + data.revision;
        loadDashboard(); loadSettlements();
      }).catch(fail);
    };
    $("newRule").onclick = function () {
      clearErr();
      put("/api/admin/commission-rules", {
        pay_object: $("ruleObject").value,
        rate_bp: parseInt($("ruleRate").value, 10) || 0,
        effective_from: today
      }).then(function (rule) {
        $("aggResult").textContent = "已新增佣金口径 #" + rule.id + "：" + rule.pay_object + " " + percent(rule.rate_bp);
        loadRules(); loadDashboard();
      }).catch(fail);
    };
    $("newSettlement").onclick = function () {
      clearErr();
      post("/api/admin/settlements", { stall_no: $("settleStall").value, period_start: today, period_end: today })
        .then(function (data) {
          $("aggResult").textContent = "结算单 " + data.settlement_no + "（版本 " + data.version + "）";
          loadSettlements();
        }).catch(fail);
    };
    $("newCategory").onclick = function () {
      clearErr();
      post("/api/admin/categories", { code: $("catCode").value, name: $("catName").value })
        .then(function (row) { $("aggResult").textContent = "已新增品类 " + row.code; loadCategories(); })
        .catch(fail);
    };
    $("reload").click();
  });
})();
