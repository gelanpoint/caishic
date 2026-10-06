#!/usr/bin/env python
"""主机侧秤端运行器 —— 把「秤端」当成**独立进程**跑起来，经**真实 HTTP** 对接中台。

对应 `T-SCALE-17` / `ADR-0005` §4 第 5 条「分离形态是新增启动方式」。

## ⚠️ 这不是硬件在环（`Q-21`）

本运行器是**主机侧秤端**：它用本机的 C 编译器把**真的 `scale-fw/core/*.c`** 编成共享库来算钱，
但它**不能替代 ESP32-S3 真机**。下列行为**一律未验证**：真实称重采样、触摸屏、WiFi 射频、
掉电时序、Flash 磨损。详见 `docs/hardware/秤端硬件选型与成本.md` 与未决项 `Q-21`。

## 它真的用了秤端固件的代码吗

用了 —— 金额计算**不是 Python 复刻**，而是 `ctypes` 调用现场编译的 `scale-fw/core/pricing.c`：

    zig cc -shared -fPIC -target x86_64-linux-gnu -std=c11 -I scale-fw/core -o <cache>/libscalecore.so scale-fw/core/*.c

这与固件在 ESP32-S3 上跑的是**同一份 C 源码**（`core/**` 不依赖任何 ESP-IDF 头，见 `ADR-0006`）。
中台侧会**独立重算并逐行比对**（`AC-030`），所以这里的算术若有偏差会被中台抓出来。

## 用法

    # 终端 A：只起中台（不托管秤端界面）
    python run.py --mode split

    # 终端 B：起一个秤端（中台没起来也能先起 —— AC-025 前半段）
    python scripts/scale_host_runner.py --mid http://127.0.0.1:8000 \
        --device-id SC-000001 --stall-no A-01 --price-cents 480 --weight-grams 780

    # 想模拟多个商家就多开几个终端，换 --device-id / --stall-no 即可
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 「现在是几点」全项目**只有一个落点**（`T-SIM-00` / `AC-024`）：本运行器**不得**直读墙钟。
# 好处不只是守规矩 —— 注入时钟后秤端的 `staged_at` 也一起受仿真时钟驱动，补传顺序可复现。
from app import clock  # noqa: E402  （必须在 sys.path 处理之后导入）

CORE_DIR = REPO_ROOT / "scale-fw" / "core"
BASE_PATH = "/api/scale/v1"
DEVICE_TOKEN_HEADER = "X-Device-Token"
PROTO_HEADER = "X-Scale-Proto"
SUPPORTED_PROTO = "1"


# --------------------------------------------------------------------------- #
# 1) 真 C 核心：现场编译 + ctypes 调用
# --------------------------------------------------------------------------- #


class CoreMath:
    """`scale-fw/core` 的宿主侧包装（金额计算的**唯一**实现，不做 Python 复刻）。"""

    def __init__(self, lib_path: Path) -> None:
        lib = ctypes.CDLL(str(lib_path))
        lib.price_amount.argtypes = [ctypes.c_int64, ctypes.c_int64, ctypes.POINTER(ctypes.c_int64)]
        lib.price_amount.restype = ctypes.c_bool
        self._lib = lib

    def price_amount(self, unit_price_cents: int, weight_grams: int) -> int:
        """`amount_cents = (unit_price_cents * weight_grams + 500) // 1000`（整数、无浮点）。"""
        out = ctypes.c_int64()
        if not self._lib.price_amount(unit_price_cents, weight_grams, ctypes.byref(out)):
            raise RuntimeError(f"core 拒绝计算：单价 {unit_price_cents} × {weight_grams} 克（溢出或越界）")
        return out.value


def build_core(cache_dir: Path) -> Path:
    """把 `scale-fw/core/*.c` 编成共享库；已是最新则复用。

    `-target x86_64-linux-gnu`（**不是** musl）：本进程是宿主 glibc Python，musl 共享库装不进来。
    源文件比产物新就重编，避免"改了 C 却跑旧库"的静默漂移。
    """
    lib_path = cache_dir / "libscalecore.so"
    sources = sorted(CORE_DIR.glob("*.c"))
    if not sources:
        raise SystemExit(f"[秤端] 找不到 C 核心源码：{CORE_DIR}")
    if lib_path.is_file() and lib_path.stat().st_mtime >= max(s.stat().st_mtime for s in sources):
        return lib_path
    zig = Path.home() / ".local" / "bin" / "zig"
    if not zig.is_file():
        raise SystemExit(f"[秤端] 找不到 C 编译器 {zig}（`scale-fw/core` 无法编译）")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(zig), "cc", "-shared", "-fPIC", "-target", "x86_64-linux-gnu",
        "-std=c11", "-O2", "-I", str(CORE_DIR), "-o", str(lib_path),
        *[str(s) for s in sources],
    ]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"[秤端] 编译 C 核心失败：\n{done.stderr}")
    print(f"[秤端] 已编译真 C 核心 → {lib_path}（{len(sources)} 个源文件）")
    return lib_path


# --------------------------------------------------------------------------- #
# 2) 中台客户端：标准库 http.client（不引新依赖，ADR-0004）
# --------------------------------------------------------------------------- #


class MidUnreachable(RuntimeError):
    """中台连不上 —— **不是错误，是常态**（`AC-025` 前半段）。"""


class MidClient:
    """7 个秤端接入端点的极简客户端。**每次请求新建连接**，断开即视为不可达。"""

    def __init__(self, base_url: str, token: str | None, timeout: float = 3.0) -> None:
        parts = urlsplit(base_url)
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or 80
        self._token = token
        self._timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None, key: str | None = None):
        conn = HTTPConnection(self._host, self._port, timeout=self._timeout)
        headers = {"Content-Type": "application/json", PROTO_HEADER: SUPPORTED_PROTO}
        if self._token:
            headers[DEVICE_TOKEN_HEADER] = self._token
        if key:
            headers["X-Idempotency-Key"] = key
        try:
            conn.request(method, f"{BASE_PATH}{path}", body=json.dumps(body) if body else None, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
        except (OSError, HTTPException) as exc:
            raise MidUnreachable(f"{method} {path} 连不上中台（{self._host}:{self._port}）：{exc}") from exc
        finally:
            conn.close()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return status, payload

    def activate(self, device_id: str, market_code: str | None, stall_no: str | None):
        body = {"device_id": device_id, "firmware_version": "host-runner-1.0", "hardware_rev": "host"}
        if market_code:
            body["market_code"] = market_code
        if stall_no:
            body["stall_no"] = stall_no
        return self._request("POST", "/devices/activate", body)

    def catalog(self):
        return self._request("GET", "/catalog")

    def price_list(self, business_date: str):
        return self._request("GET", f"/price-list?business_date={business_date}")

    def report(self, body: dict, key: str):
        return self._request("POST", "/transactions", body, key=key)


# --------------------------------------------------------------------------- #
# 3) 秤端本地状态：暂存队列（RL-8 / NFR-013）
# --------------------------------------------------------------------------- #


class DeviceStore:
    """秤端本地状态（档案缓存 + 待补传队列）。

    队列**按 `staged_at` 升序**补传；补传成功才删本地副本（`RL-8`：补传成功后必须清除本地副本）。
    写盘用"先写临时文件再 replace"，避免掉电留下半截 JSON（`NFR-014` / `RL-9` 的本地侧对应物）。
    """

    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "device_state.json"
        data = {"cache": {}, "staged": [], "seq": 0}
        if self.path.is_file():
            try:
                data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                print(f"[秤端] ⚠️ 本地状态文件损坏，按空状态继续（不静默丢队列以外的东西）：{self.path}")
        self.data = data

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # 原子替换

    def stage(self, body: dict) -> dict:
        """把一笔交易放进待补传队列，返回带 `staged_at` / `seq` 的记录。

        `staged_at` 取自 `app/clock.py`（**不直读墙钟**）：ISO-8601 文本本身字典序即时间序，
        同刻的并列由 `seq` 打破，故补传顺序**确定且可复现**。
        """
        self.data["seq"] += 1
        record = {
            "staged_at": clock.now_iso(),
            "seq": self.data["seq"],
            "idempotency_key": body["client_idempotency_key"],
            "body": body,
        }
        self.data["staged"].append(record)
        self.save()
        return record

    def pending(self) -> list[dict]:
        """**按 `staged_at` 升序**（同刻按 `seq`）—— 补传顺序是验收项，不是实现细节。

        `str(...)` 兜住旧状态文件里的历史类型（早期版本写的是浮点时间戳），
        避免"新旧混排时比较 str 与 float 抛 TypeError"把队列整条卡死。
        """
        return sorted(self.data["staged"], key=lambda r: (str(r["staged_at"]), r["seq"]))

    def ack(self, record: dict) -> None:
        """补传成功后**清除本地副本**（`RL-8` / `NFR-013`）。"""
        self.data["staged"] = [r for r in self.data["staged"] if r["seq"] != record["seq"]]
        self.save()


# --------------------------------------------------------------------------- #
# 4) 编排：一轮营业 + 补传
# --------------------------------------------------------------------------- #


def backfill(client: MidClient, store: DeviceStore) -> tuple[int, int]:
    """补传全部暂存交易，返回 `(成功数, 仍暂存数)`。

    两条硬要求：① 按 `staged_at` **升序**；② 某条失败**保持暂存**且**不阻断后续**
    （补传一条失败就放弃剩下的，等于把后面的交易也拖住 —— `RL-9` 不允许静默丢弃）。
    """
    ok = 0
    for record in store.pending():
        try:
            status, payload = client.report(record["body"], record["idempotency_key"])
        except MidUnreachable:
            print("[秤端] 中台仍不可达，停止本轮补传（剩余交易**继续暂存**，不丢）")
            break
        if status in (200, 201):
            store.ack(record)  # 成功 ⇒ 清除本地副本
            ok += 1
            print(f"[秤端] 补传成功 #{record['seq']} → {payload.get('transaction_no', '?')}")
        elif status == 409 and payload.get("replayed"):
            store.ack(record)  # 幂等命中：中台已有该笔，本地副本同样可以清
            ok += 1
            print(f"[秤端] 补传幂等命中 #{record['seq']}（中台已有，清除本地副本）")
        else:
            print(f"[秤端] ⚠️ 补传被拒 #{record['seq']}：{status} {payload.get('error', payload)} —— 保持暂存")
    return ok, len(store.pending())


def make_transaction(
    core: CoreMath,
    args,
    version: int,
    product_id: int,
    business_date: str,
    key: str,
    price_cents: int,
    origin: str,
) -> dict:
    """构造上报体。**金额由真 C 核心算出**，不是 Python 复刻。"""
    amount = core.price_amount(price_cents, args.weight_grams)
    return {
        "business_date": business_date,
        "captured_at": f"{business_date} 08:20:11",
        "origin": origin,
        "price_list_version": version,
        "items": [{"product_id": product_id, "weight_grams": args.weight_grams}],
        "amount_cents": amount,
        "local_lines": [
            {
                "product_id": product_id,
                "unit_price_cents": price_cents,
                "weight_grams": args.weight_grams,
                "amount_cents": amount,
            }
        ],
        "round_off_cents": 0,
        "client_idempotency_key": key,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="主机侧秤端运行器（T-SCALE-17 分离形态）",
        epilog=(
            "⚠️ 本运行器是**主机侧秤端**，不是硬件在环：真实称重采样 / 触摸 / WiFi / 掉电 / Flash 磨损"
            " 一律未验证（未决项 Q-21）。它调用的是真的 scale-fw/core/*.c，但仍不能替代 ESP32-S3 真机。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mid", default="http://127.0.0.1:8000", help="中台基址")
    parser.add_argument("--device-id", default="SC-000001", help="设备号（多开时改这个即可模拟多商家）")
    parser.add_argument("--token", default=None, help="设备令牌（预注册时烧录的那枚）")
    parser.add_argument("--market-code", default="M-0001", help="市场编码（激活用）")
    parser.add_argument("--stall-no", default="A-01", help="摊位号（激活用）")
    parser.add_argument("--state-dir", default=None, help="秤端本地状态目录（默认 data/scale/<设备号>）")
    parser.add_argument(
        "--price-cents",
        type=int,
        default=None,
        help="单价（分/公斤）。**默认不填 = 取中台价目表里该商品的价格**（正常营业）；"
        "显式填写则覆盖本地价 —— 用于**故意制造**「秤端计价 ≠ 中台重算」（AC-030 的不一致态）",
    )
    parser.add_argument("--weight-grams", type=int, default=780, help="重量（克）")
    parser.add_argument("--product-id", type=int, default=None, help="商品 id（默认取价目表第一条）")
    parser.add_argument("--business-date", default=None, help="营业日（默认今天）")
    parser.add_argument(
        "--origin",
        choices=("auto", "online", "offline"),
        default="auto",
        help="成交时是否在线。**默认 auto = 按实际链路自动判定**（与固件一致："
        "`scale-fw/main/net/sync.c` 取**暂存时**的链路状态）；显式 online/offline 仅用于覆盖",
    )
    parser.add_argument("--idempotency-key", default=None, help="幂等键（默认自动生成）")
    parser.add_argument("--count", type=int, default=1, help="本轮成交笔数")
    parser.add_argument("--backfill-only", action="store_true", help="只补传，不做新交易")
    args = parser.parse_args(argv)

    business_date = args.business_date or clock.today_iso()
    state_dir = Path(args.state_dir) if args.state_dir else REPO_ROOT / "data" / "scale" / args.device_id

    print("=" * 68)
    print(f" 主机侧秤端 · 设备 {args.device_id} → 中台 {args.mid}")
    print(" 边界：不是硬件在环（Q-21）；真机行为未验证")
    print("=" * 68)

    core = CoreMath(build_core(state_dir / "core"))
    store = DeviceStore(state_dir)
    client = MidClient(args.mid, args.token)

    # ---- ① 连中台：**连不上也继续**（AC-025 前半段） ----
    online = False
    version, product_id, price_cents = 1, args.product_id, args.price_cents
    try:
        status, payload = client.activate(args.device_id, args.market_code, args.stall_no)
        if status == 200:
            online = True
            store.data["cache"]["activate"] = payload
            print(f"[秤端] 激活成功：摊位 {payload.get('stall', {}).get('stall_no', '?')}")
            st, cat = client.catalog()
            if st == 200:
                store.data["cache"]["catalog"] = cat
            st, pl = client.price_list(business_date)
            if st == 200:
                store.data["cache"]["price_list"] = pl
                version = pl.get("price_list_version", 1)
                items = pl.get("items") or []
                if product_id is None and items:
                    product_id = items[0].get("product_id")
                # **本地价默认取中台价目表**（正常营业态）：本地计价与中台重算应当一致。
                # 只有显式传 `--price-cents` 时才覆盖 —— 那是**故意**制造 AC-030 的不一致态。
                if price_cents is None:
                    match = next((i for i in items if i.get("product_id") == product_id), None)
                    if match:
                        price_cents = match.get("unit_price_cents")
            store.save()
        else:
            print(f"[秤端] ⚠️ 激活被拒：{status} {payload.get('error', payload)}")
    except MidUnreachable as exc:
        print(f"[秤端] ⚠️ 中台不可达：{exc}")
        print("[秤端] → **继续启动**（离线可用是硬要求）。用本地缓存；无缓存则用命令行单价。")
        cached = store.data["cache"].get("price_list") or {}
        version = cached.get("price_list_version", 1)
        items = cached.get("items") or []
        if product_id is None and items:
            product_id = items[0].get("product_id")
        if price_cents is None:
            match = next((i for i in items if i.get("product_id") == product_id), None)
            if match:
                price_cents = match.get("unit_price_cents")

    if product_id is None:
        product_id = 1  # 离线且无缓存时的占位；中台侧会按自己的价目表重算并比对
    if price_cents is None:
        price_cents = 480  # 完全无来源时的兜底；此时中台会按自己的价目表重算，差异会被如实留痕
        print("[秤端] ⚠️ 本地价无来源（无缓存且未传 --price-cents），用兜底值 480；中台会重算并比对")

    # ---- ② 成交若干笔（金额由真 C 核心算）→ 先暂存，再尝试上报 ----
    if not args.backfill_only:
        # `origin` 的**默认语义与固件一致**：取**成交那一刻**的链路状态（`sync.c` 同款判定）。
        # 若默认写死 `online`，则"中台不可达时成交"会被记成 `online`，**「当时在线 vs 事后补传」
        # 的区分就丢了** —— 而这正是契约 §3.5 与中台 `ORIGIN_MAP` 存在的理由（`task-6` 用例抓出）。
        if args.origin == "auto":
            origin = "offline_backfill" if not online else "online"
        else:
            origin = "offline_backfill" if args.origin == "offline" else "online"
        stamp = clock.now_iso().replace(" ", "T").replace(":", "")
        src = "命令行覆盖" if args.price_cents is not None else "中台价目表"
        for i in range(args.count):
            key = args.idempotency_key or f"{args.device_id}-{stamp}-{i + 1}"
            body = make_transaction(
                core, args, version, product_id, business_date, key, price_cents, origin
            )
            rec = store.stage(body)
            print(f"[秤端] 本地计价：{price_cents} 分/公斤（来源：{src}）× {args.weight_grams} 克 = "
                  f"{body['amount_cents']} 分（真 C 核心）→ 已暂存 #{rec['seq']}（origin={origin}）")

    # ---- ③ 补传（按 staged_at 升序；失败保持暂存） ----
    sent, left = backfill(client, store)
    print("-" * 68)
    print(f"[秤端] 补传成功 {sent} 笔，仍暂存 {left} 笔（本地副本：{store.path}）")
    if not online and left:
        print("[秤端] 中台恢复后重跑本命令即可补传；同一幂等键不会重复入账。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
