"""只增不改的仿真事件日志（JSONL）。

两条纪律（与主系统 `audit_log` 的"只增不改"同源）：

1. **只追加**：没有任何删除/改写接口；
2. **可复现**：`json.dumps(..., sort_keys=True, separators=(",", ":"))` —— 键序与空白都由格式固定，
   否则"同 seed 两次运行逐字节相同"会因为 dict 顺序而假红。

产物用于两件事：① `T-SIM-01` 的复现判据（SHA-256 比对）；② 后续指标的**逐条复算**依据
（`docs/sim-design.md` §6 要求每个指标的分子分母都能由明细核出）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, TextIO


class EventLog:
    """JSONL 事件日志；`with event_log(path) as log: log.emit(...)`。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: TextIO = self.path.open("w", encoding="utf-8", newline="\n")
        self._count = 0

    def emit(self, kind: str, /, **fields: Any) -> None:
        """追加一条事件。

        `kind` 声明为**位置专用参数**（`/`）：否则调用方写 `emit("x", kind=...)` 会撞成
        `TypeError: got multiple values for argument 'kind'`（本模块第一版实测踩到）。
        位置专用的同时**仍禁止**字段名 `kind` —— 它会覆盖事件类型，故显式拦下。
        """
        if "kind" in fields:
            raise ValueError("字段名 `kind` 会覆盖事件类型；请改用 `agent_kind` 这类名字")
        record = {"kind": kind, **fields}
        self._fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        self._count += 1

    @property
    def count(self) -> int:
        return self._count

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def digest(self) -> str:
        """本文件内容的 SHA-256（复现判据的载体；调用前须 `close()`）。"""
        return hashlib.sha256(self.path.read_bytes()).hexdigest()
