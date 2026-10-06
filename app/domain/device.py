"""中台设备模型与设备令牌（`T-SCALE-02`；`REQ-036`；`AC-027`）。

唯一权威：
- `specs/market-trade-flow/contracts/scale-midplatform.md` §1 第 6 条（**授权只来自设备令牌，
  不来自请求参数**）、§3.1（激活）、§3.2（心跳）、§4（`MT-2001` / `MT-2005`）
- `specs/market-trade-flow/spec.md` `REQ-036`（设备注册绑定唯一的「市场 + 摊位」；
  中台拒绝未注册设备与令牌不符的请求）、`AC-027`
- `app/migrations/0003_device.sql`（`device` 表的字段语义）

三条硬口径（改动前先读）：

1. **令牌明文不入库、不进日志**（`RL-5` 同口径）：库里只存 `sha256` 摘要；本模块的异常、
   日志与返回值都不携带明文令牌。`provision_device` 是唯一接收明文的入口 ——
   调用方负责把它烧进秤端，**不得**把它写进日志或数据库。
2. **授权范围只来自设备行**：本模块从不读取请求体里的 `market_id` / `stall_id` 作为授权依据；
   激活请求里可选的 `stall_no` 只用来**比对**既有绑定，比对不一致一律 `MT-2005`，
   **绝不**据此改写绑定（契约 §1 第 6 条 + `MT-2005` 的处置建议"不自动改绑"）。
3. **错误码 → HTTP 状态的映射只有一处**：本模块只抛 `DeviceError(code, message, detail)`，
   `code` 取自契约 §4 的 `MT-2xxx`；HTTP 状态由 `app/__init__.py::ERROR_STATUS` 唯一决定，
   本模块**不另写一套状态码**（同一个规则写两遍就是下次漂移的种子）。

本模块**不依赖 HTTP**（与 `app/domain/offline.py` 同约定）：端点层只做取参、组装响应。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from typing import Any

from ..db import now_iso

#: 契约 §4 秤端接入段的错误码（本模块只抛这些码；HTTP 状态见 `app.ERROR_STATUS`）
ERR_DEVICE_TOKEN = "MT-2001"      # 设备未激活或令牌无效
ERR_DEVICE_SCOPE = "MT-2002"      # 设备与目标的（市场, 摊位）不匹配
ERR_PROTO = "MT-2003"             # 协议版本不兼容
ERR_DEVICE_PAYLOAD = "MT-2004"    # 上报载荷不合法（此处用于预注册/心跳的输入校验）
ERR_BINDING_CONFLICT = "MT-2005"  # 设备绑定冲突

#: 本中台支持的协议版本（契约 §1 第 9 条 / §7）
SUPPORTED_PROTO = frozenset({"1"})
#: 请求头 `X-Scale-Proto` 缺省视为 `1`（本中台当前只支持 v1；不按"最新版猜测解析"，
#: 因为"最新版"就是 v1 —— 见 `require_supported_proto` 的说明）
DEFAULT_PROTO = "1"

#: 令牌长度下限（与 `stall_session.session_token` 的 16 同口径；`data-model.md` §2.3）
TOKEN_MIN_LENGTH = 16
#: 令牌熵（`secrets.token_urlsafe` 的字节数）—— 生成的明文只回给调用方一次
TOKEN_BYTES = 32
#: `device_id` 长度上限（`0003_device.sql` 的 CHECK 同值）
DEVICE_ID_MAX_LENGTH = 32


class DeviceError(Exception):
    """秤端接入段的领域异常。

    `code` 必须是契约 §4 的 `MT-2xxx`；**HTTP 状态不在本类里** ——
    唯一映射点是 `app/__init__.py::ERROR_STATUS`（避免两处各写一套状态码而漂移）。
    """

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------------------
# 令牌：摘要与生成
# ---------------------------------------------------------------------------


def digest_token(token: str) -> str:
    """令牌摘要（`sha256` 十六进制，64 字符）。

    **只比对摘要**（`REQ-036`）：明文令牌仅存在于调用栈上，绝不落库、绝不进日志。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """生成一枚新设备令牌（明文只返回给预注册调用方用于烧录）。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def require_supported_proto(raw: str | None) -> str:
    """校验 `X-Scale-Proto`；不认识的版本 → `MT-2003`（**不得降级重试**）。

    缺省（请求头缺失）按 `1` 处理：本中台**只支持 v1**，"缺省"不是"更新的一版"，
    故不存在"按最新版猜测解析"的风险；显式给了不认识的值则一律拒绝。
    """
    if raw is None or raw == "":
        return DEFAULT_PROTO
    if raw not in SUPPORTED_PROTO:
        raise DeviceError(
            ERR_PROTO,
            "协议版本不兼容：固件与中台版本不匹配，需升级固件",
            {"supported": sorted(SUPPORTED_PROTO), "received": raw},
        )
    return raw


# ---------------------------------------------------------------------------
# 读取与形状校验
# ---------------------------------------------------------------------------


def _device_row(conn: sqlite3.Connection, device_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM device WHERE device_id = ?", (device_id,)).fetchone()


def _public(row: sqlite3.Row) -> dict[str, Any]:
    """设备行的对外视图：**不含 `token_digest`**（摘要也没有下发的理由）。"""
    return {
        "device_id": row["device_id"],
        "market_id": row["market_id"],
        "stall_id": row["stall_id"],
        "firmware_version": row["firmware_version"],
        "hardware_rev": row["hardware_rev"],
        "status": row["status"],
        "last_heartbeat_at": row["last_heartbeat_at"],
        "created_at": row["created_at"],
        "activated_at": row["activated_at"],
    }


def _check_device_id(device_id: Any) -> str:
    if not isinstance(device_id, str) or not (1 <= len(device_id) <= DEVICE_ID_MAX_LENGTH):
        raise DeviceError(
            ERR_DEVICE_PAYLOAD,
            f"`device_id` 必须是 1~{DEVICE_ID_MAX_LENGTH} 字符的字符串",
            {"field": "device_id"},
        )
    return device_id


def _check_token(token: Any) -> str:
    """令牌形状校验；缺失/过短一律 `MT-2001`（调用方按"未激活"处理，不得进入营业流程）。"""
    if not isinstance(token, str) or len(token) < TOKEN_MIN_LENGTH:
        raise DeviceError(
            ERR_DEVICE_TOKEN,
            "设备未激活或令牌无效",
            {"header": "X-Device-Token"},
        )
    return token


def _market_row(conn: sqlite3.Connection, market_code: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM market WHERE market_code = ? AND status = 'active'", (market_code,)
    ).fetchone()
    if row is None:
        raise DeviceError(
            ERR_DEVICE_PAYLOAD, f"市场不存在或已停用：{market_code}", {"market_code": market_code}
        )
    return row


def _stall_row(conn: sqlite3.Connection, market_id: int, stall_no: str) -> sqlite3.Row | None:
    """**市场内**取摊位：摊位号只在市场内唯一（`0002_market_scope.sql` 的 `ux_stall_no`）。"""
    return conn.execute(
        "SELECT * FROM stall WHERE market_id = ? AND stall_no = ? AND status = 'active'",
        (market_id, stall_no),
    ).fetchone()


# ---------------------------------------------------------------------------
# 预注册（运维侧接缝；契约测试据此构造"已注册设备"，不另造端点）
# ---------------------------------------------------------------------------


def provision_device(
    conn: sqlite3.Connection,
    *,
    device_id: str,
    market_code: str,
    stall_no: str,
    token: str,
) -> None:
    """预注册一台设备：把「市场 + 摊位」绑定与**令牌摘要**写进 `device` 表。

    - `market_code` 是**必填**（不给默认市场）：`AC-026` 要用两个市场，
      默认值会把多市场问题藏起来。
    - 同一 `device_id` 以**同一绑定**重复预注册 = 幂等（顺带轮换令牌摘要，便于换发令牌）；
      以**不同绑定**重复预注册 = `MT-2005`（`REQ-036`：不自动改绑）。
    - 明文 `token` 只在本函数栈上存在：落库的是 `digest_token(token)`。
    """
    device_id = _check_device_id(device_id)
    if not isinstance(token, str) or len(token) < TOKEN_MIN_LENGTH:
        raise DeviceError(
            ERR_DEVICE_PAYLOAD,
            f"`token` 至少 {TOKEN_MIN_LENGTH} 字符（预注册时生成，不得入库明文）",
            {"field": "token"},
        )
    if not isinstance(market_code, str) or not market_code:
        raise DeviceError(ERR_DEVICE_PAYLOAD, "`market_code` 必填", {"field": "market_code"})
    if not isinstance(stall_no, str) or not stall_no:
        raise DeviceError(ERR_DEVICE_PAYLOAD, "`stall_no` 必填", {"field": "stall_no"})

    market = _market_row(conn, market_code)
    stall = _stall_row(conn, int(market["id"]), stall_no)
    if stall is None:
        raise DeviceError(
            ERR_DEVICE_PAYLOAD,
            f"摊位不存在、已停用或不属于市场 {market_code}：{stall_no}",
            {"market_code": market_code, "stall_no": stall_no},
        )

    digest = digest_token(token)
    existing = _device_row(conn, device_id)
    if existing is not None:
        if (int(existing["market_id"]), int(existing["stall_id"])) != (
            int(market["id"]),
            int(stall["id"]),
        ):
            raise DeviceError(
                ERR_BINDING_CONFLICT,
                "设备绑定冲突：该 device_id 已绑定到其他（市场, 摊位），请先在运营端解绑",
                {
                    "device_id": device_id,
                    "bound": {
                        "market_id": int(existing["market_id"]),
                        "stall_id": int(existing["stall_id"]),
                    },
                    "requested": {
                        "market_id": int(market["id"]),
                        "stall_id": int(stall["id"]),
                    },
                },
            )
        conn.execute(
            "UPDATE device SET token_digest = ? WHERE device_id = ?",
            (digest, device_id),
        )
        return

    conn.execute(
        "INSERT INTO device (device_id, token_digest, market_id, stall_id, status, created_at)"
        " VALUES (?, ?, ?, ?, 'registered', ?)",
        (device_id, digest, int(market["id"]), int(stall["id"]), now_iso()),
    )


# ---------------------------------------------------------------------------
# 鉴权：授权范围只来自设备行
# ---------------------------------------------------------------------------


def authenticate_device(
    conn: sqlite3.Connection,
    token: str | None,
    *,
    device_id: str | None = None,
    require_active: bool = True,
) -> dict[str, Any]:
    """校验 `X-Device-Token` 并返回设备（**不含摘要**）。

    令牌缺失 / 无效 / 设备未预注册 / 已停用 → `MT-2001`；给了 `device_id` 而它与令牌
    所属设备不一致 → 同为 `MT-2001`（"设备未激活或令牌无效"）。
    `require_active`：数据面端点（字典 / 价目表 / 上报 / 收款 / 退货 / 心跳）要求设备已激活，
    未激活一律 `MT-2001` —— 契约 §4 的 `MT-2001` 原文就是"设备**未激活**或令牌无效"；
    只有激活端点自己传 `False`（它正是把 `registered` 变成 `active` 的那一步）。
    """
    token = _check_token(token)
    row = conn.execute("SELECT * FROM device WHERE token_digest = ?", (digest_token(token),)).fetchone()
    if row is None or row["status"] == "disabled":
        raise DeviceError(ERR_DEVICE_TOKEN, "设备未激活或令牌无效", {"header": "X-Device-Token"})
    if require_active and row["status"] != "active":
        raise DeviceError(
            ERR_DEVICE_TOKEN,
            "设备未激活：请先调用设备激活端点",
            {"header": "X-Device-Token", "status": row["status"]},
        )
    if device_id is not None and row["device_id"] != device_id:
        raise DeviceError(
            ERR_DEVICE_TOKEN,
            "设备未激活或令牌无效：请求体 device_id 与令牌所属设备不一致",
            {"header": "X-Device-Token", "field": "device_id"},
        )
    return _public(row)


def binding_of(conn: sqlite3.Connection, device: dict[str, Any]) -> dict[str, Any]:
    """把设备绑定展开成契约 §3.1 需要的 `market` / `stall` 两个窄对象。"""
    market = conn.execute("SELECT * FROM market WHERE id = ?", (device["market_id"],)).fetchone()
    stall = conn.execute("SELECT * FROM stall WHERE id = ?", (device["stall_id"],)).fetchone()
    if market is None or stall is None:
        raise DeviceError(
            ERR_DEVICE_TOKEN,
            "设备绑定的市场或摊位不存在，请重新预注册",
            {"device_id": device["device_id"]},
        )
    return {
        "device_id": device["device_id"],
        "market_id": int(market["id"]),
        "stall_id": int(stall["id"]),
        "market": {"market_code": market["market_code"], "name": market["name"]},
        "stall": {"stall_no": stall["stall_no"]},
    }


# ---------------------------------------------------------------------------
# §3.1 激活 / §3.2 心跳
# ---------------------------------------------------------------------------


def activate_device(
    conn: sqlite3.Connection,
    *,
    device_id: str,
    token: str | None,
    stall_no: str | None = None,
    firmware_version: str | None = None,
    hardware_rev: str | None = None,
) -> dict[str, Any]:
    """激活设备并返回**既有绑定**（重复激活幂等：不报错、不改绑定）。

    `stall_no` 是契约 §3.1 的**可选**字段，只用于**比对**：
    - 不带 ⇒ 以预注册的绑定为准；
    - 带上且与绑定一致 ⇒ 正常激活；
    - 带上且与绑定不一致（含该市场内查无此摊位）⇒ `MT-2005`，**绝不**据此改绑。
    """
    device_id = _check_device_id(device_id)
    device = authenticate_device(conn, token, device_id=device_id, require_active=False)

    if stall_no is not None:
        requested = _stall_row(conn, device["market_id"], stall_no) if isinstance(stall_no, str) else None
        if requested is None or int(requested["id"]) != device["stall_id"]:
            raise DeviceError(
                ERR_BINDING_CONFLICT,
                "设备绑定冲突：请求的 stall_no 与既有绑定不一致，请先在运营端解绑",
                {
                    "device_id": device_id,
                    "requested_stall_no": stall_no,
                    "bound_stall_id": device["stall_id"],
                },
            )

    conn.execute(
        "UPDATE device SET status = 'active',"
        " firmware_version = COALESCE(?, firmware_version),"
        " hardware_rev = COALESCE(?, hardware_rev),"
        " activated_at = COALESCE(activated_at, ?)"
        " WHERE device_id = ?",
        (firmware_version, hardware_rev, now_iso(), device_id),
    )
    refreshed = _device_row(conn, device_id)
    return binding_of(conn, _public(refreshed))


def heartbeat_device(
    conn: sqlite3.Connection,
    *,
    device_id: str,
    token: str | None,
    pending_count: int | None = None,
    firmware_version: str | None = None,
) -> dict[str, Any]:
    """心跳：刷新 `last_heartbeat_at`（应用层显式取 `now_iso()`），返回设备侧观测字段。

    **`pending_count` 只用于观测**（中台据此看"哪些摊位长期有未补传数据"），
    **绝不据此拒绝任何交易**（`NFR-014` / `RL-9`）—— 故本函数对它只做形状校验与回显。
    """
    device_id = _check_device_id(device_id)
    device = authenticate_device(conn, token, device_id=device_id)

    if pending_count is not None and (
        isinstance(pending_count, bool) or not isinstance(pending_count, int) or pending_count < 0
    ):
        raise DeviceError(
            ERR_DEVICE_PAYLOAD,
            "`pending_count` 必须是 ≥0 的整数",
            {"field": "pending_count"},
        )

    stamp = now_iso()
    conn.execute(
        "UPDATE device SET last_heartbeat_at = ?,"
        " firmware_version = COALESCE(?, firmware_version)"
        " WHERE device_id = ?",
        (stamp, firmware_version, device_id),
    )
    return {
        "device_id": device_id,
        "market_id": device["market_id"],
        "stall_id": device["stall_id"],
        "last_heartbeat_at": stamp,
        "firmware_version": firmware_version or device["firmware_version"],
        "pending_count": pending_count,
    }
