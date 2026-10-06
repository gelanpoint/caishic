# market-trade-flow 任务清单

- 特性目录: specs/market-trade-flow/
- 上游产物: [spec.md](./spec.md)（43 `REQ` / 33 `AC` / 15 `NFR`；其中形态 2 为 `REQ-034`~`REQ-043` / `AC-025`~`AC-033` / `NFR-015`）、[plan.md](./plan.md)、[data-model.md](./data-model.md)、[contracts/rest-api.md](./contracts/rest-api.md)、[contracts/scale-midplatform.md](./contracts/scale-midplatform.md)、[spec-history.md](./spec-history.md)（过程记录）
- 生成日期: 2026-09-30（形态 2 任务表 `§3b` 于 2026-10-06 追加）/ 生成方式: AI 生成 + 人工评审（**负责人须在 CP-A/CP-B/CP-C/CP-D 四个检查点签署后才能继续**；**形态 2 另设 `T-SCALE-00` 人工门禁**）

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
| T-002 | 项目骨架与启动入口：Flask 应用工厂、配置、端口占用检测、打印局域网 IP / 两个入口地址 / 数据文件路径、**输出编码自定（`app/console.py`）** | T-001 | | REQ-025；AC-013 | 实跑 `python run.py`，控制台**打印出局域网 IP、两个入口地址与数据文件路径**；端口被占用时给出明确提示。**`CP-D` 后补**：输出编码**不得依赖** `PYTHONUTF8`/`PYTHONIOENCODING`/locale —— 重定向时强制 UTF-8、真控制台沿用控制台编码（清掉这两个变量后用例仍须绿） | run.py、app/__init__.py、app/config.py、app/console.py、requirements.txt |
| T-003 | 启动脚本与入口导航页：`start.bat`（**纯 ASCII + CRLF**）、`start.sh`（LF）、静态入口导航、`scripts/launch.py`（两者的公共实现：演示数据目录默认落点 + 全部中文提示） | T-002 | [P] | —(基础)；AC-013 | 双击 `start.bat` 能启动（并用 `git check-attr eol -- start.bat` 证明为 `crlf`，另实测**非 ASCII 字节数 = 0**）；入口导航页可打开；**端口被占用时看到的是清晰中文提示，不是 cmd 解析错误**（`CP-D` 现场验收发现：`.bat` 内的非 ASCII 会被 cmd 按控制台代码页解析坏，故中文一律由 `scripts/launch.py` 打印） | start.bat、start.sh、scripts/launch.py、app/static/index.html |
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
| T-033 | 演示硬要求核验与断网彩排：局域网 IP、两个入口地址、**同机双浏览器窗口**（秤端 + 顾客端）全流程、端口占用检测、数据文件路径打印 | T-030 | | REQ-025；AC-013 | `AGENTS.md` §3 的 5 条硬要求**逐条现场验收**；**断网状态下跑完一遍全流程**。**`CP-D` 现场验收后补**：启动脚本这条路自身要有自动化回归（`start.bat` 纯 ASCII + CRLF、`scripts/launch.py` 真起服务、端口被占用时给清晰中文提示且无 cmd 解析错误）—— 否则"双击能起来"只剩人工走查 | tests/e2e/test_demo_requirements.py、tests/e2e/test_demo_launcher.py |
| T-034 | 并发与响应时间压测：多摊位并发写入不覆盖、交易号唯一、接口响应时间采样 | T-016、T-022 | | REQ-031；NFR-001；AC-020 | 并发写入后**条数与交易号唯一性均可核验**；响应时间采样结果与 `docs/standards/quality-gates.md` 的阈值比对 | tests/perf/test_concurrency.py（并发**正确性**：`AC-020` 逐笔核对 + 离线并发零丢弃）、tests/perf/test_latency.py（响应时间门禁 + 突发时延的磁盘归因**对照实验**） |
| T-035 | 收尾核对：质量门禁逐项核对、**干净环境实证（只安装 `requirements.txt` → `python run.py` 必须能启动）**、`scripts/reset_demo.py` 从零重建演练、依赖清单锁定、把核对结果追加进项目状态 | T-029、T-031、T-032、T-033、T-034 | | —(收尾)；NFR-003、NFR-004 | CP-D 四项逐条核对；**干净环境实证须留实测输出**（依据 `docs/adr/0004-依赖范围界定-运行期与开发期.md` §3 第 4 条）；核对记录写入 `docs/PROJECT-STATE.md` | docs/PROJECT-STATE.md（追加核对记录） |
| T-036 | **检查灵敏度验证（自动化负例，收口入口）**：① 路由表 ↔ 契约 §2 比对——**故意注册一个契约里没有的端点，比对必须变红**；② 敏感字段扫描——**故意加一个 `id_card` 字段，扫描必须命中**；③ 运行期隔离检查——**故意在 `run.py` 里 `import pytest`，检查必须变红**；④ 移除故意破坏物后必须恢复绿。四条缺一即判定对应检查不成立 | T-007、T-015 | | REQ-024；NFR-009；AC-012 | 四条负例各自的实际输出留痕（红 → 恢复绿）；作为 CP-C 第④项 | tests/contract/test_check_sensitivity.py（**七个家族的收口入口**：`T-007` 四条 + `T-029` 六形态 + `Q-19` 指标差分 + `T-034` 的 `MT-1014` 兜底；一键复跑 `python -m pytest tests/contract/test_check_sensitivity.py -q -s`） |
| T-SIM-00 | **业务日时钟接缝**（`2026-10-02` 新增；**唯一触碰已验收主系统的改动**，按 `RL-2` 已先改 `spec.md` 增设 `REQ-033`/`AC-024` 并在 `discovery.md` 记 `D-15`）：新增 `app/clock.py` 作为**时钟来源的唯一落点**；把**实测 7 处**业务日/时间戳取样全部改为经它（`pricing.py:152`、`metrics.py:82/210/284`、`offline.py:152`、`payment.py:65`、`seed.py:346`）；`db.now_iso()` 委托给它（覆盖全部时间戳调用点）；`MT_CLOCK_FILE` 未设置时取本机墙钟。**实测得出两处计划外发现并如实登记**：① 第 8 处 Python 取样点 `scripts/reset_demo.py:126`（检查脚本读墙钟 → 若种子落在注入业务日会**误报"缺价"**），已一并修正；② 迁移里 **23 处 `DEFAULT (datetime('now','localtime'))`** 由 **SQLite 求值**、`MT_CLOCK_FILE` 管不到，其中**应用层未显式赋值的 12 列**（`transaction.created_at`、`transaction_item.created_at`、`payment.created_at`、`stall_session.created_at` 及 6 张档案表的列）**仍取墙钟** —— **本批不修**（改法要么重建表、要么让每个 INSERT 显式赋值，且既有库不会拾取新 DDL，属 `Q-17` 同类风险），已由 `test_clock_guard.py` **枚举并钉死**，并在 `spec.md` §5 声明为已知边界 | T-036 | | REQ-033；AC-024 | ① **未注入时业务日与时间戳逐字段不变**（负例：不注入 → 与改动前一致；注入 → 才走注入值）；② 注入营业日 `D` → 交易、日聚合、结算单、三个使用率指标、审计留痕的业务日**全部**为 `D`；③ **墙钟直读的 ast 守卫带 4 个合成负例**，并给出"**漏改一处会怎样**"的实测证据（临时把一处改回 `date.today()` → 守卫必红）；④ 全量 `pytest -q` 与改动前**同结果**（除已知的 `D:` 卷磁盘那条与 2 条既有 mojibake 失败）；⑤ 路由表双向比对仍 `contract=31/missing=0/extra=0`；⑥ `MT_CLOCK_FILE` 指向不存在/非法文件 → **明确报错**，不静默退回墙钟；⑦ **SQL 默认值缺口被枚举钉住**（12 列），修掉任意一列都会用例变红 | app/clock.py、app/db.py、app/seed.py、app/domain/pricing.py、app/domain/metrics.py、app/domain/offline.py、app/domain/payment.py、tests/unit/clock_support.py、tests/unit/test_clock_injection.py、tests/unit/test_clock_guard.py |
| T-SIM-01 | **仿真骨架**：CLI、仿真时钟、**每 agent 独立确定性随机流**、只增不改事件日志、运行目录与"不改演示库"闸门；**隔离闸门两条**（`sim/**` 不得 `import app`；`run.py`/`app/**` 不得 `import sim`）**均带灵敏度负例**；**参数出处约束**（`calibration/params.json` 的 `provenance` 必填）**带灵敏度负例** | T-SIM-00 | | REQ-033；AC-024 | ① **同 seed 两次运行的 `metrics.json` + `events.jsonl` SHA-256 逐字节相同**；不同 seed 必须不同；② 隔离闸门故意 `import app` → **必红**，还原复绿；③ `provenance.kind=sourced` 缺 `ref`、或 `kind=assumed` 缺 `calibration` → **必红**，还原复绿；④ 运行前后演示库哈希不变；⑤ `sim/**` 只 import 标准库（故意 `import numpy` 必红） | sim/__init__.py、sim/__main__.py、sim/cli.py、sim/core/__init__.py、sim/core/clock.py、sim/core/streams.py、sim/core/events.py、sim/core/params.py、sim/core/registry.py、sim/calibration/params.json、tests/sim/sim_support.py、tests/sim/test_sim_no_app_import.py、tests/sim/test_sim_deps_isolation.py、tests/sim/test_param_provenance.py、tests/sim/test_seed_reproducibility.py |
| T-SIM-02 | **仿真环境层与时序**：市场/摊位/品类/商品/价目表、客流到达过程（营业日 → 时段块 → 周 → 月）、设备机队与故障-报修-维修队列。**对 `T-SIM-01` 文件的改动如实登记**：`sim/cli.py` 增 `--env`（跑零决策基线，缺省仍为骨架运行，故 `T-SIM-01` 判据不受影响）、`sim/core/streams.py` 的 `PURPOSES` 增 `price`（新起一条流必须登记，闭合白名单是刻意的）、`sim/calibration/params.json` 增 6 个 `assumed` 参数（`device_count`/`market_stall_count`/`product_base_price_cents_range`/`daily_arrivals_per_market`/`scale_use_baseline_rate`/`repair_mean_days`，逐条带校准思路） | T-SIM-01 | | REQ-033；AC-024 | ① 零决策基线跑 **90 营业日**无异常（子进程实跑，退出码 0）；② **守恒断言且由事件明细独立复算**（设备三态之和 = 机队规模；走秤 + 私下 = 当日到达数；各块之和 = 当日总数），并附**高故障率压测**（MTBF 10 天 → 实测 58 次故障 / 89 天存在报修或维修中设备）以证明守恒断言不是恒真；③ 时序与系统 `business_date` **同轴**（营业日从 `--start` 起连续无跳日；把仿真产出的一天注入 `MT_CLOCK_FILE` → `app.clock.today_iso()` 与之一致，与 `REQ-033` 扣合）；④ 到达过程的强度与时段块配置一致且**可由明细复算**（记录的 `lam` = 整日期望 × 块强度 / 总强度；90 天累计到达 90166 vs 期望 90000，偏离 0.18%） | sim/env/__init__.py、sim/env/market.py、sim/env/demand.py、sim/env/devices.py、tests/sim/test_env_timing.py（并改 sim/cli.py、sim/core/streams.py、sim/calibration/params.json） |
| T-SIM-03 | **商户 Agent**：效用四项分解 `U = Π − Λ − Ψ − Ω − Σ_exit·1[exit]` 与三动作（`comply`/`evade`/`exit`）的决策机制（Q 学习 + softmax；退出走 EWMA 破线规则）。**要点：佣金只按走秤流水计** ⇒ `ΔΠ = V·(1−g)·d` 与 `Λ_comply` 上升**同时发生**，这条机制链必须**能被单独观测**（给三路径占比随参数的变化，不只给最终结论）；状态量可快照 | T-SIM-02 | | REQ-033；AC-024 | ① **分解恒等式**：`ΔΠ = V·(1−g)·d` 精确成立（浮点容差内）；`Λ_comply` 含佣金而 `Λ_evade` 不含；`charge_to_merchant=false`（温州式向买方收）⇒ 商户侧佣金项为 0；② **单因子可观测**：抽佣率扫描 → **合规占比单调降、转暗占比单调升**（只给**序关系**，不给绝对月份/比率 —— 无数据处禁绝对阈值）；短秤检测概率 `p_detect` 扫描 → 转暗占比单调降；③ 退出由 EWMA 破线触发，可快照且退出后为吸收态；④ 每个机制有独立输出（三项均值 + 三路径家数 + 破线次数）；⑤ **检查类产出带灵敏度负例**（把"佣金只按走秤计"改成"按全部流水计" → 序关系判据必红）；⑥ 同 seed 可复现 | sim/agents/__init__.py、sim/agents/merchant.py、tests/sim/test_merchant_agent.py（并改 sim/calibration/params.json） |
| T-SIM-04 | **消费者 Agent**：信任更新 `T(t+1) = clip(T·(1−δ) + η(ν)·(o−T), 0, 1)`、**负面偏差 `η⁻ > η⁺`**、扫码率与信任耦合造成的**正反馈陷阱**（`T↓ → 扫码↓ → 采样机会↓ → 恢复更慢`）。**`Q3` 是循环论证**（结论由无出处的 `η⁺/η⁻/δ` 决定）⇒ 结论**只能表述为"关于参数的命题"**（如"当 `η⁻/η⁺ > k` 且 `δ` 超阈时存在不可恢复区"），**不许表述为关于现实的命题**；代码注释与报告口径一致 | T-SIM-03 | | REQ-033；AC-024 | ① **解析式与仿真相符**：`t_half = ln2 / (δ + η⁺·p_scan·1[信息有真实来源])`，仿真从扰动恢复到半程所需期数与该式一致（容差内）；② **陷阱可观测**：开启"扫码率随信任耦合"后，从同一冲击恢复所需期数**严格长于**关闭耦合时（序关系）；③ **不可恢复区**：`η⁻/η⁺` 与 `δ` 网格扫描 → 输出区域判定（三态：恢复/不恢复/边界带），结论写成**关于参数的命题**；④ `η⁻ > η⁺` 的非对称性由单期对照直接验证；⑤ 状态量可快照；⑥ **检查类产出带灵敏度负例**（把 `η⁻ > η⁺` 改成对称 → 陷阱判据必红） | sim/agents/consumer.py、tests/sim/test_consumer_agent.py（并改 sim/calibration/params.json） |
| T-SIM-05 | **市场方 + 监管 Agent**：市场方按**考核口径**分配预算（扩容 vs 维护）并带**决策迟滞**；监管按抽检率生成发现与罚金。**对 `T-SIM-02` 文件的改动如实登记**：`sim/env/devices.py` 的 `step_day()` 增可选 `max_admissions_per_day`（**缺省 `None` = 不限产能 ⇒ `T-SIM-02` 行为逐字段不变**），用于表达"维护预算 → 工台每天能开工几台" | T-SIM-04 | | REQ-033；AC-024 | ① **复现激励错配**：同一市场状态下，考核口径 = **装机量** ⇒ 维护占比 **= 0**（维护不产出该 KPI）；= **使用率** ⇒ 维护占比 **> 0**（设备停摆会拉低使用率）—— "设计缺陷长什么样"的可执行复现；② **决策迟滞可观测**：`decision_delay = D` 时预算调整**恰好在第 D 期**才生效（断言**位移量**，不只是"更慢"）；③ **复现"坏设备长期存在"这一已知现象**：装机量口径 + 迟滞下报修队列长期不消化，且**没有迟滞就复现不出来**（必须留无迟滞的对照组）；④ 监管：抽检率↑ ⇒ 发现数不降（序关系）；⑤ 状态量可快照、同 seed 可复现；⑥ **检查类产出带灵敏度负例** | sim/agents/market_admin.py、sim/agents/regulator.py、tests/sim/test_market_admin.py、tests/sim/test_regulator.py（并改 sim/env/devices.py、sim/calibration/params.json） |
| T-SIM-06 | **model 适配器 + §6 全部指标 + 6 场景 + 敏感性（`2026-10-02` 新增）**：`sim/bridge/model_adapter.py` **真跑四个 Agent**（商户/消费者/市场方/监管）并把全部事实落进只增不改的事件流（`layer="model-adapter"`，**不是骨架**）；`sim/observe/metrics.py` 把 §6 的 **22 个指标**实现成「**只读事件流**」的纯函数（每个都写分子/分母，**不另存一份状态**）；`sim/verify/sensitivity.py` 做 **OAT（一次一参数）+ 分层拉丁超立方 + 自写 Spearman**（不引新依赖）；`sim/scenarios/*.json` 落 7 个场景（`S0` + `S1`~`S6`），**每臂只差本场景声明的对照变量**；`sim/observe/report.py` 出报告（`report.md` + `metrics/scenarios/sensitivity.json`）。**口径纪律（本项目硬约束，不是建议）**：① 有真实数据的参数才允许绝对阈值，无数据的（MTBF / 罚款 / 扫码基线 / 信任权重 / 退出阈值）**只允许序关系与区间不重叠**；② `commission_rate_bp` 的「2% **不是实测费率**」必须写在**报告显眼位置**，不是只躺在 `params.json` 里；③ **`R6` 反例（商户自费 ⇒ 必须被弃用）保留并执行，结果如实报告**（设计 §7.1 已写明：若自费组活得一样好 ⇒ 模型里的『自费』没有真实成本 ⇒ 结论 26 无法被检验）；④ **"对哪些参数敏感"必须是本场景自己那个指标的曲线**（所有场景共用一张走秤率表，等于把 6 个场景的问题答成同一句话）；⑤ **每个场景声明的对照变量都必须真的被 `sim/**` 读到**。**接手人本轮自查出并修掉三个真缺陷（如实登记，三者都曾给出"看起来正常"的结论）**：① **存活判据③ 口径错** —— 拿 `M-13`（扫码率）当 `M-04`（走秤率），且没有实现设计写的"第 300–360 日"窗口 ⇒ 走秤率其实达标时判据假红；② **`S6` 的 `evade_feasibility` 根本没接线** —— 它一度只出现在 `study.py` 的 OAT 键表里、没有任何模型代码读它，于是 `S6-①堵死` 与 `S6-②不堵` 结果逐位相同：「堵死」这条对照实验什么也没测（实测：修好后 ① 走秤率 1.0000 vs ② 0.6732，**缩减档**；**收口 Agent 在 `360 营业日 × R=3` 全量档复测为 ① 0.9773~0.9841 vs ② 0.2816~0.4983** —— 方向一致、绝对量级随档位变化，故引用时**必须带档位**，不许把缩减档的数字当全量结论）；③ **`R6` 的成本权重扫描跑在出资臂上** —— `upfront=0` ⇒ `adopt_decision` 判据恒真 ⇒ 扫出一条假平线（实测都在 0.3397）。三者各带回归守卫 | T-SIM-03、T-SIM-04、T-SIM-05 | | REQ-033；AC-024 | ① **22 个指标的分子分母可由 `events.jsonl` 明细逐条复算**（`recompute_all(events)` 与运行产物对账，第三方可逐一核对）；② **非退化（抗写死）自检**：逐指标扰动其声明的来源字段 → 该指标值必须变；把某指标写死成常量 → 自检必红（`Q-19` 教训）；③ LHS **分层不退化**（每维每层恰一个样本）带负例；④ 自写 Spearman 有**并列秩**处理与负例（单调变换不变、打乱后趋 0）；⑤ **`R6` 反例场景被执行并给出实际结论**（成立 / 不成立 / 不稳健**三态**，不许只有二态）；⑥ 每个场景同时给出「对哪些参数敏感」（OAT + 秩相关），不许只给一条曲线；⑦ 同 seed 可复现；⑧ 每场景每个数字带分子分母 | sim/bridge/__init__.py、sim/bridge/model_adapter.py、sim/bridge/day_loop.py、sim/bridge/month_loop.py、sim/bridge/scenario.py、sim/bridge/study.py、sim/observe/__init__.py、sim/observe/metric_specs.py、sim/observe/metric_util.py、sim/observe/metrics.py、sim/observe/metrics_trust_cash.py、sim/observe/metrics_check.py、sim/observe/report.py、sim/verify/__init__.py、sim/verify/sensitivity.py、sim/scenarios/S0_baseline.json、sim/scenarios/S1_commission.json、sim/scenarios/S2_enforcement.json、sim/scenarios/S3_maintenance.json、sim/scenarios/S4_price_disclosure.json、sim/scenarios/S5_device_funding.json、sim/scenarios/S6_short_weight_feasibility.json、tests/sim/test_model_adapter.py、tests/sim/test_metrics_recomputable.py、tests/sim/test_scenarios_and_r6.py（**原名 `test_scenarios.py` 与 `tests/e2e/test_scenarios.py` 同名撞车** —— pytest 无 `__init__.py` 时按 basename 认模块，全量收集直接 ERROR，与 `T-031` 那次 `conftest` 撞车同类；故按「避免同名」改名登记）、tests/sim/test_verify_sensitivity.py（并改 sim/cli.py、sim/calibration/params.json、sim/core/params.py（`with_overrides`）、tests/sim/sim_support.py（`tiny_run` 极小规模集成档）） |
| T-SIM-07 | **live 适配器（`2026-10-03` 新增）**：`sim/bridge/server_launcher.py`（子进程拉起 `run.py`：**隔离 `MT_DATA_DIR` 落 `C:`** + **独立端口**（避免与开发中其它进程撞）+ **`/healthz` 就绪探测**（轮询到 200 为止、带超时上限、进程早退即报错并附日志尾）+ **业务日时钟文件** `MT_CLOCK_FILE`（按 `T-SIM-00` 已确立的契约推进，**不另造一套**）+ 被测系统树指纹 `app/**`、`specs/**`）；`sim/bridge/live_adapter.py`（**31 个端点的 stdlib `http.client` 客户端**，**不引任何新依赖**；端点覆盖**由实际请求反推** —— 拿真实路径去匹配契约模板，而不是让调用方自报「我调过」）；`sim/bridge/live_run.py`（**场景装载 = 真 `PUT /api/admin/commission-rules`**，让 `app/domain/commission.py` 算出佣金，而不是 sim 自己再算一遍 ⇒ 抽佣 0%/2%/温州式在 live 模式下**不是 sim 里的一个变量**；日初配置 `/healthz` + `GET/POST /api/admin/categories` + `POST /api/admin/aliases`；逐笔成交覆盖 8 类秤端端点 + 顾客页 2 个 + 模拟秤/支付回调 2 个；日终 `daily-aggregate`/`reconciliation`/`metrics/usage`/`dashboard`/`audit-logs`；月末 `settlements` 生成与查询）；`sim/bridge/live_evidence.py`（证据落盘 + **六条判据的判定写成纯函数**，灵敏度负例可直接喂）；`sim/cli.py` 接 `--mode=live`（**`model` 分支逐字节不变**）。**如实登记两处系统侧边界（不自行改 `app/**`）**：① 契约 §3.24 的 `rate_bp ∈ 1~10000` ⇒ live 模式的「抽佣 0%」**不可精确表达**，取下界 `1bp = 0.01%` 并在产物里标注；② 补传产生的是 `priced` 未收款交易，按 §3.29「只算已结算」口径**不影响对账等式**（`app/domain/settlement.py` 模块 docstring 已锁定该口径）。**只读系统**：live 模式只通过 31 个 HTTP 端点与主系统交互，运行前后 `app/**`、`specs/**` 树指纹必须不变 | T-SIM-00、T-SIM-06 | | REQ-033；AC-024 | ① **31/31 端点调用覆盖率清单（机读产物 `coverage.json`，缺一即红）** —— 清单由**实际请求**反推，且 `sim` 内置的 31 个模板与 `contracts/rest-api.md` §2 端点总表**双向逐条比对**（漂移即红）；② **全部营业日 `reconciliation.balanced=true`**；③ **幂等重放不产生新交易**（同 `Idempotency-Key` + 同请求体重发 → 同一 `transaction_no`、列表 `total` 不变）；④ **敏感扫描零命中**（复用 `tests/contract/sensitive_scan.py` 扫 live 事件流里的全部响应体）；⑤ **数据目录在 `C:`**（若实测在 `D:`，须记录实测 p50/p95 并说明是否降级 —— 本实现**无条件记录 p50/p95**）；⑥ **运行前后 `app/**`、`specs/**` 树指纹不变**；⑦ **六条判据各自的灵敏度负例**（漏掉端点 / `balanced` 改成 false / 幂等重放多出一笔 / 响应体塞 `id_card` / 数据目录挪到 `D:` / 树指纹变动 ⇒ 对应判定必红）；⑧ **`--mode=model` 的产物逐字节不变**（`events.jsonl` 与 `metrics.json` 的 SHA-256 与改动前相同） | sim/bridge/server_launcher.py、sim/bridge/live_adapter.py、sim/bridge/live_run.py、sim/bridge/live_scenario.py、sim/bridge/live_cli.py、sim/bridge/live_evidence.py、sim/core/console.py、tests/sim/test_live_endpoints.py、tests/sim/test_live_run.py（并改 sim/cli.py、specs/market-trade-flow/plan.md §4） |
| T-SIM-08 | **验证器（`2026-10-03` 新增）**：`sim/verify/r_criteria.py`（`R1`~`R6` 判据的**可执行声明**：文本 / 判据形式（绝对阈值 vs 序关系）/ 依赖参数 / 所用档位；含三个档位 `L1`、设备故障压力档、预算约束压力档，每个档位都带**为什么必须换这一档才看得到机制**的自证）；`sim/verify/backtest.py`（跑各判据的全部臂 → 逐子句判定 → **稳健性扫描**（无出处参数推到区间端点，看序关系是否翻转）→ **三态结论**，收敛规则 `verdict_of` **只此一处**；**「绝对阈值 vs 序关系」的纪律是机械的**：`absolute` 子句只要依赖一个 `provenance.kind=assumed` 的参数，即判 **`不可评估`** 并点名是哪一个 —— 不放宽阈值、也不算通过）；`sim/verify/consistency.py`（`model ↔ live` 一致性：**同一批成交决策流**（同 seed / 同臂 / 同 `(摊位, 商品, 重量, 支付方式)`）一边进进程内账本、一边经 HTTP 打到被测系统，逐营业日比对走秤笔数 / 总额 / 佣金 / 三指标六个分子分母 / 结算单 + `reconciliation.balanced`；**成交密度提到 ≤20 笔/日**（`live_run` 缺省只有 4 笔/日，逐笔对账密度不够）；**三处口径差显式量化而不是静默忽略**：① 私下交易系统侧不可见（只在走秤通道上对账，shadow 笔数与它对 `M-04` 的影响单列）；② 取整与计量单位（模型「每 500g + 银行家舍入」vs 系统「每公斤 + 四舍五入」：单价按 ×2 换算，期望金额用系统那条取整规则重算，差 1 分的笔数单列）；③ 佣金（模型自按日浮点累加、系统逐摊逐日 `half_up`：模型侧按系统口径独立复算后再比）；**灵敏度负例**（从 model 事件流里删掉一笔成交后一致性必须变红））；`sim/verify/calibration.py`（**档 2 匹配矩**，POM = ABM 领域方法，**明确不假称来自本项目调研**：把 `R1`~`R6` 当 6 个定性矩做 LHS + 拒绝采样，只报**区域占比**与**最敏感参数**，不报「完美参数点」；6 矩 vs 20 项待定参数的过度拟合风险写在产物里）；`sim/observe/verify_report.py`（三份验证产物的装配 + 从 `study.py` 迁来的「`R6` 三段证据装配」与「`R` 的诚实提示」，**措辞只此一处**）；`sim/verify_cli.py` 接 `--backtest` / `--backtest-only` / `--consistency` / `--calibrate`（`--mode=model` 的既有分支逐字节不变）。**按语义拆欠账**（`quality-gates.md` §1.2 的 400 行门禁，`Q-16` 裁定按语义拆、不放宽阈值、不删注释凑行数）：`sim/bridge/world_setup.py`（建世界）从 `model_adapter.py` 搬出；`sim/bridge/sensitivity_runner.py`（敏感性编排）从 `study.py` 搬出；`month_loop.py` 增 `open_period`（期初结构决策）；**为把新文件也压在门禁内**，另按语义搬出 `sim/verify/model_ledger.py`（model 侧账目 = 纯函数复算）、`sim/verify_cli.py`（验证类命令分支）、`sim/env_baseline.py`（零决策基线运行）、`sim/cli_support.py`（跨分支共用的落盘口径） | T-SIM-06、T-SIM-07 | | —（仿真侧不改规格） | ① **`R1`~`R6` 逐条产出三态结论**（`成立`/`不成立`/`不稳健`，**不许只有二态**；每条子句标明用的是**绝对阈值**还是**序关系**、该参数**有无出处**、**所用档位**，以及该结论对哪些无出处参数翻转）；② **`model ↔ live` 一致性容差 ≤ 1 分 / 1 笔**（逐营业日 + 结算单，口径差显式量化）；③ **灵敏度负例**：篡改 `model` 一处 ⇒ 一致性必红（端到端真删一笔成交，不用伪造数据）；④ **参数出处字段机械校验**（**复用 `tests/sim/test_param_provenance.py` 的 `provenance_problems` / `Params.kind`，不另写一套**）：回测引用的每个参数键必须在参数文件里登记，且它的 `kind` 必须与回测的「无出处」名单同源；⑤ **档 2 匹配矩**（做得不扎实时交**诚实状态**：做了哪些、为什么剩下的没做，不许交一个只跑了单点的版本）；⑥ 三份产物 `backtest.md`/`consistency.md`/`calibration.md` 落盘且内容与控制台一致 | sim/verify/r_criteria.py、sim/verify/backtest.py、sim/verify/consistency.py、sim/verify/calibration.py、sim/observe/verify_report.py、sim/bridge/world_setup.py、sim/bridge/sensitivity_runner.py、sim/bridge/month_loop.py、tests/sim/test_verify_backtest.py、tests/sim/test_verify_consistency.py、tests/sim/test_calibration_moments.py、sim/verify_cli.py、sim/verify/model_ledger.py、sim/env_baseline.py、sim/cli_support.py（并改 sim/cli.py、tests/sim/test_param_provenance.py、tests/sim/test_scenarios_and_r6.py、sim/bridge/study.py、specs/market-trade-flow/plan.md §4） |

| T-SIM-09 | **纯静态可视化 + 报告自检（`2026-10-03` 新增）**：`sim/observe/svg_charts.py`（**内联 SVG 图元原语**：转义 / 条形图 / 序列折线 / 三态标记 / 四类性质分箱。**纯函数、无 I/O**，故灵敏度负例可直接喂；**不含任何资源引用** —— 无 `url()`、无 `xlink:href`、无外部字体、无 JS 库）；`sim/observe/svg_report.py`（`sim/` 产物 → **单文件 `report.html`**：① 七场景 `S0`~`S6` 走秤率对照；② `R1`~`R6` 三态结论；③ 四类性质分解（`docs/sim-验证结论实况.md` V-05 的四类；**分类规则写死在代码里、由产物机械算出**，不许手填结论）；④ `V-01` 的三臂走秤率对照。**数据必须来自 `sim/` 的产物**（`study/scenarios.json` 与 `verify/backtest/backtest.json`），**一个数都不许硬编码**；产物位于 `data/` 下（`.gitignore`）⇒ 报告头部逐块登记**数据来源路径 + SHA-256 + 复现命令**，**缺产物时该块显式显示"未生成"并给出复现命令，不显示假数据**；`sim/cli.py` 增 `--svg-report`（`--mode=model` 既有分支逐字节不变）。**零外部资源是硬要求**（`AGENTS.md` §3 硬要求 4 / `REQ-025` / `AC-013`）：报告在断网机器上必须能完整打开 | T-SIM-06、T-SIM-08 | | REQ-025；AC-013 | ① **复用** `tests/contract/test_no_external_assets.py::scan_static_text()` 扫描生成的 `report.html` → **0 命中**（不另写一套正则 —— 同一条规则写两遍就是下次漂移的种子）；② **合成负例必红**：往同一份报告里塞一个协议外链引用与一个包管理器目录引用，**同一检查必须判红**（先跑正例绿、再跑负例红，否则负例无效）；③ **断网可看**：全部资源内联，无外链请求；④ 图表四块齐全且**逐块标注数据来源与档位**；⑤ 缺产物时显示"未生成"（正例）**且**注入一份构造产物后必须能显示真数据（反向断言，防止"永远显示未生成"这种假绿）；⑥ 报告自报的来源路径与 SHA-256 必须与磁盘上的文件一致（用例核对） | sim/observe/svg_charts.py、sim/observe/svg_report.py、tests/sim/test_svg_report_no_external.py（并改 sim/cli.py）
| T-SIM-10 | **结果说明书与答辩材料（`2026-10-04` 新增）**：新建 `docs/sim-results.md`（① 七场景 `S0`~`S6` + `R1`~`R6` + `V-01`~`V-06`，**每条带档位**；② **参数来源表**——`sim-design.md` §9 落成人读表，由 `sim/observe/param_table.py`（**纯函数**）渲染，`sourced` 与 `assumed` **分表、一眼可分**；③ `sim-design.md` §1 五个问题的**答案卡**：每问给答案 + **适用条件** + **「不稳健」标记**，`R1`~`R6` 六条全判不成立**必须照实按四类性质分解呈现**，`V-05` 的误读警告必须出现在同一页）；`tests/sim/test_param_table_doc.py` 核对文档里的参数表与渲染**逐字一致**（防漂移）。**硬约束**：① 每个被引用的数字都能点到《调研报告》的**具体结论号**，或显式标为假设/压力档越界取值；② **独立复核**：另起会话逐条核对引用（`AGENTS.md` RL-4 对立评审）；做不到就**明确写出哪些引用没能独立复核、风险在哪**，不许假装做了；③ 产物在 `data/`（`.gitignore`）时必须写清**复现命令与来源路径**，不许只写数字；④ 每条数字与 `docs/sim-验证结论实况.md` 一致，不一致以实况文件为准并**报告差异** | T-SIM-09、T-SIM-11 | | —（仿真侧不改规格） | ① **每个数字可点到来源**：结论号 或 `assumed` 标记 或 越界档位标注，三者必居其一，缺一即不合格；② **防漂移用例绿**：把 `params.json` 改一个值 ⇒ 该用例红（证明它真在比对）；③ **五问答案卡不许只写"成立"**：每问必须同时有「答案 + 适用条件 + 不稳健/不可评估标记」；④ 与实况文件的差异**逐条列出**（本轮已发现 1 处：`T-SIM-11` 修复后 `R3-c`/`R3-d` 由「不可评估/通过」变为「不通过」，须由父代理重新裁定）；⑤ **`git status` 不得出现 `docs/sim-验证结论实况.md`** | docs/sim-results.md、sim/observe/param_table.py、tests/sim/test_param_table_doc.py
| T-SIM-11 | **商户学习卫生缺陷修复（`2026-10-04` 新增，接手于"上一任没追到根因"的遗留项）**：`R3` 压力档（MTBF=180）下走秤率序列 `[0.594, 0.479, 0.305, 0.183, 0.727, 0, 0, 0]` 先逆势大涨再直接归零。**用测量查到根因**（非推断）：`close_month` 把**已退出/停业**商户按 `volume = max(1.0, 实现流水)` 喂进 `observe()` ⇒ "没有流水"变成"流水 1 分" ⇒ `normalized = U/1.0 = −29999.75`（比真实每元效用 ~0.25 大 **10⁵ 倍**）；`Q̄_peer` 又把**全部**摊位（含已退出）算进同伴均值 ⇒ `ρ(1−φ)·Q̄_peer ≈ 0.08 × (−7700) ≈ −616` 被加进**每个在营商户**的 `Q` ⇒ `softmax(Q/0.05)` 被垃圾值支配 ⇒ "合不合规"退化成掷硬币。**顺带坐实两件事**：① 被登记为"0.3× 组"的序列**其实是足额臂的**（足额臂实测逐位相同，0.3× 臂是另一串数）；② 中心档（MTBF=10）下"走秤归零"是**另一种机制**（10 台设备全坏 + `repair_capacity = 1.5e6 // 1.6e6 = 0` ⇒ 每一笔成交都走私下），与本缺陷无关。**修复**（两处，各自负一项语义责任）：`realized_volume_for_learning()` —— 没有实现的流水就**不学习**（顺带堵掉"没有生意的月份被判经营失败"的假出口）；`coexisting_peers()` —— 同伴网络只含**同期仍在营**的商户（离场商户已不在这个 10 摊网络里）。**不改判据、不改参数、不放宽阈值** | T-SIM-08、T-SIM-10 | | —（仿真侧不改规格） | ① **修复前后对照可复现**：同一档位（`S-约束` 压力档 MTBF=180 / 240 日 / `S3`）修复前 `M-04=[0.594,0.479,0.305,0.183,0.727,0,0,0]`、修复后 `[0.594,0.479,0.305,0.183,0.187,0.180,0.152,0.145]`；② **灵敏度负例必红**：把两个接缝用 `monkeypatch` 打回修复前写法 ⇒ `merchant_month` 里重新出现 `realized_volume_cents == 0`、`M-19` 的 min 重新被哨兵值（−29999.75 / −132499.62）钉死；③ **量级守卫**：离场商户造成的同伴项污染（>100）必须比真实每元效用差（~0.13）大两个数量级；④ **前提守卫**：测试档位必须真的会退出商户（否则守卫空转）；⑤ **参数文件逐字节未改**（这是实现修复，不是参数校准）；⑥ 全量 `pytest -q` 无回归 | sim/bridge/month_loop.py、tests/sim/test_merchant_learning_hygiene.py
| T-SIM-12 | **结论重跑 + 出处机械对照（`2026-10-04` 新增，接父代理裁定"`V-02`/`V-03`/`V-04` 必须重跑后才能引用"）**。上一任登记「修复只落在 `close_month`/`peer_mean_q`，Agent 层扫描不走这两处」**并自认那是推断不是实测**；父代理不接受推断。**三件事**：① **重跑三条**并给出**修复前后逐项对照**（带档位的实测数字，不许只写"没变"）；② **把"不受影响"换成实测** —— 给两处接缝装**调用探针**，证明三条的计算过程里调用次数为 0，**并证明探针非空转**（同一探针在集成运行里必须会响）；③ **上一任如实登记的"最大未复核面"**（`params.json.provenance.ref` 全部转引自 `sim-design.md` §9，**从未逐条打开调研报告核对原文与编号**）做成**机械检查**。**同时实测父代理点名的 `V-03` 间接污染疑虑**：信任经"欺骗率"传导，而欺骗率取决于商户走秤/私下决策比例 —— 商户决策正是被污染的那条通道 | T-SIM-11、T-SIM-10 | | —（仿真侧不改规格） | ① **三条逐位复现并锁死**（`V-02` 合规 `0.225→0.150`、`p_detect` 转暗 `0.800→0.100`；`V-03` `T*=0.7241`、半衰期 `9.561 vs 8`、`η⁻/η⁺=2.286`、耦合未恢复 vs 不耦合 22 期；`V-04` 维护占比 `0.000 vs 1.000`、队列 `40/40`、修好 0 台、末期可用 `6/10`）；② **调用探针 0 次 + 探针非空转守卫**（否则"0 次"只是探针坏了）；③ **反向灵敏度负例**：**把修复关掉**，三条数字必须**逐位不变**（排除"碰巧同种子"）；④ **`ref` 结论号存在性机械检查**（含 `结论 4 / 5 / 6` 省略重复「结论」二字的续写形态）；⑤ **`Q-xx` 一律 `assumed`** 的机械检查；⑥ **取值 == 报告原文金额**的对照表（人工对照固化，带"退回 10 倍错值必红"的负例）；⑦ **每条检查类产出都带灵敏度负例**；⑧ 全量 `pytest -q` 无回归 | tests/sim/test_fix_impact_on_registered_conclusions.py、tests/sim/test_param_provenance.py、sim/core/params.py

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

## 3b. 形态 2（秤端 / 中台拆分）任务表（`T-SCALE-*`，`2026-10-06` 新增）

> **本表的前置门禁**：`T-SCALE-00` 未完成之前，**`T-SCALE-01` 及之后一律不得开工**。
> 依据：`ADR-0005` / `ADR-0006` 状态均为 `提议`；`ADR-0006` **偏离宪法 §1**，须走宪法 §6 修订程序（AI 只起草）。
> 本表沿用 §2 的排序原则：**测试先行、依赖只指向更小号、产出文件集合不相交才标 `[P]`**。
>
> **产出文件的硬边界**：`scale-fw/**`、`tests/scale/**`、`docs/hardware/**` 已在 `plan.md` §4 按目录级授权；
> 本表逐文件登记。**未在本表登记的文件不得出现**。
>
> **两处刻意的约束（写在这里，避免实现时被"顺手优化"掉）**：
> ① `scale-fw/core/**` **不得 `#include` 任何 ESP-IDF 头文件** —— 一旦引入，主机侧单测就没了，`Q-21` 会从"已知边界"变成"完全没有验证"；
> ② 中台侧新增端点**必须复用 `app/domain/pricing.py::create_transaction`**，不得另写一份记账 —— 账口只能有一个（`ADR-0005` §3 第 4 条）。

| 编号 | 标题 | 依赖 | 并行 | 关联 REQ/AC | 验收方式 | 预估产出文件 |
| --- | --- | --- | --- | --- | --- | --- |
| T-SCALE-00 | **人工门禁（AI 不得代签）**：负责人批准 `ADR-0005`（→`已接受`）、`ADR-0006`（→`已接受`，并完成宪法 §1 修订的三方批准记录），并批准 `spec.md` §4.2 的 `NFR-008` 重新登记 | — | | —(门禁) | 两份 ADR 状态由 `提议` 改为 `已接受`；`constitution.md` §1 与 §8 有修订记录且头部版本递增；`spec.md` §4.2 的批准状态改为 `已批准`；`AGENTS.md` §1 的 ADR 登记表状态同步 | docs/adr/0005-秤端与中台拆分.md、docs/adr/0006-秤端嵌入式技术栈.md、.specify/memory/constitution.md、specs/market-trade-flow/spec.md |
| T-SCALE-01 | 中台迁移 `0002`：新增 `market` 表；`merchant` / `stall` / `product` / `price_item` / `commission_rule` / `daily_aggregate` / `settlement` 增 `market_id` 并回填默认市场。**不改写 `0001_init.sql`**（既有库不会拾取新 DDL，属 `Q-17` 同类风险） | T-SCALE-00 | | REQ-035；AC-026 | ① 从零建库后两个市场的行**互不可见、互不串账**；② **装载第二个市场只跑 DML、不改 DDL**（以迁移文件清单为证）；③ **单机演示形态不退化**：既有全量用例结果与改动前一致 | app/migrations/0002_market_scope.sql、app/db.py |
| T-SCALE-02 | 中台设备模型与令牌：`device` 表（`device_id` / 令牌摘要 / 绑定市场+摊位 / 固件版本 / 最后心跳）；令牌**只存摘要、不存明文** | T-SCALE-01 | [P] | REQ-036；AC-027 | ① 令牌明文**不入库、不进日志**（与 `RL-5` 同口径，扫描断言）；② 设备绑定唯一性由约束兜底；③ **对外接缝（契约测试按 TDD 先声明，实现照此提供）**：`app/domain/device.py::provision_device(conn, *, device_id, market_code, stall_no, token) -> None` —— 语义 = **运维侧预注册**（令牌只存摘要、直接 INSERT）。测试靠它构造"已注册设备"，**不另造端点**（`RL-1`） | app/migrations/0003_device.sql、app/domain/device.py |
| T-SCALE-02b | **中台迁移：扩展 `audit_log` 的 `event_type` 白名单**，使 `scale_amount_mismatch` 可写入（`AC-030` 的前置）。`0001_init.sql` 的 `event_type` 是 **9 值 CHECK**，SQLite **改不了 CHECK** ⇒ 必须**重建表**：drop 两个「只增不改」触发器 → 建新表 → **逐行搬移既有数据** → 换名 → 重建触发器 | T-SCALE-01 | | REQ-038；AC-030；NFR-009 | ① 重建后**既有 `audit_log` 行数一行不少**（搬移前后计数相等，且抽样比对 `payload_json`）；② `audit_log_no_update` / `audit_log_no_delete` **仍然生效**（现场各试一次 `UPDATE`/`DELETE`，必须被拒绝）；③ 新 `event_type` 可写入、旧 9 值仍可写入；④ 非法 `event_type` 仍被 CHECK 拒绝 | app/migrations/0004_audit_event_types.sql |
| T-SCALE-03 | **契约测试（先红）**：`contracts/scale-midplatform.md` 的 **7 个端点**逐条覆盖，含 `MT-2001`~`MT-2005`、幂等命中返回首次结果、`amount_mismatch` 三个必做动作（入账取中台值 / 写留痕 / 如实返回）、**越权字段不采信** | T-SCALE-02 | | REQ-034~REQ-043；AC-025~AC-033 | 每个端点先跑成**失败**；错误码逐条有用例；**`AC-030` 的负例必须真造出"不一致"状态**（改一个分位），不许只断言字段存在 | tests/scale/conftest.py、tests/scale/scale_support.py、tests/scale/scale_provision.py、tests/scale/test_scale_device.py、tests/scale/test_scale_catalog.py、tests/scale/test_scale_ingest.py、tests/scale/test_scale_settle.py |
| T-SCALE-04 | 中台实现：设备激活与心跳端点；**并把 `MT-2001`~`MT-2005` 加进 `app/__init__.py::ERROR_STATUS`**（错误码 → HTTP 状态的**唯一映射点**；不加则这 5 个码会被 `TradeError` 判为表外而落到兜底 `MT-1014`） | T-SCALE-03 | [P] | REQ-034、REQ-036、REQ-040；AC-025、AC-027 | 对应契约测试由红转绿；**重复激活返回既有绑定**（不报错、不改绑）；**`stall_no` 与既有绑定不一致 ⇒ `MT-2005`，且请求里的 `stall_no` 不得被采信为新绑定**；`pending_count` **只用于观测，不拒绝任何交易** | app/api/scale_device.py、app/__init__.py |
| T-SCALE-05 | 中台实现：字典与价目表下发端点（**窄响应体**：不含成本、不含佣金相关字段） | T-SCALE-03 | [P] | REQ-037、REQ-002；AC-028 | 对应契约测试由红转绿；**响应字段白名单逐一相等**（多一个字段即失败）；价目表为空时返回空数组**不报错** | app/api/scale_catalog.py |
| T-SCALE-06 | 中台实现：**交易上报 + 重算比对 + 留痕**（复用 `pricing.create_transaction`；在线与补传共用同一端点） | T-SCALE-03、T-SCALE-05、**T-SCALE-02b** | | REQ-038、REQ-039、REQ-041；AC-029、AC-030、AC-031 | ① 入账金额一律为中台重算值；② 不一致时**三件事齐全**（入账取中台值 / `audit_log` 留痕 `event_type = scale_amount_mismatch` / 响应如实返回）；③ 幂等命中返回**首次**结论（`replayed=true`），不是重新比对的结论；④ 补传的 `business_date` 取**暂存时**的营业日 | app/api/scale_ingest.py、app/domain/scale_ingest.py |
| T-SCALE-07 | 中台实现：收款确认（现金 / 取回收款码）与退货申请端点 | T-SCALE-06 | | REQ-043、REQ-042、REQ-009、REQ-010、REQ-013；AC-033、AC-002 | ① `qr_payload` **指向中台顾客页**，不得指向秤端；② 现金写**同一张支付流水表**（不另立现金表）；③ 退货冲正与佣金扣减**全部在中台计算**，重复申请只冲减一次 | app/api/scale_settle.py |
| T-SCALE-08 | **固件核心 · 计价**（纯 C，不依赖 ESP-IDF）：`money.h` 定点类型 + `pricing.c` 的 `half_up_div` / `price_amount` / 合计与抹零 | T-SCALE-00 | [P] | REQ-038；AC-029 | ① **主机侧编译并跑通**（`zig cc -target x86_64-linux-musl`）；② 与 Python 权威实现同输入同输出；③ **全程整数、无浮点**（机械扫描固件核心不得出现 `float`/`double`） | scale-fw/core/money.h、scale-fw/core/pricing.c、scale-fw/core/pricing.h、scale-fw/test/test_pricing.c |
| T-SCALE-09 | **golden vectors 生成器**：从 Python 权威实现导出向量集（含 0/1 克、50 公斤边界、半值四舍五入、抹零、改价） | T-SCALE-08 | | REQ-038；AC-029 | ① 删掉向量文件后重跑脚本**逐字节还原**；② `--check` 与现有文件比对不写入；③ **灵敏度**：把 Python 侧口径改一个分位 ⇒ `test_pricing` **必红**，还原复绿 | scripts/gen_pricing_vectors.py、scale-fw/test/vectors/pricing_golden.json |
| T-SCALE-10 | **固件核心 · 本地暂存队列**：定长记录 + CRC；状态 `staged` / `acked` / `discarded`；达阈值只告警仍接受；写入失败明确报错 | T-SCALE-08 | [P] | REQ-040、REQ-041、REQ-030；NFR-013、NFR-014；AC-025、AC-031 | ① **绝不静默丢弃**：写满/写失败路径有用例，失败必须返回错误且**不留半条记录**；② 补传成功后**清除本地副本**可被扫描核对；③ 断电语义用"记录级 CRC + 追加写"表达，并有**截断记录被判无效**的用例 | scale-fw/core/queue.c、scale-fw/core/queue.h、scale-fw/test/test_queue.c |
| T-SCALE-11 | **固件核心 · 报文编解码**：7 端点的请求/响应窄编解码；**越权字段不采信**；错误码解析 | T-SCALE-08 | [P] | REQ-034、REQ-036、REQ-038；AC-027 | ① 编解码**往返一致**（含中文与特殊字符转义）；② 响应里出现 `stall_id` / `market_id` 时**不得改变授权范围**（用例构造该场景）；③ 未知错误码**不得当成功** | scale-fw/core/proto.c、scale-fw/core/proto.h、scale-fw/test/test_proto.c |
| T-SCALE-12 | **固件胶水 · 网络同步器**：WiFi + HTTP 客户端；激活 → 拉字典与价目表 → 上报 → 补传编排（按 `staged_at` 升序） | T-SCALE-10、T-SCALE-11 | | REQ-034、REQ-037、REQ-041；AC-025、AC-028 | ① 补传顺序由用例断言（乱序即红）；② 补传失败该条**保持暂存**、不阻断后续；③ **中台不可达时秤端仍能进入营业界面**（`AC-025` 的前半段）；④ **`sync.c` 必须不依赖 ESP-IDF**（`2026-10-06` 补登记）：编排逻辑只依赖 `core/` 冻结接口 + `sync.h` 自声明的**平台端口**（WiFi 状态 / HTTP 请求 / 单调时钟 / 存储读写），ESP-IDF 只出现在端口实现里 ⇒ 上面 ①②③ 三条从"静态检查"升级为**主机侧实测断言**（`test_sync.c`）。这是 `ADR-0006` §3.2 对冲 (a)「核心不依赖 ESP-IDF 才能在无硬件时被验证」向**编排层**的延伸 | scale-fw/main/net/http_client.c、scale-fw/main/net/sync.c、scale-fw/main/net/sync.h、**scale-fw/test/test_sync.c** |
| T-SCALE-13 | **固件胶水 · 称重 / 界面 / 落盘**：HX711 与 UART 仪表两路采样、LVGL 图标选品与计价界面、二维码展示、LittleFS/NVS 落盘、`app_main.c` 开机编排、ESP-IDF 工程与构建说明 | T-SCALE-12 | | REQ-004、REQ-005、REQ-043；AC-006、AC-033 | ① **可在无硬件条件下做到的是"编译通过 + 静态检查"**，该边界必须写进 README（不得宣称已硬件验证，见 `Q-21`）；② 界面**全程无文本输入**；③ 二维码内容来自中台下发的 `qr_payload`；④ **本机无 ESP-IDF**，故实际只能做到**静态检查 + 人工评审**，"编译通过"须在装好 ESP-IDF 的环境补做并如实记录；⑤ **构建前置文件**（`2026-10-06` 补登记，原清单遗漏）：`main/idf_component.yml`（声明第三方组件 `littlefs` / `lvgl` / `esp_lvgl_port` 的来源与版本）与 `partitions.csv`（LittleFS 需要独立 storage 分区）。**缺这两个文件，即便在装好 ESP-IDF 的环境也构建不起来** —— 登记理由是 `plan.md` §4「未登记的文件不得出现」这条纪律**双向生效**：不许擅自新建，但**该登记的漏登记同样是缺陷** | scale-fw/CMakeLists.txt、scale-fw/README.md、scale-fw/main/CMakeLists.txt、scale-fw/main/app_main.c、scale-fw/main/idf_component.yml、scale-fw/partitions.csv、scale-fw/main/weigh/*、scale-fw/main/ui/*、scale-fw/main/store/* |
| T-SCALE-14 | **端到端：分离形态同机双进程**（中台 + 秤端进程；断连 → 本地暂存 → 恢复 → 补传） | T-SCALE-06、T-SCALE-07、T-SCALE-12 | | REQ-034、REQ-038、REQ-040、REQ-041；AC-025、AC-028、AC-030、AC-031 | ① **先起秤端、后起中台**，秤端不卡死；② 断连期间成交 → 恢复后补传成功、**总数不翻倍**；③ 人为改一个分位 ⇒ 中台告警留痕且入账取中台值；④ 补传后秤端**无本地副本残留** | tests/e2e/test_split_mode.py |
| T-SCALE-15 | **机械检查 + 灵敏度负例**：秤端固件**不含**佣金/日聚合/结算/看板/退货冲正计算（`NFR-015`）；秤端**不承载顾客页面** | T-SCALE-13 | [P] | REQ-042、REQ-043；NFR-015；AC-032、AC-033 | ① 扫描固件源码与符号表；② **灵敏度**：故意在 `scale-fw/core/` 塞一个 `commission_cents()` ⇒ **必红**，还原复绿；③ 故意塞一个顾客页 HTML ⇒ **必红** | tests/scale/test_scale_no_ledger.py |
| T-SCALE-17 | **分离形态的可运行启动方式**（**本轮补漏**）：让两个部分能**真正分开启动、经网络对接**，而不是只在 pytest 里同机双进程。① **中台侧角色入口**：`run.py` 支持分离形态 —— 只提供 7 个秤端接入端点 + 运营端 + 顾客页，**不托管 `/scale/` 秤端界面**（那是固件的活）；形态 1（单机 all-in-one）**行为一字不变**（默认值不变）。② **主机侧秤端运行器**：可独立启动的秤端进程 —— 激活 → 拉字典与价目表 → 本地计价（**调用真 `scale-fw/core`**，不是 Python 复刻）→ 本地暂存 → 恢复后按 `staged_at` 升序补传；中台不可达时**仍能启动并完成本地暂存** | T-SCALE-04、T-SCALE-05、T-SCALE-06、T-SCALE-07 | | REQ-034、REQ-037、REQ-038、REQ-040、REQ-041；AC-025、AC-028 | ① **两个进程各自独立启动**：先起秤端、后起中台，秤端不卡死不退出；② 网络对接走**真实 HTTP**（不是进程内直调）；③ 形态 1 的 `python run.py` 默认行为与启动横幅**逐字节不变**（既有 540 条用例不得退化）；④ **不得宣称硬件在环** —— 主机侧运行器是"主机侧秤端"，`Q-21` 边界必须写进其说明与 README | run.py、scripts/scale_host_runner.py |
| T-SCALE-16 | **收尾**：全量回归（含既有 540 条**不退化**）+ 单机形态与分离形态**并存**实证 + 文档同步 | T-SCALE-14、T-SCALE-15、**T-SCALE-17** | | —(收尾) | ① `python -m pytest -q` 结果与 `T-SCALE-00` 前基线一致（除既有的 3 条平台耦合项）；② 两种形态**各自可独立启动**并跑通主链路（形态 2 的启动方式由 `T-SCALE-17` 交付，本任务只做实证）；③ 核对记录写入 `docs/PROJECT-STATE.md` | docs/PROJECT-STATE.md（追加核对记录） |

## 6b. 形态 2 的 AC 覆盖矩阵（机械核验用：`AC-025`~`AC-033`）

| AC | 契约测试 | 实现 | 验证（端到端 / 机械检查） |
| --- | --- | --- | --- |
| `AC-025` | T-SCALE-03 | T-SCALE-04、T-SCALE-10、T-SCALE-12 | T-SCALE-14 |
| `AC-026` | T-SCALE-03 | T-SCALE-01 | T-SCALE-01（两市场隔离 + 只跑 DML）、T-SCALE-16 |
| `AC-027` | T-SCALE-03 | T-SCALE-02、T-SCALE-04、T-SCALE-11 | T-SCALE-14 |
| `AC-028` | T-SCALE-03 | T-SCALE-05、T-SCALE-12 | T-SCALE-14 |
| `AC-029` | T-SCALE-03 | T-SCALE-08 | T-SCALE-08（主机侧单测）、**T-SCALE-09（golden vectors 逐条比对 + 负例）** |
| `AC-030` | T-SCALE-03 | T-SCALE-06 | T-SCALE-14（真造不一致状态） |
| `AC-031` | T-SCALE-03 | T-SCALE-06、T-SCALE-10 | T-SCALE-14 |
| `AC-032` | —（属源码/符号面检查，无契约端点） | T-SCALE-07、T-SCALE-13 | **T-SCALE-15（灵敏度：塞 `commission_cents()` 必红）** |
| `AC-033` | T-SCALE-03 | T-SCALE-07、T-SCALE-13 | **T-SCALE-15（灵敏度：塞顾客页必红）** |

> **本矩阵与 §6 的 `AC-001`~`AC-024` 矩阵相互独立、不得互相覆盖**：形态 2 **不修改**任何既有 `AC` 的判据。
> 改动本矩阵须同步 §3b 的任务表，反之亦然。
