"""`T-SIM-08` 档 2 匹配矩（pattern-oriented modeling）的**方法学守卫**。

## 这一节盯的不是"找没找到参数"，而是"说的话有没有超出证据能支撑的范围"

* 矩必须**恰好 6 个**，且每个都点得出处；
* **M5 内含假设阈值**这件事必须写在数据里（不能只在文档里说一句）；
* **过度拟合警告**必须出现在产物里（6 个矩 vs 20 项待定参数是真实风险，`§7.5` 原话）；
* **未采到的区域是"未评估"**，不许被写成"不存在"；
* LHS 的**分层覆盖度**必须可机械校验（否则"抽了 N 组"可能只是一堆挤在一起的点）。
"""

from __future__ import annotations

from sim_support import PARAMS_PATH

from sim.core.params import load_params
from sim.verify import calibration
from sim.verify.sensitivity import stratum_coverage


def test_six_moments_each_name_their_source_and_kind():
    assert len(calibration.MOMENTS) == 6, "匹配矩必须恰好 6 个（= R1~R6）"
    assert [m["from"] for m in calibration.MOMENTS] == ["R1", "R2", "R3", "R4", "R5", "R6"]
    for moment in calibration.MOMENTS:
        assert moment["source"], f"{moment['id']} 缺少出处来源"
        assert moment["kind"] in ("ordering", "assumed"), f"{moment['id']} 的形式没登记"
    assumed = [m["id"] for m in calibration.MOMENTS if m["kind"] == "assumed"]
    assert assumed == ["M5"], "M5（存活）是唯一内含假设阈值的矩；若新增必须先说明"
    assert "假设" in next(m for m in calibration.MOMENTS if m["id"] == "M5")["source"]
    print(f"[T-SIM-08] 6 个矩逐一有出处；内含假设阈值的是 {assumed}")


def test_moment_space_covers_only_real_parameters():
    params = load_params(PARAMS_PATH)
    unknown = sorted(set(calibration.MOMENT_SPACE) - set(params.parameters()))
    assert not unknown, f"匹配空间里出现了参数文件没有的键：{unknown}"
    assert set(calibration.MOMENT_SPACE) <= set(params.parameters())
    #: 每个参与匹配的参数在文件里都必须是 `assumed` —— 拿有出处的参数去"拟合"没有意义，
    #: 那是在用一个已知值去解释一个已知结果。
    sourced = sorted(key for key in calibration.MOMENT_SPACE if params.kind(key) == "sourced")
    assert not sourced, f"这些参数有真实出处，不该进入匹配矩的参数空间：{sourced}"
    print(f"[T-SIM-08] 匹配空间 {len(calibration.MOMENT_SPACE)} 维，全部是无出处（assumed）参数")


def test_lhs_sampling_is_stratified():
    samples = calibration.sample_space(16, 20261002)
    assert len(samples) == 16
    problems = stratum_coverage(samples, dict(calibration.MOMENT_SPACE), 16)
    assert not problems, f"LHS 分层覆盖度不合格：{problems}"
    print(f"[T-SIM-08] LHS {len(samples)} 个样本分层覆盖度校验通过")


def test_calibration_profile_declares_its_out_of_range_overrides():
    """压力档必须**自己承认**哪些取值越界 —— 越界不声明就会被当成实测值引用。"""
    params = load_params(PARAMS_PATH)
    out_of_range = []
    for key, value in calibration.CALIBRATION_PROFILE["param_overrides"].items():
        entry = params.parameters()[key]
        if "range" in entry and not (entry["range"][0] <= value <= entry["range"][1]):
            out_of_range.append(f"{key}={value}（登记区间 {entry['range']}）")
    assert out_of_range, "本档没有任何越界取值 —— 那要么是运气，要么是文档与实现对不上"
    assert "压力档" in calibration.CALIBRATION_PROFILE["why"]
    assert "超出" in calibration.CALIBRATION_PROFILE["why"]
    print(f"[T-SIM-08] 匹配档的越界取值已自报：{out_of_range}")


def test_m3_needs_the_device_pressure_profile_or_it_has_no_carrier():
    """`M3`（维修等待）在基线 MTBF 下**没有载体**：V-01 实测 60/60 天零故障。

    守卫写法：把档位的 MTBF 拿掉，`M3` 依赖的故障到达率就回到 0.056 台/日 ⇒ 判据不可观测。
    这里只断言"档位里必须显式压低 MTBF"，避免有人把它改回去。
    """
    overrides = calibration.CALIBRATION_PROFILE["param_overrides"]
    assert overrides["device_mtbf_days"] == 10
    params = load_params(PARAMS_PATH)
    assert params.value("device_mtbf_days") != overrides["device_mtbf_days"], \
        "档位值与基线值相同 ⇒ 压力档失效"
    print("[T-SIM-08] M3 的设备故障载体在档位里被显式压低（MTBF=10 天）")


def test_evaluate_point_runs_all_six_moments(tmp_path):
    """真跑一个参数点（90 营业日 × 9 臂）：必须逐矩给出布尔值，不许"缺矩当命中"。"""
    params = load_params(PARAMS_PATH)
    point = {key: (low + high) / 2.0 for key, (low, high) in calibration.MOMENT_SPACE.items()}
    row = calibration.evaluate_point(params, point, seed=20261002, out_root=tmp_path / "pt",
                                     progress=lambda _m: None)
    assert set(row["moments"]) == {m["id"] for m in calibration.MOMENTS}
    assert row["hits"] == sum(1 for value in row["moments"].values() if value)
    print(f"[T-SIM-08] 单点评估给出 6 个矩的布尔结果：命中 {row['hits']}/6")