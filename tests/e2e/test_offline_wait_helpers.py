"""秤端等待助手的**确定性**自检（假 `page`，不需要浏览器）—— `2026-10-02` 裁定③。

## 为什么要有这个文件

`test_offline_toggle_stage_then_sync_clears_pending` 曾出现"全量跑 4 次红 1 次"，根因已定位：
`#pending` 在 `index.html` 里的**初始值是 `—`**，要等 `refreshOffline()` 那次 GET 回来才变成
`N / 阈值 …`；用例却在 `.tile` 出现后**直接读** `pending_count(page)`，
于是 `int("—")` 抛 `ValueError` —— 一个看起来像随机失败的报错（机器越慢越容易撞）。

修法是"等异步落定"，而**等待逻辑本身也必须有灵敏度**（本项目规矩：不验证灵敏度的验证是摆设）。
本文件用一个**假 page**（可控 `inner_text` / 可控 `wait_for_function` 行为）把三种情形钉死：

1. `#pending` 仍是 `—` → **必须**带上下文报错（不能是裸 `ValueError`，否则下一个人又会当成随机失败）；
2. `#pending` 是正常数值 → 解析正确；
3. `wait_pending` 超时 → **必须**转成断言失败，且消息里同时给出原文与界面提示（便于判断"没暂存"还是"没刷新"）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from e2e_support import pending_count, wait_pending, wait_pending_ready  # noqa: E402


class FakePage:
    """最小假 page：只实现本文件用到的三个接口。"""

    def __init__(self, texts: dict[str, str], wait_raises: bool = False) -> None:
        self.texts = texts
        self.wait_raises = wait_raises
        self.wait_calls = 0

    def inner_text(self, selector: str) -> str:
        return self.texts.get(selector, "")

    def wait_for_function(self, expression: str, arg=None, timeout=None) -> None:
        self.wait_calls += 1
        if self.wait_raises:
            raise TimeoutError("模拟超时：目标条件始终不成立")


def test_pending_count_rejects_the_initial_em_dash_with_context():
    """**核心负例**：`#pending` 仍是初始的 `—` 时，必须抛出**带上下文**的断言失败。

    这正是那次偶发失败的真实输入（`—`）。若这里退回成裸 `ValueError`，本用例会红 ——
    保证"竞态被修掉"这件事本身不会退化。
    """
    with pytest.raises(AssertionError) as exc:
        pending_count(FakePage({"#pending": "—"}))
    assert "还不是数字" in str(exc.value) and "wait_pending_ready" in str(exc.value)
    # 明确禁止"裸 ValueError"这种会被人误读成随机失败的形态
    assert not isinstance(exc.value, ValueError)
    print(f"[裁定③] `—` 被判红且带上下文：{str(exc.value)[:60]}…")


def test_pending_count_parses_the_real_display_form():
    """正常形态 `N / 阈值 M` 必须解析出 N（含阈值已告警的后缀）。"""
    page = FakePage({"#pending": "3 / 阈值 200（已达告警阈值，仍可继续收银）"})
    assert pending_count(page) == 3
    print("[裁定③] `3 / 阈值 200（已达告警阈值）` → 解析为 3")


def test_wait_pending_ready_succeeds_and_reports_value():
    """`wait_for_function` 不抛错时，`wait_pending_ready` 直接返回解析值。"""
    page = FakePage({"#pending": "0 / 阈值 200"})
    assert wait_pending_ready(page, "前置") == 0
    assert page.wait_calls == 1
    print("[裁定③] wait_pending_ready 正常路径返回 0（只轮询一次，不 sleep）")


def test_wait_pending_ready_timeout_message_names_the_unrefreshed_state():
    """超时必须转成断言失败，并指明"未变成 `N / 阈值`"（区分"没刷新"与"值不对"）。"""
    page = FakePage({"#pending": "—"}, wait_raises=True)
    with pytest.raises(AssertionError) as exc:
        wait_pending_ready(page, "前置：会话开始")
    assert "未变成" in str(exc.value) and "前置：会话开始" in str(exc.value)
    print(f"[裁定③] 超时报错指明原因：{str(exc.value)[:60]}…")


def test_wait_pending_timeout_reports_raw_text_without_crashing():
    """`wait_pending` 超时后**自己**不能再崩：消息里要同时给原文与界面提示。

    修前的实现会在超时分支里再调 `pending_count(page)`，而那时 `#pending` 正是 `—` →
    于是**真正的报错信息被另一个 `ValueError` 盖掉**（掩盖原始失败原因）。
    """
    page = FakePage({"#pending": "—", "#offlineNote": "（尚未刷新）"}, wait_raises=True)
    with pytest.raises(AssertionError) as exc:
        wait_pending(page, 0, "补传后")
    message = str(exc.value)
    assert "补传后" in message and "—" in message and "尚未刷新" in message
    print(f"[裁定③] 超时消息包含原文与提示、不再二次崩：{message[:70]}…")
