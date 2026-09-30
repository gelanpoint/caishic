"""号段分配与**并发安全**的交易 / 流水落库（`T-034` 缺陷修复的落点）。

## 为什么单独一个模块（`pricing.py` 曾因此涨到 429 行、超 400 行门禁）

1. **语义不同**：计价是纯计算（单价 × 重量、抹零、改价留痕），号段分配是**并发写**；
2. **两个调用方**：`pricing`（交易）与 `payment`（收款流水）用同一套规则 —— 同一个规则写两遍
   就是下次漂移的种子，所以放在**被依赖方**（本模块），`payment` 才不必去 import `pricing` 的私有名；
3. 拆法是按语义拆（先例 `Q-16`），不是机械对半切。

## 它修的是什么（根因不是猜的）

交易号/流水号原本是"先 `SELECT MAX` 再加 1，然后 `INSERT`"，而**读号不在写锁内**：
两个并发写者（两摊位同时收银、或**并发补传**）算出同一个号，第二条 INSERT 撞
`ux_transaction_no` / `ux_payment_no` → 抛**未处理的 `sqlite3.IntegrityError`**
→ HTTP 500（且是 Flask 默认 HTML，不是契约 §1.2 格式）→ **这笔交易直接丢了**。
实测 traceback 锚点：`app/api/merchant.py:135 → app/domain/pricing.py:202`（`T-034`）。

**契约判据（改动方向由它决定，不是由"让测试变绿"决定）**：`AC-020`「多个摊位**同时**提交交易，
写入完成时**每笔交易均被完整保存**、交易号唯一、互不覆盖」；`REQ-031` 同义。
故处置是"冲突即回滚重算重试"，**不是**把并发用例的断言放宽。

为什么不用加锁/序列号表：本期是单机 `sqlite3` 的**演示级并发**（`NFR-001`：峰值 <20，
同一营业日的号段只有一个自增序列），有界重试即可收敛，且**不引入新的锁语义、不改数据模型**。
"""

from __future__ import annotations

import sqlite3

from .. import TradeError

#: 号段分配的**有界重试**次数（并发撞唯一约束时回滚 → 重算 → 重试）。
#: 为什么是 5：一次冲突意味着"读号与写号之间被另一个写者插了进去"，冲突概率随并发数上升；
#: 5 次连续失败在 20 并发下已是极端尾部；再多没有实测依据，故不写一个"看起来更安全"的大数
#: （没有依据的数字就是假指标）。**重试耗尽不是抛原始异常**：出口是契约 §4 定义的 `MT-1012`
#: （409 + §1.2 统一错误格式）—— 宁可明确报"冲突请重试"，也不能变成 500 且丢单。
ALLOCATE_RETRIES = 5


def unique_conflict(exc: sqlite3.IntegrityError, column: str) -> bool:
    """该 `IntegrityError` 是否是**指定列**上的唯一约束冲突（SQLite 的消息形态是稳定的）。

    两类冲突必须分开处置，**怎么区分写在代码里**、不靠注释口头约定：
    号段冲突 → 重算重试；幂等键冲突 → 返回首次结果（契约 §1.3）。
    """
    message = str(exc)
    return "UNIQUE constraint failed" in message and column in message


def next_transaction_no(conn: sqlite3.Connection, business_date: str) -> str:
    """生成交易号 `T-YYYYMMDD-NNNN`（按营业日流水号递增）。

    ⚠️ **本函数不是并发安全的**：`SELECT MAX` 与调用方的 `INSERT` 之间没有写锁。
    调用方必须走 `insert_transaction_row`（它带冲突重试），不要直接 INSERT。
    """
    prefix = f"T-{business_date.replace('-', '')}"
    row = conn.execute(
        'SELECT transaction_no FROM "transaction" WHERE transaction_no LIKE ? '
        "ORDER BY transaction_no DESC LIMIT 1",
        (f"{prefix}-%",),
    ).fetchone()
    sequence = int(row["transaction_no"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}-{sequence:04d}"


def next_payment_no(conn: sqlite3.Connection, business_date: str) -> str:
    """生成收款流水号 `PAY-YYYYMMDD-NNNNNN`（同 `next_transaction_no` 的并发注意）。"""
    prefix = f"PAY-{business_date.replace('-', '')}"
    row = conn.execute(
        "SELECT payment_no FROM payment WHERE payment_no LIKE ? ORDER BY payment_no DESC LIMIT 1",
        (f"{prefix}-%",),
    ).fetchone()
    sequence = int(row["payment_no"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}-{sequence:06d}"


def insert_transaction_row(
    conn: sqlite3.Connection,
    *,
    stall_id: int,
    business_date: str,
    total: int,
    origin: str,
    key: str,
    lines: list[tuple[int, int, int, int, int, int]],
) -> tuple[int, sqlite3.Row | None]:
    """插入交易行 + 明细行；**并发下交易号冲突则回滚重算重试**。

    返回 `(transaction_id, 幂等重放时应返回的既有交易行或 None)`：

    - 正常路径：`(新交易 id, None)`，且**已提交**；
    - **并发同键重发**（撞 `transaction.client_idempotency_key`）：`(0, 既有行)` ——
      调用方按契约 §1.3「重复请求返回**首次结果**」把它转成响应体，不报错、不新建；
    - **号段冲突且重试用尽**：抛契约 §4 的 `MT-1012`（409）—— **不把原始 `IntegrityError` 漏成 500**。

    为什么返回"行"而不是响应体：拼响应体是计价模块的事，本模块只管"号段 + 落库"，
    这样它也就**不必反向依赖** `pricing.transaction_payload`（否则又变成环）。
    """
    cursor = None
    for attempt in range(ALLOCATE_RETRIES):
        transaction_no = next_transaction_no(conn, business_date)
        try:
            cursor = conn.execute(
                """
                INSERT INTO "transaction"
                    (transaction_no, stall_id, business_date, status, total_amount_cents,
                     round_off_cents, origin, client_idempotency_key)
                VALUES (?, ?, ?, 'priced', ?, 0, ?, ?)
                """,
                (transaction_no, stall_id, business_date, total, origin, key),
            )
            break
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if unique_conflict(exc, "client_idempotency_key"):
                existing = conn.execute(
                    'SELECT * FROM "transaction" WHERE stall_id = ? AND client_idempotency_key = ?',
                    (stall_id, key),
                ).fetchone()
                if existing is not None:
                    return 0, existing
            if unique_conflict(exc, "transaction.transaction_no"):
                if attempt + 1 < ALLOCATE_RETRIES:
                    continue  # 号被并发抢走了 → 回滚已做，重算号再来
                raise TradeError(  # 有界重试的**明确出口**：契约 §4 的"唯一约束冲突"
                    "MT-1012",
                    "并发写入冲突（交易号被抢占），请重试",
                    {"idempotency_key": key, "attempts": ALLOCATE_RETRIES},
                ) from exc
            raise
    assert cursor is not None, "号段分配重试次数用尽（并发冲突未收敛）"

    transaction_id = int(cursor.lastrowid)
    for product_id, category_id, weight_grams, original, final, amount in lines:
        conn.execute(
            """
            INSERT INTO transaction_item
                (transaction_id, product_id, category_id, weight_grams, original_unit_price_cents,
                 final_unit_price_cents, amount_cents, price_changed, is_round_off)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (transaction_id, product_id, category_id, weight_grams, original, final, amount),
        )
    conn.commit()
    return transaction_id, None


def insert_payment_row(
    conn: sqlite3.Connection,
    *,
    business_date: str,
    transaction_id: int,
    method: str,
    amount_cents: int,
    status: str,
    confirmed_at: str | None,
    operator: str | None,
) -> str:
    """插入支付流水；**并发下流水号冲突则回滚重算重试**，返回最终使用的 `payment_no`。

    根因与契约依据同 `insert_transaction_row`（`T-034` 实测：并发收款撞 `ux_payment_no`
    → 未处理的 `IntegrityError` → 500 且丢一笔收款）。`AC-020`/`REQ-031` 要求并发下每笔都被完整保存，
    `REQ-009`/`REQ-010` 的收款同样不能被吞掉。重试用尽同样是**契约 `MT-1012`**，不是 500。
    """
    for attempt in range(ALLOCATE_RETRIES):
        payment_no = next_payment_no(conn, business_date)
        try:
            conn.execute(
                """
                INSERT INTO payment
                    (payment_no, transaction_id, method, amount_cents, status, confirmed_at, operator)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (payment_no, transaction_id, method, amount_cents, status, confirmed_at, operator),
            )
            return payment_no
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if not unique_conflict(exc, "payment.payment_no"):
                raise
            if attempt + 1 >= ALLOCATE_RETRIES:
                raise TradeError(
                    "MT-1012",
                    "并发写入冲突（收款流水号被抢占），请重试",
                    {"payment_no": payment_no, "attempts": ALLOCATE_RETRIES},
                ) from exc
    raise AssertionError("收款流水号分配重试次数用尽（并发冲突未收敛）")
