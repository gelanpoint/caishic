"""`T-SIM-07`：端点清单与覆盖记账的机械检查（`docs/sim-design.md` §2.3 / §8 `T-SIM-07` 判据①）。

## 为什么清单要"双向逐条比对"而不是"数一下有多少条"

`sim/bridge/live_adapter.py` 里内置了一份契约 §2 端点总表的副本（`ENDPOINTS`）。
如果只断言"有 N 条"，那么**契约加了端点而 sim 没跟**、或者 **sim 里那一条路径写错了**，
两边都会绿 —— 那正是 `T-036` 家族反复出现的"检查退化成摆设"。

所以这里做两件事：

1. **把契约 §2 表解析出来**（Markdown 表格，标准库正则即可），与 `ENDPOINTS` 双向逐条比对
   （条数、顺序、方法、路径全等）；任一侧多/少/改都判红；
2. **覆盖记账的灵敏度负例**：`match_endpoint` / `coverage_problems` 都是纯函数，
   合成负例可以直接喂 —— "漏一个端点必须变红"由 `test_coverage_gap_is_red` 盯住。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from sim_support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))

from sim.bridge.live_adapter import (  # noqa: E402
    ENDPOINT_KEYS,
    ENDPOINTS,
    Coverage,
    coverage_problems,
    match_endpoint,
)

CONTRACT = REPO_ROOT / "specs" / "market-trade-flow" / "contracts" / "rest-api.md"

#: 契约 §2 端点总表的一行：`| 1 | `/healthz` | GET | 说明 | 关联需求 |`
_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|\s*(GET|POST|PUT)\s*\|")


def contract_endpoints() -> list[tuple[str, str]]:
    """从契约 §2 表抽出 `[(方法, 路径)]`（按契约顺序）。

    **刻意解析 Markdown 而不是 import 什么**：契约文档不是运行期依赖，
    它只用来做"两边清单一致"的比对；解析失败会直接抛错（不会静默当成"没有端点"）。
    """
    rows: list[tuple[int, str, str]] = []
    in_table = False
    for line in CONTRACT.read_text(encoding="utf-8").splitlines():
        if line.startswith("## 2."):
            in_table = True
            continue
        if in_table and line.startswith("## 3."):
            break
        if not in_table:
            continue
        match = _ROW_RE.match(line.strip())
        if match:
            rows.append((int(match.group(1)), match.group(2), match.group(3)))
    if not rows:
        raise AssertionError(f"没从 {CONTRACT} 的 §2 表里解析出任何端点 —— 判据不能靠'解析失败当通过'")
    if [index for index, _path, _method in rows] != list(range(1, len(rows) + 1)):
        raise AssertionError("契约 §2 表的端点编号不连续 —— 比对的前提是契约本身可信")
    return [(method, path) for _index, path, method in rows]


def test_sim_manifest_matches_contract_both_ways():
    """`sim` 内置的 32 个模板 ↔ 契约 §2 表：**双向逐条相等**（顺序也算）。"""
    expected = contract_endpoints()
    assert len(ENDPOINTS) == 32, f"契约是 32 个端点，sim 里内置了 {len(ENDPOINTS)} 个"
    assert list(ENDPOINTS) == expected, (
        "sim 内置的端点清单与契约 §2 表不一致：\n"
        f"  仅 sim 有：{[e for e in ENDPOINTS if e not in expected]}\n"
        f"  仅契约有：{[e for e in expected if e not in ENDPOINTS]}\n"
        f"  顺序不同处：{[i for i, (a, b) in enumerate(zip(ENDPOINTS, expected)) if a != b][:5]}"
    )
    assert len(set(ENDPOINT_KEYS)) == 32, "端点键有重复 —— 覆盖清单会把两个端点记成同一个"
    print(f"[T-SIM-07] 端点清单与契约 §2 表双向逐条一致：{len(ENDPOINTS)} 条")


def test_match_endpoint_is_derived_from_the_real_path():
    """**路径反推**：真实请求路径 → 契约端点键（正例 + 反向负例）。"""
    assert match_endpoint("GET", "/healthz") == "GET /healthz"
    assert match_endpoint("POST", "/api/merchant/transactions/T-20261001-0001/payment") == (
        "POST /api/merchant/transactions/{transaction_no}/payment"
    )
    assert match_endpoint("GET", "/api/customer/receipts/T-1?x=1") == "GET /api/customer/receipts/{transaction_no}"
    # 反向负例：方法错 / 路径不存在 / 多出一段 —— 三种都必须匹配不到（否则覆盖会被记歪）
    assert match_endpoint("POST", "/healthz") is None
    assert match_endpoint("GET", "/api/admin/commission-rules/extra") is None
    assert match_endpoint("GET", "/api/not-in-contract") is None


def test_coverage_counts_only_what_was_actually_called():
    """覆盖清单只记**真发过的请求**（不调用的端点不得出现在清单里）。"""
    coverage = Coverage()
    coverage.record("GET", "/healthz", 200)
    coverage.record("GET", "/api/admin/reconciliation?business_date=2026-10-01", 200)
    snapshot = coverage.as_dict()
    assert snapshot["covered"] == ["GET /api/admin/reconciliation", "GET /healthz"]
    assert snapshot["covered_count"] == 2
    assert snapshot["missing"], "没调用的端点必须出现在缺口清单里"
    assert coverage_problems(snapshot), "覆盖不全必须报问题"


def test_coverage_gap_is_red():
    """**灵敏度负例**：少调一个端点 ⇒ `coverage_problems` 必红（判据①能失败）。"""
    coverage = Coverage()
    for method, path in ENDPOINTS:
        coverage.record(method, path, 200)
    full = coverage.as_dict()
    assert coverage_problems(full) == [], f"全覆盖时不该报问题：{coverage_problems(full)}"

    # 少调一个（删掉结算单查询）
    dropped = {**full, "covered": [k for k in full["covered"] if k != "GET /api/admin/settlements"]}
    problems = coverage_problems(dropped)
    assert problems and "端点覆盖缺口" in problems[0], f"少调一个端点必须判红：{problems}"

    # 请求打到了契约外的端点 ⇒ 也必须判红（不许悄悄丢）
    stray = {**full, "unmatched": [{"method": "POST", "path": "/api/admin/whoops", "status": 200}]}
    assert any("匹配不到任何契约端点" in item for item in coverage_problems(stray))

    # 清单里混进契约外端点 ⇒ 也必须判红
    polluted = {**full, "covered": [*full["covered"], "POST /api/admin/whoops"]}
    assert any("契约外的端点" in item for item in coverage_problems(polluted))


def test_unmatched_request_is_kept_not_dropped():
    """匹配不到模板的请求必须**留痕**，不能被丢掉（丢了就等于"没发生过"）。"""
    coverage = Coverage()
    coverage.record("POST", "/api/admin/whoops", 200)
    snapshot = coverage.as_dict()
    assert snapshot["unmatched"] == [{"method": "POST", "path": "/api/admin/whoops", "status": 200}]
    assert snapshot["total_requests"] == 1
