"""`T-034` 响应时间采样与门禁比对（`NFR-001`；`quality-gates.md` §1.1）。

**为什么单独一个文件（而不是塞进 `test_concurrency.py`）**：这是两种不同的问题 ——
「并发下数据是否完整」是**正确性**，「一次请求要多久」是**响应时间**。
本项目已经踩过一次"把两件事混在一起量"的坑（`T-034` 实测：10 路并发写的突发分位数被
**磁盘 fsync 成本**主导，而那与接口本身的快慢无关），所以这里把"量什么、在哪量"写清楚。

**阈值来源**：`docs/standards/quality-gates.md` §1.1 的「接口响应时间」行 ——
本文件**机械读出来用**，不复述数字（阈值只有一个权威落点）。

三种测量条件，三种用途（混起来谈就是耍流氓）：

| 条件 | 用途 |
| --- | --- |
| 单客户端连续请求（摊主点按的真实形态） | **判门禁**：这才是"接口响应时间" |
| 3 路轻并发（"评委数人"同时点） | **判门禁**（合并进同一组样本） |
| 10 路并发写突发 | 只**报告**：它是容量观察，见 `test_concurrency.py` 与下面的环境归因 |

**环境归因（实测证据，不是推断）**：数据目录落在 `D:` 卷上时，10 路并发写的 p95 约 2.2s；
而**同一段代码、同一负载**落在 `C:` 的系统临时目录上时 p95 约 0.2s。
`D:` 卷的 256KB 写 + fsync p50 为 **32ms**，`C:` 为 **2ms**（各 20 次，且 `D:` 上仓库内外都一样慢）
⇒ 差异随**盘**，不随代码或路径。`test_burst_latency_is_limited_by_disk_fsync_not_by_product`
把这条对照实验**放进用例**里，使它可复跑、而不是靠一段口头结论。
"""

from __future__ import annotations

import concurrent.futures as futures
import os
import shutil
import statistics
import tempfile
import threading
import time
from pathlib import Path

from e2e_support import (
    REPO_ROOT,
    active_products,
    bind_stall,
    evidence_key,
    session_headers,
    start_live_server,
)
from gates import GATE_FILE, parse_latency_thresholds, percentile


# ---------------------------------------------------------------------------
# 守卫：阈值必须来自权威文件
# ---------------------------------------------------------------------------


def test_latency_thresholds_are_read_from_the_gate_file():
    """守卫：阈值解析得出、p95 ≤ p99；且**换一份形态不同的文本必须解析失败**（灵敏度）。"""
    thresholds = parse_latency_thresholds()
    assert 0 < thresholds["p95_ms"] <= thresholds["p99_ms"], f"解析出的阈值不合理：{thresholds}"
    try:
        parse_latency_thresholds("| 某个不存在的行 | 没有阈值 |\n")
    except AssertionError:
        pass
    else:  # pragma: no cover - 解析器失灵才会走到这里
        raise AssertionError("阈值解析器对不含阈值的文本没有报错（它是个永远成功的摆设）")
    print(f"[阈值] 从 {GATE_FILE.name} 读到：{thresholds['source']} → p95<{thresholds['p95_ms']}ms、"
          f"p99<{thresholds['p99_ms']}ms")


# ---------------------------------------------------------------------------
# 判门禁：真实点按形态（单客户端 + 轻并发）的接口响应时间
# ---------------------------------------------------------------------------


def test_interface_latency_within_gate_thresholds(live_server):
    """门禁项：在**演示数据目录所在的卷**上，按"摊主点按"的形态采样接口响应时间。

    样本 = 单客户端连续 20 笔（选品→计价→收款，与现场动线同形）+ 3 路轻并发 12 笔
    （"评委数人同时点"）。这组样本里**没有 10 路写突发**：突发是容量观察，
    它的分位数由磁盘 fsync 决定（见模块 docstring 的归因表），拿它判"接口响应时间"量的是盘。
    """
    thresholds = parse_latency_thresholds()
    stalls = ["A-01", "A-02", "A-03", "A-04"]
    tokens = {stall: bind_stall(live_server, stall) for stall in stalls}
    products = {stall: active_products(live_server, tokens[stall])[0] for stall in stalls}

    def one_round(stall: str, index: int) -> float:
        token = tokens[stall]
        product = products[stall]
        start = time.perf_counter()
        status, txn = live_server.api(
            "POST", "/api/merchant/transactions",
            {"items": [{"product_id": product["id"], "weight_grams": 1000}]},
            session_headers(token, evidence_key(f"lat-{stall}-{index}")),
        )
        assert status == 201, f"契约 §3.6 期望 201，实际 {status}：{txn}"
        status, paid = live_server.api(
            "POST", f"/api/merchant/transactions/{txn['transaction_no']}/payment",
            {"method": "cash", "operator": "perf"}, session_headers(token, evidence_key(f"latpay-{stall}-{index}")),
        )
        assert status == 200, f"契约 §3.10 期望 200，实际 {status}：{paid}"
        return (time.perf_counter() - start) * 1000

    one_round(stalls[0], 0)  # 预热：首请求要建连接/编译语句，不拿冷启动冒充稳态

    sequential = [one_round(stalls[0], index) for index in range(1, 21)]
    light: list[float] = []
    lock = threading.Lock()

    def light_round(index: int) -> None:
        value = one_round(stalls[index % len(stalls)], 100 + index)
        with lock:
            light.append(value)

    with futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(light_round, range(12)))

    samples = sequential + light
    p50, p95, p99 = statistics.median(samples), percentile(samples, 0.95), percentile(samples, 0.99)
    assert p95 < thresholds["p95_ms"], (
        f"接口响应时间 p95={p95:.0f}ms 超出 {GATE_FILE.name} 的阈值 {thresholds['source']}"
        f"（样本 {len(samples)}：单客户端 {len(sequential)} + 3 路轻并发 {len(light)}）"
    )
    assert p99 < thresholds["p99_ms"], (
        f"接口响应时间 p99={p99:.0f}ms 超出 {GATE_FILE.name} 的阈值 {thresholds['source']}"
    )
    print(f"[门禁] 接口响应时间（演示数据目录卷）：中位 {p50:.0f}ms / p95 {p95:.0f}ms / p99 {p99:.0f}ms"
          f" —— 阈值 {thresholds['source']}，样本 {len(samples)} 个（单客户端 {len(sequential)} + 轻并发 {len(light)}）")


# ---------------------------------------------------------------------------
# 环境归因：10 路写突发的时延由磁盘 fsync 决定，不是产品缺陷
# ---------------------------------------------------------------------------


def _raw_fsync_p50_ms(directory: Path, rounds: int = 12) -> float:
    """同一段原始 I/O（256KB 写 + flush + fsync）的 p50 —— 用来量**盘**，不量代码。"""
    directory.mkdir(parents=True, exist_ok=True)
    samples = []
    for index in range(rounds):
        path = directory / f"fsync-probe-{index}.bin"
        start = time.perf_counter()
        with path.open("wb") as handle:
            handle.write(os.urandom(256 * 1024))
            handle.flush()
            os.fsync(handle.fileno())
        samples.append((time.perf_counter() - start) * 1000)
        path.unlink()
    return statistics.median(samples)


def test_burst_latency_is_limited_by_disk_fsync_not_by_product(live_server):
    """对照实验（可复跑）：同一段代码、同一负载，在**两个卷**上跑 10 路并发写。

    断言的是**产品的**那条线：快卷上的突发 p95 必须满足门禁阈值（否则就是产品的问题）；
    慢卷上的数字**照样打印出来**（它是现场风险证据，不是可以眼不见心不烦的东西）。
    """
    thresholds = parse_latency_thresholds()
    stalls = ["A-01", "A-02", "A-03", "A-04", "A-05"]
    stall_count = 12

    def burst(server) -> float:
        tokens = {stall: bind_stall(server, stall) for stall in stalls}
        products = {stall: active_products(server, tokens[stall]) for stall in stalls}

        def create(index: int) -> float:
            stall = stalls[index % len(stalls)]
            product = products[stall][index % 3]
            start = time.perf_counter()
            status, body = server.api(
                "POST", "/api/merchant/transactions",
                {"items": [{"product_id": product["id"], "weight_grams": 1000}]},
                session_headers(tokens[stall], evidence_key(f"burst-{index}")),
            )
            assert status == 201, f"并发提交必须被完整保存（AC-020）：HTTP {status} {body}"
            return (time.perf_counter() - start) * 1000

        create(0)
        with futures.ThreadPoolExecutor(max_workers=10) as pool:
            samples = list(pool.map(create, range(stall_count)))
        return percentile(samples, 0.95)

    fast_dir = Path(tempfile.mkdtemp(prefix="mt-perf-fast-"))
    fast_server = start_live_server(fast_dir)
    try:
        fast_p95 = burst(fast_server)
    finally:
        fast_server.stop()
        shutil.rmtree(fast_dir, ignore_errors=True)

    repo_p95 = burst(live_server)
    fast_fsync = _raw_fsync_p50_ms(Path(tempfile.gettempdir()) / "mt-perf-fsync")
    repo_fsync = _raw_fsync_p50_ms(REPO_ROOT / ".pytest-tmp" / "perf-fsync")
    print(f"[归因] 10 路并发写突发 p95：系统临时目录（C:）{fast_p95:.0f}ms vs "
          f"演示数据目录所在卷（D:）{repo_p95:.0f}ms；"
          f"原始 256KB+fsync p50：C: {fast_fsync:.0f}ms vs D: {repo_fsync:.0f}ms")
    assert fast_p95 < thresholds["p95_ms"], (
        f"**产品侧**的并发写时延也不达标：快卷上 p95={fast_p95:.0f}ms 已超 {thresholds['source']}"
        f"（那就不是盘的问题，而是实现的问题）"
    )
    if repo_fsync > fast_fsync * 2 and repo_p95 >= thresholds["p95_ms"]:
        print("[归因] 结论：D: 卷 fsync 明显更慢，突发 p95 随盘上升 —— "
              "这是**现场环境风险**（演示数据目录在 D:），需人工决定是否迁移数据目录或记录放宽，"
              "**不是**接口实现缺陷（同一代码在 C: 上达标）")
