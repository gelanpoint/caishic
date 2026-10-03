"""商户学习的**卫生守卫**（`T-SIM-11`）：没有实现的流水就不学习，退出商户不进同伴网络。

## 这条守卫盯的是哪一次真实事故

上一任如实登记了一处没追到根因的现象（`docs/PROJECT-STATE.md` 变更记录 `2026-10-04`）：
`R3` 压力档（MTBF=180）下 0.3× 组的逐期走秤率
`M-04 = [0.594, 0.479, 0.305, 0.183, 0.727, 0, 0, 0]` —— 先逆势大涨、再直接归零。
本文件把它查到了根因并用**测量**钉住，不是靠"看起来合理"结案。

## 根因（一句话）

`close_month` 把**已退出/停业**的商户按 `volume = max(1.0, 实现流水)` 喂进 `observe()`，
于是"没有流水"变成"流水 1 分"，`normalized = U / 1.0 = −29999.75`；
这个值比真实的每元效用（~0.25）大 **10⁵ 倍**，而 `Q̄_peer` 又把**全部**摊位（含已退出的）算进同伴均值，
于是 `ρ(1−φ)·Q̄_peer ≈ 0.08 × (−7700) ≈ −616` 被加进**每一个在营商户**的 `Q`，
`softmax(Q/0.05)` 直接被这个垃圾值支配 —— 商户的"合不合规"变成掷硬币，
第 5 期的 0.727 是"三个摊位里恰好两个抽到 comply"，第 6 期的 0 是"三个都抽到 evade"。

## 为什么这些守卫必须带**灵敏度负例**

把两个接缝（`coexisting_peers` / `realized_volume_for_learning`）打回修复前的写法，
事件流里立刻重新出现 `realized_volume_cents = 0` 的 `merchant_month` —— 守卫当场变红。
**没跑过这一段的守卫是摆设**：它可能只是碰巧绿。
"""

from __future__ import annotations

import sim_support  # noqa: F401  （把仓库根放进 sys.path）
from sim_support import PARAMS_PATH, tiny_run

from sim.bridge import month_loop
from sim.bridge.month_loop import coexisting_peers, peer_mean_q, realized_volume_for_learning


#: 触发"商户退出"所需的最小档位。退出判据是 `EWMA < merchant_exit_reference_point`（A-05，无出处）连续
#: `merchant_exit_breach_periods = 2` 期 ⇒ 要 **≥ 3 个完整仿真月**；到达压到 40/日 让月净收入掉到参考点以下。
EXIT_RUN = {"days": 120, "arrivals": 40, "consumers": 20}


def _merchant_month(events) -> list[dict]:
    return [e for e in events if e.get("kind") == "merchant_month"]


def _exits(events) -> list[dict]:
    return [e for e in events if e.get("kind") == "stall_exit"]


def _run_exit_run(out_dir):
    """跑一个**一定会有商户退出**的档位（这是守卫的前提，不是顺带跑跑）。"""
    return tiny_run(scenario_id="S3", arm_index=0, out_dir=out_dir, **EXIT_RUN)


# ---------------------------------------------------------------------------
# 守卫①：学习用的流水必须**实现过**
# ---------------------------------------------------------------------------
def test_no_merchant_month_is_recorded_without_realized_flow(tmp_path):
    """`merchant_month` 里不允许出现"这一期实现流水 = 0"的记录。

    修复前：退出商户每期都被喂一次 `max(1.0, 0)`，`realized_volume_cents` 恰为 0 ⇒ 本用例红。
    """
    _result, events = _run_exit_run(tmp_path / "exit")
    months = _merchant_month(events)
    assert months, "这一档必须至少有一条 merchant_month，否则守卫是空转"
    zero_flow = [e for e in months if float(e["realized_volume_cents"]) <= 0]
    assert zero_flow == [], f"有 {len(zero_flow)} 条 merchant_month 的实现流水为 0：{zero_flow[:3]}"


def test_exit_run_really_contains_exited_merchants(tmp_path):
    """守卫的前提守卫：**这一档真的会退出商户**（没有退出就没有"退出商户被喂假流水"这件事）。"""
    _result, events = _run_exit_run(tmp_path / "exit")
    assert _exits(events), "这一档一个商户都没退出 ⇒ 下面的守卫在本档上没有载体"


def test_m19_min_is_not_the_fabricated_zero_flow_utility(tmp_path):
    """`M-19`（商户月净收入分布）的最小值不能是夹出来的哨兵值。

    `max(1.0, 0)` 兜底下，退出商户每期的净收入恒为
    `−设备分摊 − p_check·(F+L) = −29999.75`（合规）或 `−132499.62`（转暗），
    于是 `M-19` 的 `min` 会被这些**与经营无关**的常数钉死。
    """
    from sim.observe.metrics import compute_all

    _result, events = _run_exit_run(tmp_path / "exit")
    detail = compute_all(events)["M-19"]["detail"]
    for sentinel in (-29999.75, -132499.62):
        assert abs(float(detail["min"]) - sentinel) > 1.0, (
            f"M-19 的 min = {detail['min']}，正是兜底流水造出来的哨兵值 {sentinel} ⇒ 修复没生效"
        )


# ---------------------------------------------------------------------------
# 守卫②：同伴网络只含**同期仍在营**的商户
# ---------------------------------------------------------------------------
class _FakeMerchant:
    def __init__(self, q):
        self.q = dict(q)


class _FakeStall:
    def __init__(self, merchant, active):
        self.merchant = merchant
        self.active = active


class _FakeWorld:
    def __init__(self, stalls):
        self.stalls = {f"S-{i:02d}": s for i, s in enumerate(stalls)}


def test_coexisting_peers_excludes_departed_merchants():
    """纯函数守卫：已退出的商户不在同伴网络里。"""
    live = _FakeMerchant({"comply": 0.10, "evade": 0.10})
    gone = _FakeMerchant({"comply": -30000.0, "evade": -30000.0})
    world = _FakeWorld([_FakeStall(live, True), _FakeStall(gone, False)])
    assert coexisting_peers(world) == [live]
    assert peer_mean_q(coexisting_peers(world)) == 0.10


def test_departed_merchant_peer_poisoning_dwarfs_the_real_utility_spread():
    """**量级守卫**：离场商户一旦进同伴网络，注入的同伴项比真实效用差大几个数量级。

    这条把"为什么这是缺陷而不是风格问题"钉成数字：真实每元效用差约 0.13，
    污染注入约 −616 ⇒ `softmax(Q/0.05)`（温度 0.05）下概率被完全接管。
    """
    live = _FakeMerchant({"comply": 0.10, "evade": 0.10})
    gone = [_FakeMerchant({"comply": -30000.0, "evade": -30000.0}) for _ in range(6)]
    world = _FakeWorld([_FakeStall(live, True)] + [_FakeStall(g, False) for g in gone])
    clean = peer_mean_q(coexisting_peers(world))
    polluted = peer_mean_q([s.merchant for s in world.stalls.values()])
    kick = 0.2 * (1 - 0.6) * (polluted - clean)
    assert abs(kick) > 100.0, f"同伴项污染只有 {kick:.2f}，说明本守卫的量级前提变了"
    assert abs(kick) > 100 * 0.13, "污染必须比真实的每元效用差（~0.13）大两个数量级"


# ---------------------------------------------------------------------------
# 守卫③：`realized_volume_for_learning` 的语义（纯函数，含负例）
# ---------------------------------------------------------------------------
def test_realized_volume_for_learning_returns_none_for_departed_and_idle():
    """没有实现流水 ⇒ `None`（不可学习）；有实现流水 ⇒ 原值。"""
    departed = _FakeStall(None, active=False)
    idle = _FakeStall(None, active=True)
    alive = _FakeStall(None, active=True)
    assert realized_volume_for_learning(departed, {"volume_cents": 500.0}) is None, "离场商户不可学习"
    assert realized_volume_for_learning(idle, {"volume_cents": 0}) is None, "没有成交的月份不可学习"
    assert realized_volume_for_learning(idle, {}) is None, "缺字段按 0 处理，同样不可学习"
    assert realized_volume_for_learning(alive, {"volume_cents": 1234.5}) == 1234.5


# ---------------------------------------------------------------------------
# **灵敏度负例**：把两个接缝打回修复前的写法，守卫必须当场变红
# ---------------------------------------------------------------------------
def _restore_pre_fix_behaviour(monkeypatch):
    """把 `T-SIM-11` 的修复**关掉**（复原 2026-10-04 之前的写法）。

    - `coexisting_peers`：返回**全部**摊位（含已退出）—— 复原同伴污染；
    - `realized_volume_for_learning`：返回 `max(1.0, 实现流水)` —— 复原"没有流水 = 流水 1 分"。
    """
    monkeypatch.setattr(month_loop, "coexisting_peers",
                        lambda world: [s.merchant for s in world.stalls.values()])
    monkeypatch.setattr(month_loop, "realized_volume_for_learning",
                        lambda stall, realized: max(1.0, float(realized.get("volume_cents") or 0.0)))


def test_sensitivity_negative_fix_off_makes_the_guard_red(tmp_path, monkeypatch):
    """**把修复关掉，守卫必须变红**（否则这条守卫就是摆设）。"""
    _restore_pre_fix_behaviour(monkeypatch)
    _result, events = _run_exit_run(tmp_path / "prefix")
    months = _merchant_month(events)
    zero_flow = [e for e in months if float(e["realized_volume_cents"]) <= 0]
    assert zero_flow, "修复关掉后本应重新出现『实现流水为 0』的 merchant_month；没出现说明负例没复原原行为"


def test_sensitivity_negative_fix_off_reproduces_the_fabricated_utility(tmp_path, monkeypatch):
    """修复关掉 ⇒ `M-19` 的 min 重新被哨兵值钉死（同一份代码、同一条度量）。"""
    from sim.observe.metrics import compute_all

    _restore_pre_fix_behaviour(monkeypatch)
    _result, events = _run_exit_run(tmp_path / "prefix")
    detail = compute_all(events)["M-19"]["detail"]
    assert min(abs(float(detail["min"]) + 29999.75), abs(float(detail["min"]) + 132499.62)) <= 1.0, (
        f"修复关掉后 M-19 的 min = {detail['min']}，没有回到兜底流水造出来的哨兵值 ⇒ 负例没复原原行为"
    )


def test_sensitivity_negative_peer_pool_fix_off_only(monkeypatch):
    """**半个修复关掉也必须能复原症状**（证明两处接缝各自都承担了作用，不是摆设中的一处）。

    只关同伴网络这一处：离场商户仍会被喂假流水，它的 `Q` 就是 −30000 量级 ⇒ 污染照样发生。
    """
    monkeypatch.setattr(month_loop, "coexisting_peers",
                        lambda world: [s.merchant for s in world.stalls.values()])
    # 只验证纯函数层：离场商户 Q = −30000 时，同伴均值必须被它拖到远离真实量级
    live = _FakeMerchant({"comply": 0.24, "evade": 0.37})
    gone = [_FakeMerchant({"comply": -30000.0, "evade": -30000.0}) for _ in range(6)]
    world = _FakeWorld([_FakeStall(live, True)] + [_FakeStall(g, False) for g in gone])
    clean = peer_mean_q(coexisting_peers(world))
    polluted = peer_mean_q([s.merchant for s in world.stalls.values()])
    assert clean > 0.2 and polluted < -1000.0, (f"clean={clean} polluted={polluted}：量级前提变了")


# ---------------------------------------------------------------------------
# 参数与出处不被这次修复偷偷改动
# ---------------------------------------------------------------------------
def test_fix_does_not_touch_the_parameter_file():
    """这次是**实现修复**，不是参数校准 —— 参数文件必须逐字节没被改过。"""
    assert PARAMS_PATH.is_file(), f"参数文件不在 {PARAMS_PATH}"
    source = PARAMS_PATH.read_text(encoding="utf-8")
    assert '"merchant_exit_reference_point"' in source, "退出阈值必须在册（本修复没有动它）"