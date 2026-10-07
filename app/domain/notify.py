"""催缴短信（契约 §3.37；`REQ-056` / `AC-046`）。

**本期不接真实短信网关**（`spec.md` §5 边界）：发送动作落 `payment_reminder` 表留痕
（含**脱敏**手机号与应缴金额），页面展示"已发送" —— **不假装真的发出去过**。

**留痕只落 `payment_reminder` 这一处，不写 `audit_log`**：`audit_log` 是**资金链路**只增不改
留痕（宪法 §4），催缴不是资金动作；而且要写它就得扩 `audit_log.ref_table` 白名单去指向
`payment_reminder` 行，而重建 `audit_log` 时**不得放宽任何既有约束**（`data-model.md`
§2.18 / §5.2：`ref_table` 逐字段照抄）。一个事实一个存放处：发了什么就在这张表里。

`interval_days = 2`（每隔一天）：距上次成功发送**不足**该间隔的商家进 `skipped`
（`reason = "interval_not_elapsed"`）；应缴为 0 的商家不发（`reason = "nothing_payable"`）。

`sent_at` **显式**用 `now_iso()` 写入（`REQ-033` 时钟缝）—— 表的 DEFAULT 只是兜底，
注入时钟必须能覆盖它，否则仿真/测试里的"每隔一天"会跟着墙钟走。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .. import clock
from ..db import now_iso
from .catalog import parse_business_date
from .demo_hub import payables_for_day
from .merchant_reg import mask_phone

#: 契约 §3.37：每隔一天
INTERVAL_DAYS = 2
#: 跳过原因（契约 §3.37 明文两个取值）
NOTHING_PAYABLE = "nothing_payable"
INTERVAL_NOT_ELAPSED = "interval_not_elapsed"
#: 渠道（本期只有短信；`payment_reminder.channel` 的 CHECK 也只有这一个取值）
CHANNEL = "sms"


def _days_since(previous: str, day: str) -> int:
    """上次发送到目标营业日相差几天（`sent_at` 取日期部分）。"""
    return (date.fromisoformat(day) - date.fromisoformat(str(previous)[:10])).days


def _phone_masked(conn: sqlite3.Connection, merchant_id: int) -> str:
    row = conn.execute("SELECT phone FROM merchant WHERE id = ?", (merchant_id,)).fetchone()
    return mask_phone(row["phone"]) if row is not None else "****"


def send_sms_reminders(conn: sqlite3.Connection, body) -> dict:
    """契约 §3.37：触发一轮催缴 → `{business_date, interval_days, sent[], skipped[]}`。"""
    body = body if isinstance(body, dict) else {}
    raw = body.get("business_date")
    day = clock.today_iso() if raw is None else parse_business_date(raw)

    sent: list[dict] = []
    skipped: list[dict] = []
    for payable in payables_for_day(conn, day):
        merchant_id = payable["merchant_id"]
        if payable["payable_cents"] <= 0:
            skipped.append({"merchant_id": merchant_id, "reason": NOTHING_PAYABLE})
            continue
        last = conn.execute(
            "SELECT sent_at FROM payment_reminder WHERE merchant_id = ?"
            " ORDER BY sent_at DESC, id DESC LIMIT 1",
            (merchant_id,),
        ).fetchone()
        if last is not None and _days_since(last["sent_at"], day) < INTERVAL_DAYS:
            skipped.append({"merchant_id": merchant_id, "reason": INTERVAL_NOT_ELAPSED})
            continue
        phone_masked = _phone_masked(conn, merchant_id)
        sent_at = now_iso()
        # 留痕只此一处：这张表就是"催缴已发送"的证据（不写 `audit_log`，理由见模块 docstring）
        conn.execute(
            "INSERT INTO payment_reminder"
            " (merchant_id, business_date, payable_cents, phone_masked, channel, sent_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (merchant_id, day, payable["payable_cents"], phone_masked, CHANNEL, sent_at),
        )
        sent.append(
            {
                "merchant_id": merchant_id,
                "merchant_name": payable["merchant_name"],
                "phone_masked": phone_masked,
                "payable_cents": payable["payable_cents"],
                "sent_at": sent_at,
            }
        )
    conn.commit()
    return {
        "business_date": day,
        "interval_days": INTERVAL_DAYS,
        "sent": sent,
        "skipped": skipped,
    }
