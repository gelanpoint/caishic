"""种子数据导入（`T-005`；`REQ-001` / `REQ-002` / `REQ-003` / `REQ-025`）。

**只读本地文件、不依赖外网**（`REQ-025`）：数据源是仓库内的 `app/seed_data/seed.json`，
本模块不做任何网络访问，也不引入第三方依赖（只用标准库 `json` / `sqlite3`）。

## 导入清单（严格按 `REQ-025` 与 `discovery.md` `Q3.1`，不多不少）

商户与摊位档案（`REQ-001`）、标准品类字典（`REQ-002`）、摊位别名 → 标准品类映射（`REQ-002`）、
商品（`REQ-003`/`REQ-004`）、价目表（`REQ-003`）。

**不导入**：交易 / 支付 / 退货 / 佣金口径 / 日聚合 / 结算单 —— 按 `discovery.md` `Q3.1`，
这些属「**使用中产生**」，不是种子数据；佣金口径由运营端配置（`REQ-017`）。

## 种子规模与理由（父代理 `2026-09-30` 裁定「演示规模」）

| 项 | 规模 |
| --- | --- |
| 摊位 | **10 个**（够展示多摊位经营看板；现场演示实际只用其中 1～2 个） |
| 标准品类字典 | **58 个**，覆盖**蔬菜 / 水果 / 肉类 / 水产**四大类（20 / 13 / 13 / 12） |
| 摊位别名映射 | **每个标准品类 1～3 个别名**（含真实多别名样本：「上海青 / 小油菜 / 青菜」→ 同一标准品类） |
| 每摊位商品 | **25 条**（落在「20～30 条」区间内），商品记录合计 **250 条** |
| 价目表 | 10 摊位 × 25 商品 × **2 个营业日** = **500 行** |

**为什么是演示规模而不是「1 个市场 × 300 摊位 × 约 50 商品/摊位 ≈ 1.5 万条」**：

1. 本期是**单机演示**（`spec.md` §1.2「本期实现范围」：单市场 / 单机 / 演示级并发），
   1.5 万条商品记录会让启动与种子导入变慢、演示时找商品困难 —— **可演示性与现场稳定性优先**；
2. **300 摊位属「方案论证范围」**（`spec.md` §1.2 与 `discovery.md` `Q2.2`：市场实际规模，仅文字论证、
   不进容量目标），**不进种子数据**；
3. 但**四大品类 + 别名归一必须有真实样本**（否则演示不出本系统「数据不脏」的核心设计），
   因此品类字典给到 58 个并覆盖四类，且每个品类都有别名、含至少一组三别名归一。

> 该裁定与 `data-model.md` §5 的修正注记、`discovery.md` `Q3.2` 的「修正 4」留痕对应；
> `data-model.md` §5 原把 300 摊位记为「本期种子数据」属**范围归属错误**，已在 `T-005` 实现期修正。

## 产物可复算：`seed.json` 由 `scripts/gen_seed.py` 生成

`app/seed_data/seed.json` 是**产物**，它的**生成规则**在仓库内 `scripts/gen_seed.py`（构造参数、自检、
规模依据都写在那里）。**改种子必须同步改生成器**；机械验收：

    python scripts/gen_seed.py --check     # 与生成规则比对现有 seed.json（不一致则退出码 2）

**为什么生成规则必须入库**（父代理 `2026-09-30` 裁定原话的意思）：一个**评审方无法复算的产物，不算可核验的产物**
—— 只给一份 JSON 让人"自行理解构造规则"，与「不可判定的 `AC-006`」「系统算不出的交易占比」是同一类毛病。

## 别名表承担两类语义（父代理 `2026-09-30` 裁定，必须明示）

`stall_category_alias` 的 `alias_name` **不只是同物异名**：

1. **同物异名**（上海青 / 小油菜 / 青菜）—— 真正用于**归一**的别名；
2. **品种 / 细分名**（红富士苹果 / 黄元帅苹果）—— 作为**摊位级选品入口**，让摊主无论怎么叫都能查到标准品类。

**代价（有意接受）**：`REQ-002` 规定按**标准品类**记账，因此**品种粒度在统计中不保留**
（红富士与黄元帅合并计入「苹果」）。这是在「每摊位 20～30 个 SKU」与「每标准品类 1～3 个别名」
两个约束下唯一可行的取舍；商品身份在 `product` 表，本表只负责「别名 → 标准品类」的可查性。
（同一说明见 `data-model.md` §2.5。）

## 佣金口径**不在**种子数据里（父代理 `2026-09-30` 裁定：接受不含）

按 `REQ-025` 的导入清单与 `discovery.md` `Q3.1`，种子只导入上述六类档案/字典数据。
佣金口径（`commission_rule`）属**运营端配置**（`REQ-017`）—— 这**不是遗漏，而是演示动线的一部分**：
"运营端能现场配置佣金口径"本身就是一个功能展示点，`T-020` 的测试自建规则即可。

## 幂等性（`tasks.md` `T-005` 验收方式：**重复启动不产生重复数据**）

全部按键查重后再插入，查重键如下（都对应 `data-model.md` §6 的唯一索引或表内天然键）：

| 表 | 查重键 |
| --- | --- |
| `merchant` | (`name`, `phone`) —— 本表无唯一索引，取档案三要素中的两项 |
| `stall` | `stall_no`（`ux_stall_no`） |
| `category` | `code`（`ux_category_code`） |
| `stall_category_alias` | (`stall_id`, `alias_name`)（`ux_alias_stall_name`） |
| `product` | (`stall_id`, `name`) —— 本表无唯一索引 |
| `price_item` | (`stall_id`, `business_date`, `product_id`)（`ux_price_stall_date_product`） |

**已存在的价目表行一律不覆盖**（`DO NOTHING` 语义）：演示中途重启服务不会把摊主逐条调整过的价格冲回种子值 ——
「重复启动不产生重复数据」与「不破坏现场已调整的数据」两条同时成立。

## 价目表的日期维度（`REQ-003`「复制上一营业日价格」）

按 `seed.json` 的 `business_date_offsets` 写入**两个营业日**（相对当天，因此种子文件不含绝对日期，
任何一天启动都能用）：

- **上一营业日**：`source = 'manual'` —— 这是「复制昨天价」的**复制源**；
- **当日**：`source = 'copied_previous_day'` —— `AC-013` 要求启动后**已从种子文件导入价目表**，
  故当日价目表必须就绪（否则秤端无价可计价、无法「可再次交易」）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import clock, config
from .db import connect

# 价目表写入的 source 取值（`data-model.md` §2.7 枚举）
SOURCE_MANUAL = "manual"
SOURCE_COPIED = "copied_previous_day"

# 计数器字段 → 打印名（顺序即打印顺序）
_LABELS = (
    ("merchants", "商户"),
    ("stalls", "摊位"),
    ("categories", "标准品类"),
    ("aliases", "摊位别名映射"),
    ("products", "商品"),
    ("price_items", "价目表项"),
)


class SeedDataError(RuntimeError):
    """种子文件缺失或内容不合法 —— 明确报错，**不静默跳过导入**（`REQ-030` 同款处置原则）。"""


def load_seed(seed_path: Path | str | None = None) -> dict:
    """读取并做最小结构校验的种子数据。

    校验失败一律抛 `SeedDataError`（带具体原因）——「导入 0 条」而不报错是静默失败，
    会让启动看起来成功、实际无数据可演示。
    """
    path = Path(seed_path) if seed_path is not None else config.SEED_FILE
    if not path.is_file():
        raise SeedDataError(f"种子文件不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # 明确报出是哪个文件的第几行坏了
        raise SeedDataError(f"种子文件不是合法 JSON：{path}（{exc}）") from exc

    for key in ("categories", "stalls", "business_date_offsets"):
        if key not in data:
            raise SeedDataError(f"种子文件缺少必需字段 `{key}`：{path}")
    if not data["categories"] or not data["stalls"]:
        raise SeedDataError(f"种子文件的 `categories` / `stalls` 不能为空：{path}")

    codes = set()
    for cat in data["categories"]:
        for key in ("code", "name", "aliases"):
            if key not in cat:
                raise SeedDataError(f"标准品类条目缺少 `{key}`：{cat!r}")
        if not 1 <= len(cat["aliases"]) <= 3:
            raise SeedDataError(f"标准品类 `{cat['code']}` 的别名数应在 1~3 之间：{cat['aliases']!r}")
        if cat["code"] in codes:
            raise SeedDataError(f"标准品类编码重复：{cat['code']}")
        codes.add(cat["code"])

    for stall in data["stalls"]:
        if not stall.get("products"):
            raise SeedDataError(f"摊位 `{stall.get('stall_no')}` 没有商品，不能空摊位导入")
        for prod in stall["products"]:
            if prod.get("category_code") not in codes:
                raise SeedDataError(
                    f"摊位 `{stall['stall_no']}` 的商品 `{prod.get('name')}` 引用了"
                    f"未定义的标准品类 `{prod.get('category_code')}`"
                )
    return data


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple) -> int | None:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else int(row[0])


def _bump(summary: dict, key: str, inserted: bool) -> None:
    summary[key]["inserted" if inserted else "existing"] += 1


def _merchant_id(conn: sqlite3.Connection, name: str, phone: str, summary: dict) -> int:
    found = _scalar(conn, "SELECT id FROM merchant WHERE name = ? AND phone = ?", (name, phone))
    if found is not None:
        _bump(summary, "merchants", False)
        return found
    cur = conn.execute("INSERT INTO merchant (name, phone) VALUES (?, ?)", (name, phone))
    _bump(summary, "merchants", True)
    return int(cur.lastrowid)


def _stall_id(conn: sqlite3.Connection, stall: dict, merchant_id: int, summary: dict) -> int:
    found = _scalar(conn, "SELECT id FROM stall WHERE stall_no = ?", (stall["stall_no"],))
    if found is not None:
        _bump(summary, "stalls", False)
        return found
    cur = conn.execute(
        "INSERT INTO stall (stall_no, merchant_id, name, payment_receiver_token) VALUES (?, ?, ?, ?)",
        (stall["stall_no"], merchant_id, stall.get("stall_name"), stall["payment_receiver_token"]),
    )
    _bump(summary, "stalls", True)
    return int(cur.lastrowid)


def _category_ids(conn: sqlite3.Connection, data: dict, summary: dict) -> dict[str, int]:
    ids: dict[str, int] = {}
    for cat in data["categories"]:
        found = _scalar(conn, "SELECT id FROM category WHERE code = ?", (cat["code"],))
        if found is None:
            cur = conn.execute(
                "INSERT INTO category (code, name) VALUES (?, ?)", (cat["code"], cat["name"])
            )
            found = int(cur.lastrowid)
            _bump(summary, "categories", True)
        else:
            _bump(summary, "categories", False)
        ids[cat["code"]] = found
    return ids


def _ensure_alias(
    conn: sqlite3.Connection, stall_id: int, alias_name: str, category_id: int, summary: dict
) -> None:
    found = _scalar(
        conn,
        "SELECT id FROM stall_category_alias WHERE stall_id = ? AND alias_name = ?",
        (stall_id, alias_name),
    )
    if found is not None:
        _bump(summary, "aliases", False)
        return
    conn.execute(
        "INSERT INTO stall_category_alias (stall_id, alias_name, category_id) VALUES (?, ?, ?)",
        (stall_id, alias_name, category_id),
    )
    _bump(summary, "aliases", True)


def _product_id(
    conn: sqlite3.Connection, stall_id: int, product: dict, summary: dict
) -> int:
    found = _scalar(
        conn, "SELECT id FROM product WHERE stall_id = ? AND name = ?", (stall_id, product["name"])
    )
    if found is not None:
        _bump(summary, "products", False)
        return found
    cur = conn.execute(
        "INSERT INTO product (stall_id, name, category_id, icon_key, hotkey) VALUES (?, ?, ?, ?, ?)",
        (stall_id, product["name"], product["category_id"], product.get("icon_key"), product.get("hotkey")),
    )
    _bump(summary, "products", True)
    return int(cur.lastrowid)


def _ensure_price_item(
    conn: sqlite3.Connection,
    stall_id: int,
    product_id: int,
    business_date: str,
    unit_price_cents: int,
    source: str,
    summary: dict,
) -> None:
    """价目表幂等写入：**已存在则原样保留**（不覆盖摊主已调整的价格）。"""
    found = _scalar(
        conn,
        "SELECT id FROM price_item WHERE stall_id = ? AND business_date = ? AND product_id = ?",
        (stall_id, business_date, product_id),
    )
    if found is not None:
        _bump(summary, "price_items", False)
        return
    conn.execute(
        "INSERT INTO price_item (stall_id, product_id, business_date, unit_price_cents, source)"
        " VALUES (?, ?, ?, ?, ?)",
        (stall_id, product_id, business_date, unit_price_cents, source),
    )
    _bump(summary, "price_items", True)


def empty_summary() -> dict:
    """构造全零计数器（供导入与测试共用）。"""
    return {key: {"inserted": 0, "existing": 0} for key, _ in _LABELS}


def import_seed(
    conn: sqlite3.Connection | None = None,
    *,
    seed_path: Path | str | None = None,
    today: date | None = None,
) -> dict:
    """导入种子数据并返回摘要。

    - `conn` 为 `None` 时自建连接（导入完即关闭）；传入连接时**由调用方负责提交**；
    - `today` 可注入（默认本机当天），便于测试与「营业日」相关的可复现验证。

    返回：`{"business_dates": {"previous_day": ..., "current_day": ...}, <计数器>}`。
    """
    data = load_seed(seed_path)
    business_dates = _resolve_business_dates(data, today)
    summary = empty_summary()
    summary["business_dates"] = business_dates

    own_connection = conn is None
    connection = conn if conn is not None else connect()
    try:
        # 单个短事务：种子导入要么整体可见，要么整体不可见（`ADR-0001` §3 短事务约定）
        connection.execute("BEGIN IMMEDIATE")
        try:
            category_ids = _category_ids(connection, data, summary)
            for stall in data["stalls"]:
                merchant_id = _merchant_id(
                    connection, stall["merchant"]["name"], stall["merchant"]["phone"], summary
                )
                stall_id = _stall_id(connection, stall, merchant_id, summary)

                for product in stall["products"]:
                    product["category_id"] = category_ids[product["category_code"]]
                    product_id = _product_id(connection, stall_id, product, summary)
                    for label, source in (
                        ("previous_day", SOURCE_MANUAL),
                        ("current_day", SOURCE_COPIED),
                    ):
                        _ensure_price_item(
                            connection,
                            stall_id,
                            product_id,
                            business_dates[label],
                            int(product["unit_price_cents"]),
                            source,
                            summary,
                        )
                # 别名映射：本摊位经营的每个标准品类的**全部别名**都落表（摊主怎么叫都能查到标准品类）
                for code in {p["category_code"] for p in stall["products"]}:
                    for alias_name in _aliases_of(data, code):
                        _ensure_alias(connection, stall_id, alias_name, category_ids[code], summary)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        if own_connection:
            connection.close()
    return summary


def _aliases_of(data: dict, code: str) -> list[str]:
    for cat in data["categories"]:
        if cat["code"] == code:
            return list(cat["aliases"])
    raise SeedDataError(f"未定义的标准品类：{code}")


def _resolve_business_dates(data: dict, today: date | None) -> dict:
    offsets = data.get("business_date_offsets") or {}
    base = today or clock.today()
    resolved = {}
    for label, default in (("previous_day", -1), ("current_day", 0)):
        try:
            delta = int(offsets.get(label, default))
        except (TypeError, ValueError) as exc:
            raise SeedDataError(f"`business_date_offsets.{label}` 必须是整数") from exc
        resolved[label] = (base + timedelta(days=delta)).strftime("%Y-%m-%d")
    return resolved


def summary_line(summary: dict) -> str:
    """把摘要压成一行，供 `run.py` / `scripts/reset_demo.py` 打印（同一个格式，避免两处各写一遍）。"""
    parts = []
    for key, label in _LABELS:
        counter = summary[key]
        parts.append(f"{label} 新增 {counter['inserted']} / 已存在 {counter['existing']}")
    dates = summary["business_dates"]
    return (
        f"营业日 {dates['previous_day']}（上一天）与 {dates['current_day']}（当日）｜"
        + "｜".join(parts)
    )
