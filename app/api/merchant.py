"""秤端（摊主）端点组，第一批：契约 §3.2 会话、§3.3/§3.4 价目表、§3.5 商品。

关联：`REQ-002`、`REQ-003`、`REQ-004`、`REQ-032`；`NFR-007`；`AC-006`/`AC-014`/`AC-015`/`AC-021`。
对应任务：`tasks.md` `T-015`（由 `T-008` 契约测试 `tests/contract/test_merchant_catalog.py` 驱动）。

本模块**只是 HTTP 皮肤**：解析请求 → 取本请求的连接与摊位会话 → 调用 `app/domain/catalog.py`
→ `jsonify` 结果。**业务规则一条都不在这里重写**（重写一遍就是第二份事实来源，必然与领域层漂移）；
错误一律由领域层抛 `TradeError`，交给 `app/__init__.py` 的统一错误处理器转成契约 §1.2 响应体与 §4 状态码。

三条本层才该负责的事：

1. **不做 `url_prefix`**：契约 §2 的路径已含 `/api/merchant` 前缀，蓝图上再挂一次前缀会变成
   `/api/merchant/api/merchant/...`。因此路由字符串**逐字照抄契约**（`app/__init__.py` 里也是
   `register_blueprint(bp)` 不带前缀 —— 两边一致，只有一处说了算）。
2. **请求体的解析口径**：用 `get_json(silent=True)`，拿不到 JSON 就交给领域层按 `MT-1008`
   （参数校验失败）拒绝。**故意不用 `get_json()`**：那个会在 Content-Type 不对时抛 415，
   而契约 §4 没有 415 这个码 —— 那就是在契约之外新增错误形态（违反 `RL-1`）。
3. **会话校验先于参数校验**：`X-Stall-Session` 无效一律先回 `MT-1005`(401)，
   避免"未登录也能通过参数报错探出业务规则"。

**本批次范围止于 §3.5**：§3.6 起的端点（计价、收款、退货、离线、运营端、顾客端）不在本文件内，
按 `tasks.md` §1.3 的归属由后续任务实现。文件后续会随那些任务一起长大，但**现在不预写半成品**。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import current_db
from ..domain.catalog import (
    bind_session,
    get_price_list,
    list_products,
    require_session,
    set_price_list,
)

bp = Blueprint("merchant", __name__)

#: 秤端会话请求头（契约 §1.6）；字面量只在此处出现一次，避免各处各写一遍。
SESSION_HEADER = "X-Stall-Session"


def _bound_stall(conn):
    """校验会话并返回会话绑定的摊位行（契约 §1.6 / §4 `MT-1005`）。

    数据边界（`REQ-032` / `AC-021`）由此**唯一入口**保证：端点只拿得到这一行里的 `stall_id`，
    不从请求里再接受任何摊位参数 —— 否则"摊主只能看本摊位数据"这句话就靠自觉了。
    """
    return require_session(conn, request.headers.get(SESSION_HEADER))


# ---------------------------------------------------------------------------
# §3.2 秤端绑定摊位并下发会话 Token
# ---------------------------------------------------------------------------


@bp.post("/api/merchant/session")
def create_stall_session():
    """契约 §3.2：请求体 `stall_no` → 201 下发 `session_token`。"""
    payload = bind_session(current_db(), request.get_json(silent=True))
    return jsonify(payload), 201


# ---------------------------------------------------------------------------
# §3.5 秤端可售商品（图标 / 快捷键，供"无文本输入"的选品界面）
# ---------------------------------------------------------------------------


@bp.get("/api/merchant/products")
def list_stall_products():
    """契约 §3.5：仅返回本摊位的 `status = active` 商品。"""
    conn = current_db()
    stall = _bound_stall(conn)
    return jsonify(list_products(conn, stall["stall_id"])), 200


# ---------------------------------------------------------------------------
# §3.3 / §3.4 价目表
# ---------------------------------------------------------------------------


@bp.get("/api/merchant/price-list")
def read_price_list():
    """契约 §3.3：查某营业日价目表；无记录时 `price_list_missing = true`。

    > 契约歧义（如实标注，不自行发明）：§3.3 一处写"响应(200): 数组"，另一处又要求空数组带同级
    > `price_list_missing` 标记 —— 数组挂不下同级标记。领域层取 `{business_date, items, price_list_missing}`
    > 对象形态（`items` 就是那个数组），`T-008` 的契约测试对两种形态都接受。
    """
    conn = current_db()
    stall = _bound_stall(conn)
    return jsonify(get_price_list(conn, stall["stall_id"], request.args.get("business_date"))), 200


@bp.post("/api/merchant/price-list")
def write_price_list():
    """契约 §3.4：设置/逐条调整价目表，或 `copy_from_previous_day` 复制上一营业日。"""
    conn = current_db()
    stall = _bound_stall(conn)
    return jsonify(set_price_list(conn, stall["stall_id"], request.get_json(silent=True))), 200
