"""秤端接入 · §3.1 设备激活 / §3.2 心跳（`T-SCALE-04`；`REQ-034`/`REQ-036`/`REQ-040`；`AC-025`/`AC-027`）。

唯一权威：`specs/market-trade-flow/contracts/scale-midplatform.md` §3.1、§3.2。
领域逻辑在 `app/domain/device.py`（令牌摘要比对、绑定唯一性、重复激活幂等、心跳落库），
本文件只是 HTTP 皮肤：取请求头/请求体 → 调领域函数 → 组装契约规定的响应字段。

三条口径：

1. **响应体是窄体**：字段**恰好**契约 §3.1 / §3.2 列出的那些（多一个就是契约之外的新字段，`RL-1`）。
2. **`config_changed` 如实取 `false`**：契约 §3.2 的请求体**没有**版本字段，中台没有依据说"配置变了"；
   由秤端拿响应里的 `catalog_version` 与本地缓存自行比对（臆造一个 `true` 才是撒谎）。
3. **激活端点自己不做数据面鉴权**（`require_active=False`）：它正是把 `registered` 变成 `active`
   的那一步；其余端点（心跳、字典、价目表、上报、收款、退货）一律要求设备已激活。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import clock, config, current_db
from ..domain import device as device_domain
from .scale_catalog import catalog_version_of

bp = Blueprint("scale_device", __name__)

#: 设备令牌请求头（契约 §1 认证方式）
TOKEN_HEADER = "X-Device-Token"
#: 协议版本请求头（契约 §1 第 9 条 / §7）
PROTO_HEADER = "X-Scale-Proto"
#: 本中台当前协议版本（契约 §1 第 9 条 / §7；响应里以整数给出）
PROTO_VERSION = 1


def _json_body() -> dict:
    """取请求体；不是 JSON 对象一律按秤端段的载荷码 `MT-2004` 拒绝（契约 §4）。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise device_domain.DeviceError("MT-2004", "请求体必须是 JSON 对象")
    return body


# ---------------------------------------------------------------------------
# §3.1 设备激活（重复激活返回既有绑定）
# ---------------------------------------------------------------------------


@bp.post("/api/scale/v1/devices/activate")
def activate_scale_device():
    """契约 §3.1：取回绑定与运行配置；重复激活**返回既有绑定**（不报错、不改绑）。"""
    device_domain.require_supported_proto(request.headers.get(PROTO_HEADER))
    conn = current_db()
    body = _json_body()
    binding = device_domain.activate_device(
        conn,
        device_id=body.get("device_id"),
        token=request.headers.get(TOKEN_HEADER),
        stall_no=body.get("stall_no"),
        firmware_version=body.get("firmware_version"),
        hardware_rev=body.get("hardware_rev"),
    )
    conn.commit()
    today = clock.today_iso()
    return (
        jsonify(
            {
                "market": binding["market"],
                "stall": binding["stall"],
                "business_date": today,
                "server_time": clock.now_iso(),
                "config": {
                    "offline_warn_threshold": int(config.OFFLINE_WARN_THRESHOLD),
                    # 收款码指向**中台顾客页**（REQ-043）：秤端只负责本地生成二维码
                    "customer_base_url": f"{request.host_url.rstrip('/')}/customer",
                    "catalog_version": catalog_version_of(conn, int(binding["stall_id"])),
                    "price_list_business_date": today,
                    "proto": PROTO_VERSION,
                },
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# §3.2 心跳
# ---------------------------------------------------------------------------


@bp.post("/api/scale/v1/devices/heartbeat")
def heartbeat_scale_device():
    """契约 §3.2：取业务日、服务器时间、配置版本。

    `pending_count` 由秤端自报，**只用于观测**，中台**不据此拒绝任何交易**（`NFR-014`）。
    """
    device_domain.require_supported_proto(request.headers.get(PROTO_HEADER))
    conn = current_db()
    body = _json_body()
    info = device_domain.heartbeat_device(
        conn,
        device_id=body.get("device_id"),
        token=request.headers.get(TOKEN_HEADER),
        pending_count=body.get("pending_count"),
        firmware_version=body.get("firmware_version"),
    )
    conn.commit()
    return (
        jsonify(
            {
                "business_date": clock.today_iso(),
                "server_time": clock.now_iso(),
                # 见模块 docstring 第 2 条：没有依据就不臆造 `true`
                "config_changed": False,
                "catalog_version": catalog_version_of(conn, int(info["stall_id"])),
            }
        ),
        200,
    )
