"""佣金口径与按**实收金额**计算（`REQ-017`、`AC-022`；契约 §3.23/§3.24，口径见 `data-model.md` §2.14）。

三条实现纪律，改之前先读：

1. **按实收金额计算，不按标价**（`REQ-017` / §2.14 末注）：收多少才抽多少 ——
   未收到的钱不产生佣金；退货冲正后按日重算即随之减少（`REQ-018`）。
   故本模块只接受"实收金额"作为入参，**不接受标价或订单总额**，免得调用方传错口径还以为对。
2. **费率是万分比整数**（`rate_bp` 1~10000），全程整数运算、不引入浮点（§0 全局约定）；
   折算复用 `pricing.half_up_div` —— 一个取整规则只准有一处实现。
3. **"无生效口径"与"佣金为 0"是两件不同的事**。报不报错由**调用方按各自契约**决定：
   日聚合 §3.26 / 结算 §3.27 声明了 `MT-1013`，**必须报错**；而看板 §3.12 / §3.25 未声明该码，
   **不得报错**。故本模块只提供事实（`find_effective_rule()` 返回 `None` 即无口径），
   不替调用方决定拒绝还是计 0。
"""

from __future__ import annotations

import sqlite3

from .. import TradeError
from ..db import now_iso
from .audit import write_audit
from .catalog import parse_business_date
from .pricing import half_up_div

#: `commission_rule` 的对外字段（`data-model.md` §2.14）；`created_at` 一并给出以支持审计核对
RULE_FIELDS = "id, pay_object, rate_bp, category_tier, effective_from, effective_to, created_at"
PAY_OBJECTS = ("merchant", "customer", "market")
RATE_BP_MIN, RATE_BP_MAX = 1, 10_000
CATEGORY_TIER_MAX_LEN = 32
#: 生效期开区间的哨兵（`effective_to IS NULL` 表示长期有效）
_OPEN_ENDED = "9999-12-31"


# ---------------------------------------------------------------------------
# §3.23 查询 / §3.24 配置
# ---------------------------------------------------------------------------


def list_rules(conn: sqlite3.Connection) -> list[dict]:
    """契约 §3.23：返回规则数组（含历史生效期，**不删旧行**）。"""
    rows = conn.execute(
        f"SELECT {RULE_FIELDS} FROM commission_rule ORDER BY effective_from, id"
    ).fetchall()
    return [dict(row) for row in rows]


def _require_str(raw, *, field: str, max_len: int) -> str:
    if not isinstance(raw, str) or not 1 <= len(raw.strip()) <= max_len:
        raise TradeError(
            "MT-1008",
            f"`{field}` 必须是长度 1~{max_len} 的字符串",
            {"field": field, "max_length": max_len},
        )
    return raw.strip()


def _parse_rule(body) -> dict:
    """校验 §3.24 请求体；任何不合规都抛 `MT-1008`（**不要靠数据库 CHECK 兜底**：
    那是最后一道闸门，撞上它只会得到 500 而不是契约要求的 422）。"""
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")

    pay_object = body.get("pay_object")
    if pay_object not in PAY_OBJECTS:
        raise TradeError(
            "MT-1008",
            f"`pay_object` 必须是 {'/'.join(PAY_OBJECTS)} 之一",
            {"field": "pay_object", "allowed": list(PAY_OBJECTS)},
        )

    rate_bp = body.get("rate_bp")
    if isinstance(rate_bp, bool) or not isinstance(rate_bp, int):
        raise TradeError("MT-1008", "`rate_bp` 必须是整数（万分比）", {"field": "rate_bp"})
    if not RATE_BP_MIN <= rate_bp <= RATE_BP_MAX:
        raise TradeError(
            "MT-1008",
            f"`rate_bp` 必须在 {RATE_BP_MIN}~{RATE_BP_MAX} 之间（万分比）",
            {"field": "rate_bp", "min": RATE_BP_MIN, "max": RATE_BP_MAX},
        )

    tier = body.get("category_tier")
    if tier is not None:
        tier = _require_str(tier, field="category_tier", max_len=CATEGORY_TIER_MAX_LEN)

    effective_from = parse_business_date(body.get("effective_from"), field="effective_from")
    effective_to = body.get("effective_to")
    if effective_to is not None:
        effective_to = parse_business_date(effective_to, field="effective_to")
        if effective_to < effective_from:
            raise TradeError(
                "MT-1008",
                "`effective_to` 必须 ≥ `effective_from`",
                {"field": "effective_to", "effective_from": effective_from},
            )

    return {
        "pay_object": pay_object,
        "rate_bp": rate_bp,
        "category_tier": tier,
        "effective_from": effective_from,
        "effective_to": effective_to,
    }


def _find_overlap(conn: sqlite3.Connection, rule: dict) -> sqlite3.Row | None:
    """找出生效期重叠**且档位相同**的既有规则（契约 §3.24 的 `MT-1012` 触发条件）。

    重叠判定用半开区间比较：`新.from ≤ 旧.to` 且 `旧.from ≤ 新.to`（`NULL` 视为开到无穷）。
    **档位不同不算冲突** —— 契约写的是"生效期与既有规则重叠且**档位相同**"。
    """
    return conn.execute(
        f"""
        SELECT {RULE_FIELDS} FROM commission_rule
        WHERE COALESCE(category_tier, '') = COALESCE(?, '')
          AND effective_from <= COALESCE(?, '{_OPEN_ENDED}')
          AND COALESCE(effective_to, '{_OPEN_ENDED}') >= ?
        ORDER BY id
        LIMIT 1
        """,
        (rule["category_tier"], rule["effective_to"], rule["effective_from"]),
    ).fetchone()


def create_rule(conn: sqlite3.Connection, body) -> dict:
    """契约 §3.24：配置佣金口径 → 200 + 新规则行；同时写 `commission_rule_changed` 留痕（`NFR-009`）。

    说明：契约把它写成 `PUT`，但语义是**新增一条口径**（响应是"新建的规则行"，且 §3.23 能查到、
    生效期重叠会被 `MT-1012` 拒绝），不是"替换既有规则"—— 故这里按**新增**实现，
    与 `data-model.md` §2.14"不删旧行、保留历史生效期"的追溯要求一致。
    """
    rule = _parse_rule(body)
    conflicting = _find_overlap(conn, rule)
    if conflicting is not None:
        raise TradeError(
            "MT-1012",
            "该档位在重叠生效期内已有佣金口径",
            {"conflicting_rule_id": int(conflicting["id"]), "category_tier": rule["category_tier"]},
        )

    cursor = conn.execute(
        """
        INSERT INTO commission_rule (pay_object, rate_bp, category_tier, effective_from, effective_to, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            rule["pay_object"],
            rule["rate_bp"],
            rule["category_tier"],
            rule["effective_from"],
            rule["effective_to"],
            now_iso(),
        ),
    )
    rule_id = int(cursor.lastrowid)
    write_audit(
        conn,
        event_type="commission_rule_changed",
        ref_table="commission_rule",
        ref_id=rule_id,
        payload=dict(rule),
        actor="admin",
    )
    conn.commit()
    return dict(
        conn.execute(f"SELECT {RULE_FIELDS} FROM commission_rule WHERE id = ?", (rule_id,)).fetchone()
    )


# ---------------------------------------------------------------------------
# 计算（`T-020` 的正主：看板与日聚合共用同一处口径）
# ---------------------------------------------------------------------------


def find_effective_rule(
    conn: sqlite3.Connection, business_date: str, category_tier: str | None = None
) -> sqlite3.Row | None:
    """取该营业日生效的规则；无则返回 `None`（**报不报错由调用方按契约决定**，见模块 docstring 第 3 条）。

    优先级：**档位匹配优先于全品类**（更具体者胜），同优先级取 `effective_from` 较晚、`id` 较大者。
    """
    rows = conn.execute(
        f"""
        SELECT {RULE_FIELDS} FROM commission_rule
        WHERE effective_from <= ?
          AND COALESCE(effective_to, '{_OPEN_ENDED}') >= ?
          AND (category_tier IS NULL OR category_tier = ?)
        ORDER BY (category_tier IS NULL) ASC, effective_from DESC, id DESC
        LIMIT 1
        """,
        (business_date, business_date, category_tier),
    ).fetchone()
    return rows


def commission_of(received_amount_cents: int, rule: sqlite3.Row | None) -> int:
    """按实收金额折算佣金（分，整数）；`rule is None` → 0。

    公式：`round(实收分 × rate_bp ÷ 10000)`，取整用 `pricing.half_up_div`（与计价同一套四舍五入）。
    """
    if rule is None:
        return 0
    return half_up_div(int(received_amount_cents) * int(rule["rate_bp"]), RATE_BP_MAX)
