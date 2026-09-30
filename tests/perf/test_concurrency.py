"""`T-034` 并发**正确性**压测（`REQ-031`、`NFR-001`；`AC-020`）。

要证明的两件事：

1. **多摊位并发写入互不覆盖**（`AC-020` / `REQ-031`）：并发提交后逐笔核对
   —— 每笔都落库、**交易号唯一**、每笔归属的摊位与商品明细**就是它自己的那一笔**；
2. **绝不静默丢弃**（`NFR-014`）在并发下同样成立：并发暂存 → 逐条计数 → 补传 → 零失败。

响应时间门禁的比对**不在这里**：突发时延的分位数由磁盘 fsync 决定（`T-034` 实测对照实验），
判门禁要在"摊主点按"的口径下量 —— 见 `tests/perf/test_latency.py`（阈值也只有那一个入口读）。

并发口径说明（不夸大）：`NFR-001` 的本期范围是**演示级并发**（演示者 1 人 + 评委数人，峰值 <20），
本文件的线程数就按这个量级取，**不假装它压得出生产结论**。
"""

from __future__ import annotations

import concurrent.futures as futures
import statistics
import threading
import time

from e2e_support import (
    active_products,
    bind_stall,
    count,
    evidence_key,
    session_headers,
    stall_id,
    today_iso,
)
from gates import percentile

#: 并发量级：`NFR-001` 的演示级范围（**这是线程数，不是指标阈值**）
THREADS = 10
STALLS = ["A-01", "A-02", "A-03", "A-04", "A-05"]
PER_STALL = 6


def test_concurrent_multi_stall_writes_do_not_overwrite_ac_020(live_server):
    """`AC-020`：多摊位**同时**提交交易 → 每笔完整保存、交易号唯一、互不覆盖；并采样响应时间。"""
    tokens = {stall: bind_stall(live_server, stall) for stall in STALLS}
    products = {stall: active_products(live_server, tokens[stall])[:PER_STALL] for stall in STALLS}
    before = {stall: count(live_server.connect_db(), 'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?',
                           (stall_id(live_server, stall),)) for stall in STALLS}
    conn = live_server.connect_db()
    conn.close()

    # 预热一次（首请求要建连接/编译语句，把它算进分位数等于拿冷启动冒充稳态）
    live_server.api("GET", "/api/merchant/products", headers=session_headers(tokens[STALLS[0]]))

    samples: dict[str, list[tuple[str, float]]] = {"transactions": [], "payments": []}
    lock = threading.Lock()
    weights = {1000, 1500, 800, 1200, 900, 700}

    def submit(stall: str, index: int) -> tuple[str, str, int]:
        token = tokens[stall]
        product = products[stall][index]
        weight = sorted(weights)[index]
        key = evidence_key(f"perf-{stall}-{index}")
        started = time.perf_counter()
        status, txn = live_server.api(
            "POST", "/api/merchant/transactions",
            {"items": [{"product_id": product["id"], "weight_grams": weight}]},
            session_headers(token, key),
        )
        with lock:
            samples["transactions"].append((f"{stall} create", (time.perf_counter() - started) * 1000))
        assert status == 201, f"{stall} 的第 {index + 1} 笔并发提交失败：HTTP {status} {txn}"
        started = time.perf_counter()
        status, paid = live_server.api(
            "POST", f"/api/merchant/transactions/{txn['transaction_no']}/payment",
            {"method": "cash", "operator": "perf"}, session_headers(token, evidence_key(f"pay-{stall}-{index}")),
        )
        with lock:
            samples["payments"].append((f"{stall} pay", (time.perf_counter() - started) * 1000))
        assert status == 200, f"{stall} 并发收款失败：HTTP {status} {paid}"
        return stall, txn["transaction_no"], int(txn["items"][0]["amount_cents"])

    jobs = [(stall, index) for stall in STALLS for index in range(PER_STALL)]
    with futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        results = list(pool.map(lambda args: submit(*args), jobs))

    # ① 交易号唯一
    numbers = [number for _stall, number, _amount in results]
    assert len(numbers) == len(set(numbers)), (
        f"并发下交易号必须唯一：{len(numbers)} 笔里有 {len(numbers) - len(set(numbers))} 个重复"
    )

    # ② 条数逐一核对：每个摊位恰好 +PER_STALL（互不覆盖、也没被算到别人头上）
    for stall in STALLS:
        after = count(live_server.connect_db(),
                      'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?', (stall_id(live_server, stall),))
        assert after == before[stall] + PER_STALL, (
            f"{stall} 并发后应新增 {PER_STALL} 笔（互不覆盖）：{before[stall]} → {after}"
        )

    # ③ 每笔的**内容**也要对得上：摊位、商品、重量、金额都必须是它自己那一笔
    conn = live_server.connect_db()
    try:
        for stall, number, amount in results:
            row = conn.execute(
                'SELECT t.stall_id, t.total_amount_cents, t.status, ti.weight_grams, ti.amount_cents '
                'FROM "transaction" t JOIN transaction_item ti ON ti.transaction_id = t.id '
                "WHERE t.transaction_no = ?",
                (number,),
            ).fetchone()
            assert row is not None, f"并发的这笔没落库：{number}"
            assert int(row["stall_id"]) == stall_id(live_server, stall), f"{number} 归错了摊位：{dict(row)}"
            assert row["status"] == "paid", f"{number} 并发收款后应为 paid：{dict(row)}"
            assert int(row["total_amount_cents"]) == amount == int(row["amount_cents"]), (
                f"{number} 金额自相矛盾（被别的请求覆盖了？）：{dict(row)} vs {amount}"
            )
    finally:
        conn.close()

    # ④ 响应时间采样（**本用例只报告、不设门禁**）
    #    门禁比对在 `test_latency.py`：本用例是 10 路并发写的**突发**，其分位数被
    #    "写事务串行化 × 磁盘 fsync 成本"主导（实测同一段代码在 C: 上 p95 195ms、
    #    在演示数据目录所在的 D: 上 p95 2255ms，而 D: 的 fsync p50 是 32ms vs C: 2ms）。
    #    把突发时延当"接口响应时间"来判门禁，量到的是**盘**，不是接口 —— 故这里只留证据。
    all_samples = [ms for _kind, ms in samples["transactions"]] + [ms for _kind, ms in samples["payments"]]
    slowest = sorted(
        samples["transactions"] + samples["payments"], key=lambda item: item[1], reverse=True
    )[:5]
    print(f"[并发] {len(jobs)} 笔并发写入（{len(STALLS)} 摊位 × {PER_STALL} 笔，{THREADS} 线程）："
          f"交易号唯一、逐摊位条数与内容均核对通过；"
          f"突发时延 中位 {statistics.median(all_samples):.0f}ms / p95 {percentile(all_samples, 0.95):.0f}ms / "
          f"p99 {percentile(all_samples, 0.99):.0f}ms（最慢 5 个：{[(k, round(v)) for k, v in slowest]}）"
          f" —— 突发分位数的环境归因见 tests/perf/test_latency.py")


# ---------------------------------------------------------------------------
# 离线并发：绝不静默丢弃
# ---------------------------------------------------------------------------


def test_concurrent_offline_staging_loses_nothing(live_server):
    """并发暂存 → 逐条计数 → 并发补传：**一笔都不能丢**（`NFR-014`/`RL-9`；`REQ-016`）。"""
    per_stall = 5
    tokens = {stall: bind_stall(live_server, stall) for stall in STALLS[-2:]}
    products = {stall: active_products(live_server, tokens[stall])[0] for stall in tokens}
    before = {stall: count(live_server.connect_db(), 'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?',
                           (stall_id(live_server, stall),)) for stall in tokens}

    def stage(stall: str, index: int):
        status, body = live_server.api(
            "POST", "/api/merchant/offline/queue",
            {"items": [{"product_id": products[stall]["id"], "weight_grams": 1000}]},
            session_headers(tokens[stall], evidence_key(f"offline-perf-{stall}-{index}")),
        )
        return stall, status, body

    jobs = [(stall, index) for stall in tokens for index in range(per_stall)]
    with futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        staged = list(pool.map(lambda args: stage(*args), jobs))

    for stall, status, body in staged:
        assert status == 201, f"{stall} 并发暂存必须被接受（绝不静默丢弃）：HTTP {status} {body}"

    for stall in tokens:
        status, queue = live_server.api("GET", "/api/merchant/offline/queue", headers=session_headers(tokens[stall]))
        assert status == 200, f"契约 §3.13 期望 200，实际 {status}：{queue}"
        assert queue["pending_count"] == per_stall, (
            f"{stall} 并发暂存 {per_stall} 笔后待补传应为 {per_stall}，实际 {queue['pending_count']}（丢了？）"
        )

    def sync(stall: str):
        return stall, live_server.api("POST", "/api/merchant/offline/sync", headers=session_headers(tokens[stall]))

    with futures.ThreadPoolExecutor(max_workers=len(tokens)) as pool:
        synced = list(pool.map(sync, tokens))
    for stall, (status, body) in synced:
        assert status == 200, f"契约 §3.15 期望 200，实际 {status}：{body}"
        assert body["backfilled"] == per_stall and body["failed"] == 0, f"{stall} 补传不完整：{body}"
        assert body["purged"] == per_stall and body["pending_count"] == 0, f"{stall} 本地副本未清干净：{body}"

    for stall in tokens:
        after = count(live_server.connect_db(),
                      'SELECT COUNT(*) AS n FROM "transaction" WHERE stall_id = ?', (stall_id(live_server, stall),))
        assert after == before[stall] + per_stall, (
            f"{stall} 补传后应新增 {per_stall} 笔交易（一笔都不能丢）：{before[stall]} → {after}"
        )
    print(f"[离线并发] {len(jobs)} 笔（{len(tokens)} 摊位 × {per_stall}）并发暂存 → 全部 201；"
          f"并发补传 backfilled={per_stall}/摊位、purged={per_stall}/摊位、failed=0、零丢弃")
