"""秤端接入 · §3.3 商品字典与品类 / §3.4 价目表（`T-SCALE-05`；`REQ-037`；`AC-028`）。

唯一权威：`specs/market-trade-flow/contracts/scale-midplatform.md` §1.1（窄响应体）、§3.3、§3.4。

三条硬口径：

1. **窄响应体**：只下发秤端**选品与计价所需**的字段 —— 不含成本、不含任何佣金相关字段
   （`REQ-036`、`NFR-015`；契约 §3.3 明文）。本文件里的投影函数就是"窄"的唯一落点，
   不把主契约的宽结构（`app/domain/catalog.py` 的商品/价目表端点）照搬过来。
2. **授权只来自设备令牌**：摊位范围取自 `device` 行（`device.stall_id`），**不接受请求参数**。
3. **价目表为空不得报错**（契约 §3.4）：返回空 `items` 数组 —— 让秤端有"拒绝计价并提示先设价"
   的余地，而不是在缺价时静默按 0 元成交（对应主契约 `MT-1006` 的语义）。

`catalog_version` / `price_list_version` 的语义（数据模型没有版本列，此处必须把口径说清楚）：
取**本次下发内容的 sha256 前 4 字节**作为**内容版本号**（同一内容恒得同一版本，内容一变版本就变）。
`since_version` **等于**当前版本 ⇒ 客户端已是最新，返回空数组；**不等** ⇒ 返回全量。
为什么不按"新增行号"做增量：没有逐行版本列，按行号做增量会**漏发**改名/停用这类"没有新行"的变化；
宁可多发（全量），不可漏发 —— 漏发就是秤端拿着过期价目表计价（`AC-028` 要防的正是这个）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from flask import Blueprint, jsonify, request

from .. import TradeError, clock, current_db
from ..domain import device as device_domain
from ..domain.catalog import get_price_list, list_products, parse_business_date

bp = Blueprint("scale_catalog", __name__)

#: 设备令牌请求头（契约 §1 认证方式）；字面量只在此处出现一次
TOKEN_HEADER = "X-Device-Token"
#: 协议版本请求头（契约 §1 第 9 条 / §7：不认识的版本必须显式拒绝）
PROTO_HEADER = "X-Scale-Proto"


def require_device(conn: sqlite3.Connection) -> dict:
    """校验 `X-Device-Token` 并返回设备（授权范围只来自这一行）。"""
    return device_domain.authenticate_device(conn, request.headers.get(TOKEN_HEADER))


def require_proto() -> None:
    """协议版本闸门（契约 §1 第 9 条 / §7）。"""
    device_domain.require_supported_proto(request.headers.get(PROTO_HEADER))


def _content_version(payload) -> int:
    """内容版本号（sha256 前 4 字节 → 非负整数）：同一内容恒得同一版本。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:4], "big")


def _narrow_products(conn: sqlite3.Connection, stall_id: int) -> list[dict]:
    """本摊位在营商品 → 契约 §3.3 的四个字段（`list_products` 已按 `status='active'` 过滤）。"""
    return [
        {
            "product_id": int(row["id"]),
            "name": row["name"],
            "category_id": int(row["category_id"]),
            "status": row["status"],
        }
        for row in list_products(conn, stall_id)
    ]


def _narrow_categories(conn: sqlite3.Connection, stall_id: int) -> list[dict]:
    """**本摊位商品实际用到的**品类（不是全库品类字典：秤端只需要能显示选品分组）。"""
    rows = conn.execute(
        """
        SELECT DISTINCT c.id AS category_id, c.name AS name
        FROM category c JOIN product p ON p.category_id = c.id
        WHERE p.stall_id = ? AND p.status = 'active'
        ORDER BY c.id
        """,
        (stall_id,),
    ).fetchall()
    return [{"category_id": int(row["category_id"]), "name": row["name"]} for row in rows]


def narrow_catalog(conn: sqlite3.Connection, stall_id: int, since_version: int | None = None) -> dict:
    """契约 §3.3 的窄响应体；`since_version` 等于当前内容版本时返回空数组（客户端已最新）。"""
    products = _narrow_products(conn, stall_id)
    categories = _narrow_categories(conn, stall_id)
    version = _content_version({"products": products, "categories": categories})
    if since_version is not None and since_version == version:
        products, categories = [], []
    return {"catalog_version": version, "products": products, "categories": categories}


def catalog_version_of(conn: sqlite3.Connection, stall_id: int) -> int:
    """本摊位目录的当前内容版本号（§3.1 激活配置 / §3.2 心跳复用同一口径）。"""
    return int(narrow_catalog(conn, stall_id)["catalog_version"])


def narrow_price_list(conn: sqlite3.Connection, stall_id: int, business_date: str | None) -> dict:
    """契约 §3.4 的窄响应体（离线计价的唯一依据）。

    只下发**在营商品**的当日价目；价目表为空时 `items` 就是空数组，**不报错**。
    """
    day = business_date or clock.today_iso()
    try:
        day = parse_business_date(day, field="business_date")
    except TradeError as exc:
        # 契约 §3.4 的"可能错误"只登记了 `MT-2001`，故营业日不合法按秤端段的载荷码 `MT-2004` 拒绝
        raise TradeError("MT-2004", exc.message, exc.detail) from exc
    data = get_price_list(conn, stall_id, day)
    active = {
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM product WHERE stall_id = ? AND status = 'active'", (stall_id,)
        )
    }
    items = [
        {"product_id": int(item["product_id"]), "unit_price_cents": int(item["unit_price_cents"])}
        for item in data["items"]
        if int(item["product_id"]) in active
    ]
    return {
        "business_date": data["business_date"],
        "items": items,
        "price_list_version": _content_version(items),
    }


# ---------------------------------------------------------------------------
# §3.3 商品字典与品类
# ---------------------------------------------------------------------------


@bp.get("/api/scale/v1/catalog")
def read_scale_catalog():
    """契约 §3.3：本摊位商品字典与品类，支持 `since_version` 增量。"""
    require_proto()
    conn = current_db()
    device = require_device(conn)
    raw = request.args.get("since_version")
    since: int | None = None
    if raw not in (None, ""):
        try:
            since = int(raw)
        except (TypeError, ValueError) as exc:
            raise TradeError(
                "MT-2004", "`since_version` 必须是整数", {"field": "since_version"}
            ) from exc
        if since < 0:
            raise TradeError(
                "MT-2004", "`since_version` 必须 ≥0", {"field": "since_version", "value": since}
            )
    return jsonify(narrow_catalog(conn, int(device["stall_id"]), since)), 200


# ---------------------------------------------------------------------------
# §3.4 价目表
# ---------------------------------------------------------------------------


@bp.get("/api/scale/v1/price-list")
def read_scale_price_list():
    """契约 §3.4：本摊位某营业日价目表；缺省取中台当前业务日；为空返回空数组。"""
    require_proto()
    conn = current_db()
    device = require_device(conn)
    return (
        jsonify(narrow_price_list(conn, int(device["stall_id"]), request.args.get("business_date"))),
        200,
    )
