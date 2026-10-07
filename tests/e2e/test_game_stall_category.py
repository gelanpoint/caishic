"""摊位按**主营品类**区分外观（`REQ-051` / `AC-041`，`T-GAME-07`）。

**为什么要有这组**：第一版摊位外观是 `stall_` + `(index + slot) % 4` —— 纯按**位置**轮换，
跟摊位上卖什么**毫无关系**：蔬菜摊和肉摊可能长得一模一样，演示时"看不出这是卖肉的"。
修法是**从服务端品类字典推导**（`category.code` 前缀 `V`/`F`/`M`/`A`，无单一主营 ⇒ 杂货），
前端不硬编码任何摊位的品类。

**判据分两层，缺一不可**：
1. **一致性** —— 浏览器里每个摊位实际用的精灵，必须等于**独立从库里算出来**的品类；
2. **灵敏度** —— 把某个摊位的商品换成另一个品类，**重载页面后外观必须跟着变**。
   少了第 2 层，"外观写死成对的"也能过，判据就成了摆设。
"""

from __future__ import annotations

import pytest

from e2e_support import new_page, snap
from game_support import open_game

#: `category.code` 前缀 → 精灵名。与前端 `KIND_BY_PREFIX` 同口径，但**在测试里独立写一遍** ——
#: 直接引用前端的映射就变成"用被测对象验证被测对象"，推导错了也发现不了。
PREFIX_TO_SPRITE = {"V": "stall_veg", "F": "stall_fruit",
                    "M": "stall_meat", "A": "stall_fish"}
MAJORITY = 0.6

SPRITE_JS = """(() => {
  const out = {};
  GameApp.state.entities.list.filter(e => e.kind === 'stall').forEach(e => {
    out[e.stallNo] = e.sprite;
  });
  return out;
})()"""


def _expected_from_db(server) -> dict:
    """从库里**独立**推导每个摊位应有的精灵名（不引用前端任何代码）。"""
    with server.connect_db() as conn:
        rows = conn.execute(
            "SELECT s.stall_no AS stall_no, c.code AS code FROM product p"
            " JOIN stall s ON s.id = p.stall_id"
            " JOIN category c ON c.id = p.category_id"
            " WHERE p.status = 'active'"
        ).fetchall()
    counts: dict[str, dict[str, int]] = {}
    for stall_no, code in rows:
        counts.setdefault(stall_no, {})
        prefix = code[:1].upper()
        counts[stall_no][prefix] = counts[stall_no].get(prefix, 0) + 1

    expected = {}
    for stall_no, per_prefix in counts.items():
        total = sum(per_prefix.values())
        # 固定顺序取最大，保证并列时结果稳定（与前端同规则）
        best = max(["A", "F", "M", "V"], key=lambda p: (per_prefix.get(p, 0), -ord(p)))
        expected[stall_no] = (PREFIX_TO_SPRITE[best] if per_prefix.get(best, 0) / total >= MAJORITY
                              else "stall_grocery")
    return expected


def test_every_stall_sprite_matches_its_server_side_category(browser, live_server, shots):
    """浏览器里的摊位外观 == 从库里独立推导的品类（含 `A-09`/`A-10` 这类混合摊 ⇒ 杂货）。"""
    expected = _expected_from_db(live_server)
    assert expected, "库里没有任何在售商品，推导不出品类 —— 前置条件不成立"
    assert len(set(expected.values())) >= 3, (
        f"这套数据只推导出 {set(expected.values())} 一种品类，验证不了「区分」这件事"
    )

    page = new_page(browser)
    try:
        open_game(page, live_server)
        actual = page.evaluate(SPRITE_JS)
        assert set(actual) == set(expected), (
            f"摊位集合不一致：页面 {sorted(actual)} vs 库 {sorted(expected)}"
        )
        for stall_no in sorted(expected):
            assert actual[stall_no] == expected[stall_no], (
                f"{stall_no}：页面用 {actual[stall_no]}，按库里品类应为 {expected[stall_no]}"
                " —— 摊位外观与服务端品类对不上"
            )
        snap(page, shots, "game-stall-category")
    finally:
        page.close()


@pytest.mark.parametrize("stall_no,new_prefix", [("A-01", "M"), ("A-05", "V")])
def test_stall_appearance_follows_the_data_not_a_hardcoded_table(
    browser, live_server, stall_no, new_prefix
):
    """**灵敏度**：把某摊位的在售商品全换成另一品类 ⇒ 重载后外观必须跟着变。

    这条专门打"前端写死一张摊位 → 品类对照表"的作弊做法 —— 那种实现下这条必红。
    """
    page = new_page(browser)
    try:
        open_game(page, live_server)
        before = page.evaluate(SPRITE_JS)[stall_no]

        with live_server.connect_db() as conn:
            target = conn.execute(
                "SELECT id FROM category WHERE code LIKE ? AND status = 'active' LIMIT 1",
                (new_prefix + "-%",),
            ).fetchone()
            assert target is not None, f"库里没有 {new_prefix}- 开头的品类，无法做灵敏度验证"
            # `product.icon_key` 是**独立列**（契约 §3.5 用它做无文本选品），且实测与
            # `category.code` 全量一致（250/250）。真正的"改品类"要两列一起改 ——
            # 只改 `category_id` 是**半截改动**，服务端返回的 `icon_key` 不会跟着变。
            code = conn.execute("SELECT code FROM category WHERE id = ?", (target[0],)).fetchone()[0]
            conn.execute(
                "UPDATE product SET category_id = ?, icon_key = ? WHERE stall_id ="
                " (SELECT id FROM stall WHERE stall_no = ?)",
                (target[0], code, stall_no),
            )
            conn.commit()

        page.evaluate("window.location.reload()")
        page.wait_for_function("() => window.GameApp && GameApp.state.world", timeout=30000)
        page.wait_for_function(
            "() => document.getElementById('dataBadge').className.includes('on')", timeout=30000
        )
        after = page.evaluate(SPRITE_JS)[stall_no]

        expected = PREFIX_TO_SPRITE[new_prefix]
        assert after == expected, (
            f"{stall_no} 的商品已全换成 {new_prefix} 类，外观应变成 {expected}，"
            f"实际仍是 {after} —— 说明品类是**写死的**，不是从服务端推导的"
        )
        assert after != before, "换品类前后外观没变化，这条验证没有区分度"
    finally:
        page.close()
