"""`T-SIM-07`：live 模式**实跑**（真起 `run.py` 子进程）后的六条验收判据。

对应 `docs/sim-design.md` §8 `T-SIM-07`：

| 判据 | 用例 | 形式 |
| --- | --- | --- |
| ① 31/31 端点覆盖 | `test_criterion_1_all_31_endpoints_called` | 由**实际请求**反推的清单 + 独立对契约 §2 表 |
| ② 全营业日 `balanced=true` | `test_criterion_2_every_day_balanced` | 逐日读对账三线与 `diff_cents` |
| ③ 幂等重放不产生新交易 | `test_criterion_3_idempotent_replay` | 交易号不变 + 列表 `total` 不变 |
| ④ 敏感扫描零命中 | `test_criterion_4_sensitive_scan_zero_hits` | 复用 `tests/contract/sensitive_scan.py` 扫**真收到过的响应体** |
| ⑤ 数据目录在 `C:` | `test_criterion_5_data_dir_on_c_drive` | 实测盘符 + 实测 p50/p95 |
| ⑥ `app/**`、`specs/**` 不变 | `test_criterion_6_system_tree_unchanged` | 运行前后树指纹 + 用例**独立重算**一次 |

另有一组**灵敏度负例**（`test_sensitivity_*`）：每条判据都喂一份"改坏的报告"，
断言对应判定**必须变红** —— 不验证灵敏度的验证是摆设（`AGENTS.md` / `T-036` 家族）。
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest

from sim_support import PARAMS_PATH, REPO_ROOT

from sim.bridge.live_adapter import ENDPOINTS
from sim.bridge.live_evidence import verification_problems
from sim.bridge.live_run import LiveRunConfig, find_scenario, run_live
from sim.bridge.server_launcher import system_tree_digest
from sim.core.params import load_params

#: live 跑批的天数：**必须 30**（走得到月末 ⇒ `POST/GET /api/admin/settlements` 这两个端点
#: 才有机会被调用 ⇒ 31/31 才可能成立）。这是端点覆盖的约束，不是"取整好看"。
LIVE_DAYS = 30


def _load_sensitive_scan():
    """**按路径**加载 `tests/contract/sensitive_scan.py`。

    不写 `sys.path`：判据④的禁令集合必须只用这一处权威，测试只是它的调用方
    （`tests/contract/` 里已有一套同源的复用先例）。
    """
    path = REPO_ROOT / "tests" / "contract" / "sensitive_scan.py"
    spec = importlib.util.spec_from_file_location("contract_sensitive_scan", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SENSITIVE = _load_sensitive_scan()


@pytest.fixture(scope="module")
def live(tmp_path_factory) -> dict:
    """真起一次被测服务跑完 30 个营业日，返回 `live_report.json` 的内容。

    场景用 **`S1` 的第 0 臂（向商户抽 200bp = 2%）**而不是 `S0` 基线臂，理由不是"抽佣臂更典型"，
    而是：`S0` 声明的是 **0bp**，而契约 §3.24 的 `rate_bp` 下限是 1 ⇒ 实际写入 1bp，
    在本档的日流水下**系统算出的佣金四舍五入后基本是 0** —— 那样的"佣金 > 0"断言测不到东西
    （不是佣金没生效，是口径本身太小）。用 200bp 才能让"佣金是系统算的、随口径而变"可观测。
    0% 那条臂的下界处理由 `test_commission_floor_is_reported_not_silently_applied` 单独盯。
    """
    out_dir = tmp_path_factory.mktemp("live-run")
    #: 目录名带一次性后缀：默认的 `run_id` 带时间戳（每次一份干净库），测试里显式指定就得自己保证唯一
    token = uuid4().hex[:8]
    config = LiveRunConfig(scenario_id="S1", arm_index=0, days=LIVE_DAYS, seed=20261002,
                           start="2026-10-01", txns_per_day=2, run_id=f"pytest-live-s1-{token}")
    report = run_live(load_params(PARAMS_PATH), config, out_dir)
    return report


def _response_bodies(report: dict) -> list:
    path = Path(report["responses_path"])
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 六条判据
# ---------------------------------------------------------------------------
def test_criterion_1_all_31_endpoints_called(live):
    """① 31/31 端点被**真实调用**至少一次（清单由实际请求反推，不接受手写勾选）。"""
    coverage = live["coverage"]
    assert coverage["expected_count"] == 31, "契约就是 31 个端点"
    assert coverage["covered_count"] == 31, f"缺 {coverage['missing']}"
    assert coverage["missing"] == []
    assert coverage["unmatched"] == [], "有请求没匹配到任何契约端点"
    assert set(coverage["covered"]) == {f"{m} {p}" for m, p in ENDPOINTS}
    # 产物路径可机读：覆盖清单单独落一份 JSON（判据①要求的"机读产物"）
    coverage_json = Path(live["report_path"]).with_name("coverage.json")
    assert coverage_json.is_file(), "覆盖清单必须落成独立 JSON 供第三方核对"
    assert json.loads(coverage_json.read_text(encoding="utf-8"))["covered_count"] == 31
    print(f"[判据①] 31/31 端点被真实调用（HTTP 调用 {live['calls']} 次）；清单：{coverage_json}")


def test_criterion_2_every_day_balanced(live):
    """② 全部营业日 `reconciliation.balanced=true`（且三线相等、`diff_cents=0`）。"""
    days = live["days"]
    assert len(days) == LIVE_DAYS, f"应跑满 {LIVE_DAYS} 个营业日，实际 {len(days)}"
    for row in days:
        reconciliation = row["reconciliation"]
        assert reconciliation["balanced"] is True, f"{row['business_date']} 不平：{reconciliation}"
        assert reconciliation["diff_cents"] == 0
        assert reconciliation["order_total_cents"] == reconciliation["payment_total_cents"]
        assert reconciliation["payment_total_cents"] == reconciliation["split_total_cents"]
    assert all(row["aggregate_stalls"] > 0 for row in days), "日终聚合没有真的聚合任何摊位"
    print(f"[判据②] {len(days)}/{LIVE_DAYS} 个营业日 balanced=true；"
          f"首日三线 = {days[0]['reconciliation']['order_total_cents']} 分，末日 = "
          f"{days[-1]['reconciliation']['order_total_cents']} 分")


def test_criterion_3_idempotent_replay(live):
    """③ 幂等重放不产生新交易：同键同体重发 ⇒ 同一交易号、列表总数不变。"""
    replay = live["idempotency_replay"]
    assert replay, "报告里没有幂等重放证据"
    assert replay["first_status"] == 201, "首次创建应是 201"
    assert replay["replay_status"] == 200, "幂等重放应返回 200（首次结果），不是再创建一笔"
    assert replay["transaction_no"] == replay["replayed_transaction_no"]
    assert replay["list_total_before"] == replay["list_total_after"] == 1
    print(f"[判据③] {replay['transaction_no']} 重发 → 同一交易号，列表总数 "
          f"{replay['list_total_before']} → {replay['list_total_after']}")


def test_criterion_4_sensitive_scan_zero_hits(live):
    """④ 敏感扫描零命中：扫的是**真收到过的响应体**（`live_responses.jsonl` 逐条）。"""
    bodies = _response_bodies(live)
    assert len(bodies) == live["response_bodies"] > 0
    hits: list[str] = []
    for record in bodies:
        hits.extend(SENSITIVE.scan_json_for_sensitive(record["payload"], f"{record['endpoint']} 响应"))
    assert hits == [], f"响应体命中敏感扫描禁令：{hits[:5]}"
    assert verification_problems(live, sensitive_hits=hits) == []
    print(f"[判据④] 敏感扫描 {len(bodies)} 条真实响应体：零命中")


def test_criterion_5_data_dir_on_c_drive(live):
    """⑤ 数据目录在 `C:`；并在报告里无条件记录实测 p50/p95（若在 `D:` 则据此判定降级）。"""
    data_dir = Path(live["data_dir"])
    assert data_dir.is_dir(), f"数据目录不存在：{data_dir}"
    assert (data_dir / "market_trade.sqlite3").is_file(), "被测服务的库文件不在数据目录里"
    assert data_dir.drive.upper() == "C:", (
        f"数据目录在 {data_dir.drive or '（无盘符）'}，不是 C:；实测 p50={live['latency']['p50']}ms / "
        f"p95={live['latency']['p95']}ms ⇒ 已降级"
    )
    assert live["latency"]["p50"] and live["latency"]["p95"] and live["latency"]["count"] > 0
    print(f"[判据⑤] 数据目录 {data_dir}（C:）；实测 p50={live['latency']['p50']}ms / "
          f"p95={live['latency']['p95']}ms（样本 {live['latency']['count']}）")


def test_criterion_6_system_tree_unchanged(live):
    """⑥ 运行前后 `app/**`、`specs/**` 树指纹不变（live 模式只读系统）。"""
    before, after = live["system_digest_before"], live["system_digest_after"]
    assert before["app_files"] > 0 and before["specs_files"] > 0
    assert before["app"] == after["app"], "app/** 在运行期间变了"
    assert before["specs"] == after["specs"], "specs/** 在运行期间变了"
    #: **独立重算**一次：报告里的指纹是运行器自己写的，用例不能只信它
    recomputed = system_tree_digest()
    assert recomputed["app"] == before["app"], "独立重算的 app/** 指纹与报告不一致"
    assert recomputed["specs"] == before["specs"], "独立重算的 specs/** 指纹与报告不一致"
    print(f"[判据⑥] app/**（{before['app_files']} 文件）与 specs/**（{before['specs_files']} 文件）"
          "运行前后指纹不变，且用例独立重算一致")


def test_commission_is_written_into_the_system_not_recomputed_in_sim(live):
    """**关键设计意图的证据**：佣金口径是 `PUT` 进系统的，系统算出来的佣金随口径而变。"""
    evidence = live["commission_evidence"]
    assert evidence["rules_before"] == 0 and evidence["rules_after"] == 1, "佣金口径没有真的写进系统"
    assert evidence["created_rule"]["rate_bp"] == live["commission"]["rate_bp"]
    assert evidence["created_rule"]["pay_object"] == live["commission"]["pay_object"]
    settlement_rows = [row for day in live["days"] for row in day["settlements"]]
    assert settlement_rows, "月末没有生成任何结算单"
    assert any(row["commission_amount_cents"] > 0 for row in settlement_rows), (
        "系统算出的佣金全为 0 —— 那说明口径没生效，或 sim 自己算了一份佣金"
    )
    assert live["commission_evidence"]["system_commission_total_cents"] > 0, "日佣金（系统算的）全为 0"
    #: 佣金必须**逐日**由系统算出（看板 / 日聚合同源，`stall_day_facts` 一处实现）
    assert sum(row["market_commission_cents"] for row in live["days"]) > 0, "运营端看板的佣金全为 0"
    print(f"[佣金] 写进系统的规则 rate_bp={evidence['created_rule']['rate_bp']}；"
          f"结算单里系统算出的佣金合计 "
          f"{sum(row['commission_amount_cents'] for row in settlement_rows)} 分；"
          f"日佣金合计 {live['commission_evidence']['system_commission_total_cents']} 分")


# ---------------------------------------------------------------------------
# 灵敏度负例：每条判据都必须**能失败**
# ---------------------------------------------------------------------------
def _green(live: dict) -> dict:
    return copy.deepcopy(live)


def test_sensitivity_coverage_gap_must_be_red(live):
    """关掉一个端点的覆盖记录 ⇒ 判定必红（判据①能失败）。"""
    broken = _green(live)
    broken["coverage"]["covered"] = [k for k in broken["coverage"]["covered"] if k != "GET /healthz"]
    problems = verification_problems(broken, sensitive_hits=[])
    assert any("端点覆盖缺口" in item for item in problems), problems


def test_sensitivity_unbalanced_day_must_be_red(live):
    """某营业日 `balanced=false` ⇒ 判定必红（判据②能失败）。"""
    broken = _green(live)
    broken["days"][5]["reconciliation"] = {**broken["days"][5]["reconciliation"], "balanced": False,
                                            "diff_cents": 12}
    problems = verification_problems(broken, sensitive_hits=[])
    assert any("② 对账" in item for item in problems), problems


def test_sensitivity_replay_creating_new_transaction_must_be_red(live):
    """幂等重放多出一笔 ⇒ 判定必红（判据③能失败）。"""
    broken = _green(live)
    broken["idempotency_replay"] = {**broken["idempotency_replay"], "list_total_after": 2}
    problems = verification_problems(broken, sensitive_hits=[])
    assert any("③ 幂等重放" in item for item in problems), problems


def test_sensitivity_sensitive_hit_must_be_red(live):
    """扫描命中一条 ⇒ 判定必红；**没扫描也不许判绿**（判据④能失败）。"""
    problems = verification_problems(live, sensitive_hits=["/api/admin/dashboard: 命中禁忌字段名 `id_card`"])
    assert any("④ 敏感扫描：命中" in item for item in problems), problems
    problems = verification_problems(live, sensitive_hits=None)
    assert any("未执行" in item for item in problems), "没扫描时必须报'未执行'，不许静默判绿"


def test_sensitivity_sensitive_scan_detector_is_alive():
    """扫描器本身在扫真实数据：把禁忌字段塞进一条响应体 ⇒ 必须命中（否则"零命中"是空的）。"""
    hits = SENSITIVE.scan_json_for_sensitive({"error": {"detail": {"id_card": "110101199003070011"}}}, "合成负例")
    assert hits, "合成负例都没命中 ⇒ 判据④的'零命中'什么也没证明"
    clean = SENSITIVE.scan_json_for_sensitive({"transaction_no": "T-20261001-0001", "total_amount_cents": 9463},
                                              "干净样例")
    assert clean == [], f"干净样例被误报：{clean}"


def test_sensitivity_data_dir_on_other_drive_must_be_red(live):
    """数据目录挪到 `D:` ⇒ 判定必红且**带上实测 p50/p95**（判据⑤能失败）。"""
    broken = _green(live)
    broken["data_dir"] = "D:/mt-sim/broken"
    problems = verification_problems(broken, sensitive_hits=[])
    joined = "\n".join(problems)
    assert "⑤ 数据目录" in joined, problems
    assert f"p95={broken['latency']['p95']}ms" in joined, "降级说明必须带实测 p95，不许只说一句'不在 C:'"
    #: 若连 p50/p95 都没记 ⇒ **不可判定**，必须同样判红
    without_latency = _green(live)
    without_latency["data_dir"] = "D:/mt-sim/broken"
    without_latency["latency"] = {"count": 0, "p50": None, "p95": None, "max": None}
    assert any("不可判定" in item for item in verification_problems(without_latency, sensitive_hits=[]))


def test_sensitivity_system_tree_change_must_be_red(live):
    """运行期间 `app/**` 被改动 ⇒ 判定必红（判据⑥能失败）。"""
    broken = _green(live)
    broken["system_digest_after"] = {**broken["system_digest_after"], "app": "0" * 64}
    problems = verification_problems(broken, sensitive_hits=[])
    assert any("⑥ 系统只读" in item for item in problems), problems
    missing = _green(live)
    missing["system_digest_before"] = {}
    assert any("不可判定" in item for item in verification_problems(missing, sensitive_hits=[]))


def test_all_six_criteria_green_on_the_real_run(live):
    """把六条判据一起过一遍（全部通过 ⇒ 空清单），确保上面的负例不是"本来就红"。"""
    assert verification_problems(live, sensitive_hits=[]) == []
    print("[判定] 六条判据在真实运行上一并通过（`verification_problems` 返回空清单）")


def test_commission_floor_is_reported_not_silently_applied():
    """「抽佣 0%」在 live 模式不可表达（契约 `rate_bp ≥ 1`）⇒ 必须**报告**，不许悄悄改。"""
    scenario = find_scenario("S1")
    from sim.bridge.live_run import commission_plan
    from sim.core.params import load_params as _load

    plan = commission_plan(scenario, next(a for a in scenario["arms"] if "不抽佣" in a["name"]),
                           _load(PARAMS_PATH))
    assert plan["requested_rate_bp"] == 0
    assert plan["rate_bp"] == 1 and plan["clamped"] is True
    assert "rate_bp" in plan["note"] and "系统侧边界" in plan["note"]
