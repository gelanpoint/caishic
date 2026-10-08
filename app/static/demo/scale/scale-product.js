/* 智能秤页 · 商品名称与价格（`REQ-058` / `AC-048`）。
 *
 * 只做一件事：把「预置图标 → 服务端商品」这条链子写实 —— `POST /api/merchant/products`（§3.38 upsert，
 * `X-Stall-Session` 鉴权，`stall_id` 只从会话取）。**不带 `product_id` 即新增、带上即修改**，
 * 服务端同时写当日价目表 ⇒ 新商品当天就能计价（否则 §3.6 回 `MT-1006`）。
 *
 * 从 `scale.js` 拆出来纯粹是为了守住「单文件 ≤400 行」（`docs/standards/quality-gates.md`）——
 * 本模块不持有状态，全部经参数传入，便于单独读。
 */
window.ScaleProduct = (function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function upsert(ctx, entry, name, cents) {
    return Demo.post("/api/merchant/products", {
      token: ctx.token,
      body: {
        name: name,
        unit_price_cents: cents,
        category_code: entry.category,
        icon_key: entry.icon,
        hotkey: String((ctx.entries.indexOf(entry) % 9) + 1),
        product_id: entry.productId || undefined
      }
    });
  }

  /* 「保存」按钮：校验 → upsert → 把服务端返回值写回该图标 → 重取本摊位数据 */
  function save(ctx, refresh) {
    var entry = ctx.entries[ctx.cursor];
    if (!ctx.token || !entry) { ScaleView.setSaveResult("先选择商家并选中一个商品。", true); return; }
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
    upsert(ctx, entry, name, cents).then(function (data) {
      entry.productId = data.product_id;
      entry.name = data.name;
      entry.cents = data.unit_price_cents;
      entry.listed = true;
      ScaleView.setSaveResult("已保存：" + data.name + " → ¥ " + Demo.yuan(data.unit_price_cents) +
        " /kg（服务端商品 #" + data.product_id + "，业务日 " + data.business_date +
        "，状态 " + data.status + "）—— 现在即可用它计价。", false);
      Demo.log("商品已保存：#" + data.product_id + " " + data.name + " → ¥ " +
        Demo.yuan(data.unit_price_cents) + "（§3.38 upsert，已写当日价目表）");
      return refresh();
    }).catch(function (error) {
      ScaleView.setSaveResult("保存失败：" + error.message + "（服务端未新增/未修改；本页不伪造商品）", true);
      Demo.log("保存商品失败：" + error.message, true);
    });
  }

  return { upsert: upsert, save: save };
})();
