"""离线暂存与补传（`REQ-014`~`REQ-016`、`REQ-030`；`NFR-013`、`NFR-014`；契约 §3.13~§3.15）。

本模块**不依赖 HTTP**。三条纪律（改动前先读，每一条都对应一条硬要求）：

1. **绝不静默丢弃**（`NFR-014` / `RL-9`）：达告警阈值**只提示、仍继续接受新交易**；
   暂存写入失败必须**明确报错**（`MT-1007`）且**不得留下"已记账"的假象** —— 失败条目一行都不落。
2. **补传成功后清除本地副本**（`NFR-013` / `RL-8`）：`payload_json` 置空 + `purged_at` 非空，
   使"副本已清除"成为**可扫描核对**的事实，而不是口头承诺。
3. **补传幂等键重复 = 丢弃**（`REQ-015`）：丢弃该条 + 写留痕（`discard_reason`）+ **不阻断后续补传**。
   这与第 1 条**不矛盾**：丢的是**重复副本**（原交易已在库、真钱只记一次），而且**留了痕** ——
   "静默丢弃"的标准从来不是"丢没丢"，而是"**有没有留下可追溯的记录**"。

另外两条实现边界：

- **暂存只校验载荷形状，不提前计价**（重量越界等按 `MT-1008` 拒绝，而不是 `MT-1002`）：
  暂存时没有"成交"这回事，落库的是**本地副本**；真正的计价发生在补传（§3.15）——
  计价口径因此仍只有 `pricing` 一处实现。
- **不写 `daily_aggregate`**：日聚合快照属 `T-020`；补传落的是交易事实行。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from .. import TradeError, config
from ..db import now_iso
from .audit import write_audit
from .pricing import MAX_ITEMS, MAX_WEIGHT_GRAMS, create_transaction

#: 幂等键长度上限（契约 §1.3 / `data-model.md` §2.13）
IDEMPOTENCY_KEY_MAX_LEN = 64


# ---------------------------------------------------------------------------
# §3.13 暂存状态（可视标识 + 阈值告警）
# ---------------------------------------------------------------------------


def _pending_count(conn: sqlite3.Connection, stall_id: int) -> int:
    """本摊位仍待补传的条数（只数 `staged`；已补传/已丢弃的不算）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM offline_queue WHERE stall_id = ? AND status = 'staged'",
        (stall_id,),
    ).fetchone()
    return int(row["n"])


def queue_status(conn: sqlite3.Connection, stall_id: int) -> dict:
    """契约 §3.13 暂存状态。

    - `threshold` 取自 `app/config.OFFLINE_WARN_THRESHOLD`（**告警阈值，不是硬上限**）；
    - `threshold_warned` 由"当前待补传条数是否已达阈值"**现场判定**，不读历史标记 ——
      否则补传完一批之后界面还会继续报警（标记只用于留痕，见 §3.14）。
    - `oldest_staged_at` 无待补传时为 `null`（契约样例即 `null`），不是空串。
    - `online`：**本请求已经到达服务端**，故此刻对服务端而言是在线（`true`）；
      秤端的"离线"是本机显式切换的**前端可视标识**（`REQ-014`，不落表），
      服务端不替前端猜网络状态 —— 前端切换时以本机标识为准并覆盖此值显示。
    """
    pending = _pending_count(conn, stall_id)
    oldest = conn.execute(
        "SELECT MIN(staged_at) AS t FROM offline_queue WHERE stall_id = ? AND status = 'staged'",
        (stall_id,),
    ).fetchone()["t"]
    return {
        "pending_count": pending,
        "threshold": int(config.OFFLINE_WARN_THRESHOLD),
        "threshold_warned": pending >= int(config.OFFLINE_WARN_THRESHOLD),
        "oldest_staged_at": oldest,
        "online": True,
    }


# ---------------------------------------------------------------------------
# §3.14 暂存一笔交易（断网期间）
# ---------------------------------------------------------------------------


def _parse_staged_items(body) -> list[dict]:
    """校验暂存载荷（结构与 §3.6 同构）；形状不合法一律 `MT-1008`。

    与 `pricing._parse_items()` 的差别只有一个，且是**故意**的：重量越界在这里是**载荷格式问题**
    （`MT-1008`），不是"拒绝计价"（`MT-1002`）—— 暂存阶段根本没有计价动作。
    上限仍取 `pricing.MAX_WEIGHT_GRAMS`（同一个数值只有一处定义），避免暂存出一条
    永远补传不成功的载荷。
    """
    items = body.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise TradeError(
            "MT-1008",
            f"`items` 必须是长度 1~{MAX_ITEMS} 的数组",
            {"field": "items", "max_items": MAX_ITEMS},
        )

    parsed: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise TradeError("MT-1008", f"`items[{index}]` 必须是对象", {"index": index})
        product_id = item.get("product_id")
        weight_grams = item.get("weight_grams")
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise TradeError("MT-1008", f"`items[{index}].product_id` 必须是整数", {"index": index})
        if not isinstance(weight_grams, int) or isinstance(weight_grams, bool):
            raise TradeError("MT-1008", f"`items[{index}].weight_grams` 必须是整数", {"index": index})
        if not 0 < weight_grams <= MAX_WEIGHT_GRAMS:
            raise TradeError(
                "MT-1008",
                f"`items[{index}].weight_grams` 必须在 1~{MAX_WEIGHT_GRAMS} 之间",
                {"index": index, "weight_grams": weight_grams, "max_grams": MAX_WEIGHT_GRAMS},
            )
        parsed.append({"product_id": product_id, "weight_grams": weight_grams})
    return parsed


def stage_transaction(
    conn: sqlite3.Connection, stall: sqlite3.Row, body, idempotency_key: str | None
) -> dict:
    """契约 §3.14：把一笔交易暂存到本地副本队列 → 201。

    **写入失败必须明确报错**（`MT-1007` / `AC-019`）：捕获 `sqlite3.Error` → 回滚 → 写留痕
    （`offline_queue` / `staging_write_failed`）→ 抛 `MT-1007`。
    **失败条目一行都不留**，因此不存在"报错了但库里已经有这笔"的中间态。
    """
    if not isinstance(body, dict):
        raise TradeError("MT-1008", "请求体必须是 JSON 对象")

    key = idempotency_key or body.get("client_idempotency_key")
    if not isinstance(key, str) or not key or len(key) > IDEMPOTENCY_KEY_MAX_LEN:
        raise TradeError(
            "MT-1008",
            f"缺少幂等键（请求头 Idempotency-Key，长度 ≤{IDEMPOTENCY_KEY_MAX_LEN}）",
            {"header": "Idempotency-Key"},
        )

    # 请求体自带的 `client_idempotency_key` **也要单独校验**（`data-model.md` §2.13 的列约束是 ≤64）：
    # 头部键存在时它不参与去重，但"传了一个存不进库的值还回 201"是安静的谎报 ——
    # 调用方会以为自己那条键生效了。（`T-012` 的 `body4` 用例正是钉住这一点。）
    body_key = body.get("client_idempotency_key")
    if body_key is not None and (
        not isinstance(body_key, str) or not body_key or len(body_key) > IDEMPOTENCY_KEY_MAX_LEN
    ):
        raise TradeError(
            "MT-1008",
            f"`client_idempotency_key` 必须是长度 1~{IDEMPOTENCY_KEY_MAX_LEN} 的字符串",
            {"field": "client_idempotency_key", "max_length": IDEMPOTENCY_KEY_MAX_LEN},
        )

    items = _parse_staged_items(body)
    stall_id = stall["stall_id"]
    actor = f"stall-{stall['stall_no']}"
    day = date.today().isoformat()
    # 本地副本本体：补传时正是靠它重建 §3.6 的请求体（`NFR-013` 要求补传成功后置空）
    payload_json = json.dumps({"items": items, "client_idempotency_key": key}, ensure_ascii=False)

    try:
        cursor = conn.execute(
            """
            INSERT INTO offline_queue (client_idempotency_key, stall_id, business_date, payload_json, status)
            VALUES (?, ?, ?, ?, 'staged')
            """,
            (key, stall_id, day, payload_json),
        )
        queue_id = int(cursor.lastrowid)
    except sqlite3.Error as exc:
        # 存储拒绝接受这条暂存记录（磁盘不可写 / 空间不足等）→ 明确报错，不留半条数据
        conn.rollback()
        write_audit(
            conn,
            event_type="staging_write_failed",
            ref_table="offline_queue",
            # 失败条目**没有行**可指，故 ref_id = 0；定位靠 stall_id + occurred_at + payload 里的键
            ref_id=0,
            payload={"client_idempotency_key": key, "reason": str(exc)},
            actor=actor,
            stall_id=stall_id,
        )
        conn.commit()
        raise TradeError(
            "MT-1007", "本地暂存写入失败，请检查磁盘空间或目录可写性", {"client_idempotency_key": key}
        ) from exc

    pending = _pending_count(conn, stall_id)
    threshold = int(config.OFFLINE_WARN_THRESHOLD)
    if pending >= threshold:
        # `REQ-016`：达阈值**只告警**（留痕），且**继续接受**新交易 —— 这里绝不能拒绝或丢弃
        already = conn.execute(
            "SELECT COUNT(*) AS n FROM offline_queue WHERE stall_id = ? AND over_threshold_notified = 1",
            (stall_id,),
        ).fetchone()["n"]
        if int(already) == 0:
            conn.execute("UPDATE offline_queue SET over_threshold_notified = 1 WHERE id = ?", (queue_id,))
            write_audit(
                conn,
                event_type="offline_threshold_warned",
                ref_table="offline_queue",
                ref_id=queue_id,
                payload={"pending_count": pending, "threshold": threshold},
                actor=actor,
                stall_id=stall_id,
            )
    conn.commit()

    return {
        "queue_id": queue_id,
        "status": "staged",
        "pending_count": pending,
        "threshold_warned": pending >= threshold,
    }


# ---------------------------------------------------------------------------
# §3.15 触发补传（幂等去重 + 清除本地副本）
# ---------------------------------------------------------------------------


def sync_queue(conn: sqlite3.Connection, stall: sqlite3.Row) -> dict:
    """契约 §3.15：把本摊位待补传的副本逐条补传 → 200。

    逐条三选一（**任一条失败都不阻断后续**）：
    - 键已在 `transaction` 里 → `duplicate_discarded`（丢弃重复副本 + 留痕，`REQ-015`）；
    - 补传成功 → `backfilled` 且**清除本地副本**（`payload_json` 置空 + `purged_at`，`NFR-013`）；
    - 补传抛 `TradeError`（如当日无价目表 → `MT-1006`）→ 计入 `failed` 且**该条保持 `staged`**，
      下一轮还能重试 —— 绝不把补传不成功的条目当成功清掉。

    `purged` 口径：**本次补传中本地副本被清除的条数**，含 `backfilled` 与 `duplicate_discarded`
    两类 —— 两类都让本地副本不再需要（前者已入库、后者库里早有），漏计后者会让
    "副本是否清干净"无法核对（`NFR-013` 的本意是"本地不留残件"）。
    """
    stall_id = stall["stall_id"]
    actor = f"stall-{stall['stall_no']}"
    rows = conn.execute(
        "SELECT * FROM offline_queue WHERE stall_id = ? AND status = 'staged' ORDER BY staged_at, id",
        (stall_id,),
    ).fetchall()

    backfilled = duplicate_discarded = failed = purged = 0
    for row in rows:
        key = row["client_idempotency_key"]
        existing = conn.execute(
            'SELECT id, transaction_no FROM "transaction" WHERE stall_id = ? AND client_idempotency_key = ?',
            (stall_id, key),
        ).fetchone()
        if existing is not None:
            conn.execute(
                """
                UPDATE offline_queue
                SET status = 'duplicate_discarded', discard_reason = 'duplicate_idempotency_key',
                    payload_json = NULL, synced_at = ?, purged_at = ?
                WHERE id = ?
                """,
                (now_iso(), now_iso(), row["id"]),
            )
            write_audit(
                conn,
                event_type="offline_duplicate_discarded",
                ref_table="offline_queue",
                ref_id=int(row["id"]),
                payload={
                    "client_idempotency_key": key,
                    "transaction_no": existing["transaction_no"],
                    "discard_reason": "duplicate_idempotency_key",
                },
                actor=actor,
                stall_id=stall_id,
            )
            duplicate_discarded += 1
            purged += 1
            continue

        try:
            staged = json.loads(row["payload_json"] or "{}")
            payload, _replayed = create_transaction(
                conn,
                stall,
                {"items": staged.get("items")},
                key,
                origin="backfilled",
                business_date=row["business_date"],
            )
        except TradeError:
            # 补传不成功：**保持 staged**，留在队列里等下一次（绝不当作成功清掉）
            failed += 1
            continue

        created = conn.execute(
            'SELECT id FROM "transaction" WHERE transaction_no = ?', (payload["transaction_no"],)
        ).fetchone()
        conn.execute(
            """
            UPDATE offline_queue
            SET status = 'backfilled', payload_json = NULL, synced_at = ?, purged_at = ?
            WHERE id = ?
            """,
            (now_iso(), now_iso(), row["id"]),
        )
        write_audit(
            conn,
            event_type="offline_backfilled",
            ref_table="transaction",
            ref_id=int(created["id"]),
            payload={"client_idempotency_key": key, "transaction_no": payload["transaction_no"]},
            actor=actor,
            stall_id=stall_id,
        )
        backfilled += 1
        purged += 1

    conn.commit()
    return {
        "backfilled": backfilled,
        "duplicate_discarded": duplicate_discarded,
        "failed": failed,
        "pending_count": _pending_count(conn, stall_id),
        "purged": purged,
    }
