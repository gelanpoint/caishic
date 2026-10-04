"""`T-SIM-12`：**`sourced` 参数的取值必须等于报告原文写的那个数**（人工对照表的可执行版本）。

## 为什么从 `test_param_provenance.py` 拆出来

那份文件管的是**出处字段本身**（`kind` 合不合法、`ref` 指向的结论号存不存在、`Q-xx` 有没有归
`assumed`）。本文件管的是**另一层**：`sourced` 的**取值**对不对得上报告里那个数。
两层规则的权威都是同一个 `sim.core.params`（`conclusion_blocks` / `SOURCED_REF_RE`），
**这里不另写一套校验**，只是分工；合在一起会让两个文件都撞上单文件 400 行上限。

## 人工对照表

`provenance_problems` 只查形态、查不出"这个参数对应结论里的哪个量、折算成多少分"。
那是人判断的 —— 但**人判断完之后可以把结论固化成数据**：下表就是那份人工对照表，
每行 `参数键 -> (ref 结论号, 报告原文片段, 折算后的值/区间)`。
用例做两件机械的事：① 报告原文片段**必须真的出现在**那条结论里（防止表抄错 ref）；
② 参数的 `value`/`range` 必须等于折算值。

⚠️ 这张表已查出并修掉**一处 10 倍量级错**：`network_refit_cents_per_market`
原值 12,050,000 分（= 12.05 万元），而结论 25 原文与该条目自己的 `quote` 都写 120.5 万元
= 120,500,000 分。**这正是这张表存在的理由** —— 形态校验查不出值与自己的 quote 打架。
"""

from __future__ import annotations

import json

import sim_support  # noqa: F401
from sim_support import PARAMS_PATH, REPORT_PATH

from sim.core.params import Params, conclusion_blocks, load_params, provenance_problems

#: `参数键 -> (结论号, 报告原文里必须存在的片段, 折算后的 value, 折算后的 range 或 None)`
SOURCED_AMOUNT_TABLE: dict[str, tuple[int, str, object, object]] = {
    "admin_fine_market_operator_cents": (2, "市场开办者被罚 2 万元", 2_000_000, None),
    "bank_funding_cents_3y": (19, "三年期 200 余万元", 200_000_000, None),
    "channel_fee_rate_bp": (21, "费率是 0.38%–0.6%", 38, [38, 60]),
    "channel_fee_rate_bp_range": (21, "费率是 0.38%–0.6%", None, [38, 60]),
    "device_screen_unit_price_cents": (24, "摊位屏 1100 元/台", 110_000, None),
    "merchant_cloud_software_cents_per_year": (26, "云软件 5000 元/年", 500_000, None),
    "merchant_self_funded_scale_cents": (26, "智慧电子秤 3750 元/台", 375_000, None),
    "network_refit_cents_per_market": (25, "合计占整个 366.9 万元标的的 33%", 120_500_000, None),
    "scale_unit_price_cents": (23, "普通溯源秤 **1350 元/台**", 135_000, [135_000, 256_600]),
    "subsidy_ratio_bp": (3, "智慧菜场按审计实际投入的 50% 补贴", 5_000, None),
    "verification_sampling_bp": (4, "按照 10% 的比例抽检验收", 1_000, None),
    "wenzhou_buyer_rate_bp_range": (12, "收取交易额的 **0.5%–6%**", None, [50, 600]),
}


#: `sourced` 里**取值不是报告原文里那个数**的例外名单。
#:
#: ⚠️ **父代理裁定（2026-10-03）后已清空**。原名单里唯一的一项是 `short_weight_ratio`：
#: 结论 13 只给出**定性**表述（"不能再搞八两秤了"），报告里没有任何短秤幅度数字；
#: 取值 0.2 是把俗语「八两秤」读作 8 两 / 1 斤 = 0.8 得到的**算术推算**。
#: 当年的例外理由是"**行为事实**有出处、**幅度**没有"—— 父代理裁定这不成立：
#: 标 `sourced` 等于把一个推算值包装成有出处的事实，故该参数已**降为 `assumed`**。
#:
#: **刻意保留这个空常量而不是删掉**：它让"清空"成为一条**写死**的断言
#: （`test_the_exception_list_is_empty`），下次再有人想把推算值塞回 `sourced`，
#: 要跨过的是一行被用例守着的红线，而不是一个可以悄悄绕过的隐式约定。
DERIVED_NOT_IN_TABLE: set[str] = set()


def sourced_amount_problems(params, blocks: dict[int, str], table=None) -> list[str]:
    """**人工对照表的判定函数**（纯函数，故合成负例可以直接喂伪造的 `Params`）。

    两关都过才算过：① 报告原文里**真的**有那句话（表没把 ref 抄错）；
    ② 参数取值真的等于它（没抄错也没算错）。
    """
    table = SOURCED_AMOUNT_TABLE if table is None else table
    problems: list[str] = []
    for key, (number, fragment, value, rng) in table.items():
        if fragment not in blocks.get(number, ""):
            problems.append(f"{key}: 结论 {number} 原文里找不到 {fragment!r}（对照表抄错了 ref？）")
        entry = params.parameters().get(key)
        if entry is None:
            problems.append(f"{key}: 参数文件里没有这一项")
            continue
        if value is not None and entry.get("value") != value:
            problems.append(f"{key}: 取值 {entry.get('value')} ≠ 报告折算值 {value}")
        if rng is not None and list(entry.get("range") or []) != list(rng):
            problems.append(f"{key}: 区间 {entry.get('range')} ≠ 报告折算区间 {rng}")
    return problems


def derived_magnitude_problems(params) -> list[str]:
    """`sourced` 条目若在 `quote`/`calibration` 里**自认**「幅度无出处 / 取值是推算」⇒ 判红（空 = 合规）。

    ## 为什么它**不看** `DERIVED_NOT_IN_TABLE`

    那个名单现在是空的，而"遍历一个空名单"是**死分支** —— 检查自身空转比没有检查更坏
    （同一个坑本文件上方已经吃过一次：`sim_support.DEMO_DB` 的文件名常量写错，
    那条守卫就永远走 skip 分支、永远不报信）。所以守卫**直接读参数文件**：
    只要任何 `sourced` 条目自己写下"我这个幅度没有出处"，就说明它违反了本项目的分界线
    （`AGENTS.md` §3 硬要求 8：无出处的参数只能给方向，不能给现场数值），
    它应当标 `assumed` 而不是 `sourced`。**名单空不空，它照样判得红。**

    这条守卫的判别力也高于形态校验：`provenance_problems` 只看 `ref` 的形态，
    一条 `ref` 写得工工整整（`结论 13`）的 `sourced` 条目完全能带着"幅度是推算"过关。
    """
    problems: list[str] = []
    for key, entry in params.parameters().items():
        prov = entry.get("provenance") or {}
        if prov.get("kind") != "sourced":
            continue
        text = f"{prov.get('quote') or ''} {prov.get('calibration') or ''}".strip()
        if "无出处" in text or "推算" in text:
            problems.append(
                f"{key} 标为 sourced，却在自己的出处文字里承认『幅度无出处 / 取值是推算』：{text!r}"
                " —— 『行为事实有出处、幅度没有』是 **assumed**（父代理 2026-10-03 裁定，"
                "`short_weight_ratio` 即是判例）")
    return problems


# ---------------------------------------------------------------------------
# 主闸门与负例
# ---------------------------------------------------------------------------
def test_sourced_amounts_equal_the_figure_written_in_the_report():
    """**人工对照表的机械断言**：参数取值 == 报告原文写的那个金额。"""
    blocks = conclusion_blocks(REPORT_PATH.read_text(encoding="utf-8"))
    problems = sourced_amount_problems(load_params(PARAMS_PATH), blocks)
    assert problems == [], "sourced 参数的取值与报告原文对不上：\n  " + "\n  ".join(problems)
    print(f"[T-SIM-12] {len(SOURCED_AMOUNT_TABLE)} 个 sourced 参数的取值 == 报告原文金额（逐条核对通过）")


def test_every_sourced_amount_param_is_in_the_table():
    """**防漏守卫**：`sourced` 参数**每一个**都必须在这张对照表里有行（或在下面的例外名单里）。

    漏一行就等于放弃一个参数的复核，而漏了不会有人发现 —— 故让它显式爆炸。
    """
    params = load_params(PARAMS_PATH)
    missing = sorted(set(params.sourced_ids()) - set(SOURCED_AMOUNT_TABLE) - set(DERIVED_NOT_IN_TABLE))
    assert missing == [], (
        f"这些 sourced 参数既不在金额对照表、也没登记进 DERIVED_NOT_IN_TABLE（取值不是报告里的原数）：{missing}"
    )
    print(f"[T-SIM-12] {len(params.sourced_ids())} 个 sourced 参数全部有交代；"
          f"例外名单 {sorted(DERIVED_NOT_IN_TABLE) or '（已清空 —— 无「行为有出处、幅度无出处」的例外）'}")


def test_the_exception_list_is_empty():
    """**父代理裁定（2026-10-03）的写死断言**：例外名单已清空。

    单独成一条而不是塞进上面那条：清空是**本轮的产物**，必须有自己的断言与失败信息，
    否则"有人又加回一项"只会表现为一句含糊的 `missing` 报错。
    """
    assert DERIVED_NOT_IN_TABLE == set(), (
        f"例外名单被重新加了项：{sorted(DERIVED_NOT_IN_TABLE)} —— "
        "『行为事实有出处、幅度是推算』不再允许作为 `sourced` 的理由；"
        "这类参数一律标 `assumed`，并把推算过程写进 `calibration`（`short_weight_ratio` 是判例）")


def test_no_sourced_entry_may_carry_a_derived_magnitude():
    """**本轮新增的降级守卫**：仓库里**没有**任何一条 `sourced` 参数自认「幅度是推算」。

    ⚠️ 与 `DERIVED_NOT_IN_TABLE` 的区别正是"名单为空也要干活"：本用例**不遍历名单**，
    它遍历的是**参数文件里全部 `sourced` 条目**，所以名单清空之后它照样有判别力。
    """
    params = load_params(PARAMS_PATH)
    problems = derived_magnitude_problems(params)
    assert problems == [], "有 sourced 参数的取值其实是推算出来的：\n  " + "\n  ".join(problems)
    print(f"[T-SIM-12] {len(params.sourced_ids())} 个 sourced 参数逐条检查："
          f"**无一条**以『行为有出处、幅度无出处』为由混进 `sourced`（例外名单已清空）")


def test_reinstating_the_sourced_kind_is_red():
    """**灵敏度负例**：把本轮降级**关掉**（`short_weight_ratio` 改回 `sourced`）⇒ 守卫必须判红。

    这一条是"这次改动有没有被真正守住"的直接答案。负例的关键在于**前提也要合法**：
    伪造时把 `ref` 写成完全符合 `SOURCED_REF_RE` 的 `结论 13`，于是
    `provenance_problems` **不会**报错 —— 判红的只能是我们新加的降级守卫本身，
    而不是被顺带触发的形态校验（那就成了测另一件事）。
    """
    raw = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
    assert derived_magnitude_problems(Params(raw, "合成文件")) == [], "负例前提：降级生效时必须先合规"

    entry = raw["parameters"]["short_weight_ratio"]
    entry["provenance"] = {"kind": "sourced", "ref": "结论 13",
                           "quote": "行为有出处，但幅度 0.2 是把『八两秤』按 8 两/1 斤 推算的，无出处"}
    assert provenance_problems("short_weight_ratio", entry, "合成") == [], \
        "负例前提：伪造条目本身必须先通过形态校验（否则判红的是形态校验，不是降级守卫）"

    problems = derived_magnitude_problems(Params(raw, "合成文件"))
    assert any("short_weight_ratio" in problem for problem in problems), (
        f"把 `short_weight_ratio` 改回 sourced（推算幅度）却没判红：{problems}")
    print(f"[T-SIM-12] 负例：`short_weight_ratio` 改回 sourced（形态合规、幅度是推算）⇒ 判红\n  {problems[0]}")


def test_amount_table_goes_red_when_a_value_contradicts_the_report():
    """**灵敏度负例**：把取值改回本轮修掉的那个 **10 倍错** ⇒ 判定函数必须报出问题。

    负例必须**真跑判定函数**才算数：只断言"伪造值 ≠ 期望值"证明不了这张表会红。
    用**内存里的 `Params`** 改（不碰磁盘），证明它读的是参数而不是自己抄了一份常量。
    """
    blocks = conclusion_blocks(REPORT_PATH.read_text(encoding="utf-8"))
    raw = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
    key = "network_refit_cents_per_market"
    _number, _fragment, value, _rng = SOURCED_AMOUNT_TABLE[key]
    assert sourced_amount_problems(Params(raw, "合成文件"), blocks) == [], "负例前提：伪造前必须先合规"
    raw["parameters"][key]["value"] = 12_050_000  # 本轮修掉的 10 倍量级错（= 12.05 万元）
    problems = sourced_amount_problems(Params(raw, "合成文件"), blocks)
    assert any(key in problem for problem in problems), f"退回 10 倍错值却没判红：{problems}"
    assert f"{value}" in problems[0], f"报错信息应当点出正确值 {value}：{problems[0]}"
    print(f"[T-SIM-12] 负例：`network_refit` 退回 12,050,000 分（10 倍错）⇒ 判红（{problems[0]}）")


def test_amount_table_goes_red_when_a_ref_points_at_the_wrong_conclusion():
    """**灵敏度负例（第二问）**：把对照表某行的结论号抄错 ⇒ 必须判红（防止"表抄错 ref"静默通过）。"""
    blocks = conclusion_blocks(REPORT_PATH.read_text(encoding="utf-8"))
    wrong = {**SOURCED_AMOUNT_TABLE,
             "subsidy_ratio_bp": (12, SOURCED_AMOUNT_TABLE["subsidy_ratio_bp"][1], 5_000, None)}
    problems = sourced_amount_problems(load_params(PARAMS_PATH), blocks, table=wrong)
    assert problems and "抄错了 ref" in problems[0], f"把结论 3 的原文错记成结论 12 却没判红：{problems}"
    print(f"[T-SIM-12] 负例：把 `subsidy_ratio_bp` 的出处错记为结论 12 ⇒ 判红（{problems[0]}）")


def test_conclusion_block_slices_only_its_own_paragraph():
    """**负例的前提守卫**：`conclusion_blocks(13)` 不得串进结论 26 的正文（否则对照表会抄到邻居的数）。"""
    blocks = conclusion_blocks(REPORT_PATH.read_text(encoding="utf-8"))
    assert set(blocks) == set(range(1, 27)), f"结论块不完整：{sorted(blocks)}"
    assert "八两秤" in blocks[13], "结论 13 的正文没切到"
    assert "云软件 5000 元/年" not in blocks[13], "结论 13 的块里串进了结论 26 的原文 ⇒ 切分坏了"
    assert "云软件 5000 元/年" in blocks[26], "结论 26 的正文没切到"
    assert "普通溯源秤" not in blocks[26], "结论 26 的块里串进了结论 23 的原文 ⇒ 切分坏了"
    print("[T-SIM-12] 结论块按标题切分：13/26 各自只含自己的原文（未串到邻居）")


def test_the_report_really_has_no_short_weight_magnitude():
    """**降级裁定的事实前提**：结论 13 只有定性表述，报告里**没有**任何短秤幅度数字。

    没有这一条，"降为 `assumed`" 就只是一句判断；有了它，裁定本身也是可复核的 ——
    任何人可以直接打开报告，确认 `八两秤` 那句话前后没有 `20%` / `0.2` 之类的量。
    """
    block = conclusion_blocks(REPORT_PATH.read_text(encoding="utf-8"))[13]
    assert "八两秤" in block, "结论 13 的原文没切到（前提坏了）"
    for magnitude in ("八两秤的", "短秤幅度", "20%", "0.2", "八比十"):
        assert magnitude not in block, (
            f"结论 13 里出现了『{magnitude}』—— 若报告真的写了短秤幅度，"
            "`short_weight_ratio` 就该回到 `sourced`，降级裁定需要重新评估")
    print("[T-SIM-12] 结论 13 原文只有『不能再搞八两秤了』这一定性表述，**无任何幅度数字**"
          " ⇒ `short_weight_ratio` 降 `assumed` 的事实前提成立")