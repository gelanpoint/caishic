"""秤端（摊主）端点组：契约 §3.2 会话、§3.3/§3.4 价目表、§3.5 商品、§3.6 创建交易并计价、
§3.7 交易列表、§3.8 交易详情、§3.9 改价/抹零、§3.10 确认收款、§3.11 退货冲正、§3.12 商户看板。

关联：`REQ-002`、`REQ-003`、`REQ-004`、`REQ-005`、`REQ-006`、`REQ-007`、`REQ-008`、`REQ-009`、
`REQ-010`、`REQ-012`、`REQ-013`、`REQ-021`、`REQ-026`、`REQ-027`、`REQ-028`、`REQ-032`；
`NFR-007`；`AC-001`/`AC-002`/`AC-004`/`AC-006`/`AC-007`/`AC-008`/`AC-014`/`AC-015`/`AC-016`/
`AC-017`/`AC-021`。
对应任务：`tasks.md` `T-015`（§3.2~§3.5、§3.7/§3.8/§3.12 秤端读端点 —— 后三处由 `T-008` 执行时
发现归属缺口并补进 `tasks.md` §1.3）、`T-016`（§3.6 创建交易、§3.9 改价/抹零）、
`T-017`（§3.10 确认收款）、`T-018`（§3.11 退货冲正）。

本模块**只是 HTTP 皮肤**：解析请求 → 取本请求的连接与摊位会话 → 调用 `app/domain/` 下的领域函数
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

**范围**：本提交实现 §3.2~§3.11；§3.13~§3.15 离线、运营端与顾客端端点不在本文件内，
按 `tasks.md` §1.3 的归属由 `T-019`/`T-020`~`T-023` 实现。
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
from ..domain.metrics import stall_daily_dashboard
from ..domain.payment import pay_transaction
from ..domain.pricing import change_price, create_transaction, list_transactions, transaction_detail
from ..domain.refund import refund_transaction

bp = Blueprint("merchant", __name__)

#: 秤端会话请求头（契约 §1.6）；字面量只在此处出现一次，避免各处各写一遍。
SESSION_HEADER = "X-Stall-Session"
#: 写操作幂等键请求头（契约 §1.3 / §1.7）。
IDEMPOTENCY_HEADER = "Idempotency-Key"


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


# ---------------------------------------------------------------------------
# §3.6 创建交易并计价（`AC-001` / `AC-016` / `AC-017`）
# ---------------------------------------------------------------------------


@bp.post("/api/merchant/transactions")
def create_stall_transaction():
    """契约 §3.6：选品 + 重量 → 计价落库，返回 201。

    金额与状态**全部由 `app/domain/pricing.create_transaction()` 决定**（整数分、四舍五入到分、
    `MT-1002`/`MT-1006`/`MT-1012` 都在那里；本层不重算一次，否则就有了第二份计价口径）。

    幂等重放返回 **200 而非 201**：契约 §1.7 要求"返回首次结果"，而首次之外并未新建资源，
    201（Created）会谎报"又创建了一笔"。T-009 用例对 `200/201` 都接受，但语义上只有 200 是对的。
    """
    conn = current_db()
    stall = _bound_stall(conn)
    payload, replayed = create_transaction(
        conn,
        stall,
        request.get_json(silent=True),
        request.headers.get(IDEMPOTENCY_HEADER),
    )
    return jsonify(payload), (200 if replayed else 201)


# ---------------------------------------------------------------------------
# §3.7 本摊位交易列表（看板与对账明细用）
# ---------------------------------------------------------------------------


@bp.get("/api/merchant/transactions")
def list_stall_transactions():
    """契约 §3.7：分页对象 `{"total", "items"}`，**只含本摊位数据**（`REQ-032` / `AC-021`）。

    查询参数 `business_date`（可选）、`limit`（可选，默认 50、上限 200）都在领域层解释：
    "摊位边界 + 分页"是数据边界问题，不是 HTTP 问题。
    """
    conn = current_db()
    stall = _bound_stall(conn)
    return (
        jsonify(
            list_transactions(
                conn,
                stall["stall_id"],
                request.args.get("business_date"),
                request.args.get("limit"),
            )
        ),
        200,
    )


# ---------------------------------------------------------------------------
# §3.8 交易详情与凭证数据（屏幕展示，不打印）
# ---------------------------------------------------------------------------


@bp.get("/api/merchant/transactions/<transaction_no>")
def read_transaction_detail(transaction_no):
    """契约 §3.8：§2.8 顶层 + `items` + `payments` + 固定 `printable: false`。

    `transaction_no` 由 Flask 从路由注入（**不加 `<int:...>` 转换器**：契约 §3.8 的路径参数是字符串，
    交易号由系统生成；用类型转换器会把"格式不对的交易号"在路由层判成 404，
    而那种情况应当由领域层按 `MT-1009` 给出统一错误信封）。

    不存在的交易号 → 404 `MT-1009`；存在但属其他摊位 → 403 `MT-1004`（契约 §4 / `AC-021`）。
    两个判断都在领域层按"先存在、后归属"的顺序执行。
    """
    conn = current_db()
    stall = _bound_stall(conn)
    return jsonify(transaction_detail(conn, stall["stall_id"], transaction_no)), 200


# ---------------------------------------------------------------------------
# §3.9 改价 / 抹零（留痕）
# ---------------------------------------------------------------------------


@bp.post("/api/merchant/transactions/<transaction_no>/price-change")
def change_stall_transaction_price(transaction_no):
    """契约 §3.9：改价或抹零，并写入**只增不改**的留痕。

    全部规则在 `app/domain/pricing.change_price()`：可改价状态（`MT-1001`）、
    `final_unit_price_cents` 与 `round_off_cents` 二选一（`MT-1008`）、
    幅度 >50% 需确认（`MT-1011`，**只要求确认、不阻止**）、抹零与改价分开标记（`REQ-008` / `AC-008`）。
    金额重算同样在那里（本层不重算 —— 涉钱口径只准有一处实现）。

    契约 §3.9 的请求头只要求 `X-Stall-Session`（**不要求** `Idempotency-Key`）：
    改价是"改已有行 + 写留痕"，不是新建资金事实；若在此额外强制幂等键，
    就是给契约之外的调用方新增一个必填项（`RL-1`）。
    """
    conn = current_db()
    stall = _bound_stall(conn)
    return jsonify(change_price(conn, stall, transaction_no, request.get_json(silent=True))), 200


# ---------------------------------------------------------------------------
# §3.10 确认收款（现金 / 收款码）
# ---------------------------------------------------------------------------


@bp.post("/api/merchant/transactions/<transaction_no>/payment")
def pay_stall_transaction(transaction_no):
    """契约 §3.10：`method = cash` → 200（立即成功并落支付流水）；`method = qr` → 202（待回调）。

    **现金与收款码写同一张 `payment` 表**（`REQ-010` / `data-model.md` §2.10），以 `method` 区分 ——
    为现金另建一张表会直接打碎对账等式「订单总额 = 支付流水 = 分账明细」（`AC-003`），
    而等式正是本系统的资金红线。

    状态码由领域层给出（`pay_transaction` 返回 `(响应体, 状态)`）：现金 200 / 收款码 202 是
    **契约 §3.10 的两种不同语义**（已到账 vs 已生成待回调），不是同一个 200 加个标记 ——
    编排层若在这里统一成 200，调用方就分不清"钱到了"和"码给了"。
    """
    conn = current_db()
    stall = _bound_stall(conn)
    payload, status = pay_transaction(
        conn,
        stall,
        transaction_no,
        request.get_json(silent=True),
        request.headers.get(IDEMPOTENCY_HEADER),
    )
    return jsonify(payload), status


# ---------------------------------------------------------------------------
# §3.11 退货冲正（只冲减一次）
# ---------------------------------------------------------------------------


@bp.post("/api/merchant/transactions/<transaction_no>/refund")
def refund_stall_transaction(transaction_no):
    """契约 §3.11：只冲减一次；**重复提交返回 200 + `replayed: true` 且金额不变**。

    **幂等命中不是错误**（`spec.md` §5 / 契约 §4 末注）：这条路径绝不能返回 4xx ——
    调用方（秤端）在弱网下会重发，把它当失败就会重复提示摊主"退货没成功"。
    幂等判定与状态/金额闸门的先后顺序在领域层（`app/domain/refund.py` 的模块 docstring 有说明），
    本层只负责把结果转成 200。
    """
    conn = current_db()
    stall = _bound_stall(conn)
    return (
        jsonify(
            refund_transaction(
                conn,
                stall,
                transaction_no,
                request.get_json(silent=True),
                request.headers.get(IDEMPOTENCY_HEADER),
            )
        ),
        200,
    )


# ---------------------------------------------------------------------------
# §3.12 商户端看板（交易、佣金、经营数据）
# ---------------------------------------------------------------------------


@bp.get("/api/merchant/dashboard")
def read_merchant_dashboard():
    """契约 §3.12：`business_date` 不传则取当日；聚合口径见 `data-model.md` §2.15 / §2.17。

    响应字段**恰好契约列出的六个**（`T-008` 按字段白名单断言）：这块看板是给摊主看的，
    多塞一个"看起来有用"的字段就是契约之外的新需求（`RL-1`）。
    """
    conn = current_db()
    stall = _bound_stall(conn)
    return jsonify(stall_daily_dashboard(conn, stall["stall_id"], request.args.get("business_date"))), 200
