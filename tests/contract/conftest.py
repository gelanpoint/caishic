"""契约层夹具的**入口垫片**（内容已移到同目录的 `contract_support.py`）。

为什么要拆：`tests/conftest.py` 与 `tests/contract/conftest.py` **同名**，
而两边目录都会进 `sys.path`，用例里写 `from conftest import ...` 时解析到哪一个
取决于收集顺序 —— 全量 `pytest -q` 会直接 10 个文件 ImportError（`T-035` 收尾时实测）。
故：**用例一律不再按裸名导入 conftest**，夹具靠 pytest 从本模块的命名空间发现，
助手从唯一名的 `contract_support` 导入。
"""

from contract_support import *  # noqa: F401,F403
