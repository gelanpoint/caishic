"""`tests/scale` 的**夹具入口垫片**（与 `tests/contract/conftest.py` 同一套做法）。

三个夹具全部是 `tests/contract/contract_support.py` 的**原件**，此处只做导入让 pytest 从本目录命名空间发现：

- `seeded_app`：建库 + 导入种子 + Flask 应用（会话级，指向契约测试专用数据目录）；
- `client`：`SensitiveScanClient` —— 形态 2 的每一份 JSON 响应也自动过敏感字段扫描
  （`scale-midplatform.md` §1.5 明文要求本契约响应体同受 `AC-012` 覆盖）；
- `db_conn`：直连测试库，用于**布置前置条件**（构造"未预注册设备""跨摊位交易"等状态）。

**为什么不在这里重写一份**：夹具各写一份就是漂移的种子（父代理 `2026-10-06` 裁定，
`tasks.md` `T-SCALE-03` 的产出文件已含本文件）；而 `tests/scale/conftest.py` 与
`tests/contract/conftest.py` **同名**，用例里一律**不按裸名导入 conftest**
（同名撞车会让全量 `pytest -q` 直接 ImportError，`T-035` 实测过），助手统一从 `scale_support` 导入。
"""

from __future__ import annotations

import scale_support  # noqa: F401  —— 先把 `tests/contract` 放进 `sys.path`，下面的裸名导入才成立
from contract_support import client, db_conn, seeded_app  # noqa: F401
