# market-trade-flow 任务清单

- 特性目录: specs/market-trade-flow/
- 上游产物: [spec.md](./spec.md)（32 `REQ` / 23 `AC` / 14 `NFR`）、[plan.md](./plan.md)、[data-model.md](./data-model.md)、[contracts/rest-api.md](./contracts/rest-api.md)
- 生成日期: 2026-09-30 / 生成方式: AI 生成 + 人工评审（**负责人须在 CP-A/CP-B/CP-C/CP-D 四个检查点签署后才能继续**）

## 1. 任务生成规则（本清单的推导依据）

1. `plan.md` §4 目录结构 → 骨架类任务（T-002、T-003）与各模块实现任务的**产出文件白名单**；**未列入 `plan.md` §4 的文件不允许出现**，确需新增须先改 `plan.md` §4。
2. `data-model.md` 的 19 个实体与 25 条索引 → 建模与迁移任务（T-004）；4 个状态机（`transaction` / `payment` / `offline_queue` / `settlement`）→ 归入对应流转实现任务（T-016、T-017、T-019、T-020）。
3. `contracts/rest-api.md` 的 31 个端点 → **契约测试任务先于实现任务**。为控制任务数量，按**界面/模块边界合并**（与 34 号模板示例的做法一致，此处显式记录合并规则）：
   - 健康检查（§3.1）→ T-007（路由注册比对 + 响应体敏感字段扫描）/ T-025
   - 秤端会话与目录类（§3.2~3.5）→ T-008 / T-015
   - 秤端读端点（§3.7 交易列表、§3.8 交易详情、§3.12 商户看板）→ T-008 / T-015
   - 交易创建与改价（§3.6、§3.9）→ T-009 / T-016
   - 收款与支付回调（§3.10、§3.17）→ T-010 / T-017
   - 退货冲正（§3.11）→ T-011 / T-018
   - 离线暂存与补传（§3.13~3.15）→ T-012 / T-019
   - 顾客扫码页（§3.18、§3.19）→ T-014 / T-023
   - 运营端（§3.20~3.31）→ T-013 / T-020~T-022、T-024
   - Mock 端点（§3.16、§3.17）→ 并入 T-010 / T-017

   > **规则缺口修正（`2026-09-30`，父代理裁定）**：本规则原先漏了 **§3.1**、**§3.7**、**§3.8**、**§3.12** 四处归属 ——
   > 其中 §3.7/§3.8/§3.12 由 `T-008` 执行时发现并上报（`MT-1004` 只在 §3.7 声明，若无人承接，该错误码就没有任何测试任务），
   > §3.1 为本次补录时**新发现的同类缺口**。四处**实际都已由 `T-008` / `T-007` 覆盖**，现补进规则，
   > 使「契约 §2 的 31 个端点」与「合并规则」**一一对得上** —— 否则下一个人按规则推，会以为这几处没人管。
   > **自查方法**：把 §2 端点总表的 31 行逐行与本节各条对照，应**无遗漏、无重复挂载**。
4. `spec.md` 的 23 条 `AC` → 每条至少一个验证任务；覆盖关系见 §6 覆盖矩阵（**由该矩阵机械核验，不靠人眼**）。
5. 部署/上线核对 → 收尾任务（T-035）。
6. **机械检查必须自带灵敏度验证** → 独立任务 **T-036**（依据 `AGENTS.md`：「验证要能失败……不验证灵敏度的验证是摆设」）。
7. **门禁文件的两处替代（本项目无该文件，禁止引用不存在的路径）**：
   - 34 号模板引用的**测试质量门禁文件**（SOP 内部文件，不在本仓库内）→ 本项目的对应权威是仓库内 **`docs/standards/quality-gates.md`**（**状态 `已定义`**，2026-09-30 批准）；**阈值未填入前不得写第一行产品代码**（该动作即 T-001，**已完成**）。
   - 同模板引用的**上线检查清单文件**（同为 SOP 内部文件）→ 本项目的收尾核对以 `docs/standards/quality-gates.md` + `AGENTS.md` §3 环境前置与 5 条现场演示硬要求为准（T-035）。
8. **人工环节**：T-001（填阈值）与 §5 的四个检查点属**人工/负责人环节**，AI 不得代签、不得跳过。

## 2. 任务排序原则

1. **测试先行**：契约测试任务（T-007~T-014）全部排在实现任务之前；每个契约测试任务写完须**先跑一次确认它失败**，再交给对应实现任务（「验证要能失败」）。
2. **先契约后实现**：`contracts/rest-api.md` 已冻结（2026-09-30），实现任务一律以它为唯一接口依据。
3. **先核心后边角**：主链路（选品 → 计价 → 收款 → 落库 → 凭证）优先，看板、指标导出、页面美化靠后。
4. **依赖只指向更小的编号**，禁止成环；同一依赖层内、**产出文件集合不相交且无运行时先后关系**的任务才标 `[P]`。

## 3. 任务表

| 编号 | 标题 | 依赖 | 并行 | 关联 REQ/AC | 验收方式 | 预估产出文件 |
| --- | --- | --- | --- | --- | --- | --- |
| T-001 | **开工前置门禁（人工）**：把质量阈值填入 `docs/standards/quality-gates.md` 并把「状态」改为 `已定义` | — | | —(门禁) | 该文件状态为 `已定义` 且**无 `待定` 残留**；负责人签署（CP-A 第①项） | docs/standards/quality-gates.md |
| T-002 | 项目骨架与启动入口：Flask 应用工厂、配置、端口占用检测、打印局域网 IP / 两个入口地址 / 数据文件路径 | T-001 | | REQ-025；AC-013 | 实跑 `python run.py`，控制台**打印出局域网 IP、两个入口地址与数据文件路径**；端口被占用时给出明确提示 | run.py、app/__init__.py、app/config.py、requirements.txt |
| T-003 | 启动脚本与入口导航页：`start.bat`（CRLF）、`start.sh`（LF）、静态入口导航 | T-002 | [P] | —(基础)；AC-013 | 双击 `start.bat` 能启动（并用 `git check-attr eol -- start.bat` 证明为 `crlf`）；入口导航页可打开 | start.bat、start.sh、app/static/index.html |
| T-004 | 数据模型与迁移：19 张表 DDL、25 条索引、`audit_log` 只增不改触发器、`schema_migration` | T-002 | | REQ-010、REQ-013、REQ-015、REQ-031；NFR-009 | 从零启动后表/索引数量与 `data-model.md` §1/§6 一致；**对 `audit_log` 手动执行一次 `UPDATE` 与 `DELETE`，二者都必须被拒绝** | app/migrations/0001_init.sql、app/db.py |
| T-005 | 种子数据与导入：商户/摊位档案、品类字典与别名映射、商品与价目表（本地文件，不依赖外网） | T-004 | | REQ-001、REQ-002、REQ-003、REQ-025；AC-013/AC-014/AC-015 | 断网环境下导入成功；抽查询问 `stall`/`product`/`price_item` 行数；重复启动**不产生重复数据**。**规模口径见 `data-model.md` §5 修正注记**（演示规模：10 摊位 / 40～60 标准品类 / 每摊位 20～30 商品 / 别名 1～3 个每品类）。**别名两类语义见 `data-model.md` §2.5 与 `app/seed.py` docstring**。**产物可复算**：删掉 `app/seed_data/seed.json` 后重跑 `scripts/gen_seed.py` 必须**逐字节还原**，并用 `--check` 复验 —— **生成规则不在仓库里，产物就不可核验**（父代理 `2026-09-30` 裁定要求） | app/seed.py、app/seed_data/seed.json、scripts/gen_seed.py |
| T-006 | 重置演示数据脚本（删除数据文件并重跑种子） | T-005 | | NFR-003；NFR-004 | 执行一次后系统回到初始状态且可再次交易（人工核对，CP-A 第④项） | scripts/reset_demo.py |
| T-007 | 契约测试基础设施：开发期依赖清单与**运行期隔离检查**（`ast` 扫描 `run.py` 与 `app/**` 的 import，**带灵敏度负例**）、**Flask 路由表 ↔ 契约 §2 端点总表双向一致检查**、响应体与库文件「零命中身份证/银行卡」扫描、统一错误响应格式断言 | T-006 | | REQ-024；NFR-009、NFR-010；AC-012 | 两个检查先跑成**失败**（实现尚未存在）；格式断言对已知坏样例报错；**隔离检查在故意 `import pytest` 时必须变红** | requirements-dev.txt、pytest.ini、tests/contract/conftest.py、tests/contract/sensitive_scan.py、tests/contract/test_deps_isolation.py、tests/contract/test_contract_surface.py、tests/contract/test_sensitive_scan.py |
| T-008 | 契约测试：秤端会话、商品、价目表端点（含 `MT-1004`/`MT-1005`/`MT-1009` 用例） | T-007 | [P] | REQ-003、REQ-004、REQ-008、REQ-032；AC-006/AC-014/AC-015/AC-021 | 先失败；由 T-015 实现后转为全绿 | tests/contract/test_merchant_catalog.py |
| T-009 | 契约测试：交易创建与改价（含 `MT-1002` 重量越界、`MT-1006` 价目缺失、`MT-1011` 改价确认、`MT-1012` 幂等键） | T-007 | [P] | REQ-005、REQ-006、REQ-007、REQ-027；AC-001/AC-007/AC-016/AC-017 | 先失败；由 T-016 实现后转为全绿 | tests/contract/test_transaction_price.py |
| T-010 | 契约测试：收款与支付回调（含 `MT-1001` 非法流转、重复回调幂等命中返回 200） | T-007 | [P] | REQ-009、REQ-010、REQ-011、REQ-026、REQ-029；AC-004/AC-009/AC-010 | 先失败；由 T-017 实现后转为全绿 | tests/contract/test_payment.py、tests/contract/test_payment_callback.py |
| T-011 | 契约测试：退货冲正（含 `MT-1003` 超原单、重复提交 `replayed`） | T-007 | [P] | REQ-013、REQ-028；AC-002/AC-017 | 先失败；由 T-018 实现后转为全绿 | tests/contract/test_refund.py |
| T-012 | 契约测试：离线暂存与补传（含 `MT-1007` 暂存失败、幂等键重复丢弃不阻断、`purged` 清除副本） | T-007 | [P] | REQ-014、REQ-015、REQ-016、REQ-030；NFR-013、NFR-014；AC-003/AC-018/AC-019 | 先失败；由 T-019 实现后转为全绿 | tests/contract/test_offline_stage.py、tests/contract/test_offline_sync.py |
| T-013 | 契约测试：运营端字典/别名/佣金口径/日聚合/结算/对账/指标/留痕 | T-007 | [P] | REQ-002、REQ-017、REQ-018、REQ-019、REQ-020、REQ-022；AC-005/AC-022/AC-023 | 先失败；由 T-020~T-022 实现后转为全绿 | tests/contract/test_admin.py、tests/contract/test_admin_ops.py、tests/contract/test_admin_report.py |
| T-014 | 契约测试：顾客扫码页（字段白名单、无来源字段即失败） | T-007 | [P] | REQ-008、REQ-023；AC-011 | 先失败；由 T-023 实现后转为全绿 | tests/contract/test_customer.py |
| T-015 | 实现：秤端会话绑定与目录类端点（含摊位数据边界守卫） | T-008 | | REQ-002、REQ-003、REQ-004、REQ-032；NFR-007；AC-006/AC-014/AC-015/AC-021 | T-008 由红转绿；提交信息含任务编号与 `REQ` | app/api/merchant.py、app/domain/catalog.py |
| T-016 | 实现：计价与改价/抹零（含改价留痕、标价一致率标记、交易状态机 `priced` 分支） | T-009、T-015 | | REQ-005、REQ-006、REQ-007、REQ-008、REQ-027；AC-001/AC-007/AC-016/AC-017/AC-020 | T-009 由红转绿；`AC-016` 累加用例与 `AC-017` 越界用例通过 | app/domain/pricing.py、app/domain/transactions.py、app/api/merchant.py |
| T-017 | 实现：收款、支付回调与 Mock（现金入同一支付流水表；Mock 秤与支付可注入成功/失败/超时） | T-010、T-016 | | REQ-009、REQ-010、REQ-011、REQ-026、REQ-029；AC-004/AC-009/AC-010 | T-010 由红转绿；**重复回调返回 200 且交易与佣金笔数不变**（`AC-010`） | app/domain/payment.py、app/api/mock.py |
| T-018 | 实现：退货冲正（只冲减一次，冲正后触发日聚合重算） | T-011、T-016 | | REQ-013、REQ-028；AC-002/AC-017 | T-011 由红转绿；重复退货后金额与佣金**不再变化** | app/domain/refund.py |
| T-019 | 实现：离线暂存与补传（幂等去重、达阈值只告警仍接受、失败明确报错、成功后清除本地副本） | T-012、T-016 | | REQ-014、REQ-015、REQ-016、REQ-030；NFR-013、NFR-014；AC-003/AC-018/AC-019 | T-012 由红转绿；补传后库中 `payload_json` 为空且 `purged_at` 非空 | app/domain/offline.py |
| T-020 | 实现：佣金口径匹配与按实收金额计算、日聚合与重算（多 `revision`）、结算单、对账等式 | T-013、T-017、T-018 | | REQ-017、REQ-018、REQ-019、REQ-020；AC-002/AC-008/AC-022/AC-023 | T-013 由红转绿；涉钱对账断言「订单总额 = 支付流水 = 分账明细」通过 | app/domain/commission.py、app/domain/settlement.py |
| T-021 | 实现：运营端字典与别名、佣金口径配置端点（配置变更写留痕） | T-013、T-015 | [P] | REQ-002、REQ-017；AC-014/AC-022 | T-013 对应用例由红转绿；配置变更后能在留痕里查到 | app/api/admin.py、app/domain/catalog.py |
| T-022 | 实现：看板、结算查询与三个使用率指标导出（**随指标返回分子与分母**） | T-013、T-020 | | REQ-021、REQ-022；AC-001/AC-005/AC-023 | T-013 对应用例由红转绿；导出响应含三个指标**及其分子/分母**，与库中直接统计数值一致 | app/api/admin.py、app/domain/metrics.py |
| T-023 | 实现：顾客扫码页端点（字段白名单，无顾客身份信息） | T-014、T-017 | [P] | REQ-008、REQ-023；AC-011 | T-014 由红转绿；响应字段与契约 §3.18/§3.19 白名单**逐一相等** | app/api/customer.py |
| T-024 | 实现：审计留痕写入与只读查询（改价/退货/补传/幂等命中；拒绝任何修改路径） | T-007、T-016 | | NFR-009；REQ-007、REQ-013、REQ-015 | 留痕表能查到四类事件；**对留痕执行 `UPDATE`/`DELETE` 被数据库拒绝** | app/domain/audit.py |
| T-025 | 实现：健康检查与结构化日志落盘 | T-002 | [P] | NFR-010 | `/healthz` 返回 200；`data/` 下生成日志文件且含结构化字段 | app/api/health.py |
| T-026 | 前端：秤端页面（选品 → 称重计价 → 收款 → 凭证；改价/退货；**离线状态可视标识与显式切换**） | T-017、T-019、T-024 | | REQ-004、REQ-012、REQ-014；AC-006/AC-018 | 人工走查：全程无文本输入、交互 ≤3 次；**离线标识可在界面显式切换**（不拔网线也能复现） | app/static/scale/index.html、app/static/js/scale.js、app/static/js/offline.js、app/static/css/app.css |
| T-027 | 前端：运营端页面（字典与别名、价目表、佣金口径、看板、结算、指标导出） | T-021、T-022 | [P] | REQ-002、REQ-003、REQ-017、REQ-021、REQ-022 | 人工走查各页面数据与接口返回一致；指标页显示分子/分母 | app/static/admin/index.html、app/static/js/admin.js |
| T-028 | 前端：顾客扫码页（只渲染接口返回的白名单字段） | T-023 | [P] | REQ-023；AC-011 | 页面字段与接口白名单一致；**无来源字段不得出现** | app/static/customer/index.html、app/static/js/customer.js |
| T-029 | 「无 CDN / 无外部资源」静态自检：扫描全部静态文件不得含外链域名或构建产物引用 | T-026 | [P] | REQ-025；AC-013 | 扫描通过；**故意插入一个 CDN `<script src>` 后必须变红**（灵敏度要求） | tests/contract/test_no_external_assets.py |
| T-030 | 端到端验证（一）：AC-001、AC-002、AC-003、AC-004 三大成功场景 + 现金等价入账 | T-026、T-027、T-028 | | REQ-005、REQ-009、REQ-010、REQ-012、REQ-013、REQ-014、REQ-015、REQ-018、REQ-020、REQ-021；AC-001/AC-002/AC-003/AC-004 | 四个场景全部通过并**留实测输出**（含对账等式三处数值） | tests/e2e/test_scenarios.py |
| T-031 | 端到端验证（二）：异常与边界（重量越界、退货超原单、回调重复、阈值继续接受、暂存失败、越权访问） | T-030 | | REQ-016、REQ-027、REQ-028、REQ-029、REQ-030、REQ-032；AC-009/AC-010/AC-017/AC-018/AC-019/AC-021 | 六个异常场景全部通过；**每个都断言"没有产生错误数据"**（不只是报错） | tests/conftest.py、tests/e2e/test_edge_cases.py、tests/e2e/test_edge_payment.py、tests/e2e/test_edge_offline.py |
| T-032 | 端到端验证（三）：其余 AC 全覆盖（含三个使用率指标的分子分母核对、标价一致率、别名归集、复制昨天价、扫码页字段来源） | T-030 | | REQ-002、REQ-003、REQ-006、REQ-008、REQ-022、REQ-023；AC-005/AC-006/AC-007/AC-008/AC-011/AC-014/AC-015/AC-016/AC-022/AC-023 | §6 矩阵中列出的 AC 全部通过（人工逐条对照） | tests/e2e/test_coverage_rest.py、tests/e2e/test_coverage_catalog.py、tests/e2e/test_coverage_admin.py |
| T-033 | 演示硬要求核验与断网彩排：局域网 IP、两个入口地址、**同机双浏览器窗口**（秤端 + 顾客端）全流程、端口占用检测、数据文件路径打印 | T-030 | | REQ-025；AC-013 | `AGENTS.md` §3 的 5 条硬要求**逐条现场验收**；**断网状态下跑完一遍全流程** | tests/e2e/test_demo_requirements.py |
| T-034 | 并发与响应时间压测：多摊位并发写入不覆盖、交易号唯一、接口响应时间采样 | T-016、T-022 | | REQ-031；NFR-001；AC-020 | 并发写入后**条数与交易号唯一性均可核验**；响应时间采样结果与 `docs/standards/quality-gates.md` 的阈值比对 | tests/perf/test_concurrency.py（并发**正确性**：`AC-020` 逐笔核对 + 离线并发零丢弃）、tests/perf/test_latency.py（响应时间门禁 + 突发时延的磁盘归因**对照实验**） |
| T-035 | 收尾核对：质量门禁逐项核对、**干净环境实证（只安装 `requirements.txt` → `python run.py` 必须能启动）**、`scripts/reset_demo.py` 从零重建演练、依赖清单锁定、把核对结果追加进项目状态 | T-029、T-031、T-032、T-033、T-034 | | —(收尾)；NFR-003、NFR-004 | CP-D 四项逐条核对；**干净环境实证须留实测输出**（依据 `docs/adr/0004-依赖范围界定-运行期与开发期.md` §3 第 4 条）；核对记录写入 `docs/PROJECT-STATE.md` | docs/PROJECT-STATE.md（追加核对记录） |
| T-036 | **检查灵敏度验证（自动化负例，收口入口）**：① 路由表 ↔ 契约 §2 比对——**故意注册一个契约里没有的端点，比对必须变红**；② 敏感字段扫描——**故意加一个 `id_card` 字段，扫描必须命中**；③ 运行期隔离检查——**故意在 `run.py` 里 `import pytest`，检查必须变红**；④ 移除故意破坏物后必须恢复绿。四条缺一即判定对应检查不成立 | T-007、T-015 | | REQ-024；NFR-009；AC-012 | 四条负例各自的实际输出留痕（红 → 恢复绿）；作为 CP-C 第④项 | tests/contract/test_check_sensitivity.py（**七个家族的收口入口**：`T-007` 四条 + `T-029` 六形态 + `Q-19` 指标差分 + `T-034` 的 `MT-1014` 兜底；一键复跑 `python -m pytest tests/contract/test_check_sensitivity.py -q -s`） |
| T-SIM-00 | **业务日时钟接缝**（`2026-10-02` 新增；**唯一触碰已验收主系统的改动**，按 `RL-2` 已先改 `spec.md` 增设 `REQ-033`/`AC-024` 并在 `discovery.md` 记 `D-15`）：新增 `app/clock.py` 作为**时钟来源的唯一落点**；把**实测 7 处**业务日/时间戳取样全部改为经它（`pricing.py:152`、`metrics.py:82/210/284`、`offline.py:152`、`payment.py:65`、`seed.py:346`）；`db.now_iso()` 委托给它（覆盖全部时间戳调用点）；`MT_CLOCK_FILE` 未设置时取本机墙钟。**实测得出两处计划外发现并如实登记**：① 第 8 处 Python 取样点 `scripts/reset_demo.py:126`（检查脚本读墙钟 → 若种子落在注入业务日会**误报"缺价"**），已一并修正；② 迁移里 **23 处 `DEFAULT (datetime('now','localtime'))`** 由 **SQLite 求值**、`MT_CLOCK_FILE` 管不到，其中**应用层未显式赋值的 12 列**（`transaction.created_at`、`transaction_item.created_at`、`payment.created_at`、`stall_session.created_at` 及 6 张档案表的列）**仍取墙钟** —— **本批不修**（改法要么重建表、要么让每个 INSERT 显式赋值，且既有库不会拾取新 DDL，属 `Q-17` 同类风险），已由 `test_clock_guard.py` **枚举并钉死**，并在 `spec.md` §5 声明为已知边界 | T-036 | | REQ-033；AC-024 | ① **未注入时业务日与时间戳逐字段不变**（负例：不注入 → 与改动前一致；注入 → 才走注入值）；② 注入营业日 `D` → 交易、日聚合、结算单、三个使用率指标、审计留痕的业务日**全部**为 `D`；③ **墙钟直读的 ast 守卫带 4 个合成负例**，并给出"**漏改一处会怎样**"的实测证据（临时把一处改回 `date.today()` → 守卫必红）；④ 全量 `pytest -q` 与改动前**同结果**（除已知的 `D:` 卷磁盘那条与 2 条既有 mojibake 失败）；⑤ 路由表双向比对仍 `contract=31/missing=0/extra=0`；⑥ `MT_CLOCK_FILE` 指向不存在/非法文件 → **明确报错**，不静默退回墙钟；⑦ **SQL 默认值缺口被枚举钉住**（12 列），修掉任意一列都会用例变红 | app/clock.py、app/db.py、app/seed.py、app/domain/pricing.py、app/domain/metrics.py、app/domain/offline.py、app/domain/payment.py、tests/unit/clock_support.py、tests/unit/test_clock_injection.py、tests/unit/test_clock_guard.py |
| T-SIM-01 | **仿真骨架**：CLI、仿真时钟、**每 agent 独立确定性随机流**、只增不改事件日志、运行目录与"不改演示库"闸门；**隔离闸门两条**（`sim/**` 不得 `import app`；`run.py`/`app/**` 不得 `import sim`）**均带灵敏度负例**；**参数出处约束**（`calibration/params.json` 的 `provenance` 必填）**带灵敏度负例** | T-SIM-00 | | REQ-033；AC-024 | ① **同 seed 两次运行的 `metrics.json` + `events.jsonl` SHA-256 逐字节相同**；不同 seed 必须不同；② 隔离闸门故意 `import app` → **必红**，还原复绿；③ `provenance.kind=sourced` 缺 `ref`、或 `kind=assumed` 缺 `calibration` → **必红**，还原复绿；④ 运行前后演示库哈希不变；⑤ `sim/**` 只 import 标准库（故意 `import numpy` 必红） | sim/__init__.py、sim/__main__.py、sim/cli.py、sim/core/clock.py、sim/core/streams.py、sim/core/events.py、sim/core/registry.py、sim/calibration/params.json、tests/sim/test_sim_no_app_import.py、tests/sim/test_sim_deps_isolation.py、tests/sim/test_param_provenance.py、tests/sim/test_seed_reproducibility.py |
| T-SIM-02 | **仿真环境层与时序**：市场/摊位/品类/商品/价目表、客流到达过程（营业日 → 时段块 → 周 → 月）、设备机队与故障-报修-维修队列 | T-SIM-01 | | REQ-033；AC-024 | ① 零决策基线跑 **90 营业日**无异常；② **守恒断言**（设备数守恒；成交笔数 = 走秤 + 私下）；③ 时序与系统 `business_date` **同轴**（注入 `D` → 该日全部落在 `D`）；④ 到达过程的强度与时段块配置一致（可由明细复算） | sim/env/market.py、sim/env/demand.py、sim/env/devices.py、tests/sim/test_env_timing.py |

> **`2026-09-30` `T-031`/`T-032` 走查夹具收敛 + 按语义拆分**：新增 `tests/conftest.py`（**只放 pytest 夹具**）与
> `tests/e2e_support.py`（**助手**：服务生命周期 / HTTP 与库助手 / 按 `data-model.md` §0·§5.1 口径的独立复算公式；
> 按语义拆是因为 `conftest.py` 一度到 424 行、超 `quality-gates.md` §1.2 的 400 行阈值），
> 由 `T-030`~`T-034` 共用 —— 同一条规则（服务怎么起、日志往哪写、什么算就绪）写五遍就是下次漂移的种子；
> `T-031`/`T-032` 的用例按**端点语义**拆成多个文件（输入与权限 / 支付回调 / 离线链路；交易行为 / 目录与顾客页 / 运营端读模型），
> 单文件均 ≤400 行（`Q-16` 的拆法先例：按语义拆，不做机械对半切）。

> **`T-007` 产出文件的补充说明（`2026-09-30`）**：新增仓库根 `pytest.ini`（已在 `plan.md` §4 登记）。
> 理由：pytest 默认把 basetemp 放在系统 Temp 的 `pytest-of-<user>/` 编号目录并在其中维护 `pytest-current` 目录符号链接，
> 会话收尾清理该链接时在 Windows 上抛 `PermissionError`，**导致全绿用例集也以退出码 1 结束**（实测复现 → 根因 → 修复记录见 `docs/PROJECT-STATE.md` 变更记录）。
> `pytest.ini` 只做一件事：把 basetemp 固定到仓库内 `.pytest-tmp/`。

> **`2026-09-30` `T-034`/`T-035`/`T-036` 的产出文件与契约变更（计划外文件已说明理由并同步 `plan.md` §4）**：
> - **契约 §4 新增 `MT-1014`（500 内部错误）**，按本仓契约 §5「破坏性/兼容性变更须同步四处、同一次提交内完成」执行：
>   ① `contracts/rest-api.md` §4 表（含"为什么必须有"的说明）；② `app/__init__.py` 的 `ERROR_STATUS`（**唯一映射点**，现 14 条）；
>   ③ `tests/contract/test_contract_surface.py` 的连续性断言（`1001..1014`）与条数断言（14）；
>   ④ 本条备注。**动因不是"多写一个码"**：`T-034` 实测未预期异常的响应体是 **HTML**、不符合 §1.2、
>   也没有可追溯标识 —— 调用方看不出是契约错误，也不知道"这笔交易到底记没记"。
>   新增的兜底处理器把这类异常变成**确定性反馈**（统一格式 + `request_id` + "结果不确定，别当成功"），
>   原始异常与栈**只进服务端日志**；`werkzeug` 语义族（405 等）原样放行，不被改成 500。
> - **新增测试支撑模块（唯一名的导入目标，避免"同名 `conftest` 撞车"）**：`tests/gates.py`（门禁阈值读取的唯一机器入口）、
>   `tests/contract/contract_support.py`（原 `tests/contract/conftest.py` 的助手，`conftest.py` 退化为三行垫片）。
>   动因：`tests/conftest.py` 与 `tests/contract/conftest.py` **同名**且两边目录都进 `sys.path`，
>   用例写 `from conftest import ...` 时解析到哪个取决于收集顺序 —— `python -m pytest -q` 实测 **10 个文件 ImportError（收不起用例）**。
> - **`tests/perf/` 的两个文件按语义分工**：`test_concurrency.py` = 并发**正确性**（`AC-020` 逐笔核对 / 离线并发零丢弃）；
>   `test_latency.py` = **响应时间**（门禁判定 + 100 样本；另有"突发时延随盘"的对照实验用例，用于环境归因）。
> - **`T-036` 收口入口按"结构面 vs 内容面"一分为二**（原单文件 409 行、超 400 行门禁）：`test_check_sensitivity.py` 持登记表 `FAMILIES`
>   与守卫（**跨模块**核对每个家族的负例真实存在）+ 结构面四家族；`test_check_sensitivity_content.py` 持内容面三家族
>   （敏感字段扫描 / 零外部资源 / 指标差分）。拆的理由不只行数：两类**失效方式不同** —— 结构面被"比对函数被顺手放宽"废掉，
>   内容面被"扫描器退化成永远返回空"废掉。**守卫自身也验过灵敏度**：把内容面某个家族的负例改名 → 守卫必红；还原 → 复绿。
> - **`app/domain/numbering.py`（新增，`T-034` 按语义拆出）**：号段（交易号/流水号）分配与**并发安全**的交易/流水落库。
>   拆的理由：① 计价是纯计算、号段分配是并发写，两件事；② `pricing` 与 `payment` 共用同一套重试规则，放在被依赖方
>   才不会让 `payment` 去 import `pricing` 的私有名；③ `pricing.py` 因这次修复涨到 429 行、超 400 行门禁（**拆，不放宽阈值**）。
>   拆分后实测：`pricing.py` 313 / `payment.py` 219 / `numbering.py` 187 行，`tests/contract`+`tests/perf` 仍 **242 passed**（除已知的环境项）。


> **`2026-09-30` 按 `Q-16` 拆分产出文件（纯移动，不改用例语义）**：单文件 400 行阈值裁定「拆、不放宽」后，下列文件按**端点语义**一分为二，`预估产出文件` 列已同步；`pytest tests/contract -q` 拆分前后均为 **217 passed**（收集数量与红绿分布不变）：
> `tests/contract/conftest.py` → 另出 `tests/contract/sensitive_scan.py`（夹具 vs 敏感扫描原语）；
> `tests/contract/test_payment.py` → 另出 `tests/contract/test_payment_callback.py`（§3.10 收款 vs §3.17 回调）；
> `tests/contract/test_offline.py` → `test_offline_stage.py` + `test_offline_sync.py`（§3.13/§3.14 暂存 vs §3.15 补传）；
> `tests/contract/test_admin.py` → 另出 `tests/contract/test_admin_ops.py`（§3.20~§3.24 配置类 vs §3.25~§3.31 报表/结算类）；**该文件后又按语义再拆一次**：`Q-19` 的灵敏度负例（§3.30）落地后它到 448 行再次超阈值，故再分出 `tests/contract/test_admin_report.py`（§3.25 看板 / §3.30 指标 + `Q-19` 负例 / §3.31 留痕），`test_admin_ops.py` 只留 §3.26~§3.29（聚合/结算/对账）；
> `app/domain/pricing.py` → 另出 `app/domain/transactions.py`（§3.6/§3.9 计价写路径 vs §3.7/§3.8 交易读端点）。

## 4. 完成定义（DoD，适用于每个任务）

1. **测试先行可证**：该任务涉及的契约测试或单元测试**先写出并确认其失败**，再实现到通过；提交里能看出这一步（首次失败输出或说明）。
   **检查类测试另需灵敏度证据**：任何新增的"检查/扫描/比对"必须附**故意破坏后确实变红**的负例（自动化负例见 T-036），
   做不到"故意破坏就变红"的检查视为未完成 —— 它只是走过场。
2. 该任务新增/修改的代码通过对应契约测试与单元测试；静态检查（lint/类型检查，具体项以 `docs/standards/quality-gates.md` 填定的为准）通过。
3. **质量阈值引用门禁文件**：覆盖率、复杂度、单文件行数等一律以 `docs/standards/quality-gates.md` 为准（**本文件不复述任何数值**）；该文件为 `待填入` 时**不得开工**（T-001）。
4. 提交信息包含任务编号与关联 REQ 编号，如 `T-016 REQ-005`。
5. 产出文件与 §3「预估产出文件」一致；**出现计划外文件必须在任务备注说明理由，并同步更新 `plan.md` §4**（否则违反「未列出的文件不允许出现」）。
6. **人工复核确认环节（P2 必填）**：
   - 每个阶段边界检查点（§5）由负责人确认后才进入下一批，**AI 不得自行跳过**；
   - **涉钱任务的产物须人工抽查**：任取一笔交易，核对「订单总额 = 支付流水 = 分账明细」三处数据一致（`AC-003`）；
   - `AC-012`（敏感字段零命中）与 `AC-013`（一条命令启动）为**红线项，必须留下实测输出**，不接受口头说明。

## 5. 阶段边界检查点

| 检查点 | 位置 | 评审内容 | 通过后 |
| --- | --- | --- | --- |
| CP-A | T-006 完成后 | ① `docs/standards/quality-gates.md` 已填阈值且状态为 `已定义`（**开工前置门禁**）；② 骨架产出文件与 `plan.md` §4 一致；③ 迁移脚本是否覆盖 `data-model.md` 的 19 实体与 25 索引、`audit_log` 触发器是否真的会拒绝 `UPDATE`/`DELETE`（**须现场试一次**）；④ 种子导入后按摊位数/商品数抽查 | 进入契约测试批次（T-007~T-014） |
| CP-B | T-014 完成后 | ① 契约测试全部就位；② **逐条确认它们当前是失败的**（未实现即失败，防止"测试写了个寂寞"）；③ 路由表 ↔ 端点总表一致性检查已能双向失败；④ 敏感字段扫描已能对已知坏样例报警 | 进入后端实现批次（T-015~T-025） |
| CP-C | T-025 完成后 | ① 人工走查 `AC-001`/`AC-002`/`AC-003`/`AC-004` 的行为；② 错误码行为与契约 §4 一致（重点：重复回调与重复补传**返回 200 且不重复记账**）；③ 交付物的错误提示是"确定性反馈"而非静默失败；④ **T-036 已通过（四条负例）**：故意加一个契约外端点 → 比对变红；故意加 `id_card` 字段 → 扫描命中；故意在 `run.py` `import pytest` → 隔离检查变红；移除后均恢复绿（**须留实测输出**） | 进入前端与端到端批次（T-026~T-034） |
| CP-D | T-035 完成后、交付前 | ① `AGENTS.md` §3 的 **5 条现场演示硬要求**逐条现场验收（含**断网**跑一遍、**同机双窗口**跑一遍）；② 质量门禁逐项核对；③ 未决问题（`Q-2`/`Q-3`/`Q-6`/`Q-7`/`Q-8`/`Q-9`/`Q-10`/`Q-15`）状态复核，确认没有"带着已知假指标交付" | 交付 / 演示 |

## 6. AC 覆盖矩阵（机械核验用：24 条 `AC` 每条至少一个验证任务）

| AC | 契约测试 | 实现 | 验证（端到端/压测/演示核验） |
| --- | --- | --- | --- |
| `AC-001` | T-009 | T-016、T-022 | T-030 |
| `AC-002` | T-011 | T-018、T-020 | T-030 |
| `AC-003` | T-012 | T-019 | T-030 |
| `AC-004` | T-010 | T-017 | T-030 |
| `AC-005` | T-013 | T-022 | T-032 |
| `AC-006` | T-008 | T-015、T-026 | T-032 |
| `AC-007` | T-009 | T-016 | T-032 |
| `AC-008` | T-008 | T-016、T-020 | T-032 |
| `AC-009` | T-010 | T-017 | T-031 |
| `AC-010` | T-010 | T-017 | T-031 |
| `AC-011` | T-014 | T-023、T-028 | T-032 |
| `AC-012` | T-007 | T-024 | T-007（静态+库文件扫描）、**T-036（灵敏度：故意加 `id_card` 必须命中）**、T-035 |
| `AC-013` | T-007 | T-002、T-003、T-005 | T-033 |
| `AC-014` | T-008 | T-015、T-021 | T-032 |
| `AC-015` | T-008 | T-015、T-027 | T-032 |
| `AC-016` | T-009 | T-016 | T-032 |
| `AC-017` | T-009、T-011 | T-016、T-018 | T-031 |
| `AC-018` | T-012 | T-019、T-026 | T-031 |
| `AC-019` | T-012 | T-019 | T-031 |
| `AC-020` | T-009 | T-016 | T-034 |
| `AC-021` | T-008 | T-015 | T-031 |
| `AC-022` | T-013 | T-020、T-021 | T-032 |
| `AC-023` | T-013 | T-020、T-022 | T-032 |
| `AC-024` | —（时钟接缝属实现层能力，无契约端点） | T-SIM-00 | T-SIM-00（默认逐字段不变 + 注入生效 + 7 处取样点逐个覆盖）、T-SIM-01、T-SIM-02 |

> 覆盖矩阵中的任务编号必须都存在于 §3；`AC` 与任务编号的对应关系是**核验依据**，不是说明性文字 —— 改动任务表须同步本矩阵。
