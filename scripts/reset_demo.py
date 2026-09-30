#!/usr/bin/env python
"""重置演示数据：删除数据文件 → 重建表结构 → 重跑种子导入（`T-006`）。

对应 `NFR-003` / `NFR-004` 的恢复手段（`plan.md` §4：`scripts/reset_demo.py`），
也是 `data-model.md` §7 第 3 条写明的回滚/重演路径。

**执行后系统回到「可再次交易的初始状态」**，判定口径（脚本自己逐条核对并打印，不靠人眼）：

1. 表结构齐全（按 `data-model.md` 的 19 实体，见迁移脚本 `0001_init.sql`）；
2. **交易类数据全为 0 行**：`transaction` / `transaction_item` / `payment` / `payment_callback_log`
   / `refund` / `offline_queue` / `daily_aggregate` / `settlement` / `stall_credit` / `audit_log`；
3. 档案与字典齐备：在营摊位 10、标准品类 58、摊位别名映射、商品 250；
4. **计价前置条件成立**：每个在营摊位的**全部在营商品**在**当日**都有 `price_item` 且单价 ≥1 分
   —— 没有当日单价就计不了价（`REQ-003` / `REQ-005`）；
5. **库可写**：在一个会回滚的事务里真插一条交易再撤销（证明可写，且**不留任何痕迹**）。

> ⚠️ 范围诚实说明：本期交易类 HTTP 端点自 `T-015` 才实现，因此本脚本能证明的是
> **「可交易的数据前置条件 + 库可写」**，**不能**声称"跑通了一笔真实交易"。

用法：
    python scripts/reset_demo.py              # 删除数据文件并重建（演示前重置用）
    python scripts/reset_demo.py --dry-run    # 只打印将要删除的内容，不动任何文件
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app import config  # noqa: E402  （必须在 sys.path 处理之后导入）
from app.db import connect, init_database  # noqa: E402
from app.seed import import_seed, summary_line  # noqa: E402

_LINE = "=" * 68

# 重置后必须为空的「使用中产生」的表（data-model.md §1）
_TXN_TABLES = (
    "transaction_item",
    "payment_callback_log",
    "payment",
    "refund",
    "offline_queue",
    "daily_aggregate",
    "settlement",
    "stall_credit",
    "audit_log",
    '"transaction"',
)


def data_files() -> list[Path]:
    """本次重置会删除的数据文件（含 SQLite 的 WAL / SHM 旁文件）。"""
    db = Path(config.DB_PATH)
    return [db, Path(str(db) + "-wal"), Path(str(db) + "-shm")]


def remove_data_files(dry_run: bool) -> tuple[list[Path], list[Path]]:
    """删除库文件与离线暂存文件；返回（已删除, 不存在）。

    删除失败（例如服务还在跑、Windows 上文件被占用）一律**明确报错并退出**，
    不吞掉异常装作重置成功 —— 那会让演示者以为数据已重置，实际仍是旧库。
    """
    removed: list[Path] = []
    missing: list[Path] = []
    for path in data_files():
        if not path.exists():
            missing.append(path)
            continue
        if dry_run:
            removed.append(path)
            continue
        try:
            path.unlink()
        except OSError as exc:
            raise SystemExit(
                f"[重置失败] 无法删除 {path}：{exc}\n"
                " 可能原因：本系统的服务还在运行（正在占用该文件）。\n"
                " 处理办法：先停掉正在运行的服务（Ctrl+C 或关掉命令行窗口），再执行本脚本。"
            ) from exc
        removed.append(path)

    staging = Path(config.OFFLINE_STAGING_DIR)
    if staging.is_dir():
        for item in sorted(staging.rglob("*")):
            if item.is_file():
                if not dry_run:
                    item.unlink()
                removed.append(item)
    return removed, missing


def verify_reset(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    """逐条核对「可再次交易的初始状态」，返回（是否全过, 逐条结果文本）。"""
    checks: list[str] = []
    ok = True

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not row[0].startswith("sqlite_")
    }
    checks.append(f"[{'OK' if len(tables) >= 19 else '!!'}] 表数量 = {len(tables)}（应 ≥19，含 schema_migration）")
    ok &= len(tables) >= 19

    for table in _TXN_TABLES:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        passed = count == 0
        ok &= passed
        checks.append(f"[{'OK' if passed else '!!'}] {table} 行数 = {count}（重置后应为 0）")

    active_stalls = conn.execute("SELECT COUNT(*) FROM stall WHERE status = 'active'").fetchone()[0]
    categories = conn.execute("SELECT COUNT(*) FROM category").fetchone()[0]
    products = conn.execute("SELECT COUNT(*) FROM product WHERE status = 'active'").fetchone()[0]
    aliases = conn.execute("SELECT COUNT(*) FROM stall_category_alias").fetchone()[0]
    checks.append(f"[OK] 在营摊位 = {active_stalls} / 标准品类 = {categories} / 在营商品 = {products} / 别名映射 = {aliases}")
    ok &= active_stalls > 0 and categories > 0 and products > 0 and aliases > 0

    # 计价前置条件：每个在营摊位的每个在营商品，当日都要有 ≥1 分的单价
    today = date.today().strftime("%Y-%m-%d")
    missing_price = conn.execute(
        "SELECT COUNT(*) FROM product p JOIN stall s ON s.id = p.stall_id"
        " WHERE p.status = 'active' AND s.status = 'active'"
        "   AND NOT EXISTS (SELECT 1 FROM price_item pi"
        "                    WHERE pi.stall_id = p.stall_id AND pi.product_id = p.id"
        "                      AND pi.business_date = ? AND pi.unit_price_cents >= 1)",
        (today,),
    ).fetchone()[0]
    priced = conn.execute(
        "SELECT COUNT(*) FROM price_item WHERE business_date = ?", (today,)
    ).fetchone()[0]
    passed = missing_price == 0 and priced > 0
    ok &= passed
    checks.append(
        f"[{'OK' if passed else '!!'}] 当日（{today}）价目表 {priced} 行；"
        f"缺价的在营商品 = {missing_price}（应为 0，否则秤端计不了价）"
    )

    # 库可写证明：真插一条交易再回滚，不留痕迹
    sample = conn.execute(
        "SELECT p.id, p.stall_id, pi.unit_price_cents FROM product p"
        " JOIN stall s ON s.id = p.stall_id"
        " JOIN price_item pi ON pi.stall_id = p.stall_id AND pi.product_id = p.id"
        " WHERE p.status = 'active' AND s.status = 'active' AND pi.business_date = ?"
        " LIMIT 1",
        (today,),
    ).fetchone()
    if sample is None:
        ok = False
        checks.append("[!!] 取不到任一「摊位 × 商品 × 当日单价」样本，无法验证可写性")
    else:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            'INSERT INTO "transaction" (transaction_no, stall_id, business_date, total_amount_cents,'
            " client_idempotency_key) VALUES (?, ?, ?, ?, ?)",
            ("RESET-PROBE", sample[1], today, sample[2], "reset-demo-probe"),
        )
        conn.rollback()
        residue = conn.execute('SELECT COUNT(*) FROM "transaction"').fetchone()[0]
        passed = residue == 0
        ok &= passed
        checks.append(
            f"[{'OK' if passed else '!!'}] 写入探针：对摊位 {sample[1]} / 商品 {sample[0]}（当日单价"
            f" {sample[2]} 分）试插一条交易后回滚 —— 剩余交易行数 = {residue}（应为 0）"
        )
    return ok, checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重置演示数据（删除数据文件并重跑种子导入）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将删除的内容，不改动任何文件")
    args = parser.parse_args(argv)

    print(_LINE)
    print(f" 重置演示数据 —— {config.APP_NAME}")
    print(_LINE)
    print(f" 数据目录：{config.DATA_DIR}")
    print(f" 数据文件：{config.DB_PATH}")

    removed, missing = remove_data_files(args.dry_run)
    for path in removed:
        print(f" {'将删除' if args.dry_run else '已删除'}：{path}")
    for path in missing:
        print(f" 不存在（跳过）：{path}")
    if args.dry_run:
        print(_LINE)
        print(" --dry-run：未改动任何文件，未重建数据库。")
        print(_LINE)
        return 0

    applied = init_database()
    print(f"[建库] 重建表结构，本次执行迁移：{', '.join(applied) if applied else '（无）'}")

    summary = import_seed()
    print(f"[种子] {summary_line(summary)}")

    conn = connect()
    try:
        ok, checks = verify_reset(conn)
    finally:
        conn.close()

    print("-" * 68)
    print(" 重置结果核对（判定口径见本脚本 docstring）：")
    for line in checks:
        print("  " + line)
    print("-" * 68)
    if ok:
        print(" [OK] 已回到可再次交易的初始状态（数据前置条件齐备 + 库可写）。")
        print("      注：交易类 HTTP 端点自 T-015 起实现，本脚本不能声称已跑通一笔真实交易。")
        print("      注：种子不含佣金口径 —— 【佣金口径由运营端现场配置】（REQ-017）；")
        print("          这是演示动线的一部分，T-020 的测试自建规则即可。")
    else:
        print(" [失败] 重置后核对未通过，请按上面 [!!] 行排查；不要在此状态下开始演示。")
    print(_LINE)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
