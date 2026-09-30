"""`T-007`：开发期依赖与运行期依赖的**边界**不得被打破（`ADR-0004` §3 第 3 条 + 宪法 §1）。

硬边界（违反即返工）：
1. `pytest` / `coverage` 等开发期工具**不得出现在 `requirements.txt`**；
2. **不得被 `run.py` 或 `app/**` 引用**（含 `import`；机械检查 = 标准库 `ast` 扫描）；
3. `requirements-dev.txt` 是它们的唯一清单。

检查用的是**运行期白名单**（宪法 §1）：`Flask` + Python 标准库 + 本项目自身的 `app` 包。
凡是白名单之外的 import 一律违规 —— 这条比"只盯 `pytest`"更强：`import pytest` 只是违规的一个特例，
换成别的第三方库同样拦得住（否则就是"检查只认识一个词"）。

本文件自带**灵敏度负例**：故意在源码里 `import pytest`、故意往运行期清单塞一行 → 检查必须变红。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from conftest import REPO_ROOT, REQUIREMENTS_DEV, REQUIREMENTS_RUNTIME

#: 运行期依赖白名单（宪法 §1「允许的技术栈」+ `ADR-0004` §3）
RUNTIME_ALLOWED_ROOTS: frozenset[str] = frozenset({"flask", "app"})

#: 开发期工具（仅 `requirements-dev.txt`；用于清单检查与报错措辞）
DEV_TOOL_MODULES: frozenset[str] = frozenset(
    {"pytest", "_pytest", "coverage", "coverage_html", "pluggy", "iniconfig"}
)

#: 动态导入的文本形态（`ast` 看不到 `importlib.import_module("pytest")`，故补一层文本检查）
_DYNAMIC_IMPORT_RE = re.compile(
    r"""(?:importlib\.import_module|__import__)\s*\(\s*['"](?P<name>[A-Za-z_][\w.]*)['"]"""
)


def runtime_sources() -> list[Path]:
    """运行期源码面：`run.py` + `app/**/*.py`（`ADR-0004` §3 第 3 条明文点名）。"""
    return [REPO_ROOT / "run.py", *sorted((REPO_ROOT / "app").rglob("*.py"))]


def _violation(path: Path, lineno: int, name: str) -> str:
    root = name.split(".")[0]
    if root in DEV_TOOL_MODULES:
        return f"{path.name}:{lineno} 运行期源码引用了**开发期工具** `{name}`（ADR-0004 §3）"
    return f"{path.name}:{lineno} 运行期源码引用了白名单之外的依赖 `{name}`（宪法 §1）"


def scan_runtime_imports(paths: list[Path]) -> list[str]:
    """扫描 import（含函数内嵌套 import）与动态导入字符串；返回违规清单（空 = 合规）。"""
    violations: list[str] = []
    stdlib = set(getattr(sys, "stdlib_module_names", frozenset()))
    for path in paths:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 是包内相对导入（app.*），天然在白名单内
                names = [node.module or ""] if node.level == 0 else []
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root and root not in stdlib and root not in RUNTIME_ALLOWED_ROOTS:
                    violations.append(_violation(path, node.lineno, name))
        for match in _DYNAMIC_IMPORT_RE.finditer(text):
            name = match.group("name")
            root = name.split(".")[0]
            if root not in stdlib and root not in RUNTIME_ALLOWED_ROOTS:
                line = text[: match.start()].count("\n") + 1
                violations.append(_violation(path, line, name))
    return violations


def requirement_names(text: str) -> set[str]:
    """解析依赖清单文本 → 规范化后的**包名**集合（去注释、去版本约束、转小写）。"""
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\[\s]", line, maxsplit=1)[0].strip()
        if name:
            names.add(name.lower())
    return names


def dev_tool_violations(names: set[str]) -> list[str]:
    """清单里出现开发期工具即违规。"""
    return sorted(name for name in names if name in DEV_TOOL_MODULES)


# ---------------------------------------------------------------------------
# 第 3 条：运行期源码不得引用开发期工具 / 白名单外依赖
# ---------------------------------------------------------------------------


def test_runtime_sources_import_only_stdlib_and_flask():
    """实测 `run.py` + `app/**`：白名单之外（含 `pytest` / `coverage`）一处都不许有。"""
    targets = runtime_sources()
    assert len(targets) >= 5, f"运行期源码面异常（仅 {len(targets)} 个文件）：{[p.name for p in targets]}"
    violations = scan_runtime_imports(targets)
    assert not violations, "运行期源码越出依赖白名单：\n" + "\n".join(violations)


def _write_module(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_sensitivity_dev_import_at_module_level_turns_check_red(tmp_path):
    """负例①（`T-036` 第③条的同形做法）：故意 `import pytest` → 检查必须变红。"""
    path = _write_module(tmp_path, "fake_run.py", "import pytest\n")
    violations = scan_runtime_imports([path])
    assert violations and "pytest" in violations[0], f"故意 import pytest 未被检出：{violations}"


def test_sensitivity_dev_import_from_and_nested_turn_check_red(tmp_path):
    """负例②：`from pytest import ...`、函数内嵌套 import、`import coverage` 同样必须变红。"""
    nested = _write_module(
        tmp_path,
        "fake_nested.py",
        "def helper():\n    import coverage\n    return coverage\n",
    )
    from_style = _write_module(tmp_path, "fake_from.py", "from pytest import fixture\n")
    assert scan_runtime_imports([nested]), "函数内嵌套 import 未被检出"
    assert scan_runtime_imports([from_style]), "from pytest import ... 未被检出"


def test_sensitivity_dynamic_import_turns_check_red(tmp_path):
    """负例③：`importlib.import_module("pytest")` 这种绕过 ast 的写法也必须变红。"""
    path = _write_module(
        tmp_path, "fake_dynamic.py", 'import importlib\nmod = importlib.import_module("pytest")\n'
    )
    assert scan_runtime_imports([path]), "动态导入未被检出"


def test_sensitivity_non_whitelisted_third_party_turns_check_red(tmp_path):
    """负例④：白名单之外的第三方库（如 `requests`）也必须变红 —— 检查不只认识 `pytest`。"""
    path = _write_module(tmp_path, "fake_third_party.py", "import requests\n")
    assert scan_runtime_imports([path]), "白名单外的第三方依赖未被检出"


def test_sensitivity_removing_breaker_restores_green(tmp_path):
    """负例⑤：移除破坏物（改回标准库 / Flask）→ 必须恢复绿（红-绿闭环）。"""
    path = _write_module(tmp_path, "fake_clean.py", "import json\nimport sqlite3\nfrom flask import Flask\n")
    assert scan_runtime_imports([path]) == []
    path.write_text("import pytest\n", encoding="utf-8")
    assert scan_runtime_imports([path])
    path.write_text("import json\n", encoding="utf-8")
    assert scan_runtime_imports([path]) == []


# ---------------------------------------------------------------------------
# 第 1 / 2 条：两份清单各司其职
# ---------------------------------------------------------------------------


def test_runtime_requirements_has_no_dev_tools():
    """`requirements.txt` 是运行期清单（演示机只装这份）：不得出现开发期工具。"""
    names = requirement_names(REQUIREMENTS_RUNTIME.read_text(encoding="utf-8"))
    assert names, "requirements.txt 为空？"
    violations = dev_tool_violations(names)
    assert not violations, f"运行期依赖清单混入开发期工具：{violations}"
    assert "flask" in names, f"运行期清单缺少 Flask：{sorted(names)}"


def test_dev_requirements_lists_the_dev_tools():
    """`requirements-dev.txt` 必须列出 pytest 与 coverage（`ADR-0004` §3 第 2 条）。"""
    names = requirement_names(REQUIREMENTS_DEV.read_text(encoding="utf-8"))
    assert {"pytest", "coverage"} <= names, f"开发期清单缺失工具：{sorted(names)}"


def test_sensitivity_runtime_requirements_check_red_on_injected_line():
    """负例⑥：故意把 `pytest==9.1.1` 塞进运行期清单内容 → 检查必须变红。"""
    injected = requirement_names("Flask==3.1.3\npytest==9.1.1\n# coverage==7.16.2\n")
    violations = dev_tool_violations(injected)
    assert violations == ["pytest"], f"注入的开发期工具未被检出：{violations}"


def test_dev_tools_are_available_in_this_environment():
    """本机开发环境可用性记录：`pytest` / `coverage` 均可导入（无外网时的回退见 `ADR-0004` §5）。"""
    import coverage  # noqa: F401
    import pytest as _pytest

    assert _pytest.__version__
    assert coverage.__version__
