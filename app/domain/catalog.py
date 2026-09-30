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


# ---------------------------------------------------------------------------
# §3.20 / §3.21 标准品类字典、§3.22 摊位别名映射（`REQ-002`、`AC-014`）
# ---------------------------------------------------------------------------
#
# 为什么字典维护**落在本模块**：`plan.md` §4 写明「别名映射由 `app/domain/catalog.py` 维护」，
# 且 §2.5 的别名正是"把摊位叫法归一到标准品类"的落点 —— 与秤端选品/记账口径同源，
# 分到别处就会变成"字典在 A 处维护、记账在 B 处解释"的两条口径。

CATEGORY_FIELDS = "id, code, name, status"
CATEGORY_STATUSES = ("active", "inactive")
CATEGORY_CODE_MAX_LEN = 16
CATEGORY_NAME_MAX_LEN = 32
ALIAS_NAME_MAX_LEN = 32
#: `stall_category_alias` 的对外元素（`data-model.md` §2.5；用 `stall_no` 而非内部 `stall_id`）
ALIAS_FIELDS = "a.id AS id, s.stall_no AS stall_no, a.alias_name AS alias_name, a.category_id AS category_id"


def list_categories(conn: sqlite3.Connection) -> list[dict]:
    """契约 §3.20：全部标准品类（`data-model.md` §2.4）。**不隐藏停用项** ——
    停用是"不能再选"，不是"查不到"：历史交易仍指向它，查不到反而无法核对。"""
    rows = conn.execute(f"SELECT {CATEGORY_FIELDS} FROM category ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def list_aliases(conn: sqlite3.Connection) -> list[dict]:
    """契约 §3.20：全部「摊位别名 → 标准品类」映射（`data-model.md` §2.5）。"""
    rows = conn.execute(
        f"""
        SELECT {ALIAS_FIELDS}
        FROM stall_category_alias a
        JOIN stall s ON s.id = a.stall_id
        ORDER BY a.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def create_category(conn: sqlite3.Connection, body) -> dict:
    """契约 §3.21：新增标准品类 → 201；`code` 全表唯一，重复 → `MT-1012`（409）。"""
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")

    code = body.get("code")
    if not isinstance(code, str) or not 1 <= len(code) <= CATEGORY_CODE_MAX_LEN:
        raise TradeError(
            "MT-1008",
            f"`code` 必须是长度 1~{CATEGORY_CODE_MAX_LEN} 的字符串",
            {"field": "code", "max_length": CATEGORY_CODE_MAX_LEN},
        )
    name = body.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= CATEGORY_NAME_MAX_LEN:
        raise TradeError(
            "MT-1008",
            f"`name` 必须是长度 1~{CATEGORY_NAME_MAX_LEN} 的字符串",
            {"field": "name", "max_length": CATEGORY_NAME_MAX_LEN},
        )
    status = body.get("status", "active")
    if status not in CATEGORY_STATUSES:
        raise TradeError(
            "MT-1008",
            f"`status` 必须是 {'/'.join(CATEGORY_STATUSES)} 之一",
            {"field": "status", "allowed": list(CATEGORY_STATUSES)},
        )

    # 唯一性**先查再插**：直接靠 `ux_category_code` 撞 `IntegrityError` 得到的是异常而非契约错误码，
    # 且无法区分"编码重复"与其它约束。唯一索引仍是最后一道闸门（并发下由它兜底）。
    duplicate = conn.execute("SELECT id FROM category WHERE code = ?", (code,)).fetchone()
    if duplicate is not None:
        raise TradeError("MT-1012", f"品类编码已存在：{code}", {"code": code, "existing_id": int(duplicate["id"])})

    cursor = conn.execute(
        "INSERT INTO category (code, name, status, created_at) VALUES (?, ?, ?, ?)",
        (code, name, status, now_iso()),
    )
    conn.commit()
    row = conn.execute(f"SELECT {CATEGORY_FIELDS} FROM category WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
    return dict(row)


def create_alias(conn: sqlite3.Connection, body) -> dict:
    """契约 §3.22：维护「摊位别名 → 标准品类」→ 201。

    - 摊位不存在 / 品类不存在或非 `active` → `MT-1009`（契约 §3.22 明示"品类或摊位不存在"）；
    - 同摊位同别名重复 → `MT-1012`（§2.5 的 `ux_alias_stall_name`）。
    """
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")

    stall_no = body.get("stall_no")
    if not isinstance(stall_no, str) or not 1 <= len(stall_no) <= STALL_NO_MAX_LEN:
        raise TradeError(
            "MT-1008",
            f"`stall_no` 必须是长度 1~{STALL_NO_MAX_LEN} 的字符串",
            {"field": "stall_no", "max_length": STALL_NO_MAX_LEN},
        )
    alias_name = body.get("alias_name")
    if not isinstance(alias_name, str) or not 1 <= len(alias_name) <= ALIAS_NAME_MAX_LEN:
        raise TradeError(
            "MT-1008",
            f"`alias_name` 必须是长度 1~{ALIAS_NAME_MAX_LEN} 的字符串",
            {"field": "alias_name", "max_length": ALIAS_NAME_MAX_LEN},
        )
    category_id = body.get("category_id")
    if isinstance(category_id, bool) or not isinstance(category_id, int):
        raise TradeError("MT-1008", "`category_id` 必须是整数", {"field": "category_id"})

    stall = conn.execute("SELECT id FROM stall WHERE stall_no = ?", (stall_no,)).fetchone()
    if stall is None:
        raise TradeError("MT-1009", f"摊位不存在：{stall_no}", {"stall_no": stall_no})
    category = conn.execute("SELECT id, status FROM category WHERE id = ?", (category_id,)).fetchone()
    if category is None:
        raise TradeError("MT-1009", f"标准品类不存在：{category_id}", {"category_id": category_id})
    if category["status"] != "active":
        # 停用品类不能再被映射：否则新别名会指向一个"已停用"的记账口径
        raise TradeError(
            "MT-1009",
            f"标准品类已停用，不能再建立别名映射：{category_id}",
            {"category_id": category_id, "status": category["status"]},
        )

    stall_id = int(stall["id"])
    duplicate = conn.execute(
        "SELECT id FROM stall_category_alias WHERE stall_id = ? AND alias_name = ?",
        (stall_id, alias_name),
    ).fetchone()
    if duplicate is not None:
        raise TradeError(
            "MT-1012",
            f"该摊位已存在同别名映射：{alias_name}",
            {"stall_no": stall_no, "alias_name": alias_name, "existing_id": int(duplicate["id"])},
        )

    cursor = conn.execute(
        "INSERT INTO stall_category_alias (stall_id, alias_name, category_id, created_at) VALUES (?, ?, ?, ?)",
        (stall_id, alias_name, category_id, now_iso()),
    )
    conn.commit()
    row = conn.execute(
        f"""
        SELECT {ALIAS_FIELDS}
        FROM stall_category_alias a JOIN stall s ON s.id = a.stall_id
        WHERE a.id = ?
        """,
        (int(cursor.lastrowid),),
    ).fetchone()
    return dict(row)
