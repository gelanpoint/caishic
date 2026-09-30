"""`T-007` 能力①③：路由表 ↔ 契约 §2 双向一致 + 统一错误响应格式断言。

- 关联：`REQ-024`；`NFR-009`、`NFR-010`；`AC-012`；契约 §1.2 / §2 / §4。
- 本文件同时承载**灵敏度负例**（`AGENTS.md`："验证要能失败……不验证灵敏度的验证是摆设"）：
  ① 故意注册一个契约里没有的端点 → 比对必须变红；
  ② 故意漏掉一个契约端点 → 比对必须变红；
  ③ 移除故意破坏物后必须恢复绿（用**合成的完整路由表**证明本检查不是"永远失败"的摆设）；
  ④ 统一错误格式断言必须对**已知坏样例**报错。
- 人工演示的实测输出（故意改 `app/__init__.py` 再改回）见提交信息与 `docs/PROJECT-STATE.md` 变更记录。
"""

from __future__ import annotations

import pytest
from flask import Flask

import contract_support as conftest
from contract_support import (
    CONTRACT_ENDPOINTS,
    CONTRACT_ERROR_CODES,
    NON_API_ROUTES,
    assert_error_response,
    assert_route_table_matches_contract,
    error_envelope_violations,
    registered_endpoints,
    route_table_diff,
)

# ---------------------------------------------------------------------------
# 契约解析的机械核验（防止"解析器坏了但测试还绿"）
# ---------------------------------------------------------------------------


def test_contract_parser_finds_sentinel_endpoints(contract):
    """§2 端点总表的两端（首行 / 末行）必须被解析到 —— 解析器失灵时立即失败。"""
    endpoints = contract["endpoints"]
    assert ("GET", "/healthz") in endpoints, "§2 首行端点未解析到"
    assert ("GET", "/api/admin/audit-logs") in endpoints, "§2 末行端点未解析到"


def test_contract_error_codes_are_continuous_from_1001(contract):
    """§4 错误码表编号连续（契约自述：「全项目连续编号」）。"""
    codes = sorted(contract["error_codes"])
    assert codes == [f"MT-{n}" for n in range(1001, 1014)], f"错误码表编号不连续：{codes}"


# ---------------------------------------------------------------------------
# 能力①：Flask 路由表 ↔ 契约 §2 端点总表（双向）
# ---------------------------------------------------------------------------


def test_route_table_matches_contract_two_way(seeded_app):
    """主检查：实现多一个端点或少一个端点都必须失败（契约 §6 第 1 条的落地形式）。"""
    assert_route_table_matches_contract(seeded_app)


@pytest.mark.parametrize("method,path", sorted(CONTRACT_ENDPOINTS))
def test_every_contract_endpoint_is_registered(seeded_app, method, path):
    """逐条确认（CP-B 第②项要的就是逐条）：契约 §2 的每个端点都必须在实现里注册。"""
    registered = registered_endpoints(seeded_app)
    assert (method, path) in registered, (
        f"契约 §2 已登记 {method} {path}，但 Flask 路由表里没有 —— "
        f"实现未开工（批次 3 `T-015`~`T-025`）或实现漏做"
    )


def test_non_api_routes_are_exactly_the_exempt_whitelist(seeded_app):
    """豁免表纪律：除入口导航页与静态资源外，不得有契约之外的路由。"""
    all_routes = {
        (method, conftest.normalize_rule(str(rule)))
        for rule in seeded_app.url_map.iter_rules()
        for method in rule.methods
        if method not in {"HEAD", "OPTIONS"}
    }
    unexpected = sorted(all_routes - CONTRACT_ENDPOINTS - NON_API_ROUTES)
    assert not unexpected, f"出现契约 §2 未登记、也不在豁免表内的路由：{unexpected}"


# ---------------------------------------------------------------------------
# 灵敏度负例（合成路由表：完整 → 加破坏物变红 → 移除恢复绿）
# ---------------------------------------------------------------------------


def _synthetic_app(extra: tuple[tuple[str, str], ...] = (), skip: tuple[tuple[str, str], ...] = ()):
    """构造一个**路由表与契约完全一致**的合成应用，用于隔离验证检查本身。"""
    app = Flask(f"synthetic-{extra}-{skip}")
    for method, path in sorted(CONTRACT_ENDPOINTS - set(skip)):
        app.add_url_rule(
            conftest.normalize_rule(path).replace("{", "<").replace("}", ">"),
            endpoint=f"ep_{method}_{path}".replace("/", "_").replace("{", "").replace("}", ""),
            view_func=lambda **_: ("{}", 200),
            methods=[method],
        )
    for method, path in extra:
        app.add_url_rule(
            path,
            endpoint=f"phantom_{method}_{path}".replace("/", "_"),
            view_func=lambda **_: ("{}", 200),
            methods=[method],
        )
    return app


PHANTOM = ("POST", "/api/merchant/ghost-endpoint")


def test_sensitivity_green_on_complete_route_table():
    """基线：路由表与契约一致时检查**必须是绿的** —— 证明它不是一个"永远失败"的摆设。"""
    assert_route_table_matches_contract(_synthetic_app())


def test_sensitivity_extra_endpoint_turns_check_red():
    """负例①：故意注册一个契约里没有的端点 → 比对必须变红，且指名道姓。"""
    app = _synthetic_app(extra=(PHANTOM,))
    with pytest.raises(AssertionError) as excinfo:
        assert_route_table_matches_contract(app)
    message = str(excinfo.value)
    assert PHANTOM[1] in message and "未登记" in message, f"未指出契约外端点：{message}"

    missing, extra = route_table_diff(app)
    assert extra == [PHANTOM] and missing == [], f"双向比对结果不符：missing={missing} extra={extra}"


def test_sensitivity_missing_endpoint_turns_check_red():
    """负例②：故意让实现少一个契约端点 → 比对必须变红（反方向同样成立）。"""
    dropped = ("POST", "/api/merchant/transactions")
    assert dropped in CONTRACT_ENDPOINTS
    with pytest.raises(AssertionError) as excinfo:
        assert_route_table_matches_contract(_synthetic_app(skip=(dropped,)))
    assert "未注册" in str(excinfo.value)

    missing, extra = route_table_diff(_synthetic_app(skip=(dropped,)))
    assert missing == [dropped] and extra == []


def test_sensitivity_removing_the_breaker_restores_green():
    """负例③：移除故意破坏物 → 必须**恢复绿**（红-绿闭环，不是只红不绿）。"""
    broken = _synthetic_app(extra=(PHANTOM,))
    with pytest.raises(AssertionError):
        assert_route_table_matches_contract(broken)

    # 移除同一个破坏物后，同一个检查函数必须重新通过
    assert_route_table_matches_contract(_synthetic_app(extra=()))
    assert PHANTOM[1] not in str(route_table_diff(_synthetic_app())[1])


# ---------------------------------------------------------------------------
# 能力③：统一错误响应格式（契约 §1.2 / §4）
# ---------------------------------------------------------------------------


def test_unknown_path_returns_unified_error_envelope(client):
    """未匹配路径的 404 也必须走统一错误格式（当前 `app/__init__.py` 已实现）。"""
    response = client.get("/api/definitely-not-a-contract-endpoint")
    assert response.status_code == 404
    assert_error_response(response, expected_code="MT-1009")


def test_error_envelope_accepts_the_contract_sample():
    """契约 §1.2 的原文样例必须是合规的（否则断言本身写错了）。"""
    sample = {
        "error": {
            "code": "MT-1002",
            "message": "重量超出允许范围",
            "detail": {"weight_grams": 52000, "max_grams": 50000},
        }
    }
    assert error_envelope_violations(sample, http_status=422) == []


BAD_SAMPLES = [
    pytest.param({"error": {"code": "MT-9999", "message": "契约外错误码"}}, 499, id="contract-external-code"),
    pytest.param({"error": {"code": "X-1001", "message": "码形不符"}}, 422, id="bad-code-pattern"),
    pytest.param({"error": {"message": "缺 code"}}, 422, id="missing-code"),
    pytest.param({"error": {"code": "MT-1002"}}, 422, id="missing-message"),
    pytest.param({"error": {"code": "MT-1002", "message": " "}}, 422, id="blank-message"),
    pytest.param({"error": {"code": "MT-1002", "message": "m", "detail": "not-an-object"}}, 422, id="detail-not-object"),
    pytest.param({"error": {"code": "MT-1002", "message": "m", "extra": 1}}, 422, id="extra-key-in-error"),
    pytest.param({"error": {"code": "MT-1002", "message": "m"}, "data": {}}, 422, id="extra-top-level-key"),
    pytest.param({"err": {"code": "MT-1002", "message": "m"}}, 422, id="wrong-envelope-key"),
    pytest.param({"error": "boom"}, 422, id="error-not-object"),
    pytest.param([{"error": {"code": "MT-1002", "message": "m"}}], 422, id="top-level-is-array"),
    pytest.param({"error": {"code": "MT-1002", "message": "m"}}, 500, id="http-status-mismatch"),
]


@pytest.mark.parametrize("payload,http_status", BAD_SAMPLES)
def test_error_envelope_flags_known_bad_samples(payload, http_status):
    """已知坏样例必须被报错（能力③的灵敏度体现；`T-007` 验收方式原文要求）。"""
    violations = error_envelope_violations(payload, http_status=http_status)
    assert violations, f"坏样例未被报错：{payload!r} (HTTP {http_status})"


def test_every_contract_error_code_has_a_declared_http_status():
    """§4 的 13 个错误码都要有合法 HTTP 状态（解析 + 交叉核验，防止表格漂移）。"""
    assert len(CONTRACT_ERROR_CODES) == 13, f"契约 §4 应为 13 个错误码，实际 {len(CONTRACT_ERROR_CODES)}"
    for code, status in CONTRACT_ERROR_CODES.items():
        assert 400 <= status <= 599, f"{code} 的 HTTP 状态非法：{status}"


def test_error_response_assertion_rejects_bad_sample():
    """`assert_error_response` 对不合规响应必须抛错（而不是静默通过）。"""
    from werkzeug.wrappers import Response

    wrapped = Response('{"error": {"code": "MT-9999", "message": "x"}}', mimetype="application/json")
    wrapped.status_code = 499
    with pytest.raises(AssertionError):
        assert_error_response(wrapped, expected_code="MT-1002")
