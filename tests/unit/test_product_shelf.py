"""`AC-039` 商品上架 / 下架（`REQ-049`）的**领域层**独立验证（`task-18`）。

回答三件事：**下架真把商品从"在售集合"里拿掉了吗？越权真没写库吗？非法取值真被拒吗？**

## 口径（`spec.md` 第 322 行**现行**措辞 —— 不是任务描述里的旧话）

`AC-039` ② 已由 `RL-2` 修正：断言的是该商品**从该摊位的「在售商品集合」中移除** —— 即
`data-model.md` §2.7 完整性判据的**内层集合**（"该摊位全部 `status='active'` 的商品"）变小，
从而**可能**使该摊位在「价目表维护率」里由"未维护"翻转为"已维护"。
**分母不随上下架改变**：按 §2.7 它 = `stall.status='active'` 的摊位数。故本文件把
"**分母没变**"也写成断言 —— 免得日后有人把那条笔误口径写回来。

## 为什么是"领域层"而不是打 HTTP

`AC-039` 的 ①（端点面：商品从 `GET /api/merchant/products` 与秤端字典消失）在
`tests/e2e/test_demo_game.py` 里用**真服务**验；本文件验的是**规则本身**（错误码、写库与否、
指标口径），走 `app/domain/catalog.set_product_status` 与 `app/domain/metrics.usage_metrics`，
跑得快且不依赖服务进程。两层互补，不重复。

## 验证边界

纯领域层 + 临时库（`tmp_path`），**不碰**演示库与全局数据目录。业务日取**种子自己声明的**
`business_dates.current_day`，不用墙钟去猜（`AC-039` 的指标是单日口径，猜错日期会产生假红）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # 与 `tests/unit/clock_support.py` 同一垫片
    sys.path.insert(0, str(REPO_ROOT))

from app import TradeError, db, seed  # noqa: E402
from app.domain import catalog, metrics  # noqa: E402

MY_STALL = "A-01"
OTHER_STALL = "A-02"


@pytest.fixture()
def shelf(tmp_path):
    """独立临时库（建库 + 种子）；产出 `(conn, business_date)`。"""
    path = tmp_path / "shelf.sqlite3"
    db.init_database(path)
    conn = db.connect(path)
    try:
        summary = seed.import_seed(conn)
        conn.commit()
        yield conn, summary["business_dates"]["current_day"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 助手：一律**查库**，不只看返回值
# --------------------------------------------------------------------------- #


def _stall_ids(conn) -> dict[str, int]:
    return {r["stall_no"]: r["id"] for r in conn.execute("SELECT id, stall_no FROM stall")}


def _active_product_ids(conn, stall_id: int) -> list[int]:
    return [p["id"] for p in catalog.list_products(conn, stall_id)]


def _status_of(conn, product_id: int) -> str:
    return conn.execute("SELECT status FROM product WHERE id = ?", (product_id,)).fetchone()["status"]


def _metrics(conn, day: str) -> dict:
    return metrics.usage_metrics(conn, day)


def _drop_price_item(conn, stall_id: int, product_id: int, day: str) -> None:
    """造"未维护"前置：拿掉某件在售商品当日的价目表行（**不是**改 `product.status`）。"""
    conn.execute(
        "DELETE FROM price_item WHERE stall_id = ? AND product_id = ? AND business_date = ?",
        (stall_id, product_id, day),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# `AC-039` ②：内层集合变小 ⇒ 可能翻转为"已维护"；**分母不变**
# --------------------------------------------------------------------------- #


def test_shelving_shrinks_the_inner_set_and_can_flip_the_stall_to_maintained(shelf):
    """`AC-039` ②：下架把商品移出「在售商品集合」，使"未维护"的摊位翻转为"已维护"。"""
    conn, day = shelf
    stall_id = _stall_ids(conn)[MY_STALL]

    base = _metrics(conn, day)
    assert base["price_list_maintenance_bp"] == 10000, "前置：种子状态下每个在售商品当日都有价"

    # 前置：让该摊位变成"未维护"（内层集合里有一件在售商品当日没有价目表行）
    product_id = _active_product_ids(conn, stall_id)[0]
    _drop_price_item(conn, stall_id, product_id, day)
    unmaintained = _metrics(conn, day)
    assert unmaintained["price_list_numerator"] == base["price_list_numerator"] - 1
    assert unmaintained["price_list_maintenance_bp"] < base["price_list_maintenance_bp"]
    assert unmaintained["price_list_denominator"] == base["price_list_denominator"], (
        "**分母不得随商品上下架改变**（§2.7：分母 = `stall.status='active'` 的摊位数）"
    )

    # 下架那件"没有价的在售商品" ⇒ 内层集合变小 ⇒ 该摊位翻转为"已维护"
    catalog.set_product_status(conn, stall_id, product_id, {"status": "inactive"})
    assert product_id not in _active_product_ids(conn, stall_id), "下架后必须从在售集合消失"
    assert _status_of(conn, product_id) == "inactive"
    flipped = _metrics(conn, day)
    assert flipped["price_list_numerator"] == base["price_list_numerator"], "该摊位应翻转为已维护"
    assert flipped["price_list_maintenance_bp"] == 10000
    assert flipped["price_list_denominator"] == base["price_list_denominator"], "分母始终不变"

    # ③ 再次上架 ⇒ 恢复出现，指标回落（可逆，不是单向门）
    catalog.set_product_status(conn, stall_id, product_id, {"status": "active"})
    assert product_id in _active_product_ids(conn, stall_id), "重新上架必须恢复出现"
    back = _metrics(conn, day)
    assert back["price_list_numerator"] == base["price_list_numerator"] - 1
    assert back["price_list_denominator"] == base["price_list_denominator"], "分母始终不变"


def test_shelved_product_leaves_the_active_list(shelf):
    """`AC-039` ①（领域面）：`list_products` 只返 `status='active'` ⇒ 下架即消失。"""
    conn, _ = shelf
    stall_id = _stall_ids(conn)[MY_STALL]
    product_id = _active_product_ids(conn, stall_id)[0]
    before = _active_product_ids(conn, stall_id)
    catalog.set_product_status(conn, stall_id, product_id, {"status": "inactive"})
    assert _active_product_ids(conn, stall_id) == [pid for pid in before if pid != product_id]
    catalog.set_product_status(conn, stall_id, product_id, {"status": "active"})
    assert _active_product_ids(conn, stall_id) == before


# --------------------------------------------------------------------------- #
# `AC-039` ③：越权 ⇒ `MT-1004`，且**查库**确认目标行未被改动
# --------------------------------------------------------------------------- #


def test_cross_stall_shelving_is_refused_and_leaves_the_row_untouched(shelf):
    """`AC-039` ③：改**别的摊位**的商品 ⇒ `MT-1004`，且目标行 `status` 原样。

    另附**正对照**：同一件商品用**正确的**摊位再调一次 ⇒ 真的改了。
    没有这条对照，"目标行没变"可能只是"这个调用根本什么都不会做"。
    """
    conn, _ = shelf
    ids = _stall_ids(conn)
    victim = _active_product_ids(conn, ids[OTHER_STALL])[0]
    before = _status_of(conn, victim)

    with pytest.raises(TradeError) as excinfo:
        catalog.set_product_status(conn, ids[MY_STALL], victim, {"status": "inactive"})
    assert excinfo.value.code == "MT-1004", excinfo.value
    assert _status_of(conn, victim) == before, "越权被拒后**目标行必须原样**（查库，不只看返回值）"
    assert victim in _active_product_ids(conn, ids[OTHER_STALL]), "越权不得把商品从对方的在售集合里摘掉"

    catalog.set_product_status(conn, ids[OTHER_STALL], victim, {"status": "inactive"})
    assert _status_of(conn, victim) == "inactive", "正对照：正确摊位调用必须真的改库"


# --------------------------------------------------------------------------- #
# `AC-039` ④：非法取值 ⇒ 明确错误码，**不得静默接受**
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        {"status": "deleted"},      # 契约外的取值
        {"status": "ACTIVE"},       # 大小写不算（`data-model.md` §2.6 只有小写两值）
        {"status": None},
        {"status": ""},
        {},                         # 缺字段
        {"status": "active", "extra": 1},  # 多余字段不影响合法值（应被接受，见下一条用例）
        "active",                   # 整个 body 不是对象
        None,
    ],
)
def test_invalid_status_is_refused_and_never_silently_accepted(shelf, body):
    """`AC-039` ④：非法 `status` ⇒ `MT-1008`，且**库里的 `status` 一字未动**。"""
    conn, _ = shelf
    stall_id = _stall_ids(conn)[MY_STALL]
    product_id = _active_product_ids(conn, stall_id)[0]
    before = _status_of(conn, product_id)

    if body == {"status": "active", "extra": 1}:
        # 这一支是**正对照**：合法取值 + 多余字段 ⇒ 必须接受（否则判据会误伤）
        result = catalog.set_product_status(conn, stall_id, product_id, body)
        assert result["status"] == "active"
        assert _status_of(conn, product_id) == "active"
        return

    with pytest.raises(TradeError) as excinfo:
        catalog.set_product_status(conn, stall_id, product_id, body)
    assert excinfo.value.code == "MT-1008", f"body={body!r} 应回 MT-1008，实际 {excinfo.value}"
    assert _status_of(conn, product_id) == before, f"body={body!r} 被拒后库里的 status 不得改变"


def test_unknown_product_is_mt1009_and_changes_nothing(shelf):
    """不存在的商品 ⇒ `MT-1009`（与"越权"分开，不得混成一个码）。"""
    conn, _ = shelf
    stall_id = _stall_ids(conn)[MY_STALL]
    missing = max(_active_product_ids(conn, stall_id)) + 9999
    with pytest.raises(TradeError) as excinfo:
        catalog.set_product_status(conn, stall_id, missing, {"status": "inactive"})
    assert excinfo.value.code == "MT-1009", excinfo.value
    assert conn.execute("SELECT COUNT(*) AS n FROM product WHERE id = ?", (missing,)).fetchone()["n"] == 0


def test_non_numeric_product_id_is_refused_not_a_contract_foreign_404(shelf):
    """坏标识必须是 `MT-1008`（契约内的 4xx），不能变成契约外的路由 404。"""
    conn, _ = shelf
    stall_id = _stall_ids(conn)[MY_STALL]
    with pytest.raises(TradeError) as excinfo:
        catalog.set_product_status(conn, stall_id, "not-a-number", {"status": "inactive"})
    assert excinfo.value.code == "MT-1008", excinfo.value
