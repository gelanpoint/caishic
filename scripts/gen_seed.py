#!/usr/bin/env python
"""种子数据生成器：按**确定性构造规则**生成 `app/seed_data/seed.json`（可复算）。

产出物 `app/seed_data/seed.json` 是**生成结果**，本文件是它的**生成规则**。
把规则入库的理由（父代理 `2026-09-30` 裁定）：**一个评审方无法复算的产物，不算可核验的产物** ——
只给 400 行 JSON 让评审"自行理解构造规则"，等同于把可核验性丢掉。

因此本文件必须满足：**删掉 `app/seed_data/seed.json` 后重跑本脚本，必须还原出逐字节一致的文件。**
机械验收（已实测）：

    python scripts/gen_seed.py --check     # 不写文件，只比对现有 seed.json 是否与生成结果一致
    python scripts/gen_seed.py             # 重新生成 app/seed_data/seed.json
    git diff --exit-code app/seed_data/seed.json   # 重生成后应无差异

## 规模与理由（父代理 `2026-09-30` 裁定「演示规模」）

| 项 | 取值 | 依据 |
| --- | --- | --- |
| 摊位 | **10** | 够展示「多摊位经营看板」；现场演示实际只用 1～2 个 |
| 标准品类 | **58**（蔬菜 20 / 水果 13 / 肉类 13 / 水产 12） | 覆盖四大类，满足「40～60 个」 |
| 摊位别名 | **1～3 个/标准品类**（本生成结果：58 组 / 122 条定义） | `REQ-002`；含真实多别名归一样本 |
| 每摊位商品 | **25**（落在「20～30」内） | 商品记录合计 **250** |
| 价目表 | 摊位 × 商品 × 2 个营业日 = **500 行** | `REQ-003` 需要日期维度 |

**为什么不是「1 个市场 × 300 摊位 × 约 50 商品/摊位 ≈ 1.5 万条」**：
① 本期是**单机演示**（`spec.md` §1.2），1.5 万条会让启动与导入变慢、现场找商品困难 —— **可演示性与现场稳定性优先**；
② 300 摊位属**方案论证范围**（市场实际规模，仅文字论证、不进容量目标），**不进种子数据**；
③ 但**四大品类 + 别名归一必须有真实样本**，否则演示不出本系统「数据不脏」的核心设计。
（范围归属错误的来龙去脉与修正留痕：`data-model.md` §5 修正注记、`discovery.md` `Q3.2`「修正 4」。）

## 构造规则（改这里就会改种子，改种子必须同步改这里）

1. **品类**：`CATS` 表逐条给出 `(编码, 标准品类名, 别名列表, 基准价 分/公斤)`；
   编码前缀即大类（`V` 蔬菜 / `F` 水果 / `M` 肉类 / `A` 水产）。**每个品类别名数必须在 1～3 之间**（脚本自检）。
2. **摊位**：`STALLS` 表给出 `(摊位号, 摊位名, 摊主名, 电话尾号, 经营的品类列表)`；
   前 8 个是专营摊（蔬菜 ×2 / 水果 ×2 / 肉类 ×2 / 水产 ×2），后 2 个是综合摊（每类 1 个 SKU）。
3. **每摊位 25 个 SKU 的分配**：`allocate()` —— 先给该摊位经营的每个品类分配 1 个 SKU，
   余量按「品类别名条数」为容量上限（≤3）**轮转补齐**；轮转起点随摊位偏移，使不同摊位的加品顺序不同。
   自检：`Σ容量 ≥ 25` 且 `品类数 ≤ 25`，否则直接断言失败（**不允许悄悄少生成几条**）。
4. **商品名 = 该品类的别名之一**（别名轮转，起点随摊位偏移）—— 保证「摊位别名 → 标准品类」可查表命中，
   且同一摊位内商品名不重复。**同一品类第 2、3 个 SKU 的价格按 `PRICE_DELTA` 微调**（品种不同价不同）。
5. **别名映射的语义（两类，须明示）**：① **同物异名**（上海青 / 小油菜 / 青菜）用于归一；
   ② **品种/细分名**（红富士苹果 / 黄元帅苹果）作为**摊位级选品入口**，让摊主无论怎么叫都能查到标准品类。
   **代价**：`REQ-002` 规定「按标准品类记账」，因此**品种粒度在统计中不保留**（红富士与黄元帅合并计入「苹果」）
   —— 这是本期有意接受的粒度取舍（同一说明见 `data-model.md` §2.5 与 `app/seed.py` docstring）。
6. **商户/摊位档案**：假名 + **明显假号**（`1380000` + 尾号）；收款标识为**脱敏形态** `RECV-<摊位号>****<尾号>`，
   **刻意不使用 `6222…` 这类卡号前缀**，避免评审/扫描工具把假数据误判成真实银行卡号（`NFR-012` / `RL-7`）。
7. **输出格式**：固定顺序 + 每条记录单行（`indent` 只用于结构层级），
   使同一规则**每次生成逐字节相同**（`--check` 与 `git diff` 才有意义）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app import config  # noqa: E402  （必须在 sys.path 处理之后导入）

# --------------------------------------------------------------------------
# 构造参数（改这里 = 改种子；两处必须同时改，靠 --check 兜住）
# --------------------------------------------------------------------------

# (编码, 标准品类名, 别名列表, 基准价 分/公斤)
CATS: list[tuple[str, str, list[str], int]] = [
    # ---------------- 蔬菜 20 ----------------
    ("V-01", "上海青", ["上海青", "小油菜", "青菜"], 480),
    ("V-02", "大白菜", ["大白菜", "白菜"], 260),
    ("V-03", "菠菜", ["菠菜"], 700),
    ("V-04", "生菜", ["生菜", "叶生菜"], 560),
    ("V-05", "空心菜", ["空心菜", "通菜", "蕹菜"], 620),
    ("V-06", "芹菜", ["芹菜", "香芹"], 520),
    ("V-07", "韭菜", ["韭菜"], 680),
    ("V-08", "黄瓜", ["黄瓜", "青瓜"], 480),
    ("V-09", "西红柿", ["西红柿", "番茄"], 620),
    ("V-10", "茄子", ["茄子", "矮瓜"], 520),
    ("V-11", "青椒", ["青椒", "菜椒"], 760),
    ("V-12", "土豆", ["土豆", "马铃薯", "洋芋"], 360),
    ("V-13", "白萝卜", ["白萝卜", "萝卜"], 240),
    ("V-14", "胡萝卜", ["胡萝卜", "红萝卜"], 320),
    ("V-15", "豆角", ["豆角", "四季豆"], 900),
    ("V-16", "莲藕", ["莲藕", "藕"], 1200),
    ("V-17", "苦瓜", ["苦瓜", "凉瓜"], 640),
    ("V-18", "冬瓜", ["冬瓜"], 300),
    ("V-19", "南瓜", ["南瓜"], 420),
    ("V-20", "香菜", ["香菜", "芫荽"], 1600),
    # ---------------- 水果 13 ----------------
    ("F-01", "苹果", ["苹果", "红富士苹果", "黄元帅苹果"], 1200),
    ("F-02", "香蕉", ["香蕉", "小米蕉"], 700),
    ("F-03", "橙子", ["橙子", "脐橙", "血橙"], 1000),
    ("F-04", "砂糖橘", ["砂糖橘", "沙糖桔"], 1400),
    ("F-05", "葡萄", ["葡萄", "巨峰葡萄"], 1600),
    ("F-06", "梨", ["梨", "雪梨"], 900),
    ("F-07", "西瓜", ["西瓜", "麒麟瓜"], 480),
    ("F-08", "桃子", ["桃子", "水蜜桃"], 1400),
    ("F-09", "李子", ["李子", "青李"], 1600),
    ("F-10", "芒果", ["芒果", "台农芒"], 1800),
    ("F-11", "猕猴桃", ["猕猴桃", "奇异果"], 2000),
    ("F-12", "火龙果", ["火龙果", "红心火龙果"], 1300),
    ("F-13", "柠檬", ["柠檬", "黄柠檬"], 2200),
    # ---------------- 肉类 13 ----------------
    ("M-01", "猪五花肉", ["猪五花肉", "五花肉", "上五花"], 3600),
    ("M-02", "猪里脊", ["猪里脊", "里脊肉"], 4200),
    ("M-03", "猪排骨", ["猪排骨", "肋排", "小排"], 4800),
    ("M-04", "猪蹄", ["猪蹄", "猪脚"], 3200),
    ("M-05", "猪肝", ["猪肝"], 2400),
    ("M-06", "牛腩", ["牛腩", "牛腩肉"], 7600),
    ("M-07", "牛腱子", ["牛腱子", "牛腱"], 8800),
    ("M-08", "羊肉", ["羊肉", "羊腿肉"], 9000),
    ("M-09", "整鸡", ["整鸡", "白条鸡"], 2600),
    ("M-10", "鸡翅", ["鸡翅", "翅中"], 3800),
    ("M-11", "鸭肉", ["鸭肉", "老鸭"], 3000),
    ("M-12", "猪梅花肉", ["猪梅花肉", "梅花肉"], 4400),
    ("M-13", "牛里脊", ["牛里脊", "牛柳"], 9600),
    # ---------------- 水产 12 ----------------
    ("A-01", "草鱼", ["草鱼", "鲩鱼"], 1800),
    ("A-02", "鲫鱼", ["鲫鱼", "鲫瓜子", "喜头鱼"], 2200),
    ("A-03", "鲈鱼", ["鲈鱼", "海鲈鱼"], 3600),
    ("A-04", "带鱼", ["带鱼", "冻带鱼"], 2600),
    ("A-05", "黄花鱼", ["黄花鱼", "黄鱼"], 3200),
    ("A-06", "基围虾", ["基围虾", "对虾", "明虾"], 7600),
    ("A-07", "小龙虾", ["小龙虾", "龙虾", "麻小"], 5800),
    ("A-08", "蛤蜊", ["蛤蜊", "花甲"], 1600),
    ("A-09", "螃蟹", ["螃蟹", "大闸蟹", "梭子蟹"], 8800),
    ("A-10", "鱿鱼", ["鱿鱼", "鲜鱿鱼"], 2400),
    ("A-11", "黄鳝", ["黄鳝", "鳝鱼"], 5600),
    ("A-12", "泥鳅", ["泥鳅", "泥鳅鱼"], 3400),
]

_GROUP_BY_PREFIX = {"V": "蔬菜", "F": "水果", "M": "肉类", "A": "水产"}

CAT_BY_CODE = {c[0]: c for c in CATS}
VEG = [c[0] for c in CATS if c[0].startswith("V")]
FRUIT = [c[0] for c in CATS if c[0].startswith("F")]
MEAT = [c[0] for c in CATS if c[0].startswith("M")]
FISH = [c[0] for c in CATS if c[0].startswith("A")]

# (摊位号, 摊位名, 摊主名, 电话尾号, 经营的品类列表)
STALLS: list[tuple[str, str, str, str, list[str]]] = [
    ("A-01", "老张蔬菜摊", "张建国", "0001", VEG),
    ("A-02", "李姐蔬菜摊", "李秀兰", "0002", VEG),
    ("A-03", "陈记水果摊", "陈国强", "0003", FRUIT),
    ("A-04", "赵家水果摊", "赵桂芳", "0004", FRUIT),
    ("A-05", "孙记肉铺", "孙志远", "0005", MEAT),
    ("A-06", "周记鲜肉", "周建华", "0006", MEAT),
    ("A-07", "吴记水产", "吴海燕", "0007", FISH),
    ("A-08", "郑记水产", "郑小龙", "0008", FISH),
    ("A-09", "刘记综合摊", "刘美玲", "0009", VEG[:8] + FRUIT[:6] + MEAT[:6] + FISH[:5]),
    ("A-10", "何记综合摊", "何丽娟", "0010", VEG[8:16] + FRUIT[6:13] + MEAT[6:13] + FISH[5:8]),
]

SKUS_PER_STALL = 25
MAX_SKUS_PER_CATEGORY = 3          # 与「每品类 1～3 个别名」同源：别名条数即该品类的 SKU 容量
# 同一品类的第 2、3 个 SKU 的价格微调（分/公斤）——品种不同价不同
PRICE_DELTA = (0, 120, -80)
# 秤端快捷键：10 个数字 + 16 个字母（够 25 个 SKU，且长度 ≤4，符合 data-model.md §2.6）
HOTKEYS = list("1234567890") + [chr(ord("A") + i) for i in range(16)]

BUSINESS_DATE_OFFSETS = {"previous_day": -1, "current_day": 0}
SEED_NOTE = (
    "演示规模种子数据（父代理 2026-09-30 裁定）：10 摊位 / 58 标准品类"
    "（蔬菜·水果·肉类·水产）/ 每摊位 25 商品 / 250 商品。"
    "构造规则与可复算验收见 scripts/gen_seed.py（REQ-025 的唯一导入清单来源）。"
)


def allocate(codes: list[str], stall_index: int) -> dict[str, int]:
    """给一个摊位分配 25 个 SKU：先每品类 1 个，余量按容量（别名条数，≤3）轮转补齐。"""
    cap = {c: min(len(CAT_BY_CODE[c][2]), MAX_SKUS_PER_CATEGORY) for c in codes}
    if len(codes) > SKUS_PER_STALL:
        raise AssertionError(f"摊位经营的品类数 {len(codes)} 超过每摊位 SKU 数 {SKUS_PER_STALL}")
    if sum(cap.values()) < SKUS_PER_STALL:
        raise AssertionError(
            f"品类容量不足：Σ容量={sum(cap.values())} < {SKUS_PER_STALL}（"
            f"需给这些品类补充别名，或让该摊位经营更多品类）"
        )
    plan = {c: 1 for c in codes}
    need = SKUS_PER_STALL - len(codes)
    # 轮转起点随摊位偏移，让不同摊位的加品顺序不同（更像真实的摊位差异）
    offset = stall_index % max(len(codes), 1)
    order = codes[offset:] + codes[:offset]
    i = 0
    while need > 0:
        code = order[i % len(order)]
        if plan[code] < cap[code]:
            plan[code] += 1
            need -= 1
        i += 1
    if sum(plan.values()) != SKUS_PER_STALL:
        raise AssertionError(f"SKU 分配结果不是 {SKUS_PER_STALL}：{plan}")
    return plan


def build() -> tuple[list[dict], list[dict]]:
    """按构造参数生成（并自检）种子数据结构。"""
    categories: list[dict] = []
    for code, name, aliases, price in CATS:
        if not 1 <= len(aliases) <= 3:
            raise AssertionError(f"标准品类 {code} 的别名数必须在 1~3 之间：{aliases}")
        categories.append(
            {
                "code": code,
                "name": name,
                "group": _GROUP_BY_PREFIX[code[0]],
                "aliases": aliases,
                "base_price_cents": price,
            }
        )

    stalls: list[dict] = []
    for stall_index, (stall_no, stall_name, merchant, phone_tail, codes) in enumerate(STALLS):
        plan = allocate(codes, stall_index)
        products: list[dict] = []
        for code in codes:
            aliases, price = CAT_BY_CODE[code][2], CAT_BY_CODE[code][3]
            start = stall_index % len(aliases)  # 别名轮转：不同摊位用不同叫法
            for k in range(plan[code]):
                products.append(
                    {
                        "name": aliases[(start + k) % len(aliases)],
                        "category_code": code,
                        "icon_key": code,
                        "hotkey": HOTKEYS[len(products)],
                        "unit_price_cents": price + PRICE_DELTA[k],
                    }
                )
        # ---- 自检：数量、摊位内商品名唯一、别名表唯一约束可满足、商品名取自别名表 ----
        if len(products) != SKUS_PER_STALL:
            raise AssertionError(f"摊位 {stall_no} 商品数 {len(products)} ≠ {SKUS_PER_STALL}")
        names = [p["name"] for p in products]
        if len(set(names)) != len(names):
            raise AssertionError(f"摊位 {stall_no} 商品名重复：{names}")
        all_aliases = [a for c in codes for a in CAT_BY_CODE[c][2]]
        if len(set(all_aliases)) != len(all_aliases):
            raise AssertionError(f"摊位 {stall_no} 的别名重复（违反 (stall_id, alias_name) 唯一）：{all_aliases}")
        if not set(names) <= set(all_aliases):
            raise AssertionError(f"摊位 {stall_no} 的商品名未取自别名表：{names}")
        stalls.append(
            {
                "stall_no": stall_no,
                "stall_name": stall_name,
                "payment_receiver_token": f"RECV-{stall_no}****{phone_tail}",
                "merchant": {"name": merchant, "phone": "1380000" + phone_tail},
                "products": products,
            }
        )
    return categories, stalls


def render(categories: list[dict], stalls: list[dict]) -> str:
    """固定格式渲染（每次生成逐字节相同 —— 这是 `--check` 与 `git diff` 成立的前提）。"""
    out: list[str] = ["{"]
    out.append('  "seed_version": 1,')
    out.append(f'  "note": {json.dumps(SEED_NOTE, ensure_ascii=False)},')
    out.append('  "business_date_offsets": {')
    out.append(f'    "previous_day": {BUSINESS_DATE_OFFSETS["previous_day"]},')
    out.append(f'    "current_day": {BUSINESS_DATE_OFFSETS["current_day"]}')
    out.append("  },")
    out.append('  "categories": [')
    for i, cat in enumerate(categories):
        out.append("    " + json.dumps(cat, ensure_ascii=False) + ("" if i == len(categories) - 1 else ","))
    out.append("  ],")
    out.append('  "stalls": [')
    for i, stall in enumerate(stalls):
        out.append("    {")
        out.append('      "stall_no": %s,' % json.dumps(stall["stall_no"], ensure_ascii=False))
        out.append('      "stall_name": %s,' % json.dumps(stall["stall_name"], ensure_ascii=False))
        out.append('      "payment_receiver_token": %s,' % json.dumps(stall["payment_receiver_token"], ensure_ascii=False))
        out.append('      "merchant": %s,' % json.dumps(stall["merchant"], ensure_ascii=False))
        out.append('      "products": [')
        for j, product in enumerate(stall["products"]):
            out.append("        " + json.dumps(product, ensure_ascii=False) + ("" if j == len(stall["products"]) - 1 else ","))
        out.append("      ]")
        out.append("    }" + ("" if i == len(stalls) - 1 else ","))
    out.append("  ]")
    out.append("}")
    return "\n".join(out) + "\n"


def generate_text() -> str:
    """生成种子文件全文（供写入与 `--check` 共用，避免两处各生成一遍而漂移）。"""
    categories, stalls = build()
    return render(categories, stalls)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按确定性规则生成 app/seed_data/seed.json（可复算）")
    parser.add_argument("--out", default=None, help=f"输出路径（默认 {config.SEED_FILE}）")
    parser.add_argument("--check", action="store_true", help="只与现有文件比对，不写入；不一致时退出码 2")
    args = parser.parse_args(argv)

    target = Path(args.out) if args.out else Path(config.SEED_FILE)
    text = generate_text()

    if args.check:
        if not target.is_file():
            print(f"[不一致] 目标文件不存在：{target}")
            return 2
        current = target.read_text(encoding="utf-8")
        if current == text:
            print(f"[一致] {target} 与生成规则逐字节相同（{len(text.encode('utf-8'))} 字节）")
            return 0
        print(f"[不一致] {target} 与生成规则不同 —— 说明种子文件被手改过或规则已变更；"
              f"请重跑 python scripts/gen_seed.py 后一并提交")
        return 2

    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：固定 LF，避免跨平台行尾差异让「逐字节一致」失效（.gitattributes 同口径）
    target.write_text(text, encoding="utf-8", newline="\n")
    summary = (
        f"品类 {len(CATS)} / 摊位 {len(STALLS)} / 商品 {SKUS_PER_STALL * len(STALLS)}"
        f" / 别名定义 {sum(len(c[2]) for c in CATS)}"
    )
    print(f"[已生成] {target}（{len(text.encode('utf-8'))} 字节）　{summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
