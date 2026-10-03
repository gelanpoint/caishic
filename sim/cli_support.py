"""`sim/cli.py` 里的**跨分支共用小工具**（`T-SIM-08` 拆分时顺手独立出来的一处）。

`SCHEMA_VERSION` 与 `_write_json` 此前住在 `cli.py`。把它们搬到独立模块，是因为
`env_baseline.py` / `verify_cli.py` / `bridge/live_cli.py` 都用得到，而它们**都不该**为了
写一个 JSON 就 import 整个 `cli.py`（那会把参数解析器也拖进来）。

⚠️ 只有一个 `SCHEMA_VERSION`、只有一份 `_write_json` —— 同一套落盘规则写两遍就是下次漂移的种子。
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_VERSION = 1


def _write_json(path: Path, payload: dict) -> None:
    """**全部产物**的统一落盘口径：UTF-8、缩进 2、键序保留、行尾 `\\n`。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
                          encoding="utf-8", newline="\n")