"""Flask 应用工厂、统一错误处理与静态入口。

范围（`specs/market-trade-flow/plan.md` §4）：
- 注册蓝图、静态目录、统一错误处理；
- 纯静态前端由 Flask 托管（无构建、无 CDN，`ADR-0003` / `AGENTS.md` §3 硬要求 4）。

本文件**不承载业务逻辑**：业务在 `app/domain/`，端点实现在 `app/api/`。

## 统一错误处理（契约 §1.2 / §4）

- 错误码 → HTTP 状态的**唯一落点就是本文件的 `ERROR_STATUS`**，逐条抄自
  `specs/market-trade-flow/contracts/rest-api.md` §4 错误码表（14 条）与
  `specs/market-trade-flow/contracts/scale-midplatform.md` §4 的秤端接入段（`MT-2001`~`MT-2005`，5 条）。
  **不得在别处再写一套映射** —— 同一个规则写两遍，就是下次漂移的种子。
- 业务层（`app/domain/`）**不依赖 HTTP**：只抛 `TradeError(code, message, detail)`，
  由下面的错误处理器统一转成契约 §1.2 的响应体与 §4 的状态码。
  这样"契约里的码"与"HTTP 怎么回"各自只有一个出处。
- **未预期异常也有确定的出口**：任何没被上面接住的异常，统一回 §1.2 格式 + `MT-1014`（500），
  并把**原始异常与栈只写进服务端日志**（`app.logger.exception`），响应体只给可报障的
  `request_id`。为什么必须有这条：只有 Flask 默认 500 时，响应体是 **HTML、不符合 §1.2**，
  调用方既看不出是契约错误、也拿不到任何可追溯标识，**不知道这笔交易到底记没记**——
  那就把"确定性的失败"退化成了"不知道发生了什么"。
- 未匹配路径的 404 取 `MT-1009`（唯一与 HTTP 状态无歧义的通用码）。
"""

from __future__ import annotations

import uuid

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
    # §4 的通用 5xx：未预期异常。**补它不是为了"多一个码"**，而是让"没人预料到的那一类"
    # 也有确定出口（统一格式 + 可报障标识 + 明确告知"结果不确定"），见模块 docstring。
    "MT-1014": 500,
    # ---- 秤端接入段（`contracts/scale-midplatform.md` §4；与 `MT-1xxx` 连续编号、互不重叠）----
    # 形态 2 的 5 个码**必须**落在这张表里：`TradeError` 构造时校验码在表内，
    # 不在表内会被兜底处理器当成"未预期异常"回成 `MT-1014`(500) —— 调用方拿到的码与契约不一致，
    # 等于契约白写（`test_scale_contract_surface.py` 对此有专门断言）。
    "MT-2001": 401,  # 设备未激活或令牌无效
    "MT-2002": 403,  # 设备与目标的（市场, 摊位）不匹配
    "MT-2003": 409,  # 协议版本不兼容
    "MT-2004": 422,  # 上报载荷不合法
    "MT-2005": 409,  # 设备绑定冲突
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


def is_http_error(err: BaseException) -> bool:
    """该异常是否自带 HTTP 语义（werkzeug 的 `HTTPException` 族）。

    **为什么不直接 `from werkzeug.exceptions import HTTPException`**：
    运行期源码的依赖白名单只放 Flask 与标准库（宪法 §1 / `ADR-0003`，由
    `tests/contract/test_deps_isolation.py` 逐 import 扫描）。`werkzeug` 虽然是 Flask 的硬依赖、
    装了 Flask 就一定有，但**白名单不是"能 import 就放行"** —— 一旦为它开口子，
    这道检查就再也挡不住"顺手 import 一个其实也装着的别的东西"。
    故这里按 `HTTPException` 的**接口契约**判定（`code` 是 HTTP 状态码 + 有 `get_response()`），
    不 import 它：语义等价，白名单不动。
    """
    code = getattr(err, "code", None)
    return isinstance(code, int) and 400 <= code <= 599 and callable(getattr(err, "get_response", None))


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


def create_app(*, serve_scale_ui: bool = True) -> Flask:
    """创建并返回 Flask 应用实例。

    `serve_scale_ui`（`T-SCALE-17`，默认 `True` = **形态 1 行为一字不变**）：
    形态 1（单机演示）下中台顺带托管 `/scale/` 秤端界面；形态 2（分离部署）下秤端界面归
    秤端固件，中台**不注册**该路由 ⇒ `/scale/` 返回 404。两种形态共用同一份领域代码与
    同一批业务端点，**差别只在"谁托管秤端界面"** —— 这正是 `ADR-0005`「分离形态是新增
    启动方式」的含义，而不是两套实现。
    """
    app = Flask(__name__, static_folder=str(config.STATIC_DIR), static_url_path="/static")
    # 中文不转义为 \uXXXX，便于现场用浏览器直接看响应（演示友好的可读性）
    app.json.ensure_ascii = False

    # ---- 静态入口导航页 -------------------------------------------------
    @app.get("/")
    def index():
        """入口导航页（列出操作端与顾客扫码页两个入口）。"""
        return send_from_directory(config.STATIC_DIR, "index.html")

    # ---- 三个页面入口（T-026 秤端 / T-027 运营端 / T-028 顾客扫码页） ----
    # 说明：这三个路由**不是接口**（契约 §2 不登记页面），故它们登记在契约测试的
    # `NON_API_ROUTES` 豁免表里；`run.py` 的启动横幅也按这三个路径打印入口地址，
    # 两者是同一份事实（改路径必须同时改 `NON_API_ROUTES`，否则路由表比对会红）。
    def _page(name: str):
        """返回 `app/static/<name>/index.html`；目录名固定，不接受来自请求的路径片段。"""
        return send_from_directory(str(config.STATIC_DIR / name), "index.html")

    if serve_scale_ui:
        @app.get("/scale/")
        def scale_page():
            """秤端（摊主收银台）页面。

            **分离形态（`ADR-0005`）下本路由不注册**：秤端界面归秤端（ESP32-S3 固件），
            中台只提供 7 个接入端点 + 运营端 + 顾客页。此时 `/scale/` 返回 404 ——
            这是**刻意**的，不是漏挂（`T-SCALE-17`）。
            """
            return _page("scale")

    @app.get("/admin/")
    def admin_page():
        """运营端（市场方）页面。"""
        return _page("admin")

    @app.get("/customer/")
    def customer_page():
        """顾客扫码页（不登录）。"""
        return _page("customer")

    # ---- 业务错误 → 契约 §1.2 响应 -------------------------------------
    @app.errorhandler(TradeError)
    def handle_trade_error(err: TradeError):
        """领域层抛出的契约错误码，统一转成 `{"error": {...}}` 与 §4 的状态码。"""
        return jsonify(error_body(err.code, err.message, err.detail)), err.status

    # 秤端接入段（形态 2）的设备领域异常：与 `TradeError` **同一出口、同一映射表**。
    # `app/domain/device.py` 刻意不依赖 HTTP、也不自带状态码（那会成为第二份映射），
    # 故它的 `DeviceError` 在这里被转成与 `TradeError` 完全相同的响应体与状态码。
    from .domain.device import DeviceError

    @app.errorhandler(DeviceError)
    def handle_device_error(err: DeviceError):
        """`MT-2001`~`MT-2005` → 契约 §1.2 响应体 + `ERROR_STATUS` 里的状态码。"""
        return jsonify(error_body(err.code, err.message, err.detail)), ERROR_STATUS[err.code]

    # ---- 统一错误处理 ---------------------------------------------------
    @app.errorhandler(404)
    def handle_not_found(err):
        """统一 404 响应。

        错误码取契约 §4 的 `MT-1009`（HTTP 404「资源不存在」）—— 这是**唯一与 HTTP 状态无歧义**的通用码。
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

    @app.errorhandler(Exception)
    def handle_unexpected_error(err):
        """**兜底**：没被上面任何处理器接住的异常 → 契约 §1.2 格式 + `MT-1014`（500）。

        两条纪律：

        1. **对外不给栈**：响应体只有码、一句人话、`request_id` 与请求路径 ——
           栈和原始异常**只进服务端日志**（`logger.exception` 会带 traceback，排查时查得到）。
           把实现细节回给调用方，既是泄密，也会让"内网演示里随便贴个报错"变成常态。
        2. **不吞掉 HTTP 语义**：`werkzeug` 的 `HTTPException`（404/405/…）自带状态与语义，
           直接放行让它按自己的方式回（405 就是 405，不该被兜底改成 500）。
           本处理器在 `errorhandler(404)` / `errorhandler(400)` **之后**注册，故那两条已经先接住；
           其余 HTTP 异常（如 405、413）在这里原样放行 —— 判定走 `is_http_error`（不 import werkzeug，
           见该函数的 docstring）。

        `request_id` 的用途是把"用户截图里的一行字"与"日志里的一次 traceback"对上 ——
        没有它，现场只能靠时间猜。
        """
        if is_http_error(err):
            return err
        request_id = uuid.uuid4().hex[:12]
        app.logger.exception(
            "未预期异常（request_id=%s，%s %s）", request_id, request.method, request.path
        )
        return (
            jsonify(
                error_body(
                    "MT-1014",
                    "内部错误：该请求未能完成，结果不确定（请勿当作成功）",
                    {"request_id": request_id, "path": request.path, "method": request.method},
                )
            ),
            500,
        )

    @app.teardown_appcontext
    def close_db(exc):
        """请求结束（含异常路径）关闭本请求的数据库连接 —— 不依赖"正常返回"才清理。"""
        conn = g.pop("_db_conn", None)
        if conn is not None:
            conn.close()

    # ---- 蓝图注册（端点实现逐一对应契约 §3） ----------------------------
    from .api.admin import bp as admin_bp
    from .api.customer import bp as customer_bp
    from .api.health import bp as health_bp
    from .api.merchant import bp as merchant_bp
    from .api.mock import bp as mock_bp
    # 形态 2（秤端 ↔ 中台）的 7 个端点：路径自带 `/api/scale/v1` 前缀（同 merchant 的做法，
    # 蓝图上**不再**挂 `url_prefix`，否则会变成 `/api/scale/v1/api/scale/v1/...`）
    from .api.scale_catalog import bp as scale_catalog_bp
    from .api.scale_device import bp as scale_device_bp
    from .api.scale_ingest import bp as scale_ingest_bp
    from .api.scale_settle import bp as scale_settle_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(merchant_bp)
    app.register_blueprint(mock_bp)
    app.register_blueprint(customer_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(scale_device_bp)
    app.register_blueprint(scale_catalog_bp)
    app.register_blueprint(scale_ingest_bp)
    app.register_blueprint(scale_settle_bp)

    return app
