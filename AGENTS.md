# 菜市场数字化交易与佣金系统 MVP

单市场、单机的菜市场交易与佣金 MVP：**摊主在摊位现场用秤端完成一笔称重商品的「选品 → 称重 → 计价 → 收款 → 留痕」，市场运营方在后台据此结算佣金并查看经营数据。** 核心主张：不增加摊主操作负担的前提下产出可核验的交易数据 —— 数据可信度优先于功能数量。

- **项目级别：P2** —— 依据：失败代价 `1C` / 用户规模与并发 `2B` / 生命周期 `3B`（命中兜底规则，不允许降级）；独立复核判据见 `docs/standards/classification.md`
- 技术栈：Python 3.11+ / Flask / 标准库 `sqlite3` / 纯静态前端（无构建工具、无 CDN）/ 原生进程 + 启动脚本（不用 Docker）—— 选型依据见 `docs/PROJECT-STATE.md` `Q-13`；依赖白名单见 `.specify/memory/constitution.md` §1
- 项目宪法：`.specify/memory/constitution.md`（状态 `已生效`，2026-09-30 三方批准；红线对实现环节全程有约束力）

## 0. 会话恢复入口（先读这里）

- **当前阶段 / 未决问题 / 下一步动作 → `docs/PROJECT-STATE.md`**（唯一权威，本文件不复述）
- 特性规格总入口 → `specs/market-trade-flow/spec.md`
- **阻塞性**未决项未闭环时，不得进入它卡住的那一步；**跟踪性**未决项照常推进。

## 1. 文档路由表（动手前先读对应文件，不要凭记忆作答）

| 你要做什么 | 先读 | 权威内容 |
| --- | --- | --- |
| 改需求 / 加功能 | `specs/market-trade-flow/spec.md` | `REQ` 条目（唯一权威） |
| 查验收标准 | `specs/market-trade-flow/spec.md` §7 验收标准 | `AC` 条目（含追溯） |
| 改非功能目标 | `specs/market-trade-flow/spec.md` §4 非功能需求 | 偏离项与 §4.1 批准记录；默认基线见 `docs/standards/nfr-baseline.md` |
| 查「当初为什么这样定」 | `specs/market-trade-flow/discovery.md` | 访谈问答与决策留痕 |
| 查方案的外部事实依据 | `docs/调研报告-现实情况.md` | 一手调研留痕（**外部输入，只读，不改动**） |
| 技术选型 / 架构 | `docs/adr/` + `docs/standards/architecture.md` | 已接受的 `ADR`；决策树与默认值清单 |
| 人工裁定（父代理/负责人拍板） | `docs/decisions/` | 落盘版正式裁定，**取代对话答复**；命名 `YYYY-MM-DD-<标题>.md` |
| 不可协商的原则 | `.specify/memory/constitution.md` | 红线条款（§2 安全红线 / §5 禁止事项） |
| 复核项目级别 | `docs/standards/classification.md` | 定级三问、判定表、兜底规则 |
| 质量阈值 | `docs/standards/quality-gates.md` | 阈值只在那里写一次（状态 `已定义`，2026-09-30 批准；**本文件不复述数值**） |
| 非功能默认值 | `docs/standards/nfr-baseline.md` | 按级别的基线 |

**ADR 登记（全项目唯一登记处，不另设独立 ADR 索引文件）**

| ADR | 标题 | 状态 |
| --- | --- | --- |
| `docs/adr/0001-单体与嵌入式数据库.md` | 本期采用单体 all-in-one + 嵌入式数据库（决策树叶子 a）；**部署形态为单机原生进程，见 ADR-0003** | `已接受` |
| `docs/adr/0002-环境策略-单环境.md` | 环境策略 —— 本期采用单环境（偏离 P2 默认的三环境） | `已接受` |
| `docs/adr/0003-技术栈偏离参考默认值.md` | 技术栈偏离叶子 a 参考默认值（Flask + 标准库 `sqlite3` + 纯静态前端 + 原生进程，不用 Docker） | `已接受` |
| `docs/adr/0004-依赖范围界定-运行期与开发期.md` | 依赖范围界定：宪法 §1 白名单**约束运行期依赖**；开发期工具（`pytest` / `coverage`）单列于 `requirements-dev.txt`，**不得出现在 `requirements.txt`、不得被 `run.py` 或 `app/**` 引用** | `已接受` |

## 2. 不可协商（红线速记）

- **RL-1 规格先于代码** —— 任何功能先有 spec 条目，再有实现。
- **RL-2 规格错了先改规格** —— 先改 `spec.md`、同步下游，再改代码。
- **RL-3 阻塞性未决项不清不进下一步** —— 见 §0；跟踪性未决项照常推进。
- **RL-4 合并必须评审** —— 每个 PR 都要评审；评审 = 开全新 AI 会话的对立评审（单人开发前提）。
- **RL-5 密钥绝不入库** —— 任何环境、任何分支、任何历史提交。
- **RL-6 禁止一次性大批量生成不评审** —— 跳过任务拆解直接生成整库代码，产出物作废。
- **RL-7 身份证号与银行卡号任何位置都不出现** —— 接口请求体 / 响应体 / 持久化记录中一律不得出现，收款标识脱敏（`NFR-012`）。
- **RL-8 离线暂存交易数据在补传成功后必须清除本地副本**（`NFR-013`）；加密存储是已知演进项（`docs/PROJECT-STATE.md` `Q-7`）。
- **RL-9 绝不静默丢弃任何一笔交易** —— 暂存达阈值只告警、仍继续接受；写入失败必须明确报错且不得标记为成功（`NFR-014` / `REQ-016` / `REQ-030`）。

> 完整条款见 `.specify/memory/constitution.md` §2 与 §5（含"资金链路审计只增不改"），此处只做速记，不复述全文。

## 3. 常用命令

> **（以下命令在实现环节后可用）** —— 本阶段尚无 `run.py` / `start.bat` / `requirements.txt`，
> 命令本身会失败；此节只约定**位置与行为**，不表示产物已存在。

**环境前置**

- **前置门禁（硬要求）**：`docs/standards/quality-gates.md` 的阈值必须在**写第一行产品代码之前**填入，并把该文件「状态」改为 `已定义`；阈值只在那一处写，别处只引用。（**本项已于 `2026-09-30` 完成，状态 `已定义`**，满足后方开工）
- 运行时：**Python 3.11 及以上**（演示机为自有笔记本，可提前安装环境；不做免安装打包）。
- 依赖清单**分两份**：`requirements.txt` = **运行期**（只有 `Flask`，演示机只装这份）；`requirements-dev.txt` = **开发期**工具（`pytest` / `coverage`，**不进 `requirements.txt`、不得被 `run.py` 或 `app/**` 引用、不装演示机**）。依据 `docs/adr/0004-依赖范围界定-运行期与开发期.md`。（两份清单均由实现环节创建，本阶段不预设内容）
- 不使用 Docker、不依赖外网。

```text
# 安装依赖（实现环节后可用）
python -m pip install -r requirements.txt
# 启动（实现环节后可用）：自动建库 → 导入种子数据 → 启动 HTTP 服务 → 打印访问地址
python run.py
# 或双击 start.bat（Windows；实现环节后可用）
```

**演示数据目录放哪（`2026-09-30` 裁定，现场按此执行）**

- **双击 `start.bat`**：数据目录默认落在**用户数据目录**（`%LOCALAPPDATA%\MarketTradeDemo\data`，通常属 `C:` 这类快盘），启动时打印实际路径；**已显式设置 `MT_DATA_DIR` 时不覆盖**。`start.sh` 同规则 —— 两个脚本调用**同一个** `scripts/launch.py`，规则与全部中文提示都只在那**一处**实现。
- **`python run.py` 的默认值未变**（仍是仓库内 `data/`）：开发与测试照旧，不设置时行为与从前完全一致。
- **`start.bat` 必须保持纯 ASCII**（`CP-D` 现场验收发现）：`cmd.exe` 按**控制台代码页**解析 `.bat` 的**字节**，中文会被拆断成 `'…' 不是内部或外部命令` / `" was unexpected at this time.`；出现在 `if ( )` 块里时，还会把真正该看的提示（如"端口已被占用"）顶掉。故**所有中文一律由 `scripts/launch.py` 打印**，`tests/e2e/test_demo_launcher.py` 盯着这条（加一个中文字符即变红）。
- **为什么要这样**：实测接口响应时间受**磁盘 fsync 成本**支配 —— 「点按口径」下 `C:` 为 p50 43ms/p95 73ms，仓库所在 `D:` 为 p50 297~354ms/p95 683~770ms（256KB 写 + fsync 的 p50：`C:` 2ms vs `D:` 37ms，仓库内外一样慢）。依据与已知边界见 `docs/standards/quality-gates.md` §1.1。
- **换盘/换目录**：设 `MT_DATA_DIR` 即可（如 `set MT_DATA_DIR=E:\demo-data`）；若现场实测（含 `C:`）仍不达标，**按 `specs/market-trade-flow/spec.md` §4.1 记一次放宽并写明根因是磁盘** —— 先实测再决定。

**现场演示硬要求（启动形态约定，实现环节必须逐条满足）**

1. 启动时**打印局域网 IP 地址**，便于手机真机扫码。
2. 启动时**同时打印两个入口地址**：操作端（秤端 / 运营端）与顾客扫码页。
3. **必须支持「同一台机器开两个浏览器窗口分别扮演秤端与顾客端」完成全流程演示** —— 现场 WiFi 未必可用，真机扫码必须有此降级方案。
4. **前端严禁引用任何 CDN 或外部资源**，所有静态资源一律为仓库内本地文件（不依赖外网是硬约束）。
5. 启动脚本要**检测端口占用并给出明确提示**，并**打印数据文件路径**，便于重置演示数据。

## 4. 目录约定

```text
AGENTS.md                        ← 本文件：记忆锚①（常驻规则与路由表）
.specify/memory/constitution.md  ← 项目宪法
docs/
├── PROJECT-STATE.md             ← 记忆锚②（阶段 / 未决问题 / 下一步）
├── 调研报告-现实情况.md          ← 外部输入（只读）
├── adr/                         ← 架构决策记录（NNNN-决策标题.md）
└── standards/                   ← 定级 / 架构 / NFR 基线 / 质量阈值
specs/
└── market-trade-flow/           ← 本特性：spec.md + discovery.md（一特性一目录，不共享 spec）
```

> 实现环节产出的代码与产物（`run.py`、`start.bat`、`requirements.txt`、静态前端等）落位在实现环节确定，
> 本文件**不预先登记尚不存在的路径**。

## 5. 提交规范

```text
[T-xxx] 类型: 一句话描述 (refs: specs/market-trade-flow/spec.md, REQ-xxx)
```

类型：`feat` / `fix` / `refactor` / `test` / `docs` / `chore`

## 6. AI 协作约定

- 动手前先读 §1 路由表指向的文件，**不要凭记忆作答**。
- 发现规格与现实矛盾：按 RL-2 先改规格，再改代码。
- 不确定的事写进 `docs/PROJECT-STATE.md` 的未决问题，**不要臆造**；查不到就记未决项。
- 修改宪法条款必须走人工评审，AI 只能起草与提议（见 `.specify/memory/constitution.md` §6）。
- 不要把需求细节、验收标准或阈值数字追加到本文件 —— 它们属于 §1 路由表里的文件。
