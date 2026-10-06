"""秤端交易上报的**载荷校验**与**重算比对**（`T-SCALE-06`；`REQ-038`/`REQ-039`/`REQ-041`）。

唯一权威：`specs/market-trade-flow/contracts/scale-midplatform.md` §1.1（幂等总表）、§3.5（上报）、
§5（上报与重算比对语义）；`spec.md` `REQ-038`（以中台重算结果入账）、`REQ-039`（幂等键）、
`AC-030`（不一致时三件事齐全）、`AC-031`。

四条硬口径（改动前先读）：

1. **账口唯一**：本模块**不写任何记账 SQL** —— 交易与明细一律经
   `app/domain/pricing.py::create_transaction` 落库（`ADR-0005` §3 第 4 条）。
   本模块只做"校验 + 比对 + 留痕 + 组装响应"。
2. **金额不一致不是错误**（契约 §3.5 末注 / §4 末注）：仍返回 2xx，且**三件事一件都不能省** ——
   入账取中台重算值、写 `audit_log`（`event_type = scale_amount_mismatch`）、响应如实返回
   `amount_mismatch` + `mismatch_detail`。把差异做成错误码会逼出"拒绝该笔（违反 `RL-9`）"或
   "秤端自己改金额（账口从两个变成三个）"两个坏选择。
3. **授权只来自设备行**：请求体里的 `stall_id` / `market_id` 只用于**暴露越权**（`MT-2002`），
   绝不作为授权依据；上报的商品也必须属于设备绑定的摊位。
4. **幂等命中返回首次结论**（契约 §3.5 末条）：重复上报**不是**重新比对 —— 否则同一笔在重试
   前后会给出两个说法。首次结论从**首次写入的留痕**读回（不一致必然留痕，见第 2 条），
   命中而查无留痕即"首次是一致的"。

载荷错误码用 `MT-2004`（契约 §4「上报载荷不合法」）而**不是**主契约的 `MT-1002`（重量越界）：
`MT-2002`~`MT-2005` 是秤端接入段的码，§3.5 的"可能错误"里没有 `MT-1002` —— 故重量越界在本段
先被拦成 `MT-2004`，不让它落到 `create_transaction` 的 `MT-1002` 上（那会是本契约之外的错误形态）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import TradeError
from .audit import write_audit
from .catalog import parse_business_date
from .pricing import MAX_ITEMS, MAX_WEIGHT_GRAMS, create_transaction

#: 留痕事件类型（`0004_audit_event_types.sql` 已把它加进 `audit_log.event_type` 白名单）
MISMATCH_EVENT = "scale_amount_mismatch"
#: 幂等键长度上限（契约 §1.3 / `data-model.md` §2.8）
IDEMPOTENCY_KEY_MAX_LEN = 64
#: 秤端 `origin` → `transaction.origin` 枚举（`data-model.md` §2.8；补传落 `backfilled`）
ORIGIN_MAP = {"online": "online", "offline_backfill": "backfilled"}


# ---------------------------------------------------------------------------
# 载荷校验（`MT-2004`）
# ---------------------------------------------------------------------------


def _int_field(value: Any, field: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """整数校验：**排除 `bool`**（Python 里 `True` 是 `int` 的子类，不排除会把布尔当 1 收下）。"""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TradeError("MT-2004", f"`{field}` 必须是整数", {"field": field})
    if minimum is not None and value < minimum:
        raise TradeError("MT-2004", f"`{field}` 必须 ≥{minimum}", {"field": field, "value": value})
    if maximum is not None and value > maximum:
        raise TradeError(
            "MT-2004",
            f"`{field}` 必须 ≤{maximum}",
            {"field": field, "value": value, "max": maximum},
        )
    return value


def _parse_report(body: Any, header_key: str | None) -> dict:
    """校验 §3.5 请求体并归一化；任何不合规都抛 `MT-2004`。"""
    if not isinstance(body, dict):
        raise TradeError("MT-2004", "请求体必须是 JSON 对象")

    key = header_key if header_key else body.get("client_idempotency_key")
    if not isinstance(key, str) or not 1 <= len(key) <= IDEMPOTENCY_KEY_MAX_LEN:
        raise TradeError(
            "MT-2004",
            f"缺少幂等键（请求头 Idempotency-Key，长度 1~{IDEMPOTENCY_KEY_MAX_LEN}）",
            {"header": "Idempotency-Key"},
        )
    body_key = body.get("client_idempotency_key")
    if body_key is not None and body_key != key:
        raise TradeError(
            "MT-2004",
            "请求体 `client_idempotency_key` 与请求头 `Idempotency-Key` 不一致",
            {"field": "client_idempotency_key"},
        )

    # 营业日：复用主契约同一套解析（`catalog.parse_business_date`），但错误码换成秤端段的 `MT-2004`
    try:
        business_date = parse_business_date(body.get("business_date"), field="business_date")
    except TradeError as exc:
        raise TradeError("MT-2004", exc.message, exc.detail) from exc

    origin_raw = body.get("origin", "online")
    if origin_raw not in ORIGIN_MAP:
        raise TradeError(
            "MT-2004",
            "`origin` 必须是 online 或 offline_backfill",
            {"field": "origin", "allowed": sorted(ORIGIN_MAP)},
        )

    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_ITEMS:
        raise TradeError(
            "MT-2004",
            f"`items` 必须是长度 1~{MAX_ITEMS} 的数组",
            {"field": "items", "max_items": MAX_ITEMS},
        )
    items: list[dict] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise TradeError("MT-2004", f"`items[{index}]` 必须是对象", {"index": index})
        product_id = _int_field(item.get("product_id"), f"items[{index}].product_id", minimum=1)
        # 重量 >0 且 ≤50 公斤（`REQ-027`）：本段按 `MT-2004` 拒绝，见模块 docstring
        weight = _int_field(
            item.get("weight_grams"),
            f"items[{index}].weight_grams",
            minimum=1,
            maximum=MAX_WEIGHT_GRAMS,
        )
        items.append({"product_id": product_id, "weight_grams": weight})

    raw_lines = body.get("local_lines")
    if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= MAX_ITEMS:
        raise TradeError(
            "MT-2004",
            f"`local_lines` 必须是长度 1~{MAX_ITEMS} 的数组（秤端本地计价明细，用于定位差异）",
            {"field": "local_lines", "max_items": MAX_ITEMS},
        )
    lines: list[dict] = []
    for index, line in enumerate(raw_lines):
        if not isinstance(line, dict):
            raise TradeError("MT-2004", f"`local_lines[{index}]` 必须是对象", {"index": index})
        lines.append(
            {
                "product_id": _int_field(
                    line.get("product_id"), f"local_lines[{index}].product_id", minimum=1
                ),
                "weight_grams": _int_field(
                    line.get("weight_grams"),
                    f"local_lines[{index}].weight_grams",
                    minimum=1,
                    maximum=MAX_WEIGHT_GRAMS,
                ),
                "unit_price_cents": _int_field(
                    line.get("unit_price_cents"), f"local_lines[{index}].unit_price_cents", minimum=0
                ),
                "amount_cents": _int_field(
                    line.get("amount_cents"), f"local_lines[{index}].amount_cents", minimum=0
                ),
            }
        )

    amount_cents = _int_field(body.get("amount_cents"), "amount_cents", minimum=0)
    if body.get("round_off_cents") is not None:
        _int_field(body["round_off_cents"], "round_off_cents", minimum=0)
    if body.get("price_list_version") is not None:
        _int_field(body["price_list_version"], "price_list_version", minimum=0)
    if body.get("captured_at") is not None and not isinstance(body["captured_at"], str):
        raise TradeError("MT-2004", "`captured_at` 必须是字符串", {"field": "captured_at"})

    return {
        "key": key,
        "business_date": business_date,
        "origin": ORIGIN_MAP[origin_raw],
        "items": items,
        "local_lines": lines,
        "amount_cents": amount_cents,
    }


def _scope_guard(device: dict, body: dict) -> None:
    """请求体里出现 `stall_id` / `market_id` 且与绑定不一致 → `MT-2002`（**不采信**，`REQ-036`）。"""
    for field in ("stall_id", "market_id"):
        if field in body and body[field] is not None and body[field] != device[field]:
            raise TradeError(
                "MT-2002",
                f"请求体 `{field}` 与设备绑定不一致（授权只来自设备令牌）",
                {"field": field, "bound": device[field]},
            )


def _product_guard(conn: sqlite3.Connection, device: dict, items: list[dict]) -> None:
    """上报的商品必须存在、在营、且属于设备绑定的摊位（否则 `MT-2004` / `MT-2002`）。"""
    for item in items:
        row = conn.execute(
            "SELECT stall_id, status FROM product WHERE id = ?", (item["product_id"],)
        ).fetchone()
        if row is None or row["status"] != "active":
            raise TradeError(
                "MT-2004",
                f"商品不存在或已停用：{item['product_id']}",
                {"product_id": item["product_id"]},
            )
        if int(row["stall_id"]) != int(device["stall_id"]):
            raise TradeError(
                "MT-2002",
                "不得上报非本摊位的商品",
                {"product_id": item["product_id"]},
            )


# ---------------------------------------------------------------------------
# 重算比对（契约 §5.2：逐行比对 product_id / weight_grams / unit_price_cents / amount_cents）
# ---------------------------------------------------------------------------


def compare_lines(
    authoritative_items: list[dict],
    reported_lines: list[dict],
    reported_total: int,
    authoritative_total: int,
) -> dict | None:
    """逐行比对；一致返回 `None`，不一致返回 `mismatch_detail`（含逐行差异与两个总额）。

    `authoritative_items` 是 `create_transaction` 落库后的明细（`final_unit_price_cents` 即权威单价）。
    按 `product_id` 配对：任一侧缺行也算差异（缺的那侧给 `null`），**不静默跳过**。
    """
    authoritative = {int(item["product_id"]): item for item in authoritative_items}
    reported = {int(line["product_id"]): line for line in reported_lines}
    diffs: list[dict] = []
    for product_id in sorted(set(authoritative) | set(reported)):
        auth = authoritative.get(product_id)
        rep = reported.get(product_id)
        fields: list[str] = []
        for name, auth_key, rep_key in (
            ("weight_grams", "weight_grams", "weight_grams"),
            ("unit_price_cents", "final_unit_price_cents", "unit_price_cents"),
            ("amount_cents", "amount_cents", "amount_cents"),
        ):
            auth_value = None if auth is None else int(auth[auth_key])
            rep_value = None if rep is None else int(rep[rep_key])
            if auth_value != rep_value:
                fields.append(name)
        if fields:
            diffs.append(
                {
                    "product_id": product_id,
                    "fields": fields,
                    "reported": None
                    if rep is None
                    else {
                        "weight_grams": int(rep["weight_grams"]),
                        "unit_price_cents": int(rep["unit_price_cents"]),
                        "amount_cents": int(rep["amount_cents"]),
                    },
                    "authoritative": None
                    if auth is None
                    else {
                        "weight_grams": int(auth["weight_grams"]),
                        "unit_price_cents": int(auth["final_unit_price_cents"]),
                        "amount_cents": int(auth["amount_cents"]),
                    },
                }
            )
    if not diffs and reported_total == authoritative_total:
        return None
    return {
        "reported_amount_cents": int(reported_total),
        "authoritative_amount_cents": int(authoritative_total),
        "lines": diffs,
    }


def _first_conclusion(conn: sqlite3.Connection, transaction_id: int, authoritative_total: int) -> dict:
    """读回**首次上报**的比对结论（幂等命中专用，契约 §3.5 末条）。

    首次不一致必然写过 `scale_amount_mismatch` 留痕（契约 §5.4 三件事之一），
    故按 `ref_table='transaction'` + `ref_id` 精确回查即可 —— 查不到即"首次是一致的"。
    """
    row = conn.execute(
        "SELECT payload_json FROM audit_log WHERE event_type = ? AND ref_table = 'transaction'"
        " AND ref_id = ? ORDER BY id LIMIT 1",
        (MISMATCH_EVENT, transaction_id),
    ).fetchone()
    if row is None:
        return {
            "amount_mismatch": False,
            "mismatch_detail": None,
            "reported_amount_cents": int(authoritative_total),
        }
    data = json.loads(row["payload_json"])
    return {
        "amount_mismatch": True,
        "mismatch_detail": data.get("mismatch_detail"),
        "reported_amount_cents": int(data.get("reported_amount_cents", authoritative_total)),
    }


# ---------------------------------------------------------------------------
# 入口：上报（在线与补传共用）
# ---------------------------------------------------------------------------


def ingest_transaction(
    conn: sqlite3.Connection,
    device: dict,
    body: Any,
    header_key: str | None,
    *,
    receipt_url_base: str,
) -> tuple[dict, bool]:
    """处理一次 §3.5 上报；返回 `(响应体, 是否新建交易)`。

    `receipt_url_base` 由端点层给出（中台顾客页前缀）—— 领域层不依赖 HTTP 上下文，
    但响应里的 `receipt_url` 必须是**中台**的地址（`REQ-043`：秤端不承载顾客页）。
    """
    report = _parse_report(body, header_key)
    _scope_guard(device, body)
    _product_guard(conn, device, report["items"])

    stall = {"stall_id": int(device["stall_id"])}
    payload, replayed = create_transaction(
        conn,
        stall,
        {
            "items": report["items"],
            "client_idempotency_key": report["key"],
        },
        report["key"],
        origin=report["origin"],
        business_date=report["business_date"],
    )

    authoritative_total = int(payload["total_amount_cents"])
    transaction_id = int(
        conn.execute(
            'SELECT id FROM "transaction" WHERE transaction_no = ?', (payload["transaction_no"],)
        ).fetchone()["id"]
    )

    if replayed:
        conclusion = _first_conclusion(conn, transaction_id, authoritative_total)
    else:
        detail = compare_lines(
            payload["items"], report["local_lines"], report["amount_cents"], authoritative_total
        )
        conclusion = {
            "amount_mismatch": detail is not None,
            "mismatch_detail": detail,
            "reported_amount_cents": int(report["amount_cents"]),
        }
        if detail is not None:
            # 契约 §5.4：不一致必须留痕（含设备号、幂等键、两个金额、逐行差异），原始上报值不得改写
            write_audit(
                conn,
                event_type=MISMATCH_EVENT,
                ref_table="transaction",
                ref_id=transaction_id,
                payload={
                    "device_id": device["device_id"],
                    "idempotency_key": report["key"],
                    "transaction_no": payload["transaction_no"],
                    "authoritative_amount_cents": authoritative_total,
                    "reported_amount_cents": int(report["amount_cents"]),
                    "mismatch_detail": detail,
                },
                actor=str(device["device_id"])[:32],
                stall_id=int(device["stall_id"]),
            )
            conn.commit()

    return (
        {
            "transaction_no": payload["transaction_no"],
            "business_date": payload["business_date"],
            "status": payload["status"],
            # 入账金额**一律**是中台重算值（`authoritative_amount_cents`）
            "authoritative_amount_cents": authoritative_total,
            "reported_amount_cents": conclusion["reported_amount_cents"],
            "amount_mismatch": conclusion["amount_mismatch"],
            "mismatch_detail": conclusion["mismatch_detail"],
            "replayed": replayed,
            "receipt_url": f"{receipt_url_base}/{payload['transaction_no']}",
        },
        not replayed,
    )
