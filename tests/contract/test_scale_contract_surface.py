"""形态 2（秤端 ↔ 中台）契约的「路由表 ↔ 契约 §2」双向核验（`RL-1` 的形态 2 版）。

**为什么单独一个文件，而不是并进 `test_contract_surface.py`**：
后者的判据是 `rest-api.md` 的 **31 端点 / 14 错误码**，条数写死是**刻意的**
（"契约表被改动时必须有人来看一眼这里"）。形态 2 有**自己的契约文件**与**自己的错误码段**
（`MT-2xxx`），并进去会让那套判据漂移 —— 漂移掉的正是那个提示。
故两个契约**各自核验、互不掩盖**：谁少登记谁在自己的文件里变红。

关联：`REQ-034`~`REQ-043`；`AC-025`~`AC-033`；`specs/market-trade-flow/contracts/scale-midplatform.md` §2 / §4。
"""

from __future__ import annotations

import pytest
from flask import Flask

from contract_support import (
    SCALE_CONTRACT_ENDPOINTS,
    SCALE_CONTRACT_ERROR_CODES,
    load_scale_contract_text,
    normalize_rule,
    parse_scale_contract_endpoints,
    parse_scale_error_codes,
    registered_endpoints,
)

#: 形态 2 端点的路径前缀（用于把"秤端接入段"从全部路由里圈出来）
SCALE_PREFIX = "/api/scale/"

#: 条数写死的用意与 `test_contract_surface.py` 同：**契约被改动时必须有人来看一眼这里**。
EXPECTED_ENDPOINT_COUNT = 7
EXPECTED_ERROR_CODE_COUNT = 5


# ---------------------------------------------------------------------------
# 契约解析的机械核验（防止"解析器坏了但测试还绿"）
# ---------------------------------------------------------------------------


def test_contract_file_exists_and_is_non_empty():
    assert len(load_scale_contract_text()) > 0


def test_parser_extracts_exactly_the_expected_endpoints():
    """§2 端点总表应恰好抽出 7 个端点，且全部落在秤端接入前缀下。"""
    assert len(SCALE_CONTRACT_ENDPOINTS) == EXPECTED_ENDPOINT_COUNT, (
        f"形态 2 契约 §2 应为 {EXPECTED_ENDPOINT_COUNT} 个端点，"
        f"实际 {len(SCALE_CONTRACT_ENDPOINTS)}：{sorted(SCALE_CONTRACT_ENDPOINTS)}"
    )
    outside = sorted(p for _, p in SCALE_CONTRACT_ENDPOINTS if not p.startswith(SCALE_PREFIX))
    assert not outside, f"形态 2 的端点应全部以 {SCALE_PREFIX} 开头，实际有例外：{outside}"


def test_parser_extracts_exactly_the_expected_error_codes():
    """§4 应恰好抽出 5 个 `MT-2xxx`，且不混入主契约的 `MT-1xxx`。"""
    assert len(SCALE_CONTRACT_ERROR_CODES) == EXPECTED_ERROR_CODE_COUNT, (
        f"形态 2 契约 §4 应为 {EXPECTED_ERROR_CODE_COUNT} 个错误码，"
        f"实际 {len(SCALE_CONTRACT_ERROR_CODES)}：{sorted(SCALE_CONTRACT_ERROR_CODES)}"
    )
    wrong_prefix = sorted(c for c in SCALE_CONTRACT_ERROR_CODES if not c.startswith("MT-2"))
    assert not wrong_prefix, f"形态 2 的错误码段应为 `MT-2xxx`，实际有例外：{wrong_prefix}"


def test_parser_depends_on_the_endpoint_row_structure():
    """灵敏度：把**一行的反引号拿掉**，解析结果必须少一个（证明解析器真的在读行结构）。

    注意不要用"破坏表头"来测 —— `_section()` 靠的是**章节标题**定位、正则匹配的是**数据行**，
    破坏表头对解析结果毫无影响，那种写法是**假灵敏度**（本项目明令禁止"走过场的检查"）。
    """
    text = load_scale_contract_text().replace(
        "| 1 | `/api/scale/v1/devices/activate` | POST |", "| 1 | /api/scale/v1/devices/activate | POST |"
    )
    parsed = parse_scale_contract_endpoints(text)
    assert len(parsed) == EXPECTED_ENDPOINT_COUNT - 1, (
        f"拿掉一行端点的反引号后应解析出 {EXPECTED_ENDPOINT_COUNT - 1} 个，实际 {len(parsed)}"
    )
    assert len(parse_scale_contract_endpoints()) == EXPECTED_ENDPOINT_COUNT, "原契约解析结果必须不变"


def test_parser_depends_on_the_section_heading():
    """灵敏度：把 §2 的**章节标题换掉**，解析必须**报错**（而不是静默返回空集）。

    注意：**不能**用"在标题后面追加字符"来破坏 —— `_section()` 用 `text.index(heading)` 做的是
    **子串查找**，`"## 2. 端点总表-已破坏"` 里仍然含有 `"## 2. 端点总表"`，照样能命中。
    必须换成**完全不同的标题**才构成真破坏（本用例第一版就栽在这里，留作前车之鉴）。
    """
    text = load_scale_contract_text().replace("## 2. 端点总表", "## 2. 端点一览")
    assert "## 2. 端点总表" not in text, "破坏不彻底：原标题仍作为子串存在"
    with pytest.raises(ValueError):
        parse_scale_contract_endpoints(text)


def test_parser_depends_on_the_error_row_structure():
    """灵敏度：拿掉一个错误码的反引号，错误码解析必须少一个。"""
    text = load_scale_contract_text().replace("| `MT-2001` | 401 |", "| MT-2001 | 401 |")
    parsed = parse_scale_error_codes(text)
    assert len(parsed) == EXPECTED_ERROR_CODE_COUNT - 1, (
        f"拿掉一行错误码的反引号后应解析出 {EXPECTED_ERROR_CODE_COUNT - 1} 个，实际 {len(parsed)}"
    )
    assert len(parse_scale_error_codes()) == EXPECTED_ERROR_CODE_COUNT, "原契约解析结果必须不变"


def test_parser_rejects_a_contract_without_the_error_section():
    """灵敏度：把 §4 的**章节标题换掉**，错误码解析必须报错（同上：必须换成完全不同的标题）。"""
    text = load_scale_contract_text().replace("## 4. 统一错误码表", "## 4. 错误码一览")
    assert "## 4. 统一错误码表" not in text, "破坏不彻底：原标题仍作为子串存在"
    with pytest.raises(ValueError):
        parse_scale_error_codes(text)


# ---------------------------------------------------------------------------
# 双向一致：契约 §2 ↔ Flask 路由表
# ---------------------------------------------------------------------------


def _scale_route_diff(app):
    """只针对**秤端接入前缀**做双向比对：`(契约有而实现无, 实现有而契约无)`。"""
    expected = {e for e in SCALE_CONTRACT_ENDPOINTS if e[1].startswith(SCALE_PREFIX)}
    actual = {e for e in registered_endpoints(app) if e[1].startswith(SCALE_PREFIX)}
    return sorted(expected - actual), sorted(actual - expected)


@pytest.mark.parametrize("method,path", sorted(SCALE_CONTRACT_ENDPOINTS))
def test_every_scale_contract_endpoint_is_registered(seeded_app, method, path):
    """逐条确认：形态 2 契约 §2 的每个端点都必须在实现里注册（`T-SCALE-04`~`T-SCALE-07`）。"""
    registered = registered_endpoints(seeded_app)
    assert (method, path) in registered, (
        f"形态 2 契约 §2 已登记 {method} {path}，但 Flask 路由表里没有 —— "
        f"实现未开工（`T-SCALE-04`~`T-SCALE-07`）或实现漏做"
    )


def test_no_scale_route_outside_the_contract(seeded_app):
    """反向：秤端接入前缀下**不得**出现契约未登记的路由（`RL-1`）。"""
    missing, extra = _scale_route_diff(seeded_app)
    assert not missing, f"形态 2 契约已登记但实现未注册（{len(missing)} 个）：{missing}"
    assert not extra, (
        f"实现已注册但形态 2 契约未登记（{len(extra)} 个，违反 RL-1「规格先于代码」）：{extra}"
    )


# ---------------------------------------------------------------------------
# 错误码：契约 §4 ↔ `app.ERROR_STATUS`（唯一映射点）
# ---------------------------------------------------------------------------


def test_every_scale_error_code_has_a_declared_http_status():
    for code, status in SCALE_CONTRACT_ERROR_CODES.items():
        assert 400 <= status <= 599, f"{code} 的 HTTP 状态非法：{status}"


def test_every_scale_error_code_is_mapped_in_error_status():
    """契约 §4 的每个 `MT-2xxx` 都必须落在 `app.ERROR_STATUS` 里。

    否则错误处理器认不出它，会把它当成"未预期异常"回成 `MT-1014` —— 调用方拿到的码
    与契约写的不一致，等于契约白写。`app/__init__.py` 的 docstring 明文规定
    **错误码 → HTTP 状态的唯一落点就是 `ERROR_STATUS`**。
    """
    from app import ERROR_STATUS

    missing = sorted(c for c in SCALE_CONTRACT_ERROR_CODES if c not in ERROR_STATUS)
    assert not missing, f"形态 2 契约 §4 已登记但 `ERROR_STATUS` 未映射（{len(missing)} 个）：{missing}"

    mismatched = sorted(
        f"{c}: 契约 {SCALE_CONTRACT_ERROR_CODES[c]} vs ERROR_STATUS {ERROR_STATUS[c]}"
        for c in SCALE_CONTRACT_ERROR_CODES
        if c in ERROR_STATUS and ERROR_STATUS[c] != SCALE_CONTRACT_ERROR_CODES[c]
    )
    assert not mismatched, f"契约与 `ERROR_STATUS` 的状态码不一致：{mismatched}"


# ---------------------------------------------------------------------------
# 灵敏度负例（合成路由表：完整 → 加破坏物变红 → 移除恢复绿）
# ---------------------------------------------------------------------------


def _synthetic_scale_app(
    extra: tuple[tuple[str, str], ...] = (), skip: tuple[tuple[str, str], ...] = ()
) -> Flask:
    """构造一个**秤端接入路由与契约完全一致**的合成应用，用于隔离验证检查本身。"""
    app = Flask(f"synthetic-scale-{extra}-{skip}")
    for method, path in sorted(SCALE_CONTRACT_ENDPOINTS - set(skip)):
        rule = normalize_rule(path).replace("{", "<").replace("}", ">")
        app.add_url_rule(
            rule,
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


def test_synthetic_full_route_table_is_green():
    """本检查不是"永远失败"的摆设：完整的合成路由表必须通过。"""
    missing, extra = _scale_route_diff(_synthetic_scale_app())
    assert (missing, extra) == ([], [])


def test_missing_endpoint_turns_the_check_red():
    """灵敏度①：故意漏掉一个契约端点 → 必须变红。"""
    dropped = sorted(SCALE_CONTRACT_ENDPOINTS)[0]
    missing, _ = _scale_route_diff(_synthetic_scale_app(skip=(dropped,)))
    assert dropped in missing, f"漏掉 {dropped} 后未被判为缺失 —— 检查失效"
    # 还原后必须复绿（证明红是破坏物造成的，不是检查本身有问题）
    assert _scale_route_diff(_synthetic_scale_app())[0] == []


def test_phantom_endpoint_turns_the_check_red():
    """灵敏度②：故意注册一个契约外的秤端路由 → 必须变红。"""
    phantom = ("GET", f"{SCALE_PREFIX}v1/phantom")
    _, extra = _scale_route_diff(_synthetic_scale_app(extra=(phantom,)))
    assert phantom in extra, f"注册契约外的 {phantom} 后未被判为多余 —— 检查失效"
    assert _scale_route_diff(_synthetic_scale_app())[1] == []
