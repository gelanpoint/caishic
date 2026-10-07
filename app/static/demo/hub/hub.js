/* 中台页逻辑（`REQ-056` / `REQ-057`）。
 *
 * 真实调用：`GET /api/demo/hub`（事件表 + 应缴表，每 1.5 秒轮询）、
 *          `POST /api/demo/sms-reminders`（催缴短信，每两日一次）、
 *          `GET|PUT /api/admin/commission-rules`（佣金口径：读状态 + 一键配置演示档位）。
 *
 * **零硬编码业务数据**：本文件里没有一条商家、摊位、金额或事件是写死的 ——
 * 全部来自上面的响应。`EVENT_LABELS` 只是**展示用**的中文名（未知类型原样显示，不隐藏）。
 */
(function () {
  "use strict";

  var POLL_MS = 1500;
  var EVENT_LABELS = {
    transaction_confirmed: "确认即支付（计入应缴）",
    transaction_cancelled: "取消即撤销（不计入应缴，留痕）",
    price_change: "改价留痕",
    refund_applied: "退货冲正",
    refund_duplicate_hit: "退货重复命中（幂等）",
    offline_backfilled: "离线补传",
    offline_duplicate_discarded: "补传重复丢弃（幂等）",
    payment_callback_duplicate_hit: "支付回调重复命中（幂等）",
    staging_write_failed: "暂存写入失败（明确报错）",
    offline_threshold_warned: "离线暂存阈值告警",
    commission_rule_changed: "佣金规则变更",
    scale_amount_mismatch: "秤端金额与中台重算不一致"
  };

  var timer = null;
  var polling = true;
  var lastDay = null;
  var ruleReady = false;   /* 服务端是否已有生效口径 —— 由 GET /api/admin/commission-rules 决定，不猜 */

  /* 演示档位费率（**请求参数**，不是展示数字）：2%。
     为什么页面上要能点这一下：`commission_rule` 在种子数据里**故意留空**（`app/seed.py` 明文：
     属运营端配置 `REQ-017`，"这不是遗漏，而是演示动线的一部分"）⇒ 不配一次口径，应缴恒为 0。
     现场把"运营端能现场配置佣金口径"这个**真实能力**点出来，比往种子里塞一条默认口径更诚实，
     也不动 `REQ-025` 的导入清单。费率 2% 是**演示档位、非调研结论**；本方案主张**不向商户收钱**。 */
  var DEMO_RULE_BP = 200;

  function $(id) { return document.getElementById(id); }

  function label(type) {
    return EVENT_LABELS[type] ? EVENT_LABELS[type] + "（" + type + "）" : type;
  }

  function stamp() {
    var now = new Date();
    return String(now.getHours()).padStart(2, "0") + ":" + String(now.getMinutes()).padStart(2, "0") +
      ":" + String(now.getSeconds()).padStart(2, "0");
  }

  /* 轮询每 1.5 秒**重建**表格（`Demo.clear` + 重新 append），会把 `.table-wrap` 的滚动位置打回顶部。
     应缴表有 11 行（10 个种子商家 + 刚注册的演示商家），而演示商户那一行恰在折叠线以下 ——
     不保住滚动位置，就等于"必须在这 1.5 秒内看完"，**实际看不到**（`2026-10-08` 真机截图复核发现）。
     故重建前后保存 / 恢复 `scrollTop`：数据照旧实时刷新，只是不再把人的阅读位置一起清掉。 */
  function saveScroll(box) {
    var tops = Array.prototype.map.call(box.querySelectorAll(".table-wrap"),
      function (wrap) { return wrap.scrollTop; });
    return function restore() {
      Array.prototype.forEach.call(box.querySelectorAll(".table-wrap"), function (wrap, index) {
        if (index < tops.length) { wrap.scrollTop = tops[index]; }
      });
    };
  }

  function renderEvents(events) {
    var box = $("eventTable");
    var restoreScroll = saveScroll(box);
    Demo.clear(box);
    if (!events.length) {
      box.appendChild(Demo.node("div", "empty",
        "本营业日还没有事件。去智能秤页走一笔（确认 → 模拟扫码），这一行会实时冒出来。"));
    } else {
      var columns = [{ label: "时间" }, { label: "事件类型" }, { label: "摊位" }, { label: "商家" },
        { label: "交易号" }, { label: "金额", num: true }];
      box.appendChild(Demo.table(columns, events.slice().reverse(), function (row) {
        return [row.occurred_at, label(row.event_type), row.stall_no, row.merchant_name || "—",
          row.transaction_no || "—", row.amount_cents === null || row.amount_cents === undefined ?
            "—" : "¥ " + Demo.yuan(row.amount_cents)];
      }));
    }
    $("eventMeta").textContent = "共 " + events.length + " 条事件（服务端按 occurred_at 升序返回，本页倒序显示，最新在上）" +
      "　｜　操作人/来源：" + (events.length ? events[events.length - 1].actor || "—" : "—");
    restoreScroll();
  }

  function renderPayables(payables) {
    var box = $("payableTable");
    var restoreScroll = saveScroll(box);
    Demo.clear(box);
    if (!payables.length) {
      box.appendChild(Demo.node("div", "empty", "本营业日还没有已确认交易，因此没有应缴。"));
    } else {
      var columns = [{ label: "商家" }, { label: "摊位" }, { label: "已确认笔数", num: true },
        { label: "实收金额", num: true }, { label: "佣金", num: true }, { label: "应缴金额", num: true }];
      box.appendChild(Demo.table(columns, payables, function (row) {
        return [row.merchant_name, row.stall_no, row.paid_txn_count,
          "¥ " + Demo.yuan(row.received_amount_cents), "¥ " + Demo.yuan(row.commission_cents),
          "¥ " + Demo.yuan(row.payable_cents)];
      }));
    }
    var total = payables.reduce(function (sum, row) { return sum + (row.payable_cents || 0); }, 0);
    var received = payables.reduce(function (sum, row) { return sum + (row.received_amount_cents || 0); }, 0);
    $("payableMeta").textContent = "共 " + payables.length + " 个商家有账" +
      "　｜　应缴合计 ¥ " + Demo.yuan(total) + "（前端只做求和展示，逐行数字均来自服务端）";
    /* 有实收、却一分佣金都没有 ⇒ 只可能是"佣金口径还没配"。如实说明，不让看表的人以为系统坏了。 */
    var hint = $("payableHint");
    if (hint) {
      if (received > 0 && total === 0) {
        hint.style.display = "";
        hint.textContent = "提示：本营业日有实收 ¥ " + Demo.yuan(received) +
          "，但佣金与应缴都是 0 —— 服务端 `commission_rule` 还没有生效口径（运营端配置项，REQ-017）。" +
          "点上面的「配置演示佣金口径（2% · 演示档位）」即可现场配置，本页的「佣金 / 应缴金额」会立刻出现非零值。" +
          "注意：2% 是演示档位、不是调研结论；本方案主张不向商户收钱，这里演示的只是「可配置性」。";
      } else {
        hint.style.display = "none";
      }
    }
    restoreScroll();
  }

  /* ---- 佣金口径：读状态 + 一键配置（都是真实端点，不伪造费率） ---- */
  function renderRuleState(rules) {
    var day = lastDay || Demo.todayLocal();
    var effective = (rules || []).filter(function (row) {
      return row.effective_from <= day && (!row.effective_to || row.effective_to >= day);
    });
    ruleReady = effective.length > 0;
    var state = $("ruleState");
    var button = $("btnSeedRule");
    if (!state || !button) { return; }
    if (ruleReady) {
      var rule = effective[effective.length - 1];
      state.className = "badge on";
      state.textContent = "佣金口径已生效：rate_bp=" + rule.rate_bp + "（" + (rule.rate_bp / 100) +
        "%）· pay_object=" + rule.pay_object + " · 生效自 " + rule.effective_from;
      button.disabled = true;
      button.textContent = "佣金口径已配置";
    } else {
      state.className = "badge warn";
      state.textContent = "佣金口径：服务端 commission_rule 无生效口径 ⇒ 应缴恒为 0（可一键配置演示档位）";
      button.disabled = false;
      button.textContent = "配置演示佣金口径（2% · 演示档位）";
    }
  }

  function loadRules() {
    return Demo.get("/api/admin/commission-rules").then(function (rules) {
      renderRuleState(rules);
    }).catch(function (error) {
      var state = $("ruleState");
      if (state) {
        state.className = "badge err";
        state.textContent = "佣金口径状态读取失败：" + error.message;
      }
    });
  }

  function seedRule() {
    var button = $("btnSeedRule");
    var box = $("ruleResult");
    /* 契约 §3.24：`effective_from`（`YYYY-MM-DD`）是**必填** —— 缺了服务端回 `MT-1008`
       （我第一版漏了它，实测报错）。取**服务端下发的营业日**，不自己编日期。 */
    var effectiveFrom = lastDay || Demo.todayLocal();
    button.disabled = true;
    box.className = "muted";
    box.textContent = "提交中…（PUT /api/admin/commission-rules，rate_bp=" + DEMO_RULE_BP +
      "，effective_from=" + effectiveFrom + "）";
    Demo.api("PUT", "/api/admin/commission-rules",
      { body: { pay_object: "merchant", rate_bp: DEMO_RULE_BP, effective_from: effectiveFrom } })
      .then(function (rule) {
      box.className = "ok-text";
      box.textContent = "已配置演示佣金口径：rate_bp=" + rule.rate_bp + "（" + (rule.rate_bp / 100) +
        "%）· pay_object=" + rule.pay_object + " · 生效自 " + rule.effective_from +
        "。这是演示档位、非调研结论；本方案主张不向商户收钱，这里演示的是「运营端能现场配置」这个能力。";
      Demo.toast("已配置演示佣金口径 " + (rule.rate_bp / 100) + "%（服务端已写口径并留痕）");
      Demo.log("佣金口径已配置：rate_bp=" + rule.rate_bp + "（PUT /api/admin/commission-rules）");
      return refresh();            /* 同一页立刻重取中台：应缴当场变非零 */
    }).then(loadRules).catch(function (error) {
      box.className = "err-text";
      box.textContent = "配置失败：" + error.message + "（服务端未新增口径，本页不伪造费率）" +
        (error.code === "MT-1012" ? "。提示：已存在同档位且生效期重叠的口径 —— 这是服务端的重叠保护。" : "");
      Demo.toast("配置佣金口径失败：" + error.message, true);
      return loadRules();
    });
  }

  function refresh() {
    return Demo.get("/api/demo/hub").then(function (data) {
      lastDay = data.business_date;
      Demo.setBadge("dayBadge", "营业日：" + data.business_date, "on");
      renderEvents(data.events || []);
      renderPayables(data.payables || []);
      $("lastUpdated").textContent = "最近取数 " + stamp() +
        "（事件 " + (data.events || []).length + " 条 / 应缴 " + (data.payables || []).length + " 行）";
    }).catch(function (error) {
      Demo.setBadge("dayBadge", "中台取数失败", "err");
      $("lastUpdated").textContent = "取数失败：" + error.message;
      var box = $("eventTable");
      Demo.clear(box);
      box.appendChild(Demo.node("div", "err-text", "读取 GET /api/demo/hub 失败：" + error.message));
      Demo.log("中台取数失败：" + error.message, true);
    });
  }

  function sendReminders() {
    $("smsBtn").disabled = true;
    $("smsResult").className = "muted";
    $("smsResult").textContent = "发送中…（POST /api/demo/sms-reminders）";
    Demo.post("/api/demo/sms-reminders", { body: { business_date: lastDay || undefined } })
      .then(function (data) {
        var sent = data.sent || [];
        var skipped = data.skipped || [];
        $("smsResult").className = "";
        $("smsResult").textContent = "";
        $("smsResult").appendChild(Demo.node("div", "ok-text",
          "营业日 " + data.business_date + "：已发送 " + sent.length + " 条，跳过 " + skipped.length +
          " 条（服务端口径：每隔 " + data.interval_days + " 天一次）"));
        sent.forEach(function (row) {
          $("smsResult").appendChild(Demo.node("div", "muted",
            "已发送 → " + row.merchant_name + "（" + (row.phone_masked || "—") + "）应缴 ¥ " +
            Demo.yuan(row.payable_cents) + "　@ " + row.sent_at));
        });
        skipped.forEach(function (row) {
          $("smsResult").appendChild(Demo.node("div", "muted",
            "跳过 → 商家 #" + row.merchant_id + "：" +
            (row.reason === "interval_not_elapsed" ? "距上次发送不足间隔" :
              row.reason === "nothing_payable" ? "应缴为 0" : row.reason)));
        });
        Demo.toast("催缴短信：已发送 " + sent.length + " 条，跳过 " + skipped.length + " 条");
        return refresh();
      }).catch(function (error) {
        $("smsResult").className = "err-text";
        $("smsResult").textContent = "催缴发送失败：" + error.message + "（服务端未发送任何短信）";
        Demo.toast("催缴失败：" + error.message, true);
      }).then(function () { $("smsBtn").disabled = false; });
  }

  function setPolling(on) {
    polling = on;
    if (timer) { clearInterval(timer); timer = null; }
    if (on) { timer = setInterval(function () { if (!document.hidden) { refresh(); } }, POLL_MS); }
    $("pollToggle").textContent = on ? "暂停轮询" : "恢复轮询";
    Demo.setBadge("pollBadge", on ? "轮询中（1.5s）" : "已暂停", on ? "on" : "warn");
  }

  function boot() {
    Demo.nav("hub");
    $("refreshBtn").onclick = refresh;
    $("pollToggle").onclick = function () { setPolling(!polling); };
    $("smsBtn").onclick = sendReminders;
    $("btnSeedRule").onclick = seedRule;
    document.addEventListener("visibilitychange", function () {
      Demo.setBadge("pollBadge", document.hidden ? "页面在后台，轮询暂停" : "轮询中（1.5s）",
        document.hidden ? "warn" : "on");
      if (!document.hidden) { refresh(); }
    });
    refresh().then(function () { setPolling(true); return loadRules(); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
