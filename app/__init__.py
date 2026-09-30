"""Flask 应用工厂与静态入口。

范围（`specs/market-trade-flow/plan.md` §4）：
- 注册蓝图、静态目录、统一错误处理；
- 纯静态前端由 Flask 托管（无构建、无 CDN，`ADR-0003` / `AGENTS.md` §3 硬要求 4）。

本文件**不承载业务逻辑**：业务在 `app/domain/`，端点实现在 `app/api/`。
"""

from __future__ import annotations

from flask import Flask, jsonify, request, send_from_directory

from . import config


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
                {
                    "error": {
                        "code": "MT-1009",
                        "message": "资源不存在",
                        "detail": {"path": request.path, "method": request.method},
                    }
                }
            ),
            404,
        )

    return app
