"""多智能体仿真包（`docs/sim-design.md`）。

**两条不可协商的边界**（由 `tests/sim/` 的机械检查强制）：

1. **只依赖 Python 标准库** —— 不新增运行期依赖，`ADR-0004` 的两期边界不变；
2. **唯一跨侧接口是 HTTP 契约的 38 个端点** —— `sim/**` **不得** `import app`。

第 2 条不是洁癖：若直接调领域层函数，`--mode=live` 证明的就只是"领域函数能跑"，
端点、会话鉴权、错误码、幂等、日聚合这些契约行为**全部被绕过**，
"这套设计真的能跑"这句话就失去了证据。
"""

from __future__ import annotations

__all__ = ["SCHEMA_VERSION"]

SCHEMA_VERSION = 1
