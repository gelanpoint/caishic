#!/usr/bin/env python3
"""从 Python 权威计价实现导出 golden vectors（`T-SCALE-09` / `REQ-038` / `AC-029`）。

本脚本是「两份实现漂移」风险的对冲 b（`ADR-0006` §3.2）：它把 `app/domain/pricing.py`
的**实际源码**执行起来，用同一份实现算出向量集，产物交给 C 侧 `scale-fw/test/test_pricing.c`
逐条比对。向量文件因此是**交叉实现的可执行契约**，不是手抄的期望值。

三条使用约定：

1. **产物可复算**：删掉 `scale-fw/test/vectors/pricing_golden.json` 后重跑本脚本，
   必须逐字节还原（生成过程不含时间戳、不含随机源 —— 伪随机序列用脚本内自带的 LCG，
   不依赖 `random` 模块的实现版本）。
2. **`--check` 只比对不写入**：与现有产物不一致时退出码 1，可用于门禁。
3. **`--authority` 可指向权威实现的副本**：灵敏度实验用它把 Python 侧口径改一个分位，
   观察 C 侧 `test_pricing` 变红；**不需要**改动仓库里的 `app/domain/pricing.py`。

用法：
    .venv/bin/python scripts/gen_pricing_vectors.py            # 生成（覆盖产物）
    .venv/bin/python scripts/gen_pricing_vectors.py --check    # 比对不写入
    .venv/bin/python scripts/gen_pricing_vectors.py --authority /tmp/mutated.py --out /tmp/v.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUTHORITY = REPO_ROOT / "app" / "domain" / "pricing.py"
DEFAULT_OUT = REPO_ROOT / "scale-fw" / "test" / "vectors" / "pricing_golden.json"

FORMAT = "caishic-scale-pricing-golden/v1"
DO_NOT_EDIT = "本文件是产物：由 scripts/gen_pricing_vectors.py 生成，禁止手改（AC-029）"

# 权威侧 `change_price` 需要的**最小**表结构（只为让真实写路径跑起来，不是产品 schema）。
_SCHEMA = """
CREATE TABLE "transaction"(id INTEGER PRIMARY KEY, transaction_no TEXT, stall_id INT,
  status TEXT, round_off_cents INT DEFAULT 0, total_amount_cents INT DEFAULT 0, updated_at TEXT);
CREATE TABLE transaction_item(id INTEGER PRIMARY KEY, transaction_id INT,
  original_unit_price_cents INT, final_unit_price_cents INT, weight_grams INT,
  amount_cents INT, price_changed INT DEFAULT 0, is_round_off INT DEFAULT 0);
CREATE TABLE audit_log(id INTEGER PRIMARY KEY, event_type TEXT, stall_id INT, ref_table TEXT,
  ref_id INT, payload_json TEXT, actor TEXT, occurred_at TEXT);
"""


def load_authority(path: Path):
    """在 `app.domain` 包上下文里执行**指定文件**的源码，返回模块对象。

    必须是包上下文：`pricing.py` 用相对导入（`from .. import TradeError`）。
    用 `spec_from_file_location` 的**文件路径**可以指向任意副本，故灵敏度实验无需改动原文件。
    """
    sys.path.insert(0, str(REPO_ROOT))
    import app  # noqa: F401  （建立 `app` 包，供 `from .. import` 解析）
    import app.domain  # noqa: F401

    name = "app.domain._golden_authority"
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载权威实现：{path}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "app.domain"
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def lcg(seed: int):
    """脚本内自带的最小伪随机序列（Numerical Recipes LCG）——与 Python 版本无关，可复现。"""
    state = seed & 0xFFFFFFFF
    while True:
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        yield state


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute(
        'INSERT INTO "transaction"(id, transaction_no, stall_id, status, round_off_cents) VALUES(1, "T-GOLD-1", 1, "priced", 0)'
    )
    conn.commit()
    return conn


def build_half_up_vectors(pricing) -> list[dict]:
    pairs = [
        (0, 1000), (1, 1000), (499, 1000), (500, 1000), (501, 1000), (999, 1000),
        (1000, 1000), (1499, 1000), (1500, 1000), (2500, 1000), (999999, 1000),
        (1, 3), (2, 3), (3, 3), (5, 3), (7, 2), (8, 2),
        # 负数：钉住「与 Python // 同义（向下取整）」——C 的截断除法在这里会给出不同结果
        (-1, 1000), (-500, 1000), (-501, 1000), (-1000, 1000), (-1500, 1000),
        (-2500, 1000), (-1499, 1000), (-3, -2),
    ]
    out = []
    for index, (numerator, denominator) in enumerate(pairs, start=1):
        out.append({
            "id": f"half_up-{index:03d}",
            "kind": "half_up",
            "numerator": numerator,
            "denominator": denominator,
            "quotient": pricing.half_up_div(numerator, denominator),
        })
    return out


def build_amount_vectors(pricing) -> list[dict]:
    max_weight = pricing.MAX_WEIGHT_GRAMS
    pairs = [
        # 契约 §3.5 的样例（`contracts/scale-midplatform.md`）
        (320, 780),
        # 0 克 / 1 克边界
        (320, 0), (320, 1), (500, 1), (499, 1), (1, 1),
        # 半值四舍五入（恰为 .5 分：加半个除数后进位，不是银行家舍入）
        (1, 500), (3, 500), (5, 100), (7, 100), (1, 499), (2, 250),
        # 50 公斤边界（REQ-027 上限，取自权威实现的 MAX_WEIGHT_GRAMS）
        (320, max_weight), (1, max_weight), (0, max_weight), (9999, max_weight),
        # 常见市场价
        (198, 1230), (160, 250), (250, 1000), (1200, 750), (88, 330),
        (65535, 1), (100000, 999),
    ]
    gen = lcg(20261006)
    for _ in range(30):
        unit = next(gen) % 200000
        weight = next(gen) % (max_weight + 1)
        pairs.append((unit, weight))
    out = []
    for index, (unit_price_cents, weight_grams) in enumerate(pairs, start=1):
        out.append({
            "id": f"amount-{index:03d}",
            "kind": "amount",
            "unit_price_cents": unit_price_cents,
            "weight_grams": weight_grams,
            "amount_cents": pricing.price_amount(unit_price_cents, weight_grams),
        })
    return out


def build_total_vectors(pricing) -> list[dict]:
    """合计与抹零：`total_cents` 由权威 `_recompute_total` 在最小库上真实算出。"""
    cases = [
        ([250], 0),
        ([250, 130], 0),
        ([250, 130], 30),
        ([250, 130], 380),      # 抹零等于合计 → 0
        ([250, 130], 500),      # 抹零超过合计 → 0（max(0, ...)）
        ([1], 1),
        ([99999, 1, 1], 0),
        ([1200, 340, 78], 18),
        ([0, 0], 0),
    ]
    out = []
    for index, (line_amounts, round_off) in enumerate(cases, start=1):
        conn = make_conn()
        conn.execute('UPDATE "transaction" SET round_off_cents = ? WHERE id = 1', (round_off,))
        for amount in line_amounts:
            conn.execute(
                "INSERT INTO transaction_item(transaction_id, original_unit_price_cents, "
                "final_unit_price_cents, weight_grams, amount_cents) VALUES(1, 100, 100, 1000, ?)",
                (amount,),
            )
        conn.commit()
        total = pricing._recompute_total(conn, 1)
        items_total = sum(line_amounts)
        assert total == max(0, items_total - round_off), "权威 _recompute_total 与预期口径不符"
        out.append({
            "id": f"total-{index:03d}",
            "kind": "total",
            "line_amounts": list(line_amounts),
            "round_off_cents": round_off,
            "items_total_cents": items_total,
            "total_cents": total,
        })
    return out


def _seed_item(conn, pricing, unit_price_cents: int, weight_grams: int) -> None:
    """预置一条明细行。

    金额**必须**用权威 `price_amount` 算 —— 抹零向量不经过改价、其 `items_total_cents`
    直接取自这一行的 `amount_cents`；若这里用截断除法（`//1000`）预置，
    向量里的合计就不是真实流水会出现的值（`(320, 780)` 截断得 249 而权威得 250）。
    """
    amount = pricing.price_amount(unit_price_cents, weight_grams)
    conn.execute(
        "INSERT INTO transaction_item(id, transaction_id, original_unit_price_cents, "
        "final_unit_price_cents, weight_grams, amount_cents) VALUES(1, 1, ?, ?, ?, ?)",
        (unit_price_cents, unit_price_cents, weight_grams, amount),
    )
    conn.commit()


def build_price_change_vectors(pricing) -> list[dict]:
    """改价：金额与总额由权威 `change_price` 真实算出，标价标记从库里读回。"""
    threshold = pricing.CONFIRM_THRESHOLD_PERCENT
    cases = [
        (320, 320, 780),    # 不改
        (320, 300, 780),    # 小幅下调
        (320, 1000, 780),   # 超阈值（需确认）
        (320, 480, 780),    # 恰好 +50% → 不需确认（判据是严格大于）
        (320, 481, 780),    # 略超 +50% → 需确认
        (320, 160, 780),    # 恰好 −50% → 不需确认
        (320, 159, 780),    # 略超 −50% → 需确认
        (320, 0, 780),      # 改到 0
        (0, 500, 780),      # 原价为 0：判据被 original > 0 短路
        (198, 250, 1230),
    ]
    out = []
    for index, (original, final, weight) in enumerate(cases, start=1):
        conn = make_conn()
        _seed_item(conn, pricing, original, weight)
        stall = {"stall_id": 1, "stall_no": "A-012"}
        body = {"item_id": 1, "final_unit_price_cents": final}
        needs_confirm = False
        try:
            pricing.change_price(conn, stall, "T-GOLD-1", dict(body))
        except Exception as exc:  # TradeError(MT-1011) 即「需确认」；其它异常不允许出现
            if getattr(exc, "code", None) != "MT-1011":
                raise
            needs_confirm = True
        expected_confirm = original > 0 and abs(final - original) * 100 > original * threshold
        assert needs_confirm == expected_confirm, "权威 MT-1011 判据与阈值口径不符"
        if needs_confirm:
            pricing.change_price(conn, stall, "T-GOLD-1", dict(body, confirm_over_threshold=True))
        item = conn.execute(
            "SELECT amount_cents, price_changed, is_round_off FROM transaction_item WHERE id = 1"
        ).fetchone()
        txn = conn.execute('SELECT total_amount_cents FROM "transaction" WHERE id = 1').fetchone()
        # 权威侧不变量：改价计入标价一致率（price_changed=1），且不是抹零
        assert item["price_changed"] == 1 and item["is_round_off"] == 0, "改价标记语义变了"
        out.append({
            "id": f"price_change-{index:03d}",
            "kind": "price_change",
            "original_unit_price_cents": original,
            "final_unit_price_cents": final,
            "weight_grams": weight,
            "final_amount_cents": item["amount_cents"],
            "needs_confirm": needs_confirm,
            "total_cents": txn["total_amount_cents"],
        })
    return out


def build_round_off_vectors(pricing) -> list[dict]:
    """抹零：只减总额、**不计入**标价一致率（`REQ-008` / `AC-008`），标记从库里读回。"""
    cases = [
        (320, 780, 50),
        (320, 780, 0),
        (320, 780, 250),
        (320, 780, 999),    # 超过合计 → 总额 0
        (198, 1230, 4),
        (500, 1000, 1),
    ]
    out = []
    for index, (unit_price, weight, round_off) in enumerate(cases, start=1):
        conn = make_conn()
        _seed_item(conn, pricing, unit_price, weight)
        stall = {"stall_id": 1, "stall_no": "A-012"}
        pricing.change_price(conn, stall, "T-GOLD-1", {"item_id": 1, "round_off_cents": round_off})
        item = conn.execute(
            "SELECT amount_cents, price_changed, is_round_off FROM transaction_item WHERE id = 1"
        ).fetchone()
        txn = conn.execute('SELECT total_amount_cents FROM "transaction" WHERE id = 1').fetchone()
        # 权威侧不变量：抹零不计入标价一致率（price_changed=0），且被标记为抹零
        assert item["price_changed"] == 0 and item["is_round_off"] == 1, "抹零标记语义变了"
        assert item["amount_cents"] == pricing.price_amount(unit_price, weight), "抹零行的金额必须由权威计价产生"
        out.append({
            "id": f"round_off-{index:03d}",
            "kind": "round_off",
            "line_amounts": [item["amount_cents"]],
            "round_off_cents": round_off,
            "items_total_cents": item["amount_cents"],
            "total_cents": txn["total_amount_cents"],
        })
    return out


def build_boundary_vectors(pricing) -> list[dict]:
    """重量上限与明细行数上限：判据的阈值取自权威实现，不在 C 侧硬编码。"""
    max_weight = pricing.MAX_WEIGHT_GRAMS
    max_items = pricing.MAX_ITEMS
    out = []
    for index, weight in enumerate([0, 1, 2, max_weight - 1, max_weight, max_weight + 1], start=1):
        out.append({
            "id": f"weight_range-{index:03d}",
            "kind": "weight_range",
            "weight_grams": weight,
            "max_weight_grams": max_weight,
            "priceable": 0 < weight <= max_weight,
        })
    for index, count in enumerate([0, 1, 2, max_items - 1, max_items, max_items + 1], start=1):
        out.append({
            "id": f"items_count-{index:03d}",
            "kind": "items_count",
            "item_count": count,
            "max_items": max_items,
            "ok": 1 <= count <= max_items,
        })
    return out


def build_document(authority_path: Path) -> dict:
    pricing = load_authority(authority_path)
    vectors: list[dict] = []
    vectors += build_half_up_vectors(pricing)
    vectors += build_amount_vectors(pricing)
    vectors += build_total_vectors(pricing)
    vectors += build_price_change_vectors(pricing)
    vectors += build_round_off_vectors(pricing)
    vectors += build_boundary_vectors(pricing)

    counts: dict[str, int] = {}
    for vector in vectors:
        counts[vector["kind"]] = counts.get(vector["kind"], 0) + 1

    try:
        authority_label = str(authority_path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        authority_label = str(authority_path.resolve())

    return {
        "format": FORMAT,
        "do_not_edit": DO_NOT_EDIT,
        "generated_by": "scripts/gen_pricing_vectors.py",
        "authority": authority_label,
        "authority_sha256": hashlib.sha256(authority_path.read_bytes()).hexdigest(),
        "formula": "amount_cents = half_up_div(unit_price_cents * weight_grams, 1000)",
        "rounding": "half_up_not_bankers",
        "limits": {
            "max_items": pricing.MAX_ITEMS,
            "max_weight_grams": pricing.MAX_WEIGHT_GRAMS,
            "confirm_threshold_percent": pricing.CONFIRM_THRESHOLD_PERCENT,
        },
        "counts": counts,
        "vector_count": len(vectors),
        "vectors": vectors,
    }


def render(document: dict) -> bytes:
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False)
    return (text + "\n").encode("utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="从 Python 权威计价实现导出 golden vectors")
    parser.add_argument("--authority", type=Path, default=DEFAULT_AUTHORITY,
                        help="权威实现路径（默认 app/domain/pricing.py；可指向副本做灵敏度实验）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="产物路径")
    parser.add_argument("--check", action="store_true", help="只与现有产物比对，不写入")
    args = parser.parse_args(argv)

    if not args.authority.is_file():
        print(f"[FAIL] 权威实现不存在：{args.authority}", file=sys.stderr)
        return 2

    payload = render(build_document(args.authority))

    if args.check:
        if not args.out.is_file():
            print(f"[FAIL] 产物不存在：{args.out}（复算失败：删掉产物后必须能还原）", file=sys.stderr)
            return 1
        existing = args.out.read_bytes()
        if existing == payload:
            print(f"[OK] {args.out} 与权威实现复算结果逐字节一致（{len(payload)} 字节）")
            return 0
        print(f"[FAIL] {args.out} 与权威实现复算结果不一致"
              f"（现有 {len(existing)} 字节 / 复算 {len(payload)} 字节）", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(payload)
    document = json.loads(payload.decode("utf-8"))
    print(f"[OK] 已生成 {args.out}：{document['vector_count']} 条向量，"
          f"authority_sha256={document['authority_sha256'][:12]}…，分类 {document['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
