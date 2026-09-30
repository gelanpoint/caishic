"""敏感字段扫描原语（`REQ-024` / `NFR-012`；`AC-012`）。对应任务：`tasks.md` `T-007`。

**为什么单独成文件**：这些原语是"检查"而不是"夹具" —— 它们被 `SensitiveScanClient`（在
`conftest.py`）与 `test_sensitive_scan.py` / `test_customer.py` 复用，语义上是**一套判据的三种入口**
（响应体 / 任意文本 / 数据库文件），放在一起才能保证三处的禁令集合不会各写各的（`Q-16` 的按语义拆分）。

**判据集合的唯一来源**：`data-model.md` 全局约定 + `REQ-024`。改禁令集合必须同时改这里与本模块的
单元负例（`test_sensitive_scan.py` 里的"干净样例不得误报"），否则就是一个只增不减的假检查。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

#: 字段名禁令：与 `data-model.md` §全局约定 逐条对齐（`id_card*` / `id_no*` / `bank_card*` /
#: `card_no*` / `bank_account*`），并额外国产中文列名两种写法。
FORBIDDEN_FIELD_RE = re.compile(r"^(?:id_card|id_no|bank_card|card_no|bank_account)|身份证|银行卡", re.IGNORECASE)
#: 身份证号（18 位，含出生日期段）与银行卡号形态（16~19 位连续数字）。
#: 注：身份证号若以 `X` 结尾，会同时命中银行卡形态（`\d{16,19}` 后的 `X` 不算数字）——
#: 两者都属禁令范围，重复命中不影响判定，但不宣称能区分二者。
ID_CARD_VALUE_RE = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
BANK_CARD_VALUE_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")


def scan_json_for_sensitive(payload, where: str = "响应体") -> list[str]:
    """递归扫描 JSON 的字段名与字符串取值；返回可读命中清单（空 = 零命中）。"""
    hits: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                if isinstance(key, str) and FORBIDDEN_FIELD_RE.search(key):
                    hits.append(f"{where}: 命中禁忌字段名 `{child}`")
                walk(value, child)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            if ID_CARD_VALUE_RE.search(node):
                hits.append(f"{where}: 取值疑似身份证号 → `{path}`")
            if BANK_CARD_VALUE_RE.search(node):
                hits.append(f"{where}: 取值疑似银行卡号 → `{path}`")

    walk(payload, "")
    return hits


def scan_text_for_sensitive(text: str, where: str = "文本") -> list[str]:
    """扫描文本（源码 / SQL / 库文件字节）中的禁忌标识符与取值形态。"""
    hits = [
        f"{where}: 命中禁忌标识符 `{token}`"
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
        if FORBIDDEN_FIELD_RE.search(token)
    ]
    if ID_CARD_VALUE_RE.search(text):
        hits.append(f"{where}: 命中身份证号形态的取值")
    if BANK_CARD_VALUE_RE.search(text):
        hits.append(f"{where}: 命中银行卡号形态的取值")
    return hits


def scan_db_file(db_path: Path | str) -> list[str]:
    """扫描**库文件**：SQLite 模式（对象名 / 列名）+ 全文件字节（16~19 位连续数字即异常）。"""
    path = Path(db_path)
    if not path.is_file():
        return [f"库文件不存在：{path}"]

    hits: list[str] = []
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    finally:
        connection.close()

    for obj_type, name, sql in rows:
        if FORBIDDEN_FIELD_RE.search(name or ""):
            hits.append(f"{path.name}: 模式对象名禁忌 `{name}`（{obj_type}）")
        for identifier in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql or ""):
            if FORBIDDEN_FIELD_RE.search(identifier):
                hits.append(f"{path.name}: DDL（{name}）含禁忌标识符 `{identifier}`")

    # 库文件是二进制，按 latin-1 逐字节解码可无损检索 ASCII 数字串
    hits.extend(scan_text_for_sensitive(path.read_bytes().decode("latin-1"), f"{path.name} 原始字节"))
    return hits
