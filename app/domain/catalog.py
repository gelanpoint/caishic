"""摊位会话、秤端商品与价目表（`REQ-002`、`REQ-003`、`REQ-004`、`REQ-032`）。

契约 §3.2 会话、§3.3/§3.4 价目表、§3.5 商品。本模块**不依赖 HTTP**（无 Flask import），
错误一律抛 `TradeError(契约错误码, ...)`，由 `app/__init__.py` 统一转成响应。

两条容易出错的约定，先写在前面：

1. **秤端数据边界**（`REQ-032` / `AC-021`）：会话只认 `stall_session` 里那一行绑定的摊位，
   任何"取本摊位数据"的查询都**必须带 `stall_id = 会话绑定摊位`**，不能从请求里再接受一个摊位参数。”
2. **「上一营业日」= 目标日期的前一个自然日**（不是"最近一个有数据的日子"）：
   契约 §3.4 要求"上一营业日无可复制数据 → `MT-1009`"，若取"最近一天有数据的日子"，
   那么隔 60 天也能往前捞到旧价、`MT-1009` 永远触发不了（本行为由 `T-008` 契约用例锁定）。
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import date, timedelta

from .. import TradeError
from ..db import now_iso

#: `stall.stall_no` 传输层约束（契约 §3.2：长度 1~16）
STALL_NO_MAX_LEN = 16
#: 会话 Token 长度（契约 §3.2：16~64；`data-model.md` §2.3 同）
SESSION_TOKEN_BYTES = 24
_BUSINESS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PRODUCT_FIELDS = "id, stall_id, name, category_id, icon_key, hotkey, status"


def parse_business_date(raw, *, field: str = "business_date") -> str:
    """校验营业日文本（契约 §1.4 的 `YYYY-MM-DD`）；不合法抛 `MT-1008`。"""
    if not isinstance(raw, str) or not _BUSINESS_DATE_RE.match(raw):
        raise TradeError("MT-1008", f"`{field}` 必须是 YYYY-MM-DD 格式的字符串", {"field": field})
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise TradeError("MT-1008", f"`{field}` 不是合法日期：{raw}", {"field": field}) from exc
    return raw


# ---------------------------------------------------------------------------
# §3.2 会话
# ---------------------------------------------------------------------------


def bind_session(conn: sqlite3.Connection, body) -> dict:
    """绑定摊位并下发会话 Token（契约 §3.2）；未知/停用摊位 → `MT-1009`。"""
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")

    stall_no = body.get("stall_no")
    if not isinstance(stall_no, str) or not 1 <= len(stall_no) <= STALL_NO_MAX_LEN:
        raise TradeError(
            "MT-1008",
            f"`stall_no` 必须是长度 1~{STALL_NO_MAX_LEN} 的字符串",
            {"field": "stall_no", "max_length": STALL_NO_MAX_LEN},
        )

    stall = conn.execute(
        "SELECT id, stall_no, name FROM stall WHERE stall_no = ? AND status = 'active'", (stall_no,)
    ).fetchone()
    if stall is None:
        # 不存在**或已停用**都按"资源不存在"处理：不向调用方区分二者（避免泄露停用摊位清单）
        raise TradeError("MT-1009", f"摊位不存在或已停用：{stall_no}", {"stall_no": stall_no})

    session_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    conn.execute(
        "INSERT INTO stall_session (stall_id, session_token) VALUES (?, ?)",
        (stall["id"], session_token),
    )
    conn.commit()
    return {
        "session_token": session_token,
        "stall_no": stall["stall_no"],
        "stall_name": stall["name"] or stall["stall_no"],
    }


def require_session(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row:
    """校验 `X-Stall-Session` 并返回会话绑定的摊位行（契约 §1.6 / §4 `MT-1005`）。

    返回的列是**一个摊位行的可用子集**，含 `payment_receiver_token`（脱敏值）——
    它是契约 §3.10 收款码响应 `receiver_token_masked` 的来源（`REQ-024` / `NFR-012`）。
    放在这里而不是让收款端点自己再查一次：同一个摊位行只准有一条查询路径，
    否则"会话绑定的是哪个摊位"就有两处各自解释的机会（本函数是 `REQ-032` 边界的唯一入口）。
    """
    if not isinstance(token, str) or not token:
        raise TradeError("MT-1005", "缺少会话 Token（请求头 X-Stall-Session）", {"header": "X-Stall-Session"})

    row = conn.execute(
        """
        SELECT s.id AS stall_id, s.stall_no, s.name AS stall_name, s.status AS stall_status,
               s.payment_receiver_token, ss.id AS session_id
        FROM stall_session ss
        JOIN stall s ON s.id = ss.stall_id
        WHERE ss.session_token = ? AND ss.is_active = 1
        """,
        (token,),
    ).fetchone()
    if row is None or row["stall_status"] != "active":
        raise TradeError("MT-1005", "会话无效或已失效，请重新绑定摊位", {"header": "X-Stall-Session"})

    conn.execute("UPDATE stall_session SET last_seen_at = ? WHERE id = ?", (now_iso(), row["session_id"]))
    conn.commit()
    return row


# ---------------------------------------------------------------------------
# §3.5 商品
# ---------------------------------------------------------------------------


def list_products(conn: sqlite3.Connection, stall_id: int) -> list[dict]:
    """本摊位**在营**商品（契约 §3.5；`AC-006` 要求含 `icon_key` / `hotkey`，供无文本输入选品）。"""
    rows = conn.execute(
        f"SELECT {PRODUCT_FIELDS} FROM product WHERE stall_id = ? AND status = 'active' ORDER BY id",
        (stall_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# §3.3 / §3.4 价目表
# ---------------------------------------------------------------------------


def _price_rows(conn: sqlite3.Connection, stall_id: int, business_date: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, stall_id, product_id, business_date, unit_price_cents, source
        FROM price_item WHERE stall_id = ? AND business_date = ? ORDER BY product_id
        """,
        (stall_id, business_date),
    ).fetchall()
    return [dict(row) for row in rows]


def get_price_list(conn: sqlite3.Connection, stall_id: int, business_date) -> dict:
    """查询某营业日价目表（契约 §3.3）。

    响应取**对象形态** `{"business_date", "items", "price_list_missing"}`：契约 §3.3 同时要求
    "数组"与"空数组 + `price_list_missing: true`"，而数组无法携带同级标记；对象形态同时满足两句话的
    意图（`items` 就是那个数组，标记在旁），`tasks.md` `T-008` 已就此时标注过该契约歧义。
    """
    day = parse_business_date(business_date)
    items = _price_rows(conn, stall_id, day)
    return {"business_date": day, "items": items, "price_list_missing": not items}


def set_price_list(conn: sqlite3.Connection, stall_id: int, body) -> dict:
    """设置/逐条调整价目表，或复制上一营业日价格（契约 §3.4）。"""
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")
    day = parse_business_date(body.get("business_date"))
    copy_previous = bool(body.get("copy_from_previous_day"))

    if copy_previous:
        return _copy_from_previous_day(conn, stall_id, day)

    items = body.get("items")
    if not isinstance(items, list):
        raise TradeError("MT-1008", "`items` 必须是数组（或用 copy_from_previous_day 复制）", {"field": "items"})
    if len(items) > 200:
        raise TradeError("MT-1008", "`items` 单次最多 200 条", {"field": "items", "max_items": 200})

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise TradeError("MT-1008", f"`items[{index}]` 必须是对象", {"index": index})
        product_id = item.get("product_id")
        unit_price_cents = item.get("unit_price_cents")
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise TradeError("MT-1008", f"`items[{index}].product_id` 必须是整数", {"index": index})
        if not isinstance(unit_price_cents, int) or isinstance(unit_price_cents, bool) or unit_price_cents < 1:
            # data-model §2.7：unit_price_cents ≥ 1 —— 不得以 0 元设价（与 MT-1006「不得 0 元成交」同源）
            raise TradeError(
                "MT-1008",
                f"`items[{index}].unit_price_cents` 必须是 ≥1 的整数",
                {"index": index, "min": 1},
            )
        owner = conn.execute("SELECT stall_id FROM product WHERE id = ?", (product_id,)).fetchone()
        if owner is None:
            raise TradeError("MT-1009", f"商品不存在：{product_id}", {"product_id": product_id})
        if owner["stall_id"] != stall_id:
            raise TradeError("MT-1004", "不得为其他摊位的商品设价", {"product_id": product_id})

        conn.execute(
            """
            INSERT INTO price_item (stall_id, product_id, business_date, unit_price_cents, source)
            VALUES (?, ?, ?, ?, 'manual')
            ON CONFLICT (stall_id, business_date, product_id)
            DO UPDATE SET unit_price_cents = excluded.unit_price_cents,
                          source = 'manual',
                          updated_at = ?
            """,
            (stall_id, product_id, day, unit_price_cents, now_iso()),
        )
    conn.commit()
    return {"business_date": day, "source": "manual", "items": _price_rows(conn, stall_id, day)}


def _copy_from_previous_day(conn: sqlite3.Connection, stall_id: int, day: str) -> dict:
    """复制**前一个自然日**的价格；那一天没有任何可复制数据 → `MT-1009`（契约 §3.4）。"""
    previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    source_rows = _price_rows(conn, stall_id, previous)
    if not source_rows:
        raise TradeError(
            "MT-1009",
            f"上一营业日（{previous}）没有可复制的价目表",
            {"business_date": day, "previous_business_date": previous},
        )

    for row in source_rows:
        conn.execute(
            """
            INSERT INTO price_item (stall_id, product_id, business_date, unit_price_cents, source)
            VALUES (?, ?, ?, ?, 'copied_previous_day')
            ON CONFLICT (stall_id, business_date, product_id)
            DO UPDATE SET unit_price_cents = excluded.unit_price_cents,
                          source = 'copied_previous_day',
                          updated_at = ?
            """,
            (stall_id, row["product_id"], day, row["unit_price_cents"], now_iso()),
        )
    conn.commit()
    return {"business_date": day, "source": "copied_previous_day", "items": _price_rows(conn, stall_id, day)}
