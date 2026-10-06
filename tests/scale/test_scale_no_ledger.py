"""`T-SCALE-15` 机械检查：秤端**没有账本**（`AC-032`）+ **不承载顾客页面**（`AC-033`）。

回答一个问题：**秤端固件里有没有长出"佣金 / 日聚合 / 结算 / 看板 / 退货冲正计算"？**

`NFR-015` 把"秤端不参与金额计算"定成硬边界，但**漏改一处不会报错** —— 现场表现为"秤端算的佣金
与中台算的不一致"，而两边都不会崩。故这里把它变成**机械约束**，并按 `T-036` 家族的规矩
**自带灵敏度负例**（不验证灵敏度的验证是摆设）。

## 判据必须是"语义"而不是"数词"（否则一写就假红）

我先逐个看过 `scale-fw/` 里出现的相关词再定判据。下面四类**合法**，**不得**禁：

| 词 | 出现在哪 | 为什么合法 |
| --- | --- | --- |
| `settle` / `sync_settle` | `core/proto.{c,h}`、`main/net/sync.{c,h}`、`main/ui/ui.h` | 契约 §3.6 的**收款**端点（扫码/现金收款），不是"佣金结算" |
| `refund` / `refund_amount_cents` | `core/proto.{c,h}` | 契约 §3.7 的**退货申请**字段：秤端提交顾客要求的金额，**不计算**冲正 |
| `commission_delta_cents` | 仅 `test/test_proto.c` 的**响应样例字符串** | 中台响应里的字段，秤端**只解析接收**（`REQ-042`：佣金由中台现算） |
| `customer_base_url` / `/customer/...` | `core/proto.{c,h}`、`main/net/sync.{c,h}` | 指向**中台**的 URL（`AC-033`：秤端只生成二维码，不承载页面） |

故判据落在**定义出来的符号**与**赋值动作**上（"计算"才违规），而不是"文本里出现某词"。

## 验证边界（与 `scale-fw/README.md` §1 口径一致）

本文件是**静态检查**：只读源码文本，**不编译、不运行** `scale-fw/**`。`main/**` 在本机既不能编译
也不能运行（无 ESP-IDF / 无 ESP32 硬件，未决项 `Q-21`），故**不得**把本文件的绿灯读成"秤端已验证"。
运行时行为分别由 `main/net/sync.c` 的主机侧单测（`test_sync`）与 `tests/e2e/test_split_mode.py` 覆盖。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # 与 `tests/unit/clock_support.py` 同一垫片
    sys.path.insert(0, str(REPO_ROOT))

SCALE_FW = REPO_ROOT / "scale-fw"
SOURCE_SUFFIXES = (".c", ".h")
PAGE_SUFFIXES = (".html", ".htm", ".css", ".js", ".svg")

#: `NFR-015` 禁的**计算型**符号。**刻意不含裸 `settle`**（那是 §3.6 收款端点，合法）
FORBIDDEN_SYMBOLS = (
    "commission", "settlement", "daily_aggregate", "dashboard", "margin", "profit", "rate_bp",
)
_FORBIDDEN_SYMBOL_RE = re.compile(r"\b(?:" + "|".join(FORBIDDEN_SYMBOLS) + r")\w*", re.IGNORECASE)

#: **仅接收**的字段名（中台响应里的佣金扣减；秤端只解析，不计算）
ALLOWED_RECEIVED_FIELDS = frozenset({"commission_delta_cents"})

#: 顾客页面标记（`AC-033`：秤端**不承载**顾客页面）
PAGE_MARKERS = ("<!doctype", "<html", "<body", "<script", "text/html")

#: C 函数定义（`static ret name(args) {`）；只认**定义**，不认调用（调用行不以 `{` 收尾）
_FUNC_DEF_RE = re.compile(
    r"^[ \t]*(?:static\s+)?(?:const\s+)?[A-Za-z_][\w \t\*]*?\b([A-Za-z_]\w*)\s*\([^;{)]*\)\s*\{",
    re.MULTILINE,
)
_C_KEYWORDS = frozenset({"if", "for", "while", "switch", "return", "sizeof", "else", "do", "defined"})

#: 对"佣金类"标识符**赋值** = 计算（而非接收）
_COMMISSION_ASSIGN_RE = re.compile(
    r"\b(commission\w*|settlement\w*|daily_aggregate\w*)\s*(?:=|\+=|-=|\*=|/=)"
)

#: 灵敏度负例的探针文件名（前缀固定，便于"用完必须还原"的守卫断言）
PROBE_PREFIX = "__sensitivity_probe"


# --------------------------------------------------------------------------- #
# 扫描器（纯函数：同一套判据既扫真源码，也扫负例探针）
# --------------------------------------------------------------------------- #


def source_files(root: Path = SCALE_FW) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES)


def page_files(root: Path = SCALE_FW) -> list[Path]:
    """顾客页面类文件（`AC-033`：秤端一个都不该有）。"""
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in PAGE_SUFFIXES)


def defined_symbols(text: str) -> set[str]:
    return {m.group(1) for m in _FUNC_DEF_RE.finditer(text)} - _C_KEYWORDS


def find_ledger_violations(text: str, where: str) -> list[str]:
    """账本计算的两类证据：① 定义了禁词函数 ② 对佣金类标识符赋值。"""
    out: list[str] = []
    for symbol in sorted(defined_symbols(text)):
        if symbol in ALLOWED_RECEIVED_FIELDS or not _FORBIDDEN_SYMBOL_RE.search(symbol):
            continue
        out.append(f"{where}: 定义了 `{symbol}()` —— `NFR-015` 禁的账本计算")
    out.extend(
        f"{where}: 对 `{m.group(1)}` 赋值（计算而非接收）" for m in _COMMISSION_ASSIGN_RE.finditer(text)
    )
    return out


def find_page_violations(text: str, where: str) -> list[str]:
    low = text.lower()
    return [f"{where}: 出现顾客页面标记 `{marker}`" for marker in PAGE_MARKERS if marker in low]


def scan_ledger(root: Path = SCALE_FW) -> list[str]:
    violations: list[str] = []
    for path in source_files(root):
        violations.extend(
            find_ledger_violations(path.read_text(encoding="utf-8", errors="replace"), str(path.relative_to(REPO_ROOT)))
        )
    return violations


def scan_customer_page(root: Path = SCALE_FW) -> list[str]:
    violations = [f"{p.relative_to(REPO_ROOT)}: 顾客页面类文件（`AC-033` 不允许）" for p in page_files(root)]
    for path in source_files(root):
        violations.extend(
            find_page_violations(path.read_text(encoding="utf-8", errors="replace"), str(path.relative_to(REPO_ROOT)))
        )
    return violations


# --------------------------------------------------------------------------- #
# 判据一：扫描面本身是真的（否则"零命中"可能只是扫了个空目录）
# --------------------------------------------------------------------------- #


def test_scan_surface_is_not_empty():
    """**正对照**：扫描面必须真的覆盖秤端固件，否则绿灯是"扫了空目录"换来的。"""
    files = {p.relative_to(SCALE_FW).as_posix() for p in source_files()}
    assert len(files) >= 15, f"扫描面太小，判据可能已失效：{sorted(files)}"
    for expected in ("core/pricing.c", "core/proto.c", "core/queue.c", "main/net/sync.c", "main/app_main.c"):
        assert expected in files, f"扫描面缺了 `{expected}`（判据会漏掉真实代码）"


# --------------------------------------------------------------------------- #
# 判据二 / 三：真源码零命中
# --------------------------------------------------------------------------- #


def test_no_ledger_computation_in_the_scale_terminal():
    """`AC-032`：秤端不得出现佣金 / 日聚合 / 结算 / 看板的**计算**。"""
    assert scan_ledger() == []


def test_scale_terminal_does_not_host_the_customer_page():
    """`AC-033`：秤端不承载顾客页面（无页面类文件、无页面标记）。"""
    assert scan_customer_page() == []


def test_legitimate_vocabulary_stays_allowed():
    """把四类**合法**词固化成断言：将来有人放宽/收紧禁词表时，这条会挡住"误伤合规词"。"""
    for term in ("sync_settle", "settle_count", "refund_amount_cents", "customer_base_url"):
        assert not _FORBIDDEN_SYMBOL_RE.search(term), f"`{term}` 是合法的（§3.6 收款 / §3.7 退货申请 / 指向中台），不得入禁词表"
    # `commission_delta_cents` **会**命中禁词正则，但它是"仅接收"字段，必须被显式豁免
    assert _FORBIDDEN_SYMBOL_RE.search("commission_delta_cents")
    assert "commission_delta_cents" in ALLOWED_RECEIVED_FIELDS


# --------------------------------------------------------------------------- #
# 判据四：灵敏度负例（**故意塞违规物 ⇒ 必红；用完还原 ⇒ 复绿**）
# --------------------------------------------------------------------------- #


def test_sensitivity_injected_commission_function_turns_the_scan_red():
    """负例一：往 `scale-fw/core/` 塞一个 `commission_cents()` ⇒ 扫描**必红**。"""
    probe = SCALE_FW / "core" / f"{PROBE_PREFIX}_commission.c"
    probe.write_text(
        "/* 灵敏度探针：故意在秤端核心塞一个佣金计算（用完即删） */\n"
        "int64_t commission_cents(int64_t received_cents, int64_t rate_bp) {\n"
        "    return received_cents * rate_bp / 10000;\n"
        "}\n",
        encoding="utf-8",
    )
    try:
        violations = scan_ledger()
        assert violations, "塞进 `commission_cents()` 后扫描必须变红 —— 否则这条判据是摆设"
        assert any("commission_cents" in v for v in violations), violations
    finally:
        probe.unlink()
    assert scan_ledger() == [], "负例用完必须还原（删除探针后扫描必须复绿）"


def test_sensitivity_injected_customer_page_turns_the_scan_red():
    """负例二：往 `scale-fw/core/` 塞一个顾客页 HTML ⇒ 扫描**必红**。"""
    probe = SCALE_FW / "core" / f"{PROBE_PREFIX}_page.html"
    probe.write_text(
        "<!DOCTYPE html>\n<html><body><h1>顾客扫码页</h1><script>/* 探针 */</script></body></html>\n",
        encoding="utf-8",
    )
    try:
        violations = scan_customer_page()
        assert violations, "塞进顾客页 HTML 后扫描必须变红 —— 否则这条判据是摆设"
        assert any("__sensitivity_probe_page.html" in v for v in violations), violations
    finally:
        probe.unlink()
    assert scan_customer_page() == [], "负例用完必须还原（删除探针后扫描必须复绿）"


def test_no_sensitivity_probe_left_behind():
    """守卫：探针**一个都不许留在仓库里**（负例崩溃/中断时由这条兜住）。"""
    leftovers = [p.relative_to(REPO_ROOT).as_posix() for p in SCALE_FW.rglob(f"{PROBE_PREFIX}*")]
    assert leftovers == [], f"灵敏度探针没清干净：{leftovers}"

def test_sensitivity_html_markers_embedded_in_c_source_also_go_red():
    """负例二的**第二臂**：顾客页 HTML 塞进 `.c` 源文件（**不靠扩展名**）也必须被抓到。

    只看扩展名是不够的 —— 真把页面托管出去的做法正是"把 HTML 字符串嵌进 C 源"。
    """
    probe = SCALE_FW / "main" / "ui" / f"{PROBE_PREFIX}_embedded.c"
    probe.write_text(
        'const char *kHostedPage = "<!DOCTYPE html><html><body>顾客扫码页</body></html>";\n',
        encoding="utf-8",
    )
    try:
        violations = scan_customer_page()
        assert any("<!doctype" in v for v in violations), violations
    finally:
        probe.unlink()
    assert scan_customer_page() == [], "负例用完必须还原（删除探针后扫描必须复绿）"
