"""业务日与时间戳的**来源**（唯一落点；`REQ-033` / `AC-024`，任务 `T-SIM-00`）。

## 为什么要有这个模块（不是洁癖）

系统里每一处业务日（`transaction.business_date`、日聚合、结算单、三个使用率指标）与每一处
时间戳（`created_at`、`confirmed_at`、`audit_log.occurred_at` …）原先各自直接取
`date.today()` / `datetime.now()`。后果有两个，都不是风格问题：

1. **"现在是哪一天"有 7 个出处** —— 同一个规则写 7 遍，就是下次漂移的种子（实测：`pricing.py:152`、
   `metrics.py:82/210/284`、`offline.py:152`、`payment.py:65`、`seed.py:346`）。
2. **跨营业日在离线环境里无法复现** —— 多智能体仿真（`docs/sim-design.md`）与跨营业日回归需要一个
   **可推进的业务日**：一个仿真营业日必须能在毫秒级被走完，而墙钟一天不会因此缩短。

## 语义边界（写在这里以免被误读）

1. **默认 = 本机墙钟。** 不设 `MT_CLOCK_FILE` 时，本模块与原来的
   `date.today()` / `datetime.now()` **逐字段一致** —— 这是 `AC-024` 前半段的判据。
2. **注入 = 把时钟来源说清楚，不是伪造时间。** 设了 `MT_CLOCK_FILE` 时，业务日与**全部时间戳**
   （含审计留痕）一律取自该文件的值；此时**审计时间戳 = 仿真时钟，不是墙钟**
   （已在 `spec.md` §5「边界与例外」声明）。
3. **时钟文件每次调用都重读**，因此仿真侧可以在**不重启服务、不新增端点**的前提下推进时间
   （每推进一个时段块把文件内容改掉即可）。这是选"文件内容"而不是"环境变量里的值"的原因 ——
   进程内的环境变量改不了，重启服务又要重跑迁移与种子。
4. **配了却读不到 / 读不懂 → 立刻报错**，绝不静默退回墙钟。静默退回会让"注入没生效"表现为
   "数据落在今天"，那是最难查的一类错；给出确定性反馈比让程序继续跑更重要。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

#: 时钟来源文件的环境变量名。文件内容为 `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`（UTF-8，允许首尾空白）。
ENV_CLOCK_FILE = "MT_CLOCK_FILE"

#: 允许的时钟文件取值格式（先试带时刻的，再试只有日期的）
_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


class ClockSourceError(RuntimeError):
    """时钟来源被配置但不可用（文件不存在 / 内容非法）。

    **故意不继承 `ValueError`**：`ValueError` 在业务代码里常被当作"参数校验失败"顺手接住，
    那正是"静默退回"的入口；用一个独立的异常类型让"时钟坏了"不可能被误接。
    """


def _read_injected() -> datetime | None:
    """读取注入的时钟值；未配置返回 `None`。

    配置了但不可用时抛 `ClockSourceError` —— **不返回 None**（返回 None 等于静默退回墙钟）。
    """
    raw_path = os.environ.get(ENV_CLOCK_FILE)
    if raw_path is None or not raw_path.strip():
        return None

    path = Path(raw_path.strip())
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ClockSourceError(
            f"{ENV_CLOCK_FILE} 指向的时钟文件读不到：{path}（{exc}）。"
            "拒绝静默退回墙钟：那会让「注入没生效」看起来像「数据落在今天」。"
        ) from exc

    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ClockSourceError(
        f"{ENV_CLOCK_FILE} 的内容不是合法时钟值：{text!r}"
        "（允许 `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`）"
    )


def now() -> datetime:
    """当前时刻：未注入 = 本机墙钟；注入 = 时钟文件的值（每次调用重读）。"""
    injected = _read_injected()
    return injected if injected is not None else datetime.now()


def now_iso() -> str:
    """当前时刻的 ISO-8601 文本（`YYYY-MM-DD HH:MM:SS`；`data-model.md` §0 时间字段约定）。

    全系统**时间戳**的统一取值口（`app/db.py::now_iso` 转发到本函数）。
    """
    return now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> date:
    """当前业务日：未注入 = 本机当天；注入 = 时钟文件里的日期部分。"""
    return now().date()


def today_iso() -> str:
    """当前业务日的 `YYYY-MM-DD` 文本。全系统**业务日字段**的统一取值口。"""
    return today().isoformat()
