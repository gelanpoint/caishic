"""秤端契约测试的**造场景**助手：设备预注册 / 激活 / 市场与摊位常量（`T-SCALE-03`）。

与 `scale_support.py` 的分工（单文件阈值见 `docs/standards/quality-gates.md`，此处只引用不复述）：

- 本文件：**造前置状态**（设备、令牌、市场号、摊位号、"无价目表的营业日"）；
- `scale_support.py`：契约调用与核对助手（请求头 / 统一错误响应 / 窄响应体白名单 / 7 个端点的调用助手 / 库核对）。

依赖方向**单向**：本文件 `import scale_support`（要用它的 `activate`），`scale_support` **不** import 本文件
—— 否则就是环形导入。
"""

from __future__ import annotations

import sqlite3

from scale_support import SEED_STALL_NO, activate

#: 演示设备：明文令牌只存在于**测试与烧录侧**，中台库里只存摘要（`sha256` hex）——
#: `tasks.md` `T-SCALE-02` 判据①：令牌明文不入库、不进日志（`RL-5` 同口径）。
DEMO_DEVICE_ID = "SC-000123"
DEMO_DEVICE_TOKEN = "demo-burned-in-token-SC-000123"
#: 契约 §3.1 响应示例里的市场号；优先从库里读真值（见 `default_market_code`）
CONTRACT_MARKET_CODE = "M-0001"
#: 第二个摊位（构造"跨摊位越权"用；种子数据里真实存在）
OTHER_STALL_NO = "A-02"
#: 一个**没有任何价目表**的营业日（构造"空价目表"与 `MT-1006` 态用，不动种子数据）
EMPTY_PRICE_DATE = "2099-01-01"


def provision_device(conn, *, device_id: str, market_code: str, stall_no: str, token: str) -> None:
    """运维侧**预注册**一台设备（中台侧预注册 → 令牌烧录到秤端）。

    **声明的接口**（父代理 `2026-10-06` 裁定）：`app/domain/device.py::provision_device(conn, *,
    device_id, market_code, stall_no, token) -> None`，语义 = 直接登记一行设备（令牌**只存摘要**）。

    为什么测试需要这个口子：契约只写"令牌由中台侧预注册时生成并烧录"，**没有端点**
    （`RL-1` 也不许测试自己造端点），故预注册只能走领域层。
    """
    from app.domain import device as device_domain

    provision = getattr(device_domain, "provision_device", None)
    assert callable(provision), (
        "`T-SCALE-02` 未提供 "
        "`app.domain.device.provision_device(conn, *, device_id, market_code, stall_no, token)`"
        " —— 秤端契约测试无法取得已注册设备的令牌（契约 §1 只说'中台侧预注册'，没有端点）"
    )
    provision(conn, device_id=device_id, market_code=market_code, stall_no=stall_no, token=token)
    conn.commit()


def default_market_code(conn) -> str:
    """取库里的默认市场号（`T-SCALE-01` 的 `market` 表）；表还没建时退回契约 §3.1 示例值。"""
    try:
        row = conn.execute("SELECT market_code FROM market ORDER BY id LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        row = None
    return str(row["market_code"]) if row is not None else CONTRACT_MARKET_CODE


def registered_device(
    conn,
    *,
    device_id: str = DEMO_DEVICE_ID,
    stall_no: str = SEED_STALL_NO,
    token: str = DEMO_DEVICE_TOKEN,
    market_code: str | None = None,
) -> str:
    """幂等地保证演示设备已**预注册**，返回其明文令牌（激活端点接受 `registered` 态）。"""
    row = conn.execute("SELECT COUNT(*) AS n FROM device WHERE device_id = ?", (device_id,)).fetchone()
    if int(row["n"]) == 0:
        provision_device(
            conn,
            device_id=device_id,
            market_code=market_code or default_market_code(conn),
            stall_no=stall_no,
            token=token,
        )
    return token


def active_device_token(
    client,
    conn,
    *,
    device_id: str = DEMO_DEVICE_ID,
    stall_no: str = SEED_STALL_NO,
    token: str = DEMO_DEVICE_TOKEN,
    market_code: str | None = None,
) -> str:
    """预注册 **+ 激活** 一台设备，返回其明文令牌。

    数据面（字典 / 价目表 / 上报 / 收款 / 退货 / 心跳）要求设备**已激活**，未激活一律 `MT-2001`
    （`T-SCALE-02` 的 `activate_device` 语义）；故数据面用例必须先走一次 §3.1 激活，
    否则会拿到 `MT-2001` 而误判成"端点没实现"。
    """
    registered = registered_device(
        conn, device_id=device_id, stall_no=stall_no, token=token, market_code=market_code
    )
    response = activate(client, registered, device_id=device_id)
    assert response.status_code == 200, (
        f"前置失败：§3.1 激活未成功（{response.status_code}）——"
        f"数据面要求设备已 active：{response.get_data(as_text=True)[:300]}"
    )
    return registered
