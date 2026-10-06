"""§3.1 设备激活 / §3.2 心跳的契约测试（`T-SCALE-03`）。

覆盖：`REQ-034`、`REQ-036`、`REQ-040`；`AC-027`（未注册设备不产生任何交易行 —— 前半段在
`test_scale_ingest.py`，此处覆盖"已注册设备绑定到注册摊位"）；错误码 `MT-2001` / `MT-2003` / `MT-2005`。

纪律（`AGENTS.md`「验证要能失败」）：错误码用例都**真造触发条件**（缺令牌 / 错令牌 / 不支持的协议版本 /
与既有绑定不一致的 `stall_no`），不是只断言字段存在。
"""

from __future__ import annotations

from scale_provision import (
    DEMO_DEVICE_ID,
    OTHER_STALL_NO,
    active_device_token,
    registered_device,
)
from scale_support import (
    ACTIVATE_CONFIG_KEYS,
    ACTIVATE_KEYS,
    HEARTBEAT_KEYS,
    SEED_STALL_NO,
    activate,
    assert_narrow_body,
    assert_scale_error_response,
    heartbeat,
    json_of,
)


def _token(client, db_conn) -> str:
    """保证演示设备已预注册（`T-SCALE-02`）并返回其明文令牌（**激活端点接受 `registered`**）。"""
    return registered_device(db_conn)


def _active_token(client, db_conn) -> str:
    """心跳等**数据面**要求设备已激活（未激活 → `MT-2001`），故先走一次 §3.1 激活。"""
    return active_device_token(client, db_conn)


def _bound_stall_no(db_conn, device_id: str = DEMO_DEVICE_ID) -> str:
    """库里的**真实绑定**（不取自响应，避免"响应自己说自己对"）。"""
    row = db_conn.execute(
        "SELECT s.stall_no AS stall_no FROM device d JOIN stall s ON s.id = d.stall_id "
        "WHERE d.device_id = ?",
        (device_id,),
    ).fetchone()
    assert row is not None, f"设备 {device_id} 未预注册（`T-SCALE-02`）"
    return str(row["stall_no"])


def test_activate_returns_binding_and_narrow_config(client, db_conn):
    """§3.1：激活取回绑定 + 运行配置，响应体为窄体且字段白名单逐一相等（`AC-027`、`REQ-034`/`REQ-036`）。"""
    response = activate(client, _token(client, db_conn), device_id=DEMO_DEVICE_ID)
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    payload = json_of(response)
    assert_narrow_body(payload, ACTIVATE_KEYS, "§3.1 激活响应")
    assert set(payload["market"]) == {"market_code", "name"}, payload["market"]
    assert set(payload["stall"]) == {"stall_no"}, payload["stall"]
    assert set(payload["config"]) == ACTIVATE_CONFIG_KEYS, payload["config"]
    assert payload["stall"]["stall_no"] == _bound_stall_no(db_conn)
    assert payload["config"]["proto"] == 1


def test_activate_with_matching_stall_no_is_accepted(client, db_conn):
    """§3.1：`stall_no` 与既有绑定**一致** ⇒ 正常激活（声明不是授权，一致即无害）。"""
    response = activate(client, _token(client, db_conn), device_id=DEMO_DEVICE_ID, stall_no=SEED_STALL_NO)
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    assert json_of(response)["stall"]["stall_no"] == SEED_STALL_NO


def test_activate_repeat_returns_existing_binding_without_rebinding(client, db_conn):
    """§3.1 + §1.1：重复激活返回**既有绑定**，不报错、不新建绑定（`REQ-036`）。"""
    token = _token(client, db_conn)
    first = activate(client, token, device_id=DEMO_DEVICE_ID)
    assert first.status_code == 200, first.get_data(as_text=True)[:300]
    second = activate(client, token, device_id=DEMO_DEVICE_ID)
    assert second.status_code == 200, (
        f"重复激活必须 200（秤端断电重启会反复走这一步，报错会把它卡在开机）："
        f"{second.status_code} {second.get_data(as_text=True)[:300]}"
    )
    assert json_of(second)["stall"]["stall_no"] == json_of(first)["stall"]["stall_no"]
    rows = db_conn.execute(
        "SELECT COUNT(*) AS n FROM device WHERE device_id = ?", (DEMO_DEVICE_ID,)
    ).fetchone()
    assert int(rows["n"]) == 1, f"重复激活不得新建绑定，实际 device 行数 = {rows['n']}"


def test_activate_with_conflicting_stall_no_returns_mt2005_and_binding_unchanged(client, db_conn):
    """§3.1 / §4 `MT-2005`：`stall_no` 与既有绑定不一致 ⇒ 409，且**绑定不得被改变**。

    这是 `MT-2005` **唯一可构造的入口**（原契约请求体没有摊位字段，该码等于白登记 —— 父代理
    `2026-10-06` 按 `RL-2` 补了可选 `stall_no`）。`stall_no` 是**声明不是授权**：
    中台只能用它**暴露不一致**，不能据此改绑。
    """
    token = _token(client, db_conn)
    assert _bound_stall_no(db_conn) == SEED_STALL_NO
    # 用一个**真实存在**的别的摊位（A-02）：这样"不一致"无歧义，不会与"摊位不存在"混为一谈
    response = activate(client, token, device_id=DEMO_DEVICE_ID, stall_no=OTHER_STALL_NO)
    assert response.status_code == 409, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2005")
    assert _bound_stall_no(db_conn) == SEED_STALL_NO, "越权声明不得改绑"
    after = activate(client, token, device_id=DEMO_DEVICE_ID)
    assert after.status_code == 200, after.get_data(as_text=True)[:300]
    assert json_of(after)["stall"]["stall_no"] == SEED_STALL_NO


def test_activate_without_token_returns_mt2001(client):
    """§4 `MT-2001`：缺 `X-Device-Token` ⇒ 401，且**不得进入营业流程**。"""
    response = activate(client, None, device_id=DEMO_DEVICE_ID)
    assert response.status_code == 401, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2001")


def test_activate_with_unknown_token_returns_mt2001(client):
    """§4 `MT-2001`：令牌不在中台预注册记录里 ⇒ 401（未注册设备一律拒绝）。"""
    response = activate(client, "not-a-registered-device-token", device_id=DEMO_DEVICE_ID)
    assert response.status_code == 401, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2001")


def test_activate_with_unsupported_proto_returns_mt2003(client, db_conn):
    """§4 `MT-2003`：`X-Scale-Proto` 不在支持集合内 ⇒ 409，**不得按最新版猜测解析**。"""
    response = activate(client, _token(client, db_conn), device_id=DEMO_DEVICE_ID, proto="99")
    assert response.status_code == 409, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2003")


def test_heartbeat_returns_business_date_and_config_version(client, db_conn):
    """§3.2：心跳取业务日、服务器时间、配置版本；响应体字段白名单逐一相等（`REQ-040`）。"""
    response = heartbeat(client, _active_token(client, db_conn), device_id=DEMO_DEVICE_ID, pending_count=3)
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    payload = json_of(response)
    assert_narrow_body(payload, HEARTBEAT_KEYS, "§3.2 心跳响应")
    assert isinstance(payload["config_changed"], bool)
    assert isinstance(payload["catalog_version"], int)


def test_heartbeat_high_pending_count_is_accepted(client, db_conn):
    """§3.2 + `NFR-014`：`pending_count` 由秤端自报，中台**不据此拒绝任何交易**（此处先证心跳不被拒）。

    "不拒绝交易"的端到端证法在 `test_scale_ingest.py::test_pending_count_does_not_block_ingest`。
    """
    response = heartbeat(client, _active_token(client, db_conn), device_id=DEMO_DEVICE_ID, pending_count=999_999)
    assert response.status_code == 200, (
        f"暂存积压不是拒绝理由（NFR-014：达阈值只告警、仍继续接受）："
        f"{response.status_code} {response.get_data(as_text=True)[:300]}"
    )


def test_heartbeat_without_token_returns_mt2001(client):
    """§4 `MT-2001`：心跳同样要求设备令牌。"""
    response = heartbeat(client, None, device_id=DEMO_DEVICE_ID)
    assert response.status_code == 401, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2001")


def test_heartbeat_with_unsupported_proto_returns_mt2003(client, db_conn):
    """§4 `MT-2003`：心跳同样受协议版本闸门约束。"""
    response = heartbeat(client, _active_token(client, db_conn), device_id=DEMO_DEVICE_ID, proto="0")
    assert response.status_code == 409, response.get_data(as_text=True)[:300]
    assert_scale_error_response(response, "MT-2003")
