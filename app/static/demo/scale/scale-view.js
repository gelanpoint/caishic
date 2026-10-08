/* 智能秤页 · 视图层（`REQ-057`）。
 *
 * 这里只做"把数据画出来"，不做任何业务判断：
 * - 预置商品图标复用仓库内像素资源 `/static/game/sprites/`（**本地文件，无外链**）；
 *   manifest 或 PNG 缺失时降级为**纯色块 + 商品名**（不报错、不白屏）；
 * - 金额三态（`AC-047` ④）：**未选品类 ⇒ 不显示任何金额**；**已选未确认 ⇒ 本地预览**
 *   （**该值是在浏览器里按 §3.6 同口径算出来的估算**，不落库、不产生交易；界面上必须标注「本地预览」）；
 *   **确认后 ⇒ 服务端计价并锁定**（标注「已锁定」）。
 *   预览值绝不冒充实收 —— 两者不一致时由 `scale.js` 显示服务端值并说明差异。
 */
window.ScaleView = (function () {
  "use strict";

  var SPRITE_DIR = "/static/game/sprites/";
  var ICON_PX = 48;                     /* 16px 美术 × 3 倍，像素锐利 */

  /* 预置商品图标（页面本地默认值：名称与单价都可由用户在「设置商品名称与价格」里改）。
     覆盖 蔬菜 / 水果 / 肉 / 水产 / 杂货 五类；`category` 是服务端品类字典里的 code。 */
  var CATALOG = [
    { icon: "item_0", name: "青菜", cents: 480, category: "V-01", kind: "蔬菜" },
    { icon: "item_1", name: "土豆", cents: 560, category: "V-02", kind: "蔬菜" },
    { icon: "item_2", name: "苹果", cents: 1200, category: "F-01", kind: "水果" },
    { icon: "item_3", name: "香蕉", cents: 900, category: "F-02", kind: "水果" },
    { icon: "item_4", name: "猪肉", cents: 2600, category: "M-01", kind: "肉" },
    { icon: "item_5", name: "鸡翅", cents: 3200, category: "M-02", kind: "肉" },
    { icon: "item_6", name: "草鱼", cents: 1800, category: "A-01", kind: "水产" },
    { icon: "item_7", name: "调料杂货", cents: 1500, category: "V-03", kind: "杂货" }
  ];

  var art = { ready: false, sprites: {}, sheets: {}, images: {}, note: "美术资源加载中…" };

  function $(id) { return document.getElementById(id); }
  function node(tag, className, text) { return Demo.node(tag, className, text); }

  /* ---- 像素图标：按 manifest 取图（与游戏页同一套本地资源） ---- */
  function loadArt() {
    return fetch(SPRITE_DIR + "manifest.json", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) { throw new Error("manifest 不可取（HTTP " + response.status + "）"); }
        return response.json();
      })
      .then(function (manifest) {
        art.sprites = manifest.sprites || {};
        var jobs = Object.keys(manifest.sheets || {}).map(function (name) {
          return new Promise(function (resolve) {
            var sheet = manifest.sheets[name];
            if (typeof Image === "undefined") { resolve(); return; }
            var image = new Image();
            image.onload = function () { art.sheets[name] = true; art.images[name] = image; resolve(); };
            image.onerror = function () { art.sheets[name] = false; resolve(); };
            image.src = SPRITE_DIR + sheet.file;
          });
        });
        return Promise.all(jobs).then(function () {
          art.ready = Object.keys(art.images).length > 0;
          art.note = art.ready ? "图标来自仓库内像素资源" :
            "像素资源不可用，已降级为色块 + 商品名";
        });
      })
      .catch(function (error) {
        art.ready = false;
        art.note = "像素资源未就绪（" + error.message + "），已降级为色块 + 商品名";
      });
  }

  function drawIcon(canvas, entry) {
    if (!canvas) { return; }
    var ctx = canvas.getContext("2d");
    canvas.width = ICON_PX;
    canvas.height = ICON_PX;
    ctx.imageSmoothingEnabled = false;
    var sprite = art.sprites[entry.icon];
    if (art.ready && sprite && art.sheets[sprite.sheet] && art.images[sprite.sheet]) {
      ctx.drawImage(art.images[sprite.sheet], sprite.x, sprite.y, sprite.w, sprite.h, 0, 0, ICON_PX, ICON_PX);
      return;
    }
    var hue = 0;
    for (var i = 0; i < entry.name.length; i += 1) { hue = (hue * 31 + entry.name.charCodeAt(i)) % 360; }
    ctx.fillStyle = "hsl(" + hue + ", 42%, 46%)";
    ctx.fillRect(0, 0, ICON_PX, ICON_PX);
    ctx.fillStyle = "rgba(255,255,255,0.92)";
    ctx.font = "12px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(entry.name.slice(0, 4), ICON_PX / 2, ICON_PX / 2);
  }

  /* ---- 合并：本地预置图标 × 服务端商品 × 服务端价目表 ----
     服务端有该图标对应的商品时，**一律以服务端为准**（名称/单价/product_id 都取服务端的）。 */
  function merge(products, prices) {
    var byIcon = {};
    (products || []).forEach(function (product) {
      if (product.icon_key && !byIcon[product.icon_key]) { byIcon[product.icon_key] = product; }
    });
    var cents = {};
    (prices || []).forEach(function (row) { cents[row.product_id] = row.unit_price_cents; });
    return CATALOG.map(function (entry) {
      var product = byIcon[entry.icon];
      if (!product) {
        return { icon: entry.icon, name: entry.name, cents: entry.cents, category: entry.category,
          kind: entry.kind, productId: null, listed: false };
      }
      var price = cents[product.id];
      return {
        icon: entry.icon, name: product.name, cents: typeof price === "number" ? price : null,
        category: entry.category, kind: entry.kind, productId: product.id, listed: true,
        hotkey: product.hotkey
      };
    });
  }

  function renderIcons(entries, selectedIndex, handlers) {
    var box = $("iconGrid");
    if (!box) { return; }
    box.innerHTML = "";
    entries.forEach(function (entry, index) {
      var tile = node("div", "icon-tile" + (index === selectedIndex ? " selected" : ""));
      tile.title = "左键选中；右键放上去（加入本笔交易）";
      var canvas = document.createElement("canvas");
      tile.appendChild(canvas);
      tile.appendChild(node("div", "nm", entry.name));
      tile.appendChild(node("div", "id", entry.kind));
      tile.appendChild(node("div", "pr", entry.cents === null ? "未设价" : "¥ " + Demo.yuan(entry.cents) + " /kg"));
      tile.appendChild(node("div", "id", entry.productId ? "服务端商品 #" + entry.productId : "未上架"));
      tile.onclick = function () { handlers.onSelect(index); };
      tile.oncontextmenu = function (event) {
        event.preventDefault();
        handlers.onPlace(index);
      };
      box.appendChild(tile);
      drawIcon(canvas, entry);
    });
    var hint = $("iconHint");
    if (hint) { hint.textContent = art.note + "（8 个预置图标覆盖 蔬菜/水果/肉/水产/杂货）"; }
  }

  /* LED 三格。**金额的三种状态必须分清**（`AC-047` ④）：
     - 秤盘空 / 未选品类 ⇒ 不显示任何金额（"请选品类"）；
     - 选了品类但**未确认** ⇒ 本地预览「重量 × 单价」，单位行明确写「预览」；
     - 确认之后 ⇒ 服务端锁定金额（取 `txn.total_amount_cents`），单位行写「已锁定」。
     预览口径与 §3.6 一致（`(单价 × 克 + 500) / 1000` 整数四舍五入），但**它只是预览**。 */
  function renderLed(view) {
    var priceCell = $("ledPrice");
    var entry = view.entry;
    if (priceCell) {
      priceCell.textContent = !entry ? "—" :
        (entry.cents === null ? "未设价" : Demo.yuan(entry.cents));
    }
    var priceUnit = $("ledPriceUnit");
    if (priceUnit) {
      priceUnit.textContent = entry ? "元 / kg（" + entry.name + "）" : "元 / kg（选品类后显示）";
    }
    var weightCell = $("ledWeight");
    if (weightCell) { weightCell.textContent = String(view.weightGrams); }

    var amountCell = $("ledAmount");
    var amountUnit = $("ledAmountUnit");
    var locked = view.lockedCents !== null && view.lockedCents !== undefined;
    var preview = view.previewCents;
    if (amountCell) {
      var isNumber = locked || (preview !== null && preview !== undefined);
      amountCell.className = "v small" + (isNumber ? "" : " txt");
      if (locked) { amountCell.textContent = Demo.yuan(view.lockedCents); }
      else if (preview !== null && preview !== undefined) { amountCell.textContent = Demo.yuan(preview); }
      else { amountCell.textContent = view.placed ? "请选品类" : "—"; }
    }
    if (amountUnit) {
      amountUnit.textContent = locked ? "元（服务端计价 · 已锁定）" :
        (preview !== null && preview !== undefined ? "元（本地预览，确认后以服务端为准）" :
          (view.placed ? "元（请用 ▲▼ 选择品类）" : "元（先放一件上秤盘）"));
    }
  }

  /* 秤盘（**至多一件**）。状态与 LED 一一对应，且不显示任何未确认的价格。 */
  function renderPan(pan, entry, previewCents, lockedCents) {
    var box = $("basket");
    var count = $("basketCount");
    if (count) { count.textContent = pan.placed ? "1" : "0"; }
    if (!box) { return; }
    box.className = "";
    box.innerHTML = "";
    if (!pan.placed) {
      box.className = "muted";
      box.textContent = "秤盘是空的。右键上面的商品图标，放一件上去（秤只感知重量，品类要你来选）。";
      return;
    }
    var line = node("div", "row");
    line.appendChild(node("span", null, "秤盘上：1 件"));
    line.appendChild(node("span", "muted", pan.weightGrams + " g"));
    box.appendChild(line);
    var detail = node("div", "row");
    if (!entry) {
      detail.appendChild(node("span", "warn-text", "品类：待选品类（用 ▲▼ 或左键点图标选择）"));
      detail.appendChild(node("span", "spacer"));
      detail.appendChild(node("span", "muted", "金额：—（未选品类，不显示价格）"));
    } else {
      detail.appendChild(node("span", null, "品类：" + entry.name + "（" + entry.kind + "）"));
      detail.appendChild(node("span", "muted", entry.productId ? "服务端商品 #" + entry.productId : "尚未在服务端建立"));
      detail.appendChild(node("span", "spacer"));
      if (lockedCents !== null && lockedCents !== undefined) {
        detail.appendChild(node("span", "ok-text", "金额：¥ " + Demo.yuan(lockedCents) + "（服务端计价，已锁定）"));
      } else {
        detail.appendChild(node("span", "muted", "单价 ¥ " + Demo.yuan(entry.cents) + " /kg　金额 ¥ " +
          (previewCents === null || previewCents === undefined ? "—" : Demo.yuan(previewCents)) + "（本地预览）"));
      }
    }
    box.appendChild(detail);
  }

  function renderTxnResult(txn) {
    var box = $("txnResult");
    if (!box) { return; }
    box.className = "";
    box.innerHTML = "";
    box.appendChild(node("div", "ok-text", "交易号 " + txn.transaction_no + "（状态 " + txn.status + "）"));
    (txn.items || []).forEach(function (item) {
      var line = node("div", "muted");
      line.textContent = "商品 #" + item.product_id + "：" + item.weight_grams + " g × ¥ " +
        Demo.yuan(item.final_unit_price_cents) + " = ¥ " + Demo.yuan(item.amount_cents);
      box.appendChild(line);
    });
    var total = node("div", null, "合计（服务端返回）：¥ " + Demo.yuan(txn.total_amount_cents));
    total.style.fontWeight = "700";
    box.appendChild(total);
  }

  function showQr(payload, receiverMasked, paymentNo) {
    var area = $("qrArea");
    if (area) { area.style.display = ""; }
    var payloadBox = $("qrPayload");
    if (payloadBox) {
      payloadBox.textContent = "qr_payload（服务端下发，未在前端拼接）：" + payload +
        "　｜　支付单号 " + paymentNo + "　｜　收款标识（脱敏）" + (receiverMasked || "—");
    }
    /* 收款码的说明文字由 `Demo.renderQr` 按"真码 / 占位图"两种情况分别写入 `#qrNote` */
    Demo.renderQr($("qrCanvas"), payload);
  }

  function hideQr() {
    var area = $("qrArea");
    if (area) { area.style.display = "none"; }
  }

  function showSuccess(txn, payment) {
    var screen = $("successScreen");
    if (!screen) { return; }
    screen.style.display = "";
    var amount = $("successAmount");
    if (amount) { amount.textContent = "¥ " + Demo.yuan(txn.total_amount_cents); }
    var meta = $("successMeta");
    if (meta) {
      meta.textContent = "交易号 " + txn.transaction_no + "　｜　支付单号 " + payment.payment_no +
        "　｜　收款标识（脱敏）" + (payment.receiver_token_masked || "—") +
        "　｜　状态 " + payment.transaction_status;
    }
  }

  function hideSuccess() {
    var screen = $("successScreen");
    if (screen) { screen.style.display = "none"; }
  }

  function setEditTarget(entry) {
    var box = $("editTarget");
    if (!box) { return; }
    box.textContent = entry
      ? "当前选中：" + entry.name + "（" + entry.kind + "）｜" +
        (entry.productId ? "服务端商品 #" + entry.productId : "尚未在服务端建立")
      : "先在左边选中一个预置商品。";
  }

  function setSaveResult(text, isError) {
    var box = $("saveResult");
    if (!box) { return; }
    box.className = isError ? "err-text" : "ok-text";
    box.textContent = text;
  }

  return {
    CATALOG: CATALOG,
    loadArt: loadArt,
    merge: merge,
    renderIcons: renderIcons,
    renderLed: renderLed,
    renderPan: renderPan,
    renderTxnResult: renderTxnResult,
    showQr: showQr,
    hideQr: hideQr,
    showSuccess: showSuccess,
    hideSuccess: hideSuccess,
    setEditTarget: setEditTarget,
    setSaveResult: setSaveResult,
    artState: function () { return art; }
  };
})();
