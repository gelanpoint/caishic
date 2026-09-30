"""门禁阈值读取（`docs/standards/quality-gates.md` 的**唯一**机器入口）。

为什么单独一个模块：阈值不许在测试里复述（门禁文件开头就写明这条纪律），只能**读**；
而 `tests/e2e_support.py` 是端到端夹具（已接近 400 行门禁），门禁阈值是跨层共用的
（`tests/perf/` 也在用），所以放在唯一名的独立模块里 —— 顺带避开"同名 `conftest` 撞车"。
"""

from __future__ import annotations

import re

from e2e_support import REPO_ROOT

#: 阈值文件的唯一权威落点
GATE_FILE = REPO_ROOT / "docs" / "standards" / "quality-gates.md"


def percentile(samples: list[float], fraction: float) -> float:
    """最近秩法取分位数（样本少时也不插值造假：宁可偏高一点）。"""
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round(fraction * len(ordered) + 0.5)) - 1))
    return ordered[index]


def parse_latency_thresholds(text: str | None = None) -> dict:
    """从**权威文件**里机械抽取「接口响应时间」阈值（`quality-gates.md` §1.1）。

    解析失败即断言失败：门禁文件形态变了，检查不该悄悄失效。
    """
    source = text if text is not None else GATE_FILE.read_text(encoding="utf-8")
    row = re.search(r"\|\s*接口响应时间[^|]*\|([^|]*)\|", source)
    assert row, f"未能在 {GATE_FILE.name} 里找到「接口响应时间」阈值行（门禁文件形态变了吗？）"
    cell = row.group(1)
    p95 = re.search(r"p95\s*<\s*([\d.]+)\s*ms", cell)
    p99 = re.search(r"p99\s*<\s*([\d.]+)\s*(ms|s)", cell)
    assert p95 and p99, f"阈值行的写法无法解析：{cell!r}"
    return {
        "p95_ms": float(p95.group(1)),
        "p99_ms": float(p99.group(1)) * (1000 if p99.group(2) == "s" else 1),
        "source": cell.strip(),
    }
