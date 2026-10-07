"""演示控制台：商家注册与列表（契约 §3.33 / §3.34；`REQ-053`、`AC-043`）。

三条纪律：

1. **收款码明文只活在本次请求的内存里**：`mask_receiver_code()` 当场把它变成掩码形态，
   此后**只写掩码**（`stall.payment_receiver_token` 的 CHECK 还强制"必须含 `****`"）。
   明文既不落库、也不进日志、也不回显（`REQ-024` / `NFR-012`）。
2. **摊位号由服务端分配**：`D-` + 两位序号顺序递增，**不接受请求指定** ——
   让调用方指定摊位号，等于把"这个商家绑哪个摊位"的授权交给请求体（同 `REQ-032` 的口径）。
3. **响应只回脱敏值**：手机号回 `phone_masked`（前 3 + `****` + 后 4），收款标识回掩码。
   手机号**原文**按契约 §3.34 的口径落库（`merchant.phone` 本就是非空列，供 `REQ-056` 催缴用），
   但**不出现在任何响应里**。
"""

from __future__ import annotations

import re
import sqlite3

from .. import TradeError
from ..db import now_iso

#: 契约 §3.34 的传输层约束
NAME_MIN_LEN, NAME_MAX_LEN = 1, 32
#: 与 `merchant.phone` 的 CHECK 一致（`0001_init.sql`：长度 1~20 且仅数字与 `-`）
PHONE_MIN_LEN, PHONE_MAX_LEN = 1, 20
RECEIVER_CODE_MIN_LEN, RECEIVER_CODE_MAX_LEN = 4, 64
#: 服务端分配的演示摊位号前缀（`D-01`、`D-02`…）
STALL_NO_PREFIX = "D-"
_STALL_NO_RE = re.compile(r"^D-(\d+)$")
_PHONE_RE = re.compile(r"^[0-9-]+$")


def mask_receiver_code(code: str) -> str:
    """收款码脱敏（契约 §3.34 的两档口径）：**≥7 字符**保留前 2 与后 4；**4~6 字符**保留前 1 与后 1。

    为什么短码要单独一档：按"前 2 + 后 4"处理 4 字符码会得到 `12****1234` —— 明文正是它的后四位，
    整串也能拼回来，等于**没脱敏**（`NFR-012`）；而收款码的下限就是 4 字符（§3.34，注册页也这么写），
    故这是可达输入。两档都保证首尾保留段**不覆盖整个原串**，结果含 `****` 且长度 ≤32
    （满足 `stall.payment_receiver_token` 的 CHECK）；形状与 `mask_phone` 的短号分支一致。
    """
    if len(code) >= 7:
        return f"{code[:2]}****{code[-4:]}"
    return f"{code[:1]}****{code[-1:]}"


def mask_phone(phone: str) -> str:
    """手机号脱敏：保留前 3 与后 4，中间 `****`（`REQ-024` 口径；过短时只留首末各一位）。"""
    if len(phone) < 7:
        return f"{phone[:1]}****{phone[-1:]}" if phone else "****"
    return f"{phone[:3]}****{phone[-4:]}"


def _require_text(body: dict, field: str, min_len: int, max_len: int) -> str:
    raw = body.get(field)
    if not isinstance(raw, str) or not min_len <= len(raw) <= max_len:
        raise TradeError(
            "MT-1008",
            f"`{field}` 必须是长度 {min_len}~{max_len} 的字符串",
            {"field": field, "min_length": min_len, "max_length": max_len},
        )
    return raw


def _next_stall_no(conn: sqlite3.Connection) -> str:
    """按 `D-NN` 顺序分配下一个摊位号（同市场唯一由 `ux_stall_no` 兜底）。"""
    used = [
        int(match.group(1))
        for row in conn.execute("SELECT stall_no FROM stall WHERE stall_no LIKE 'D-%'")
        if (match := _STALL_NO_RE.match(row["stall_no"]))
    ]
    return f"{STALL_NO_PREFIX}{max(used, default=0) + 1:02d}"


def register_merchant(conn: sqlite3.Connection, body) -> dict:
    """契约 §3.34：注册商家（建 `merchant` + 它的 `stall`）→ 响应恰好契约列出的 5 个字段。"""
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")
    name = _require_text(body, "merchant_name", NAME_MIN_LEN, NAME_MAX_LEN)
    phone = _require_text(body, "phone", PHONE_MIN_LEN, PHONE_MAX_LEN)
    if not _PHONE_RE.match(phone):
        raise TradeError("MT-1008", "`phone` 只能含数字与 `-`", {"field": "phone"})
    receiver_code = _require_text(
        body, "receiver_code", RECEIVER_CODE_MIN_LEN, RECEIVER_CODE_MAX_LEN
    )

    # 明文 `receiver_code` 到此为止：下面只写 `masked`（不落库、不进日志、不回显）
    masked = mask_receiver_code(receiver_code)
    stamp = now_iso()
    merchant_id = int(
        conn.execute(
            "INSERT INTO merchant (name, phone, status, created_at, updated_at)"
            " VALUES (?, ?, 'active', ?, ?)",
            (name, phone, stamp, stamp),
        ).lastrowid
    )
    stall_no = _next_stall_no(conn)
    conn.execute(
        "INSERT INTO stall (stall_no, merchant_id, name, payment_receiver_token, status, created_at)"
        " VALUES (?, ?, ?, ?, 'active', ?)",
        (stall_no, merchant_id, name, masked, stamp),
    )
    conn.commit()
    return {
        "merchant_id": merchant_id,
        "stall_no": stall_no,
        "merchant_name": name,
        "phone_masked": mask_phone(phone),
        "receiver_token_masked": masked,
    }


def list_merchants(conn: sqlite3.Connection) -> dict:
    """契约 §3.33：已注册商家列表（按 `merchant_id` 升序，保证演示可复现）。

    **不筛 `D-` 前缀**：种子导入的摊位（`A-01`…）同样是"已注册商家"，演示页要能选它们；
    响应里带 `stall_no`，页面自己就能区分。收款标识**只回库里存的掩码值**（`REQ-024`）。
    """
    rows = conn.execute(
        """
        SELECT m.id AS merchant_id, m.name AS merchant_name, m.created_at AS registered_at,
               s.stall_no, s.name AS stall_name, s.payment_receiver_token AS receiver_token_masked
        FROM merchant m
        JOIN stall s ON s.merchant_id = m.id
        ORDER BY m.id, s.id
        """
    ).fetchall()
    return {"items": [dict(row) for row in rows]}
