"""评委环境仿真：解压交付包 → 在"没装 Flask + 没外网"的环境里真起服务 → 打四个页面。

用 Python 而不是 PowerShell：长轮询 + 子进程在 PowerShell 里连续两次被静默中断
（退出码 4294967295、无任何输出），而 Python 的 subprocess/urllib 在本机已被走查脚本验证可靠。

判定标准刻意定死：**必须真起服务并打通页面**，不是"能 import"。
（上一轮就是只验了 `import flask` 就宣布兜底成立，结果 werkzeug 启动时
`importlib.metadata.version()` 抛 PackageNotFoundError，服务秒退。）
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

TMP = Path(tempfile.gettempdir())
ZIP = Path(sys.argv[1]) if len(sys.argv) > 1 else None   # 必填：没有就报错，不猜本机路径
DST = Path(sys.argv[2]) if len(sys.argv) > 2 else TMP / "mt-judge"
DATA = DST.with_name(DST.name + "-data")
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8871
PAGES = ("/scale/", "/customer/", "/admin/", "/")


def log(msg):
    print(msg, flush=True)


def port_open(port, timeout=1.0):
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    if ZIP is None or not ZIP.is_file():
        # ⚠️ 曾经把默认值写死成开发机上的 D:\release\...，
        # 结果在别人机器上「照着文档跑」直接失败 —— **默认路径不该假设自己是谁**。
        print("用法：python scripts/judge_sim.py <交付包.zip> [解压目录] [端口]")
        print(f"      当前找不到交付包：{ZIP or '(未指定)'}")
        print("      先构建：python scripts/build_submission.py --out <输出目录>")
        return 2
    if DST.exists():
        shutil.rmtree(DST)
    if DATA.exists():
        shutil.rmtree(DATA)
    DST.mkdir(parents=True)
    with zipfile.ZipFile(ZIP) as zf:
        zf.extractall(DST)
    root = DST / ZIP.stem
    log(f"[1] 解压到 {root}")

    # 评委机器：没有用户 site-packages（Flask 真装不上）、没有外网
    env = {k: v for k, v in os.environ.items()}
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["MT_DATA_DIR"] = str(DATA)

    probe = subprocess.run([sys.executable, "-c", "import flask"], env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    log(f"[2] 无 Flask 环境自证：returncode={probe.returncode} "
        f"({'确实没有' if probe.returncode else '⚠️ 竟然有，这条验证无效'})")
    if probe.returncode == 0:
        log("    ⇒ 无法在真缺依赖的条件下验证，放弃而不是给出假绿结论")
        return 2

    env["PYTHONPATH"] = str(root / "offline-deps")

    pf = subprocess.run([sys.executable, "scripts/preflight.py"], cwd=str(root), env=env,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = [ln for ln in (pf.stdout + pf.stderr).splitlines() if ln.strip()][-2:]
    log(f"[3] preflight：exit={pf.returncode} · " + " | ".join(tail))

    proc = subprocess.Popen([sys.executable, "run.py", "--port", str(PORT)],
                            cwd=str(root), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    t0 = time.monotonic()
    up = False
    try:
        while time.monotonic() - t0 < 90:
            if proc.poll() is not None:
                out, err = proc.communicate()
                log(f"[4] !! 进程已退出，退出码 {proc.returncode}")
                log("    stderr 末尾：")
                for ln in (err or "").decode("utf-8", "replace").splitlines()[-6:]:
                    log("      " + ln)
                return 1
            if port_open(PORT):
                up = True
                break
            time.sleep(0.5)

        if not up:
            log("[4] !! 90s 内端口未开放（进程仍在）")
            return 1
        log(f"[4] 服务就绪：{time.monotonic() - t0:.1f}s")

        results = []
        for p in PAGES:
            url = f"http://127.0.0.1:{PORT}{p}"
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    body = r.read().decode("utf-8", "replace")
                    results.append((p, r.status, len(body),
                                    'charset="utf-8"' in body, "�" in body))
            except urllib.error.HTTPError as e:
                results.append((p, e.code, 0, False, False))
            except Exception as e:  # noqa: BLE001
                results.append((p, f"FAIL {e}", 0, False, False))
        for p, code, size, utf8, moji in results:
            log(f"[5] {p:<11} HTTP {code} · {size} 字节 · 声明 utf-8={utf8} · 含替换字符={moji}")

        ok = all(str(c) == "200" and n > 500 for _, c, n, _, _ in results)
        log("[6] 结论：" + ("四个入口页全部 200 且有内容 —— 交付包在无外网环境可用"
                            if ok else "有页面未达标 —— 交付包还不能算可用"))
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())