"""`T-036` 灵敏度家族**收口入口**：把"这些检查还能不能失败"集中到一处，一条命令跑完。

## 一键复跑

    python -m pytest tests/contract/test_check_sensitivity.py tests/contract/test_check_sensitivity_content.py -q -s
    （逐条打印「破坏 → 必红 → 还原 → 复绿」；`-s` 才能看到证据行）

本文件是**收口入口**：持有登记表 `FAMILIES` 与守卫，并覆盖**结构面**四家族
（路由表 ↔ 契约 §2 / 运行期依赖隔离 / 统一错误格式 / 未预期异常的兜底 500）；
**内容与数据面**三家族（敏感字段扫描 / 零外部资源 / 指标分子差分）在
`tests/contract/test_check_sensitivity_content.py` —— 守卫会**跨模块**核对它们真实存在。

## 为什么要有这个文件（而不是"以前都验过"）

"检查曾经能失败"不是证据 —— 检查会因为**扫描器退化成永远返回空**、**比对函数被顺手放宽**、
**端点/字段/依赖清单改了而检查没跟着改**而**悄悄变成摆设**。这种变绿是**假绿**：
最能误导人的失败模式就是"CI 全绿，但检查早就不检查了"。
所以每个家族都在这里放一条**随每次 pytest 一起跑的负例**：把真实产物破坏掉，
**必须**判红；还原后**必须**复绿；两侧都断言，缺一侧都不算数。

## 家族登记表（`FAMILIES`）

| id | 来源 | 检查什么 |
| --- | --- | --- |
| `T-007-1` | `T-007` ① | 路由表 ↔ 契约 §2 **双向**比对（多一个/少一个都要红） |
| `T-007-2` | `T-007` ② | 敏感字段扫描（响应体 / 迁移 SQL / 库文件 三入口） |
| `T-007-3` | `T-007` ③ | 运行期依赖隔离（`ast` 扫描 + 依赖清单） |
| `T-007-4` | `T-007` ④ | 统一错误响应格式（契约 §1.2 envelope） |
| `T-029` | `T-029` | 零外部资源（六种形态 + 干净样例 + URL 层解析） |
| `Q-19` | `Q-19` | 使用率指标分子必须**来自真查**（差分灵敏度） |
| `T-034-500` | `T-034` | 未预期异常必须给**契约 §1.2 格式**（`MT-1014`），而不是 Flask 默认 HTML |

`T-034-500` 是压测时实测出来的：未预期异常的响应体是 **HTML**、不符合 §1.2、也没有任何可追溯标识
—— 调用方既看不出这是契约错误，也**不知道这笔交易到底记没记**。
按契约 §5 补 `MT-1014` 后，这个家族把"兜底处理器还在不在、有没有泄漏栈"钉进每次 pytest。
"""

from __future__ import annotations

import shutil
import importlib
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from contract_support import (
    CONTRACT_PATH,
    REPO_ROOT,
    assert_error_response,
    assert_route_table_matches_contract,
    error_envelope_violations,
    json_of,
    parse_error_codes,
    registered_endpoints,
    route_table_diff,
)
from test_deps_isolation import dev_tool_violations, requirement_names, scan_runtime_imports

#: 家族实现所在的模块（`T-036` 按"结构面 vs 内容面"拆成两个文件：
#: 结构面 = 路由表 / 依赖隔离 / 错误格式 / 兜底 500；内容面 = 敏感字段 / 零外部资源 / 指标差分）。
#: 这里存**模块名**而不是直接 import：守卫要能核对"登记的那个函数在那边的模块里真的存在"。
STRUCTURE_MODULE = "test_check_sensitivity"
CONTENT_MODULE = "test_check_sensitivity_content"
KNOWN_MODULES = frozenset({STRUCTURE_MODULE, CONTENT_MODULE})

#: 家族登记：id → (说明, 实现模块, 负例测试函数名)。守卫会核对**登记的函数真实存在**，
#: 于是"把某个家族的负例删掉"会立刻让守卫变红（这是本文件存在的主要理由）。
FAMILIES: dict[str, tuple[str, str, str]] = {
    "T-007-1": (
        "路由表 ↔ 契约 §2 双向比对", STRUCTURE_MODULE,
        "test_family_t007_1_route_table_two_way_red_then_green",
    ),
    "T-007-2": (
        "敏感字段扫描（响应体/迁移 SQL/库文件）", CONTENT_MODULE,
        "test_family_t007_2_sensitive_field_scan_red_then_green",
    ),
    "T-007-3": (
        "运行期依赖隔离（ast 扫描 + 清单）", STRUCTURE_MODULE,
        "test_family_t007_3_runtime_isolation_red_then_green",
    ),
    "T-007-4": (
        "统一错误响应格式（§1.2 envelope）", STRUCTURE_MODULE,
        "test_family_t007_4_error_envelope_red_then_green",
    ),
    "T-029": (
        "静态资源零外部依赖（六形态 + 干净样例）", CONTENT_MODULE,
        "test_family_t029_no_external_assets_red_then_green",
    ),
    "Q-19": (
        "使用率指标分子必须来自真查", CONTENT_MODULE,
        "test_family_q19_metric_numerator_red_then_green",
    ),
    "T-034-500": (
        "未预期异常 → 契约 §1.2 格式（MT-1014）", STRUCTURE_MODULE,
        "test_family_t034_500_fallback_red_then_green",
    ),
}

#: 必须被覆盖的家族全集（`T-036` 的验收口径；少一个即守卫变红）
REQUIRED_FAMILIES = frozenset(FAMILIES)


def test_registry_covers_every_family_and_each_negative_exists():
    """守卫：登记表非空、每个家族都有**真实存在**的负例函数、且没有多余登记。

    注意这里查的是**跨模块**的存在性（本文件与 `..._content.py`）：
    拆文件之后，"负例被删掉"既可能是本文件被改，也可能是被搬走后再被删 —— 两种都必须变红。
    """
    assert set(FAMILIES) == REQUIRED_FAMILIES
    for family, (description, module, negative) in FAMILIES.items():
        assert description.strip(), f"{family} 缺说明"
        assert module in KNOWN_MODULES, f"{family} 登记的模块不在已知集合里：{module}"
        target = importlib.import_module(module)
        assert callable(getattr(target, negative, None)), (
            f"{family} 登记的负例 `{module}.{negative}` 不存在 —— 检查被删了/改名了，"
            "而登记表还在说它被覆盖了（这正是「假绿」的入口）"
        )
    print(f"[收口] 已登记 {len(FAMILIES)} 个灵敏度家族：" + "、".join(sorted(FAMILIES)))


# ---------------------------------------------------------------------------
# T-007-1 路由表双向比对
# ---------------------------------------------------------------------------


def test_family_t007_1_route_table_two_way_red_then_green():
    """破坏：给**真实应用实例**注册一个契约里没有的端点 → 双向比对必红；新起实例 → 复绿。"""
    from app import create_app

    def fresh_app():
        # 用**真实工厂**新起实例：还原不需要去改 Flask 内部结构（`url_map` 是内部对象），
        # 也就不会把"破坏"泄漏给同模块的其他用例（契约 conftest 的 seeded_app 是 session 作用域）。
        return create_app()

    green = fresh_app()
    assert_route_table_matches_contract(green)
    print(f"[T-007-1] 破坏前：真实应用路由表与契约 §2 完全一致（{len(registered_endpoints(green))} 条）")

    broken = fresh_app()
    broken.add_url_rule("/api/sensitivity-phantom", endpoint="sensitivity_phantom", view_func=lambda: ("{}", 200))
    with pytest.raises(AssertionError) as failure:
        assert_route_table_matches_contract(broken)
    assert "/api/sensitivity-phantom" in str(failure.value), f"红是红了，但没指出多出来的端点：{failure.value}"
    missing, extra = route_table_diff(broken)
    assert ("GET", "/api/sensitivity-phantom") in extra and not missing, f"双向比对的返回不对：{missing} {extra}"
    print(f"[T-007-1] 破坏后：多出端点被抓住 → {extra}")

    assert_route_table_matches_contract(fresh_app())
    print("[T-007-1] 还原后：复绿")


# ---------------------------------------------------------------------------
# T-007-3 运行期依赖隔离
# ---------------------------------------------------------------------------


def test_family_t007_3_runtime_isolation_red_then_green(tmp_path):
    """破坏：在**真实 run.py 的副本**里 `import pytest` → 必红；去掉 → 复绿。

    为什么用副本：真文件是运行期源码，测试去改它会让"顺手忘了还原"变成产品事故。
    副本取自真实文件（不是手写的假样例），所以"注入了什么、扫描器看没看见"仍是真实链路。
    """
    copy = tmp_path / "run.py"
    shutil.copyfile(REPO_ROOT / "run.py", copy)
    assert scan_runtime_imports([copy]) == []

    copy.write_text("import pytest\n" + copy.read_text(encoding="utf-8"), encoding="utf-8")
    violations = scan_runtime_imports([copy])
    assert violations, "`import pytest` 没被判红 —— 依赖隔离检查是摆设"
    assert "开发期工具" in violations[0], f"判红了但归类不对：{violations[0]}"
    print(f"[T-007-3] 破坏：{violations[0]}")

    copy.write_text(copy.read_text(encoding="utf-8").replace("import pytest\n", "", 1), encoding="utf-8")
    assert scan_runtime_imports([copy]) == []
    print("[T-007-3] 还原后：复绿")

    # 另一半：依赖清单（真实清单 vs 注入后的清单）
    real_requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert dev_tool_violations(requirement_names(real_requirements)) == []
    injected = real_requirements + "\npytest==8.3.4\n"
    hits = dev_tool_violations(requirement_names(injected))
    assert hits, "运行期清单里注入 pytest 没被判红"
    print(f"[T-007-3] 清单入口：真实清单零违规；注入 pytest → {hits[0]}")


# ---------------------------------------------------------------------------
# T-007-4 统一错误响应格式
# ---------------------------------------------------------------------------


def test_family_t007_4_error_envelope_red_then_green(client):
    """破坏：把错误体换成 HTML / 少字段 / 用契约外错误码 → 必红；真实错误响应 → 复绿。"""
    contract_codes = parse_error_codes()
    assert contract_codes, "契约 §4 的错误码表一个都没解析出来（解析器或契约形态变了）"

    good = {"error": {"code": "MT-1009", "message": "不存在", "detail": {"path": "/nope"}}}
    assert error_envelope_violations(good, 404, contract_codes) == []

    html = "<!doctype html><html><title>500 Internal Server Error</title></html>"
    assert error_envelope_violations(html, 500, contract_codes), "HTML 错误体没被判红"
    print("[T-007-4] 破坏①：HTML 错误体（Flask 默认 500 的形态）被判红")

    missing_code = {"error": {"message": "没有 code"}}
    assert error_envelope_violations(missing_code, 422, contract_codes), "缺 `code` 的体没被判红"
    print("[T-007-4] 破坏②：缺 `code` 字段被判红")

    off_contract = {"error": {"code": "MT-9999", "message": "契约外的码"}}
    violations = error_envelope_violations(off_contract, 422, contract_codes)
    assert violations, "契约外错误码没被判红（那 §4 的表就白写了）"
    print(f"[T-007-4] 破坏③：契约外错误码被判红 → {violations[0]}")

    # 复绿：真实应用的 404（走真实路由链路，不是合成体）
    response = client.get("/api/sensitivity-no-such-path")
    payload = json_of(response)
    assert response.status_code == 404, f"契约 §4 期望 404，实际 {response.status_code}"
    assert_error_response(response, "MT-1009")
    assert error_envelope_violations(payload, response.status_code, contract_codes) == []
    print("[T-007-4] 还原后：真实 404 响应符合 §1.2 envelope，复绿")


# ---------------------------------------------------------------------------
# T-034-500 未预期异常 → 契约 §1.2 格式（MT-1014）
# ---------------------------------------------------------------------------


def _fallback_violations(response, contract_codes: dict) -> list[str]:
    """检查"未预期异常"的响应是否合规；返回问题清单（空 = 合规）。

    检查项就是这条修复的验收口径：**契约格式**（不是 HTML）、状态 500、码是 `MT-1014`、
    带 `request_id`（现场能拿它与日志对上）、**不泄漏实现细节**（栈/路径/异常原文都不该出现）。
    """
    problems: list[str] = []
    text = response.get_data(as_text=True)
    if response.status_code != 500:
        problems.append(f"未预期异常应以 500 返回，实际 {response.status_code}")
    for leak in ("Traceback", "site-packages", "RuntimeError", "sqlite3."):
        if leak in text:
            problems.append(f"响应体泄漏了实现细节（出现 {leak!r}）—— 栈只该进日志")
    if not response.is_json:
        return problems + ["响应体不是 JSON（Flask 默认 500 就是 HTML，调用方无从判断结果）"]
    payload = response.get_json()
    problems.extend(error_envelope_violations(payload, response.status_code, contract_codes))
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict) or error.get("code") != "MT-1014":
        problems.append(f"错误码应为 MT-1014（契约 §4 的通用 5xx），实际 {error}")
    elif not (error.get("detail") or {}).get("request_id"):
        problems.append("缺少 `detail.request_id`（现场没法把截图与日志里的 traceback 对上）")
    return problems


def test_family_t034_500_fallback_red_then_green():
    """破坏：撤掉兜底异常处理器（= 修复没上线的状态）→ 必红；新起未破坏的实例 → 复绿。

    这里**真的把处理器摘掉再跑一遍**（不是拿一段假 HTML 冒充）：撤掉之后 Flask 回到
    默认 500 —— 响应体是 HTML、不符合 §1.2，检查必须抓住它。
    """
    from app import create_app

    contract_codes = parse_error_codes()

    def probe(broken: bool):
        app = create_app()

        @app.get("/api/sensitivity-boom")
        def boom():
            raise RuntimeError("故意抛一个没人声明的异常（灵敏度演练）")

        if broken:
            # Flask 把"按异常类注册"的处理器放在 `error_handler_spec[None][None][<类>]`。
            # 摘掉 `Exception` 那一格 = 回到"修复没上线"的状态（只动本测试**自建**的实例）。
            # 摘不到就**报错**（不能因为 Flask 内部结构变了就默认这条负例通过）。
            removed = app.error_handler_spec[None][None].pop(Exception, None)
            assert removed is not None, (
                "没能摘掉兜底处理器（Flask 的处理器表结构变了？）—— 负例必须重写，不能默认通过"
            )
        return app.test_client().get("/api/sensitivity-boom")

    good = probe(broken=False)
    assert _fallback_violations(good, contract_codes) == [], (
        f"修复在位时未预期异常仍不合规：{good.get_data(as_text=True)[:200]}"
    )
    print(f"[T-034-500] 修复在位：未预期异常 → HTTP {good.status_code} + "
          f"{good.get_json()['error']['code']}（契约格式，带 request_id）")

    bad = probe(broken=True)
    problems = _fallback_violations(bad, contract_codes)
    assert problems, "把兜底处理器撤掉后居然还判合规 —— 这条检查是摆设"
    assert any("不是 JSON" in item for item in problems), f"破坏后没抓住 HTML 响应：{problems}"
    print(f"[T-034-500] 破坏（撤掉兜底处理器）→ 必红：{problems[0]}")

    again = probe(broken=False)
    assert _fallback_violations(again, contract_codes) == [], "还原后没有复绿"
    print("[T-034-500] 还原后：复绿")
