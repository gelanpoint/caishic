/* 商家注册页逻辑（`REQ-053` / `REQ-057`）。
 *
 * 真实调用：`POST /api/demo/merchants`（注册）、`GET /api/demo/merchants`（列表）。
 * 本页**不硬编码任何商家**：列表每一行都来自服务端响应。
 * 明文收款码只存在于输入框与本次请求体内，页面**不回显**它（只显示服务端回的脱敏值）。
 */
(function () {
  "use strict";

  var FIELDS = [
    { key: "merchant_name", id: "name", min: 1, max: 32, label: "商家名" },
    { key: "phone", id: "phone", min: 1, max: 20, label: "手机号" },
    { key: "receiver_code", id: "code", min: 4, max: 64, label: "收款码" }
  ];

  function $(id) { return document.getElementById(id); }

  function readForm() {
    var body = {};
    FIELDS.forEach(function (field) { body[field.key] = String($(field.id).value || "").trim(); });
    return body;
  }

  /* 本地只做"明显不合契约"的拦截，**权威校验在服务端**（服务端不合法会回 MT-1008，页面照实显示） */
  function localError(body) {
    var name = body.merchant_name;
    if (name.length < 1 || name.length > 32) { return "商家名长度必须是 1~32 字符。"; }
    if (!/^[0-9-]{1,20}$/.test(body.phone)) { return "手机号只能是数字与「-」，长度 1~20。"; }
    if (body.receiver_code.length < 4 || body.receiver_code.length > 64) { return "收款码长度必须是 4~64 字符。"; }
    return null;
  }

  function showResult(kind, title, rows) {
    var box = $("formResult");
    Demo.clear(box);
    box.className = kind === "ok" ? "ok-text" : "err-text";
    box.appendChild(Demo.node("div", null, title));
    (rows || []).forEach(function (pair) {
      var line = Demo.node("div", "muted");
      line.textContent = pair[0] + "：" + pair[1];
      box.appendChild(line);
    });
  }

  function submit() {
    var body = readForm();
    var problem = localError(body);
    if (problem) {
      showResult("err", "本地校验未通过：" + problem + "（没有提交任何请求）");
      return;
    }
    $("submitBtn").disabled = true;
    showResult("ok", "提交中…（POST /api/demo/merchants）");
    Demo.post("/api/demo/merchants", { body: body }).then(function (data) {
      showResult("ok", "已注册：商家 #" + data.merchant_id + "（服务端返回如下脱敏值）", [
        ["商家名", data.merchant_name],
        ["摊位号", data.stall_no + "（服务端按 D- 两位序号分配，不接受请求指定）"],
        ["手机号（脱敏）", data.phone_masked],
        ["收款码（脱敏）", data.receiver_token_masked],
        ["明文收款码", "未回显、未落库（REQ-024）"]
      ]);
      Demo.toast("已注册 " + data.merchant_name + "（摊位 " + data.stall_no + "）");
      Demo.log("注册成功：merchant_id=" + data.merchant_id + " stall_no=" + data.stall_no +
        " receiver_token_masked=" + data.receiver_token_masked);
      FIELDS.forEach(function (field) { $(field.id).value = ""; });
      return loadMerchants();
    }).catch(function (error) {
      showResult("err", "注册失败：" + error.message + "（服务端未写入任何数据）");
      Demo.toast("注册失败：" + error.message, true);
      Demo.log("注册失败：" + error.message, true);
    }).then(function () { $("submitBtn").disabled = false; });
  }

  function loadMerchants() {
    var box = $("merchantTable");
    Demo.clear(box);
    box.appendChild(Demo.node("div", "empty", "读取中…（GET /api/demo/merchants）"));
    return Demo.get("/api/demo/merchants").then(function (data) {
      var items = data.items || [];
      $("listMeta").textContent = "共 " + items.length + " 个已注册商家（按 merchant_id 升序）";
      Demo.clear(box);
      if (!items.length) {
        box.appendChild(Demo.node("div", "empty", "还没有商家。填上面的表单注册一个，再去智能秤页。"));
        return;
      }
      var columns = [{ label: "商家 id" }, { label: "商家名" }, { label: "摊位" }, { label: "摊位名" },
        { label: "收款码（脱敏）" }, { label: "手机号" }, { label: "注册时间" }, { label: "操作" }];
      box.appendChild(Demo.table(columns, items, function (row) {
        var link = Demo.node("a", null, "去智能秤 →");
        link.href = "/demo/scale/?merchant_id=" + row.merchant_id;
        link.style.color = "#4fd1a5";
        return [row.merchant_id, row.merchant_name, row.stall_no, row.stall_name,
          row.receiver_token_masked,
          /* 列表端点按 §3.33 **不回手机号**（只回脱敏收款码）——如实写出来，而不是留一列空白 */
          row.phone_masked || "（§3.33 不回手机号）",
          row.registered_at, link];
      }));
    }).catch(function (error) {
      Demo.clear(box);
      box.appendChild(Demo.node("div", "err-text", "读取商家列表失败：" + error.message));
      $("listMeta").textContent = "读取失败";
      Demo.log("读取商家列表失败：" + error.message, true);
    });
  }

  function boot() {
    Demo.nav("register");
    $("submitBtn").onclick = submit;
    $("clearBtn").onclick = function () {
      FIELDS.forEach(function (field) { $(field.id).value = ""; });
      showResult("ok", "表单已清空。");
    };
    $("reloadBtn").onclick = loadMerchants;
    loadMerchants();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
