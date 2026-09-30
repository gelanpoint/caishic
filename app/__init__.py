"""Flask 应用工厂、统一错误处理与静态入口。

范围（`specs/market-trade-flow/plan.md` §4）：
- 注册蓝图、静态目录、统一错误处理；
- 纯静态前端由 Flask 托管（无构建、无 CDN，`ADR-0003` / `AGENTS.md` §3 硬要求 4）。

本文件**不承载业务逻辑**：业务在 `app/domain/`，端点实现在 `app/api/`。

## 统一错误处理（契约 §1.2 / §4）

- 错误码 → HTTP 状态的**唯一落点就是本文件的 `ERROR_STATUS`**，逐条抄自
  `specs/market-trade-flow/contracts/rest-api.md` §4 错误码表（13 条）。**不得在别处再写一套映射**
  —— 同一个规则写两遍，就是下次漂移的种子。
- 业务层（`app/domain/`）**不依赖 HTTP**：只抛 `TradeError(code, message, detail)`，
  由下面的错误处理器统一转成契约 §1.2 的响应体与 §4 的状态码。
  这样"契约里的码"与"HTTP 怎么回"各自只有一个出处。
- 未匹配路径的 404 取 `MT-1009`（唯一与 HTTP 状态无歧义的通用码）；契约 §4 没有定义
  405 / 500 的通用码，故**本文件不自行发明错误码**（发明即等于在契约之外新增需求，违反 `RL-1`）。
"""

from __future__ import annotations

from flask import Flask, g, jsonify, request, send_from_directory

from . import config
from .db import connect

#: 契约 §4「统一错误码表」的错误码 → HTTP 状态（**唯一映射点**，改动前先改契约）。
ERROR_STATUS: dict[str, int] = {
    "MT-1001": 409,
    "MT-1002": 422,
    "MT-1003": 422,
    "MT-1004": 403,
    "MT-1005": 401,
    "MT-1006": 409,
    "MT-1007": 500,
    "MT-1008": 422,
    "MT-1009": 404,
    "MT-1010": 409,
    "MT-1011": 409,
    "MT-1012": 409,
    "MT-1013": 409,
}


class TradeError(Exception):
    """契约错误码的业务异常（**领域层与端点层共用的唯一错误出口**）。

    `code` 必须是 `ERROR_STATUS` 里的键（即契约 §4 定义的码）—— 构造时即校验，
    免得把契约外的码漏进响应体。
    """

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        if code not in ERROR_STATUS:
            raise ValueError(f"{code} 不在契约 §4 错误码表内（不得发明契约外错误码，RL-1）")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail

    @property
    def status(self) -> int:
        """该错误码在契约 §4 规定的 HTTP 状态。"""
        return ERROR_STATUS[self.code]


def error_body(code: str, message: str, detail: dict | None = None) -> dict:
    """构造契约 §1.2 的统一错误响应体。"""
    error: dict = {"code": code, "message": message}
    if detail is not None:
        error["detail"] = detail
    return {"error": error}


def current_db():
    """**每个请求一个** SQLite 连接（经 `flask.g` 复用，请求结束时由 teardown 关闭）。

    连接获取的落点放在本文件（HTTP 层），而不是 `app/db.py` ——
    `db.py` 只负责"怎么连库"，不该知道 Flask 的请求上下文。
    """
    conn = g.get("_db_conn")
    if conn is None:
        conn = connect()
        g._db_conn = conn
    return conn


def create_app() -> Flask:
    """创建并返回 Flask 应用实例。"""
    app = Flask(__name__, static_folder=str(config.STATIC_DIR), static_url_path="/static")
    # 中文不转义为 \uXXXX，便于现场用浏览器直接看响应（演示友好的可读性）
    app.json.ensure_ascii = False

    # ---- 静态入口导航页 -------------------------------------------------
    @app.get("/")
    def index():
        """入口导航页（列出操作端与顾客扫码页两个入口）。"""
        return send_from_directory(config.STATIC_DIR, "index.html")

    # ---- 业务错误 → 契约 §1.2 响应 -------------------------------------
    @app.errorhandler(TradeError)
    def handle_trade_error(err: TradeError):
        """领域层抛出的契约错误码，统一转成 `{"error": {...}}` 与 §4 的状态码。"""
        return jsonify(error_body(err.code, err.message, err.detail)), err.status

    # ---- 统一错误处理 ---------------------------------------------------
    @app.errorhandler(404)
    def handle_not_found(err):
        """统一 404 响应。

        错误码取契约 §4 的 `MT-1009`（HTTP 404「资源不存在」）—— 这是**唯一与 HTTP 状态无歧义**的通用码。
        其余错误码的绑定与断言属 `T-007`（契约测试）与 `T-024`（留痕）的范围：
        契约 §4 没有定义 405 / 500 的通用码，**本文件不自行发明错误码**
        （发明即等于在契约之外新增需求，违反 `RL-1`）。
        """
        return (
            jsonify(
                error_body(
                    "MT-1009",
                    "资源不存在",
                    {"path": request.path, "method": request.method},
                )
            ),
            404,
        )

    @app.errorhandler(400)
    def handle_bad_request(err):
        """请求体不是合法 JSON 等客户端错误 → 契约 §4 的 `MT-1008`（422 参数校验失败）。"""
        return jsonify(error_body("MT-1008", "参数校验失败", {"reason": str(err.description)})), 422

    @app.teardown_appcontext
    def close_db(exc):
        """请求结束（含异常路径）关闭本请求的数据库连接 —— 不依赖"正常返回"才清理。"""
        conn = g.pop("_db_conn", None)
        if conn is not None:
            conn.close()

    # ---- 蓝图注册（端点实现逐一对应契约 §3） ----------------------------
    from .api.customer import bp as customer_bp
    from .api.health import bp as health_bp
    from .api.merchant import bp as merchant_bp
    from .api.mock import bp as mock_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(merchant_bp)
    app.register_blueprint(mock_bp)
    app.register_blueprint(customer_bp)

    return app
