"""商品档案与当日价格的新增/修改（契约 §3.38；`REQ-058`、`AC-048`）。

**为什么需要它**：`INSERT INTO product` 原先只出现在种子导入里、`UPDATE product` 只改 `status`
（`REQ-049`）—— 于是"设置商品名称与价格"在服务端没有载体，而 §3.6 计价要 `product_id`、
§3.4 价目表对不存在的 `product_id` 直接报错，秤页主链路卡死。本模块补上这一环。

三条纪律：

1. **`stall_id` 只从会话来**（`REQ-032`）：本模块只接受调用方传入的 `stall_id`，
   请求体里没有、也不接受任何摊位参数；改别的摊位的商品 ⇒ `MT-1004`（先判存在、再判归属）。
2. **商品与当日价一次写成**：只写 `product` 而不写 `price_item`，新商品当天就**不可计价**
   （§3.6 会回 `MT-1006`），演示动线会在下一步断掉 —— 故两件事同事务完成。
3. **幂等 = 同摊位同名复用**（契约 §3.38）：不带 `product_id` 时，同名商品**更新而非新增**；
   带上 `product_id` 时以 `product_id` 为准（此时同名是允许的，契约未禁止重名）。
"""

from __future__ import annotations

import sqlite3

from .. import TradeError, clock
from ..db import now_iso

#: 契约 §3.38 的传输层约束
NAME_MIN_LEN, NAME_MAX_LEN = 1, 50
CATEGORY_CODE_MAX_LEN = 16
ICON_KEY_MAX_LEN = 32
HOTKEY_MIN_LEN, HOTKEY_MAX_LEN = 1, 4
DEFAULT_CATEGORY_CODE = "V-01"
PRODUCT_FIELDS = "id, stall_id, name, category_id, icon_key, hotkey, status"


def _require_text(raw, field: str, min_len: int, max_len: int) -> str:
    if not isinstance(raw, str) or not min_len <= len(raw) <= max_len:
        raise TradeError(
            "MT-1008",
            f"`{field}` 必须是长度 {min_len}~{max_len} 的字符串",
            {"field": field, "min_length": min_len, "max_length": max_len},
        )
    return raw


def _optional_text(raw, field: str, max_len: int, *, min_len: int = 0):
    """可缺省字段：`None` 原样返回（表示"沿用旧值"），否则校验长度。"""
    if raw is None:
        return None
    if not isinstance(raw, str) or not min_len <= len(raw) <= max_len:
        raise TradeError(
            "MT-1008",
            f"`{field}` 必须是长度 {min_len}~{max_len} 的字符串",
            {"field": field, "min_length": min_len, "max_length": max_len},
        )
    return raw


def _category_id(conn: sqlite3.Connection, code) -> int:
    code = DEFAULT_CATEGORY_CODE if code is None else _optional_text(code, "category_code", CATEGORY_CODE_MAX_LEN)
    row = conn.execute("SELECT id FROM category WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise TradeError("MT-1009", f"标准品类不存在：{code}", {"category_code": code})
    return int(row["id"])


def upsert_product(conn: sqlite3.Connection, stall_id: int, body) -> tuple[dict, int]:
    """契约 §3.38：新增（201）或修改（200）本摊位商品，并写当日价目表。

    返回 `(响应体, HTTP 状态)` —— 状态码由"是不是新建"决定，编排层不猜。
    """
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")
    name = _require_text(body.get("name"), "name", NAME_MIN_LEN, NAME_MAX_LEN)
    unit_price_cents = body.get("unit_price_cents")
    if isinstance(unit_price_cents, bool) or not isinstance(unit_price_cents, int) or unit_price_cents < 1:
        raise TradeError(
            "MT-1008", "`unit_price_cents` 必须是 ≥1 的整数", {"field": "unit_price_cents", "min": 1}
        )
    category_id = _category_id(conn, body.get("category_code"))
    icon_key = _optional_text(body.get("icon_key"), "icon_key", ICON_KEY_MAX_LEN)
    hotkey = _optional_text(body.get("hotkey"), "hotkey", HOTKEY_MAX_LEN, min_len=HOTKEY_MIN_LEN)

    product_id = body.get("product_id")
    if product_id is not None and (isinstance(product_id, bool) or not isinstance(product_id, int) or product_id < 1):
        raise TradeError("MT-1008", "`product_id` 必须是正整数", {"field": "product_id"})

    if product_id is None:
        # 幂等：同摊位同名 ⇒ 复用既有行（演示时点两次不会出两个商品）
        existing = conn.execute(
            f"SELECT {PRODUCT_FIELDS} FROM product WHERE stall_id = ? AND name = ? ORDER BY id LIMIT 1",
            (stall_id, name),
        ).fetchone()
        product_id = int(existing["id"]) if existing is not None else None
    else:
        existing = conn.execute(f"SELECT {PRODUCT_FIELDS} FROM product WHERE id = ?", (product_id,)).fetchone()
        if existing is None:
            raise TradeError("MT-1009", f"商品不存在：{product_id}", {"product_id": product_id})
        if existing["stall_id"] != stall_id:
            raise TradeError("MT-1004", "不得修改其他摊位的商品", {"product_id": product_id})

    if product_id is None:
        cursor = conn.execute(
            "INSERT INTO product (stall_id, name, category_id, unit, icon_key, hotkey, status, created_at)"
            " VALUES (?, ?, ?, 'kg', ?, ?, 'active', ?)",
            (stall_id, name, category_id, icon_key, hotkey, now_iso()),
        )
        product_id = int(cursor.lastrowid)
        created = True
    else:
        # 缺省字段**沿用旧值**（契约 §3.38：icon_key / hotkey "缺省沿用旧值"）
        previous = conn.execute(f"SELECT {PRODUCT_FIELDS} FROM product WHERE id = ?", (product_id,)).fetchone()
        conn.execute(
            "UPDATE product SET name = ?, category_id = ?, icon_key = ?, hotkey = ? WHERE id = ?",
            (
                name,
                category_id,
                previous["icon_key"] if icon_key is None else icon_key,
                previous["hotkey"] if hotkey is None else hotkey,
                product_id,
            ),
        )
        created = False

    business_date = clock.today_iso()
    conn.execute(
        """
        INSERT INTO price_item (stall_id, product_id, business_date, unit_price_cents, source)
        VALUES (?, ?, ?, ?, 'manual')
        ON CONFLICT (stall_id, business_date, product_id)
        DO UPDATE SET unit_price_cents = excluded.unit_price_cents,
                      source = 'manual',
                      updated_at = ?
        """,
        (stall_id, product_id, business_date, unit_price_cents, now_iso()),
    )
    conn.commit()

    row = conn.execute(f"SELECT {PRODUCT_FIELDS} FROM product WHERE id = ?", (product_id,)).fetchone()
    return (
        {
            "product_id": int(row["id"]),
            "name": row["name"],
            "icon_key": row["icon_key"],
            "hotkey": row["hotkey"],
            "status": row["status"],
            "business_date": business_date,
            "unit_price_cents": unit_price_cents,
        },
        201 if created else 200,
    )
