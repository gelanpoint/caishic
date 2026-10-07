"""`AC-043` / `§3.34` 的**脱敏边界**回归（`task-27` 的 RL-4 评审补件）。

`mid` 正在按新契约修 `§3.34` 的短收款码行为，故本文件**先红后绿**。

为什么单列：脱敏规则"保留前 2 + 后 4"在**短码**上会退化成**可逆** —— 4 位码 `4567` 若渲染成
`45****4567`，明文整串就在掩码里（③ 挂），披露字符数（2+4=6）还**大于**明文长度（④ 挂）。
所以这里取 **4 / 5 / 6 / 7 / 14** 五个长度逐个钉住四条：

1. 掩码含 `****`；
2. 掩码长度 ≤32（`stall.payment_receiver_token` 的 CHECK）；
3. **明文不是掩码的子串**；
4. **无法由掩码拼回明文** —— 掩码披露的字符数必须**严格少于**明文长度（至少一位不可知）。

第 ④ 条是原断言没有的，也是最关键的一条：③ 只挡"整串被抄进去"，挡不住"头尾拼起来正好是整串"。
"""

from __future__ import annotations

import pytest

from contract_support import assert_exact_keys
from demo_console_support import REGISTER_KEYS, registered

#: 契约 §3.34 的 `receiver_code` 边界：下界 4、常见长度、上界附近
CODE_LENGTHS = [4, 5, 6, 7, 14]


def _code(length: int) -> str:
    """造一个**只含数字、长度精确**的收款码（同时保证各长度之间互不相同）。"""
    return "".join(str((index + length) % 10) for index in range(length))


@pytest.mark.parametrize("length", CODE_LENGTHS)
def test_receiver_code_masking_is_irreversible_at_every_boundary_length(client, db_conn, length):
    """`§3.34`：4/5/6/7/14 五个长度都要**不可逆**脱敏（不是"抄一遍头尾"）。"""
    code = _code(length)
    payload, _ = registered(client, code=code)
    assert_exact_keys(payload, REGISTER_KEYS, "§3.34 注册响应")
    mask = payload["receiver_token_masked"]

    assert "****" in mask, f"长度 {length}：掩码必须含 `****`，实际 {mask!r}"
    assert len(mask) <= 32, f"长度 {length}：掩码必须 ≤32（表 CHECK），实际 {len(mask)}"
    assert code not in mask, (
        f"长度 {length}：掩码里出现了收款码明文（§3.34 要求明文不落库、不回显）—— 掩码={mask!r} 明文={code!r}"
    )

    disclosed = len(mask.replace("****", ""))
    assert disclosed < length, (
        f"长度 {length}：掩码披露了 {disclosed} 个字符、明文只有 {length} 个 ⇒ **明文可被拼回**，"
        f"脱敏不可逆被破坏（掩码={mask!r}）。短码必须少露头尾（至少留一位不可知）。"
    )

    stored = db_conn.execute(
        "SELECT payment_receiver_token FROM stall WHERE stall_no = ?", (payload["stall_no"],)
    ).fetchone()["payment_receiver_token"]
    assert stored == mask, f"库里存的必须就是响应里那个掩码：库里={stored!r} 响应={mask!r}"
    assert code not in stored, f"库里不得含收款码明文：{stored!r}"


@pytest.mark.parametrize("length", CODE_LENGTHS)
def test_masked_code_cannot_be_used_to_recover_the_plaintext(client, db_conn, length):
    """`§3.34` 第 ④ 条的**独立复算**：把掩码当"部分已知的串"，明文必须仍不可唯一确定。

    做法：掩码披露的头/尾若已覆盖整串，则 `len(head) + len(tail) >= len(明文)` 且能拼出明文。
    这里直接断言"存在至少一个位置既不在头部披露段、也不在尾部披露段"。
    """
    code = _code(length)
    payload, _ = registered(client, code=code)
    mask = payload["receiver_token_masked"]
    head, sep, tail = mask.partition("****")
    assert sep == "****", f"掩码必须恰有一段 `****`：{mask!r}"

    unknown_positions = [
        index for index in range(len(code))
        if index >= len(head) and index < len(code) - len(tail)
    ]
    assert unknown_positions, (
        f"长度 {length}：头 {len(head)} 位 + 尾 {len(tail)} 位已覆盖整串明文（{code!r} ← {mask!r}）"
        " ⇒ 明文可由掩码拼回，脱敏无效"
    )
