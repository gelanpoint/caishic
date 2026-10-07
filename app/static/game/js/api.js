/* 演示游戏 · 数据层（T-GAME-03；REQ-044 / REQ-046 / REQ-047）。
 *
 * 一条纪律：**本文件里没有任何商品、价格、摊位常量** —— 摊位清单来自运营端看板（契约 §3.25），
 * 在售商品来自 §3.5，单价来自 §3.3/§3.4，全部是既有端点的真实响应。
 * 前端只做"取回来 → 展示"，不做估算、不做兜底默认值：取不到就如实报"未就绪"。
 *
 * 会话：既有服务里"读某个摊位的价目表"必须带**该摊位**的 `X-Stall-Session`（§3.2 下发）。
 * 故每个摊位按需绑定一个会话并缓存在 sessionStorage（同一浏览器会话内不重复绑定），
 * 这样悬停任意摊位都取得到真实数据，而不必把玩家正在扮演的商家切来切去。
 *
 * 不新增任何服务端能力（NFR-016）：本文件调用的端点逐个都是既有契约端点，
 * 唯一的例外是 §3.5x 的上下架端点 —— 它是 REQ-049 独立立项的服务端能力。
 */
window.GameApi = (function () {
  "use strict";

  var TOKEN_PREFIX = "game.stallToken.";
  var memoryTokens = {};
  var stallCache = {};
  /* 业务日以**服务端返回的**为准（看板响应里的 business_date），不在前端自己造日期口径。
     契约 §3.3 的 `business_date` 是**必填**查询参数（缺了回 MT-1008），故读价目表必须带上它。 */
  var currentDay = null;

  function makeError(code, message, status, payload) {
    var error = new Error(code + ": " + message);
    error.code = code;
    error.status = status;
    error.payload = payload;
    return error;
  }

  function request(method, path, options) {
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
          throw makeError(envelope.code || "?", envelope.message || ("HTTP " + response.status),
            response.status, payload);
        }
        return payload;
      });
    });
  }

  function todayIso() {
    var now = new Date();
    return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  }

  /* 摊位清单：§3.25 市场方看板覆盖**全部在营摊位**（含当日零交易者）。
     这里按 stall_no 排序只为**布局稳定**（看板本身按交易额排行），不是数据加工。 */
  function roster() {
    return request("GET", "/api/admin/dashboard?business_date=" + todayIso()).then(function (data) {
      var stalls = (data.stalls || []).map(function (row) { return row.stall_no; });
      stalls.sort();
      currentDay = data.business_date;
      return { businessDate: data.business_date, stalls: stalls };
    });
  }

  function day() { return currentDay || todayIso(); }

  function tokenFor(stallNo) {
    if (memoryTokens[stallNo]) { return Promise.resolve(memoryTokens[stallNo]); }
    var saved = sessionStorage.getItem(TOKEN_PREFIX + stallNo);
    if (saved) {
      memoryTokens[stallNo] = saved;
      return Promise.resolve(saved);
    }
    return request("POST", "/api/merchant/session", { body: { stall_no: stallNo } })
      .then(function (data) {
        memoryTokens[stallNo] = data.session_token;
        sessionStorage.setItem(TOKEN_PREFIX + stallNo, data.session_token);
        return data.session_token;
      });
  }

  /* 在售商品 × 单价 的合并：**以 §3.5 的商品清单为骨架**（它只返回 `status = active`），
     故下架商品不会出现在合并结果里（AC-035 的"下架的不显示"由这条保证，不是靠前端过滤名单）。 */
  function joinProducts(stallNo, products, priceList) {
    var cents = {};
    (priceList.items || []).forEach(function (row) { cents[row.product_id] = row.unit_price_cents; });
    var items = (products || []).map(function (product) {
      var price = cents[product.id];
      return {
        productId: product.id,
        name: product.name,
        iconKey: product.icon_key,
        hotkey: product.hotkey,
        unitPriceCents: typeof price === "number" ? price : null
      };
    });
    return {
      stallNo: stallNo,
      businessDate: priceList.business_date,
      priceListMissing: !!priceList.price_list_missing,
      items: items,
      loadedAt: Date.now()
    };
  }

  function stallData(stallNo, force) {
    if (!force && stallCache[stallNo]) { return Promise.resolve(stallCache[stallNo]); }
    return tokenFor(stallNo).then(function (token) {
      return Promise.all([
        request("GET", "/api/merchant/products", { token: token }),
        request("GET", "/api/merchant/price-list?business_date=" + encodeURIComponent(day()), { token: token })
      ]).then(function (both) {
        var data = joinProducts(stallNo, both[0], both[1]);
        stallCache[stallNo] = data;
        return data;
      });
    });
  }

  /* §3.4 调价：只提交这一条 `product_id → unit_price_cents`，其余条目不动。
     返回值是服务端重算后的整张价目表 —— 调用方**必须**用它刷新界面，不得改本地数字。 */
  function setPrice(stallNo, productId, unitPriceCents, businessDate) {
    return tokenFor(stallNo).then(function (token) {
      return request("POST", "/api/merchant/price-list", {
        token: token,
        body: {
          business_date: businessDate || day(),
          items: [{ product_id: productId, unit_price_cents: unitPriceCents }]
        }
      }).then(function (payload) {
        stallCache[stallNo] = null;
        return payload;
      });
    });
  }

  /* §3.5x 上下架（REQ-049，独立立项的服务端能力）：`status` 取值来自 data-model 的 `active`/`inactive`。
     本函数**不猜端点是否已落地** —— 未落地时它就是一个真实的 404，界面如实显示（不假装成功）。 */
  function setStatus(stallNo, productId, status) {
    return tokenFor(stallNo).then(function (token) {
      return request("POST", "/api/merchant/products/" + productId + "/status", {
        token: token,
        body: { status: status }
      }).then(function (payload) {
        stallCache[stallNo] = null;
        return payload;
      });
    });
  }

  function stallProfile(stallNo) {
    return request("GET", "/api/customer/stalls/" + encodeURIComponent(stallNo) + "/profile");
  }

  /* 顾客买单（`REQ-052` / `AC-042`）：走**既有**两个端点 —— §3.6 创建计价 + §3.10 确认收款
     （现金）。**不新增端点、不直接改库**（`REQ-044`）。
     两处要点：
     - **幂等键必须唯一**：复用同一个键会被 `MT-1012` 拒掉，或者被服务端认成同一笔而不落新账；
       这里用 `摊位 + 序号 + 时间戳` 拼，保证每笔都不同。
     - **失败原样抛出**，由调用方**明确记录** —— 绝不吞掉（`RL-9`：不静默丢弃任何一笔交易）。 */
  function buy(stallNo, productId, weightGrams, seq) {
    var key = "game-" + stallNo + "-" + seq + "-" + Date.now();
    return tokenFor(stallNo).then(function (token) {
      return request("POST", "/api/merchant/transactions", {
        token: token,
        idempotencyKey: key,
        body: {
          items: [{ product_id: productId, weight_grams: weightGrams }],
          client_idempotency_key: key
        }
      }).then(function (txn) {
        return request("POST", "/api/merchant/transactions/" + txn.transaction_no + "/payment", {
          token: token,
          idempotencyKey: key + "-pay",
          body: { method: "cash", operator: "顾客（演示）" }
        }).then(function (paid) {
          return {
            transactionNo: txn.transaction_no,
            totalCents: txn.total_amount_cents,
            weightGrams: weightGrams,
            productId: productId,
            status: paid.transaction_status
          };
        });
      });
    });
  }

  function dashboard(businessDate) {
    return request("GET", "/api/admin/dashboard?business_date=" + (businessDate || day()));
  }

  function usageMetrics(businessDate) {
    return request("GET", "/api/admin/metrics/usage?business_date=" + (businessDate || day()));
  }

  function auditLogs(limit) {
    return request("GET", "/api/admin/audit-logs?limit=" + (limit || 8));
  }

  return {
    todayIso: todayIso,
    roster: roster,
    stallData: stallData,
    setPrice: setPrice,
    setStatus: setStatus,
    stallProfile: stallProfile,
    buy: buy,
    dashboard: dashboard,
    usageMetrics: usageMetrics,
    auditLogs: auditLogs,
    clearCache: function () { stallCache = {}; },
    cachedStall: function (stallNo) { return stallCache[stallNo] || null; },
    yuan: function (cents) { return (cents / 100).toFixed(2); }
  };
})();
