/* 演示游戏 · 操作面板（T-GAME-03；REQ-045 / REQ-046 / REQ-047）。
 *
 * `AC-036` 的全部写操作都在这里，且都遵守同一条纪律：
 * **只有服务端说成功才算成功** —— 失败时把错误码与原文摊开给用户看（`MT-1004` 越权、
 * `MT-1009` 端点未注册、`MT-1008` 参数不合法…），绝不假装成功、绝不在本地先把数字改掉。
 * 成功后一律**重新取数**（`GameApi.stallData(..., true)`）再渲染 —— 地图标牌与悬停清单
 * 因此反映的是服务端的新状态，而不是前端的乐观更新。
 */
window.GameOps = (function () {
  "use strict";

  /* 本次会话内被下架的商品的本地账：只为给出"上架"入口（§3.5 商品清单只返回在售商品，
     下架后服务端不会再返回它，界面就没法把它再点回来）。条目本身来自服务端响应，不是硬编码；
     账落在 sessionStorage（**同一浏览器会话内刷新页面不丢**），换标签页/换浏览器即清空。 */
  var SHELF_PREFIX = "game.shelved.";
  var shelvedOut = {};

  function shelfOf(stallNo) {
    if (!shelvedOut[stallNo]) {
      var raw = null;
      try { raw = sessionStorage.getItem(SHELF_PREFIX + stallNo); } catch (error) { raw = null; }
      var list = [];
      if (raw) {
        try { list = JSON.parse(raw) || []; } catch (error) { list = []; }
      }
      shelvedOut[stallNo] = list;
    }
    return shelvedOut[stallNo];
  }

  function saveShelf(stallNo) {
    try { sessionStorage.setItem(SHELF_PREFIX + stallNo, JSON.stringify(shelvedOut[stallNo] || [])); }
    catch (error) { /* 存不了也不影响本次会话内的界面 */ }
  }

  function node(tag, className, text) { return GamePanels.node(tag, className, text); }

  function setResult(target, text, isError) {
    target.textContent = text;
    target.className = "result" + (isError ? " err" : " ok");
  }

  function priceText(cents) { return cents === null || cents === undefined ? "未设价" : "¥" + GameApi.yuan(cents); }

  function refreshAndRender(stallNo, hooks, message, isError) {
    return GameApi.stallData(stallNo, true).then(function (data) {
      hooks.syncStall(stallNo, data);
      merchantOps(stallNo, data, hooks, message, isError);
      return data;
    });
  }

  /* ---- 商家：点智能秤后的操作面板（AC-036） ----------------------------- */
  function merchantOps(stallNo, data, hooks, keepMessage, keepError) {
    var body = GamePanels.opsPanel("摊位 " + stallNo + " · 智能秤（商家操作）");
    if (!body) { return; }
    body.appendChild(node("div", "muted",
      "调价与上架 / 下架都提交到真实端点；成功后地图标牌与悬停清单会重新取数刷新。"));

    var result = node("div", "result" + (keepError ? " err" : keepMessage ? " ok" : ""), keepMessage || "");
    body.appendChild(result);

    if (!data) {
      body.appendChild(node("div", "muted", "该摊位数据未取到（服务端未响应）。点右上角刷新重试。"));
      return;
    }
    if (!data.items.length) {
      body.appendChild(node("div", "muted",
        data.priceListMissing ? "该摊位当日价目表缺失（§3.3 的 price_list_missing）。" : "该摊位当前没有在售商品。"));
    }
    data.items.forEach(function (item) {
      body.appendChild(priceRow(stallNo, data, item, hooks, result));
    });

    var shelved = shelfOf(stallNo);
    if (shelved.length) {
      body.appendChild(node("h4", "op-head", "本次会话内已下架（点「上架」可恢复）"));
      shelved.forEach(function (item) {
        var line = node("div", "op-row");
        line.appendChild(node("span", "op-name", item.name));
        line.appendChild(node("span", "op-price", priceText(item.unitPriceCents) + " · 已下架"));
        line.appendChild(GamePanels.button("上架", "primary", function () {
          submitStatus(stallNo, item, "active", hooks, result);
        }));
        body.appendChild(line);
      });
    }
  }

  function priceRow(stallNo, data, item, hooks, result) {
    var line = node("div", "op-row");
    line.appendChild(node("span", "op-name", item.name));
    line.appendChild(node("span", "op-price", priceText(item.unitPriceCents)));

    var input = document.createElement("input");
    input.type = "number";
    input.step = "0.01";
    input.min = "0.01";
    input.className = "op-input";
    input.value = item.unitPriceCents === null ? "" : (item.unitPriceCents / 100).toFixed(2);
    input.setAttribute("aria-label", item.name + " 新单价（元）");
    line.appendChild(input);

    line.appendChild(GamePanels.button("设价", "primary", function () {
      submitPrice(stallNo, data, item, input, hooks, result);
    }));
    line.appendChild(GamePanels.button("下架", "ghost", function () {
      submitStatus(stallNo, item, "inactive", hooks, result);
    }));
    return line;
  }

  function submitPrice(stallNo, data, item, input, hooks, result) {
    var raw = String(input.value === undefined || input.value === null ? "" : input.value).trim();
    var yuan = Number(raw);
    if (!raw || !isFinite(yuan) || yuan <= 0) {
      setResult(result, "单价必须是大于 0 的数字（元）——本次没有提交任何请求。", true);
      return;
    }
    var cents = Math.round(yuan * 100);
    if (cents < 1) {
      setResult(result, "单价不足 0.01 元（`unit_price_cents` 必须 ≥ 1）——本次没有提交任何请求。", true);
      return;
    }
    setResult(result, "提交中…（POST /api/merchant/price-list）", false);
    GameApi.setPrice(stallNo, item.productId, cents, data.businessDate).then(function (payload) {
      var fresh = (payload.items || []).filter(function (row) { return row.product_id === item.productId; })[0];
      var now = fresh ? fresh.unit_price_cents : cents;
      GamePanels.log("调价成功：" + stallNo + " " + item.name + " → " + priceText(now) +
        "（业务日 " + payload.business_date + "，source=" + payload.source + "）");
      return refreshAndRender(stallNo, hooks, "调价成功：" + item.name + " → " + priceText(now) +
        "（服务端已生效，source=" + payload.source + "）", false);
    }).catch(function (error) {
      setResult(result, "调价失败：" + error.message + "（服务端价格未改变）", true);
      GamePanels.log("调价失败：" + stallNo + " " + item.name + " —— " + error.message, true);
    });
  }

  function submitStatus(stallNo, item, status, hooks, result) {
    var verb = status === "inactive" ? "下架" : "上架";
    setResult(result, verb + "中…（POST /api/merchant/products/" + item.productId + "/status）", false);
    GameApi.setStatus(stallNo, item.productId, status).then(function (payload) {
      var list = shelfOf(stallNo);
      if (status === "inactive") {
        if (!list.some(function (row) { return row.productId === item.productId; })) { list.push(item); }
      } else {
        shelvedOut[stallNo] = list.filter(function (row) { return row.productId !== item.productId; });
      }
      saveShelf(stallNo);
      GamePanels.log(verb + "成功：" + stallNo + " " + item.name + "（服务端返回 " +
        JSON.stringify(payload) + "）");
      return refreshAndRender(stallNo, hooks, verb + "成功：" + item.name +
        "（服务端返回 " + JSON.stringify(payload) + "）", false);
    }).catch(function (error) {
      var hint = error.status === 404 ? "（该端点由 REQ-049 独立立项，当前服务端未注册此路径）" : "";
      setResult(result, verb + "失败：" + error.message + hint + "（服务端状态未改变）", true);
      GamePanels.log(verb + "失败：" + stallNo + " " + item.name + " —— " + error.message, true);
    });
  }

  /* ---- 顾客：点摊位看 §3.18 顾客可见信息（只读） ------------------------ */
  function customerProfile(stallNo, data) {
    var body = GamePanels.opsPanel("摊位 " + stallNo + " · 顾客可见信息（只读，§3.18）");
    if (!body) { return; }
    var profileBox = node("div", null, "读取中…");
    body.appendChild(profileBox);
    var listBox = node("div", "op-list");
    body.appendChild(node("h4", "op-head", "当前在售商品与单价（取自价目表）"));
    body.appendChild(listBox);
    renderItemList(listBox, data);

    GameApi.stallProfile(stallNo).then(function (profile) {
      profileBox.innerHTML = "";
      profileBox.appendChild(GamePanels.row("摊位名", profile.stall_name));
      profileBox.appendChild(GamePanels.row("是否在营", profile.in_business ? "在营" : "未营业"));
      profileBox.appendChild(GamePanels.row("标价一致率", (profile.price_consistency_bp / 100).toFixed(2) + "%（万分比 " +
        profile.price_consistency_bp + "）"));
      profileBox.appendChild(GamePanels.row("计算时刻", profile.computed_at));
      profileBox.appendChild(node("div", "muted",
        "本面板**没有任何写入口**：顾客视角只读，调价与上下架在商家视角点智能秤。"));
    }).catch(function (error) {
      profileBox.textContent = "读取摊位信息失败：" + error.message;
      profileBox.className = "err";
    });
  }

  function renderItemList(box, data) {
    if (!box) { return; }
    box.innerHTML = "";
    if (!data || !data.items.length) {
      box.appendChild(node("div", "muted", data ? "当前无在售商品" : "该摊位数据未取到"));
      return;
    }
    data.items.forEach(function (item) {
      box.appendChild(GamePanels.row(item.name, priceText(item.unitPriceCents)));
    });
  }

  /* ---- 管理员：点电脑看市场看板 / 指标 / 留痕（只读） ------------------- */
  function adminPanel(businessDate) {
    var body = GamePanels.opsPanel("管理端 · 市场看板（只读，§3.25 / §3.30 / §3.31）");
    if (!body) { return; }
    var market = node("div", null, "读取中…");
    var stalls = node("div", "op-list");
    var metrics = node("div", "op-list");
    var audits = node("div", "op-list");
    body.appendChild(market);
    body.appendChild(node("h4", "op-head", "摊位汇总（按交易总额排行）"));
    body.appendChild(stalls);
    body.appendChild(node("h4", "op-head", "使用率指标（分子 / 分母一起看）"));
    body.appendChild(metrics);
    body.appendChild(node("h4", "op-head", "最近留痕（只读）"));
    body.appendChild(audits);

    GameApi.dashboard(businessDate).then(function (data) {
      market.innerHTML = "";
      market.appendChild(GamePanels.row("营业日", data.business_date));
      market.appendChild(GamePanels.row("走秤笔数", data.market.txn_count));
      market.appendChild(GamePanels.row("交易总额", "¥" + GameApi.yuan(data.market.gross_amount_cents)));
      market.appendChild(GamePanels.row("佣金合计", "¥" + GameApi.yuan(data.market.commission_amount_cents)));
      stalls.innerHTML = "";
      data.stalls.slice(0, 6).forEach(function (row) {
        stalls.appendChild(GamePanels.row(row.stall_no + "（" + row.txn_count + " 笔）",
          "¥" + GameApi.yuan(row.gross_amount_cents)));
      });
    }).catch(function (error) { market.textContent = "看板读取失败：" + error.message; market.className = "err"; });

    GameApi.usageMetrics(businessDate).then(function (data) {
      metrics.innerHTML = "";
      metrics.appendChild(GamePanels.row("摊位使用率",
        (data.stall_usage_bp / 100).toFixed(2) + "%（分子 " + data.stall_usage_numerator +
        " / 分母 " + data.stall_usage_denominator + "）"));
      metrics.appendChild(GamePanels.row("现金交易占比",
        (data.cash_txn_share_bp / 100).toFixed(2) + "%（分子 " + data.cash_txn_numerator +
        " / 分母 " + data.cash_txn_denominator + "）"));
      metrics.appendChild(GamePanels.row("价目表维护率",
        (data.price_list_maintenance_bp / 100).toFixed(2) + "%（分子 " + data.price_list_numerator +
        " / 分母 " + data.price_list_denominator + "）"));
    }).catch(function (error) { metrics.textContent = "指标读取失败：" + error.message; metrics.className = "err"; });

    GameApi.auditLogs(8).then(function (data) {
      audits.innerHTML = "";
      audits.appendChild(node("div", "muted", "共 " + data.total + " 条，最多显示 8 条"));
      (data.items || []).forEach(function (item) {
        audits.appendChild(GamePanels.row(item.event_type + " · " + item.ref_table, item.occurred_at));
      });
    }).catch(function (error) { audits.textContent = "留痕读取失败：" + error.message; audits.className = "err"; });
  }

  return {
    merchantOps: merchantOps,
    customerProfile: customerProfile,
    adminPanel: adminPanel,
    renderItemList: renderItemList,
    shelved: function (stallNo) { return shelfOf(stallNo); }
  };
})();
