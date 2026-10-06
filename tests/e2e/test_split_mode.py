"""`T-SCALE-14` 分离形态端到端：**同机双进程**（中台 = `run.py`，秤端 = 主机侧运行器）。

覆盖 `AC-025`（中台不可达仍营业 / 恢复后补传）、`AC-030`（人为改一个分位 ⇒ 告警留痕且入账取中台值）、
`AC-031`（幂等：补传不翻倍）、`RL-8` / `NFR-013`（补传成功后清除本地副本）、`AC-033`（形态 2 不托管秤端界面）。

## ⚠️ 验证边界（与 `scale-fw/README.md` §1 口径一致，**不得**读成"秤端已验证"）

1. **这不是硬件在环**（未决项 `Q-21`）：秤端进程用 `scripts/scale_host_runner.py` 代表。它经 `ctypes`
   调用**现场编译的真 `scale-fw/core/*.c`**（`zig cc -shared -fPIC -target x86_64-linux-gnu -std=c11`），
   所以**金额算术确实是固件那份 C 源码**、不是 Python 复刻；但真实称重采样 / 触摸屏 / WiFi 射频 /
   掉电时序 / Flash 磨损**一律未验证**。
2. **`main/**` 在本机既不能编译也不能运行**（无 ESP-IDF、无 ESP32 硬件）—— 故本文件对 `AC-025`
   **前半段**（中台不可达时秤端仍能进入营业界面）的验证是**编排层**的：`main/net/sync.c` 的
   主机侧单测（`test_sync`，内存假端口）+ 本文件的运行器进程行为。**"界面真的画出来了"没有被验证。**
3. 本文件**不宣称**任何 `main/**` 的"编译通过"或"已验证"。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

from e2e_support import (  # noqa: E402  (与既有 e2e 用例同一导入名)
    LiveServer,
    error_code,
    free_port,
    item_amount,
    start_live_server,
    today_iso,
    wait_until_healthy,
)

DEVICE_ID = "SC-900001"
TOKEN = "e2e-split-token-0123456789"
STALL_NO = "A-01"
MARKET_CODE = "M-0001"
WEIGHT_GRAMS = 780
RUNNER = "scripts/scale_host_runner.py"


# --------------------------------------------------------------------------- #
# 助手：秤端进程 / 中台进程 / 本地状态
# --------------------------------------------------------------------------- #


def _run_runner(
    state_dir: Path,
    mid: str,
    *,
    count: int = 0,
    key: str | None = None,
    backfill_only: bool = False,
    origin: str = "online",
    product_id: int | None = None,
    price_cents: int | None = None,
    timeout: int = 240,
) -> subprocess.CompletedProcess:
    """把主机侧秤端当**独立进程**跑一轮（真实 HTTP，不走 Flask test client）。"""
    cmd = [
        sys.executable, RUNNER, "--mid", mid, "--state-dir", str(state_dir),
        "--device-id", DEVICE_ID, "--token", TOKEN, "--market-code", MARKET_CODE,
        "--stall-no", STALL_NO, "--weight-grams", str(WEIGHT_GRAMS),
        "--business-date", today_iso(),
    ]
    if price_cents is not None:
        cmd += ["--price-cents", str(price_cents)]
    if product_id is not None:
        cmd += ["--product-id", str(product_id)]
    if key:
        cmd += ["--idempotency-key", key]
    cmd += ["--origin", origin]
    cmd += ["--backfill-only"] if backfill_only else ["--count", str(count)]
    return subprocess.run(
        cmd, cwd=str(REPO_ROOT), env={**os.environ}, capture_output=True, text=True, timeout=timeout
    )


def _assert_runner_ok(done: subprocess.CompletedProcess, where: str) -> None:
    assert done.returncode == 0, f"{where}：秤端进程退出码 {done.returncode}\n--- stdout ---\n{done.stdout}\n--- stderr ---\n{done.stderr}"


def _state_path(state_dir: Path) -> Path:
    return state_dir / "device_state.json"


def _staged(state_dir: Path) -> list[dict]:
    """秤端本地**待补传队列**（`RL-8`：补传成功后必须为空）。"""
    path = _state_path(state_dir)
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["staged"]


def _provision(server: LiveServer) -> None:
    """把设备**预注册**到中台库（设备是出厂烧录的，没有"注册端点"，故直连库布置前置条件）。"""
    from app.domain.device import provision_device

    conn = server.connect_db()
    try:
        provision_device(conn, device_id=DEVICE_ID, market_code=MARKET_CODE, stall_no=STALL_NO, token=TOKEN)
        conn.commit()
    finally:
        conn.close()


def _authoritative_unit_price(server: LiveServer) -> tuple[int, int]:
    """中台权威价目表里的一条 `(product_id, unit_price_cents)`。

    显式喂给秤端，使"秤端本地计价"与"中台重算"在**未篡改时必然一致** —— 否则 `AC-030` 的
    "改一个分位"就无法与"本来就对不上"区分开（测试必须自己保证前置条件成立）。
    """
    conn = server.connect_db()
    try:
        row = conn.execute(
            "SELECT product_id, unit_price_cents FROM price_item WHERE business_date = ?"
            " ORDER BY product_id LIMIT 1",
            (today_iso(),),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"{today_iso()} 没有价目表 —— 种子未导入？"
    return int(row["product_id"]), int(row["unit_price_cents"])


def _txn_count(server: LiveServer) -> int:
    conn = server.connect_db()
    try:
        return int(conn.execute('SELECT COUNT(*) AS n FROM "transaction"').fetchone()["n"])
    finally:
        conn.close()


def _txn_no(stdout: str) -> str:
    """从秤端日志里取中台回告的交易号（用于证明"幂等命中返回**首次结论**"）。"""
    match = re.search(r"→ (T-\d{8}-\d+)", stdout)
    assert match, f"日志里没有交易号：\n{stdout}"
    return match.group(1)


def _tamper_amount(state_dir: Path, delta_cents: int) -> int:
    """把暂存那笔的金额**人为改一个分位**（顶层与逐行同时改，模拟"秤端本地算错"）。"""
    path = _state_path(state_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["staged"]) == 1, f"预期队列里正好 1 笔，实际 {len(data['staged'])}"
    body = data["staged"][0]["body"]
    body["amount_cents"] += delta_cents
    body["local_lines"][0]["amount_cents"] += delta_cents
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(body["amount_cents"])


def _start_split_mid(data_dir: Path) -> LiveServer:
    """`run.py --mode split`：中台角色，**不注册 `/scale/`**（秤端界面归秤端固件）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    log_path = data_dir / "server-split.log"
    handle = log_path.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "run.py", "--port", str(port), "--mode", "split"],
        cwd=str(REPO_ROOT), env={**os.environ, "MT_DATA_DIR": str(data_dir)},
        stdout=handle, stderr=subprocess.STDOUT,
    )
    server = LiveServer(f"http://127.0.0.1:{port}", port, proc, log_path, data_dir, handle)
    try:
        wait_until_healthy(server)
    except AssertionError:
        server.stop()
        raise
    return server


# --------------------------------------------------------------------------- #
# ① 先起秤端、后起中台：秤端**不卡死**，且离线照常成交（`AC-025` 前半段）
# --------------------------------------------------------------------------- #


def test_scale_terminal_starts_before_the_mid_and_keeps_working_offline(tmp_path):
    """中台**根本没起**（端口无人监听）时，秤端必须能启动并完成一笔本地计价与暂存。"""
    state = tmp_path / "scale"
    dead_mid = f"http://127.0.0.1:{free_port()}"

    done = _run_runner(state, dead_mid, count=1, key="e2e-offline-1", price_cents=480, origin="offline")
    _assert_runner_ok(done, "① 离线启动")
    assert "中台不可达" in done.stdout and "继续启动" in done.stdout, done.stdout

    staged = _staged(state)
    assert len(staged) == 1, "离线也必须照常成交（暂存不丢，`RL-9`）"
    # 金额来自**真 C 核心**（`scale-fw/core/pricing.c`），与中台口径同为四舍五入到分
    assert staged[0]["body"]["amount_cents"] == item_amount(480, WEIGHT_GRAMS)
    # `origin` 必须如实标来源（契约 §3.5）。⚠️ 运行器**不会**自己按链路状态判定，要靠 `--origin`：
    # 固件是 `sync.c:228 request.offline_backfill = (sync->link != SYNC_LINK_UP);` 自动判定的，
    # 运行器默认 `--origin online`（中台不可达时也如此）⇒ 两者口径不一致，**已报 Lead**（不在我写作用域）。
    assert staged[0]["body"]["origin"] == "offline_backfill", "离线成交要如实标来源（契约 §3.5）"


# --------------------------------------------------------------------------- #
# ② + ④ 断连期间成交 → 恢复后补传成功、**总数不翻倍**、本地无副本残留
# --------------------------------------------------------------------------- #


def test_offline_deal_is_backfilled_after_recovery_and_never_doubles(tmp_path):
    """`AC-025` 后半段 + `AC-031` + `RL-8`：补传成功、幂等不翻倍、本地副本清空。"""
    data_dir, state = tmp_path / "mid", tmp_path / "scale"
    server = start_live_server(data_dir)
    try:
        _provision(server)
        product_id, price = _authoritative_unit_price(server)

        # 阶段 A：在线一笔（顺带把档案与价目表缓存进秤端本地）
        a = _run_runner(state, server.base, count=1, key="e2e-A", product_id=product_id, price_cents=price)
        _assert_runner_ok(a, "② 阶段 A 在线")
        assert "激活成功" in a.stdout, a.stdout
        assert _txn_count(server) == 1

        # 阶段 B：中台停掉 ⇒ 断连期间成交（用本地缓存），必须**暂存不丢**
        port = server.port
        server.stop()
        b = _run_runner(state, f"http://127.0.0.1:{port}", count=1, key="e2e-B", product_id=product_id, price_cents=price, origin="offline")
        _assert_runner_ok(b, "② 阶段 B 断连")
        assert "中台不可达" in b.stdout, b.stdout
        assert len(_staged(state)) == 1, "断连期间的成交必须留在本地队列"
        queued_snapshot = _state_path(state).read_text(encoding="utf-8")

        # 阶段 C：中台恢复（同端口同数据目录）⇒ 补传
        server = start_live_server(data_dir, port=port)
        c = _run_runner(state, server.base, backfill_only=True)
        _assert_runner_ok(c, "② 阶段 C 补传")
        assert "补传成功 1 笔" in c.stdout and "仍暂存 0 笔" in c.stdout, c.stdout
        assert _txn_count(server) == 2, "两笔成交（在线 1 + 补传 1）"
        backfilled_no = _txn_no(c.stdout)
        conn = server.connect_db()
        try:
            origins = [r["origin"] for r in conn.execute('SELECT origin FROM "transaction" ORDER BY id')]
        finally:
            conn.close()
        assert origins == ["online", "backfilled"], f"入库来源必须区分「当时在线」与「事后补传」：{origins}"
        assert _staged(state) == [], "④ 补传成功后**不得**留下本地副本（`RL-8` / `NFR-013`）"

        # 重放臂：把同一笔塞回队列再补传 ⇒ 幂等命中，**总数不得翻倍**
        _state_path(state).write_text(queued_snapshot, encoding="utf-8")
        d = _run_runner(state, server.base, backfill_only=True)
        _assert_runner_ok(d, "② 重放臂")
        # 中台对幂等命中回 **200 + `replayed=true`**（契约 §3.5；`tests/scale` 有对应契约用例），
        # 故运行器走的是"成功"分支（它的 `幂等命中` 分支留给 409+replayed）—— 判据要落在**实质上**：
        assert _txn_no(d.stdout) == backfilled_no, "幂等命中必须返回**首次结论**（同一交易号），不得新建"
        assert _txn_count(server) == 2, "**同一幂等键重放不得产生第二笔**（`AC-031`）"
        assert _staged(state) == [], "幂等命中同样要清本地副本"
    finally:
        server.stop()


# --------------------------------------------------------------------------- #
# ③ 人为改一个分位 ⇒ 中台告警留痕、入账取中台值（`AC-030`）
# --------------------------------------------------------------------------- #


def test_tampered_cent_is_flagged_and_the_midplatform_amount_is_booked(tmp_path):
    """端到端走**真实 HTTP** 复核 `AC-030` 的三件事：留痕 / 入账取中台值 / 如实回告。"""
    data_dir, state = tmp_path / "mid", tmp_path / "scale"
    server = start_live_server(data_dir)
    try:
        _provision(server)
        product_id, price = _authoritative_unit_price(server)
        _run_runner(state, server.base, count=1, key="e2e-cache", product_id=product_id, price_cents=price)

        # 断连期间成交（金额正确），随后**人为改一个分位**
        port = server.port
        server.stop()
        _run_runner(state, f"http://127.0.0.1:{port}", count=1, key="e2e-tamper", product_id=product_id, price_cents=price, origin="offline")
        correct = _staged(state)[0]["body"]["amount_cents"]
        tampered = _tamper_amount(state, +1)
        assert tampered == correct + 1

        # 中台恢复 ⇒ 补传（差异必须被中台发现，而不是被静默接受）
        server = start_live_server(data_dir, port=port)
        done = _run_runner(state, server.base, backfill_only=True)
        _assert_runner_ok(done, "③ 补传")
        assert _staged(state) == [], "差异笔同样要补传成功（`RL-9`：不得拒收）"

        conn = server.connect_db()
        try:
            booked = conn.execute(
                'SELECT total_amount_cents FROM "transaction" ORDER BY id DESC LIMIT 1'
            ).fetchone()["total_amount_cents"]
            rows = conn.execute(
                "SELECT payload_json FROM audit_log WHERE event_type = 'scale_amount_mismatch'"
            ).fetchall()
        finally:
            conn.close()

        assert booked == correct, f"入账必须取**中台重算值** {correct}，实际 {booked}"
        assert booked != tampered, "不得把秤端多报的一分入账"
        assert len(rows) == 1, f"必须**恰好**一条差异留痕，实际 {len(rows)} 条"
        payload = json.loads(rows[0]["payload_json"])
        assert payload["device_id"] == DEVICE_ID
        assert payload["idempotency_key"] == "e2e-tamper"
        assert payload["authoritative_amount_cents"] == correct
        assert payload["reported_amount_cents"] == tampered
        assert payload["mismatch_detail"], "留痕里要有逐行差异明细（可核验）"
    finally:
        server.stop()


# --------------------------------------------------------------------------- #
# 形态 2 边界：中台**不托管**秤端界面（`AC-033` / `ADR-0005`）
# --------------------------------------------------------------------------- #


def test_split_mode_mid_does_not_serve_the_terminal_ui(tmp_path):
    """`run.py --mode split` 下 `/scale/` 必须 404，但秤端接入 API 仍由中台提供。"""
    server = _start_split_mid(tmp_path / "mid-split")
    try:
        status, _ = server.api("GET", "/scale/")
        assert status == 404, f"分离形态下中台不得托管秤端界面，实际 {status}"

        status, payload = server.api("GET", "/api/scale/v1/catalog")
        assert status == 401, "秤端接入 API 仍应由中台提供（未带令牌 ⇒ 401）"
        assert error_code(payload) == "MT-2001"
    finally:
        server.stop()


def test_split_mode_banner_does_not_print_the_terminal_entry(tmp_path):
    """启动横幅要**如实**：分离形态不打印 `/scale/` 入口（否则现场按错地址会看到 404）。"""
    server = _start_split_mid(tmp_path / "mid-split-banner")
    try:
        log = server.log_text()
        assert "/api/scale/v1/" in log, log
        assert "本形态不托管" in log, f"分离形态横幅必须说明不托管秤端界面：\n{log}"
    finally:
        server.stop()
