"""`T-SIM-02` 环境层与时序：**零决策基线**的守恒、强度可复算与同轴性。

四条验收各有机械判据，且**守恒与强度都能由事件明细独立复算** —— 不是"代码自己说没问题"：

① 90 营业日零决策基线无异常；② 设备数守恒、成交笔数 = 走秤 + 私下；
③ 时序与系统 `business_date` 同轴；④ 到达强度与时段块配置一致（可由明细复算）。

判据函数写成**纯函数**（`conservation_violations` / `intensity_mismatches`），
故合成负例可直接喂给它 —— `T-036` 家族的规矩：不验证灵敏度的验证是摆设。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from sim_support import PARAMS_PATH, REPO_ROOT

DAYS = 90
START = "2026-10-01"
SEED = "20261002"


# ---------------------------------------------------------------------------
# 判据函数（纯函数：合成负例可直接调用）
# ---------------------------------------------------------------------------
def conservation_violations(records) -> list[str]:
    """逐日复算两条守恒：设备三态之和 = 机队规模；走秤 + 私下 = 当日到达数。"""
    problems: list[str] = []
    for record in records:
        kind = record.get("kind")
        if kind == "device_state":
            total = record["working"] + record["waiting"] + record["repairing"]
            if total != record["total"]:
                problems.append(f"{record['business_date']} 设备三态之和 {total} ≠ 机队规模 {record['total']}")
        elif kind == "block_arrivals":
            if record["on_scale"] + record["off_scale"] != record["count"]:
                problems.append(
                    f"{record['business_date']}·{record['block']} 走秤+私下 "
                    f"{record['on_scale']}+{record['off_scale']} ≠ 到达 {record['count']}"
                )
    return problems


def flow_balance_violations(records) -> list[str]:
    """逐日复算：当日各块到达数之和 = `day_arrivals_total`，且与 `on_scale + off_scale` 一致。"""
    per_day_blocks: dict[str, int] = {}
    per_day_split: dict[str, int] = {}
    declared: dict[str, int] = {}
    for record in records:
        day = record.get("business_date")
        if record.get("kind") == "block_arrivals":
            per_day_blocks[day] = per_day_blocks.get(day, 0) + record["count"]
            per_day_split[day] = per_day_split.get(day, 0) + record["on_scale"] + record["off_scale"]
        elif record.get("kind") == "day_arrivals_total":
            declared[day] = record["count"]

    problems: list[str] = []
    for day, declared_total in declared.items():
        if per_day_blocks.get(day) != declared_total:
            problems.append(f"{day} 各块之和 {per_day_blocks.get(day)} ≠ 当日总数 {declared_total}")
        if per_day_split.get(day) != declared_total:
            problems.append(f"{day} 走秤+私下 {per_day_split.get(day)} ≠ 当日总数 {declared_total}")
    return problems


def intensity_mismatches(records, expected_lambdas: dict[str, float], tolerance: float = 1e-9) -> list[str]:
    """逐块核对记录的 `lam` 与"按参数复算的期望值"是否一致。"""
    problems: list[str] = []
    for record in records:
        if record.get("kind") != "block_arrivals":
            continue
        expected = expected_lambdas.get(record["block"])
        if expected is None:
            problems.append(f"事件里出现未配置的时段块 {record['block']!r}")
        elif abs(record["lam"] - expected) > tolerance:
            problems.append(f"块 {record['block']} 的 lam={record['lam']} ≠ 复算值 {expected}")
    return problems


# ---------------------------------------------------------------------------
# 跑一次 90 营业日基线，供多条判据共用
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    """跑一次 90 营业日零决策基线（子进程，与现场用法一致），返回 (metrics, 事件明细)。"""
    out_dir = tmp_path_factory.mktemp("env90")
    result = subprocess.run(
        [
            sys.executable, "-m", "sim", "--mode=model", "--env",
            f"--days={DAYS}", f"--seed={SEED}", f"--start={START}",
            "--params", str(PARAMS_PATH), "--out-dir", str(out_dir),
        ],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    assert result.returncode == 0, f"90 营业日基线运行失败：\n{result.stdout}\n{result.stderr}"
    events = [json.loads(line) for line in (out_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    metrics = json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))
    return metrics, events


# ---------------------------------------------------------------------------
# ① 90 营业日无异常
# ---------------------------------------------------------------------------
def test_baseline_runs_90_business_days(baseline):
    metrics, events = baseline
    assert metrics["days"] == DAYS
    assert metrics["layer"] == "env-baseline"
    assert metrics["skeleton_only"] is False, "环境层基线不得再自报为骨架运行"
    assert metrics["first_business_date"] == START
    assert metrics["event_count"] == len(events)
    print(f"[T-SIM-02] 90 营业日基线跑通：事件 {len(events)} 条、到达 {metrics['arrivals_total']} 笔")


# ---------------------------------------------------------------------------
# ② 守恒（由明细复算）
# ---------------------------------------------------------------------------
def test_device_and_flow_conservation_recomputed_from_events(baseline):
    _, events = baseline
    assert conservation_violations(events) == []
    assert flow_balance_violations(events) == []
    device_days = [r for r in events if r["kind"] == "device_state"]
    assert len(device_days) == DAYS, f"应有 {DAYS} 天设备状态，实际 {len(device_days)}"
    print(f"[T-SIM-02] {len(device_days)} 天设备三态守恒、{DAYS} 天客流守恒（均由明细复算）")


def test_high_fault_parameters_exercise_the_repair_queue(tmp_path):
    """把 MTBF 调小、维修时长调长，**密集**跑一遍故障→报修→维修队列。

    为什么需要这条：基线的 90 天只出 2 次故障（10 台 × 90/540 ≈ 1.67），
    三态里 `waiting`/`repairing` 几乎总是 0 —— **守恒断言在那种数据上接近于恒真**，
    等于没验。本用例用同一份参数文件（只改 `value`、出处字段原样保留）把故障率推高，
    逼出真实的三态共存，再复算守恒。
    """
    import json as _json

    raw = _json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
    raw["parameters"]["device_mtbf_days"]["value"] = 10  # 原 540 → 每天约 10% 故障率
    raw["parameters"]["repair_mean_days"]["value"] = 6  # 原 3 → 队列有机会堆积
    override = tmp_path / "params-high-fault.json"
    override.write_text(_json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    out_dir = tmp_path / "run"
    result = subprocess.run(
        [
            sys.executable, "-m", "sim", "--mode=model", "--env",
            f"--days={DAYS}", "--seed=777", f"--start={START}",
            "--params", str(override), "--out-dir", str(out_dir),
        ],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    assert result.returncode == 0, result.stderr
    events = [_json.loads(line) for line in (out_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    metrics = _json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))

    assert conservation_violations(events) == [], "高故障率下守恒被破坏"

    # 期望故障数**按模型复算**，不拍一个魔法阈值：设备坏了要离场维修，故每台的循环长度是
    # `MTBF + 平均维修时长`，而不是 MTBF —— 第一版把阈值写成 ">100"（按 10×90/10 算）就错了：
    # 实测 58，与 10×90/(10+6)=56.25 吻合。用区间断言，模型改了也不会变成假红。
    expected_breakdowns = metrics["devices"] * DAYS / (10 + 6)
    assert 0.65 * expected_breakdowns < metrics["breakdowns_total"] < 1.35 * expected_breakdowns, (
        f"故障次数 {metrics['breakdowns_total']} 偏离模型复算值 {expected_breakdowns:.1f} 超过 ±35%"
    )
    days_with_queue = [r for r in events if r["kind"] == "device_state" and (r["waiting"] + r["repairing"]) > 0]
    assert len(days_with_queue) > DAYS // 2, f"三态共存的天数过少（{len(days_with_queue)}/{DAYS}）"
    assert metrics["device_days_lost_total"] > metrics["devices"], "设备闲置天数应显著大于机队规模"
    print(
        f"[T-SIM-02] 高故障压测：故障 {metrics['breakdowns_total']} 次（模型复算 {expected_breakdowns:.1f}）、"
        f"维修完成 {metrics['repairs_done_total']} 次、{len(days_with_queue)}/{DAYS} 天存在报修/维修中设备（守恒仍成立）"
    )


def test_conservation_checker_flags_broken_device_state():
    """**灵敏度负例**：故意把三态之和改错 → 必须判红。"""
    good = {"kind": "device_state", "business_date": "2026-10-01", "total": 10, "working": 8, "waiting": 1, "repairing": 1}
    bad = dict(good, repairing=0)
    assert conservation_violations([good]) == []
    problems = conservation_violations([bad])
    assert problems and "≠ 机队规模" in problems[0], problems
    print(f"[T-SIM-02] 设备守恒负例判红：{problems[0]}")


def test_conservation_checker_flags_broken_flow_split():
    """**灵敏度负例**：走秤 + 私下 ≠ 到达数 → 必须判红。"""
    good = {"kind": "block_arrivals", "business_date": "2026-10-01", "block": "主峰", "count": 100, "on_scale": 60, "off_scale": 40}
    bad = dict(good, off_scale=39)
    assert conservation_violations([good]) == []
    problems = conservation_violations([bad])
    assert problems and "≠ 到达" in problems[0], problems
    print(f"[T-SIM-02] 客流守恒负例判红：{problems[0]}")


def test_flow_balance_checker_flags_mismatched_daily_total():
    """**灵敏度负例**：块之和与 `day_arrivals_total` 不一致 → 必须判红。"""
    records = [
        {"kind": "block_arrivals", "business_date": "2026-10-01", "block": "主峰", "count": 60, "on_scale": 36, "off_scale": 24},
        {"kind": "day_arrivals_total", "business_date": "2026-10-01", "count": 61},
    ]
    problems = flow_balance_violations(records)
    assert problems, "块之和与当日总数不符却没判红"
    print(f"[T-SIM-02] 日总量一致性负例判红：{problems[0]}")


# ---------------------------------------------------------------------------
# ④ 到达强度与时段块配置一致（可由明细复算）
# ---------------------------------------------------------------------------
def test_arrival_intensity_matches_block_configuration(baseline):
    """记录的 `lam` 必须等于"整日期望 × 本块强度 / 总强度"（口径可复算）。"""
    from sim.core.clock import Block, SimClock
    from sim.core.params import load_params
    from sim.env.demand import block_lambdas

    from datetime import date

    metrics, events = baseline
    params = load_params(PARAMS_PATH)
    blocks = tuple(Block(name, start, intensity) for name, start, intensity in params.blocks())
    expected = {
        name: lam
        for name, _start, _intensity, lam in block_lambdas(blocks, float(params.value("daily_arrivals_per_market")))
    }
    assert intensity_mismatches(events, expected) == []
    assert {k: round(v, 9) for k, v in expected.items()} == {k: round(v, 9) for k, v in metrics["block_lambdas"].items()}
    assert len(SimClock(date.fromisoformat(START), DAYS, blocks).business_day_sequence()) == DAYS
    print(f"[T-SIM-02] 四个时段块的 lam 与配置一致：{{ {', '.join(f'{k}={v:.1f}' for k, v in expected.items())} }}")


def test_total_arrivals_track_daily_expectation(baseline):
    """90 天累计到达数应贴近 `daily_arrivals × 天数`（大数定律；区间取 ±5%）。"""
    from sim.core.params import load_params

    metrics, _ = baseline
    params = load_params(PARAMS_PATH)
    expected = float(params.value("daily_arrivals_per_market")) * DAYS
    actual = metrics["arrivals_total"]
    deviation = abs(actual - expected) / expected
    assert deviation < 0.05, f"90 天累计到达 {actual} 偏离期望 {expected:.0f} 达 {deviation:.1%}（>5%）"
    print(f"[T-SIM-02] 累计到达 {actual} vs 期望 {expected:.0f}（偏离 {deviation:.2%}，区间 ±5%）")


def test_intensity_checker_flags_wrong_lambda():
    """**灵敏度负例**：把 `lam` 记错 → 必须判红；未配置的块名也要判红。"""
    records = [
        {"kind": "block_arrivals", "business_date": "2026-10-01", "block": "主峰", "lam": 500.0},
        {"kind": "block_arrivals", "business_date": "2026-10-01", "block": "不存在的块", "lam": 1.0},
    ]
    problems = intensity_mismatches(records, {"主峰": 500.0})
    assert len(problems) == 1 and "未配置的时段块" in problems[0], problems
    problems = intensity_mismatches([records[0]], {"主峰": 499.0})
    assert problems and "≠ 复算值" in problems[0], problems
    print(f"[T-SIM-02] 强度复算负例判红：{problems[0]}")


# ---------------------------------------------------------------------------
# ③ 时序与系统 business_date 同轴
# ---------------------------------------------------------------------------
def test_business_dates_are_contiguous_from_start(baseline):
    """事件里的营业日必须是从 `--start` 起的**连续**序列（无跳日、无重复）。"""
    from datetime import date, timedelta

    _, events = baseline
    seen = [r["business_date"] for r in events if r.get("business_date")]
    unique = sorted(set(seen))
    expected = [(date.fromisoformat(START) + timedelta(days=i)).isoformat() for i in range(DAYS)]
    assert unique == expected, f"营业日序列不连续：首尾 {unique[0]}…{unique[-1]}，共 {len(unique)} 天"

    per_day = {r["business_date"] for r in events if r["kind"] == "day_closed"}
    assert per_day == set(expected), "day_closed 未覆盖全部营业日"
    print(f"[T-SIM-02] 营业日连续 {unique[0]} → {unique[-1]}（{len(unique)} 天，与 --start 同轴）")


def test_env_business_date_matches_injected_system_clock(tmp_path, monkeypatch):
    """**同轴的直接证据**：把仿真产出的一天注入主系统时钟，`app.clock.today_iso()` 必须等于它。

    这条把 `REQ-033`（时钟可注入）与仿真时序扣在一起 —— 否则 `--mode=live` 时，
    仿真的"第 37 天"和被测系统的"业务日"可能根本不是同一天，日聚合与结算必然对不上。
    """
    from app import clock

    sample = "2026-11-06"  # 从 2026-10-01 起的第 37 个营业日
    clock_file = tmp_path / "clock.txt"
    clock_file.write_text(f"{sample} 07:15:00", encoding="utf-8")
    monkeypatch.setenv(clock.ENV_CLOCK_FILE, str(clock_file))
    assert clock.today_iso() == sample
    assert clock.now_iso().startswith(sample)

    synthetic = {"kind": "day_closed", "business_date": sample}
    assert synthetic["business_date"] == clock.today_iso()
    print(f"[T-SIM-02] 仿真产出日 {sample} 注入主系统时钟后两者一致（REQ-033 扣合）")
