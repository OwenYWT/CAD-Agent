# CAD-Agent 解耦提交前审查

## 结论

- 状态：`PASS_WITH_LIMITATIONS`（本地实现与可用环境）。GitHub 分支保护 `BLOCKED`；远端 CI 结果另附。
- Reviewer：Codex `/root`，作者自查，不代替独立人工审查。
- Review ID：`codex-decoupling`；日期：2026-09-21。
- 范围：`62084d5` 到 `refactor/module-boundaries` 当前工作树，含新增文件；原目录未提交内容未搬入。
- 本次只读审查期间修改业务代码或应用测试：**No**。此前实施与修复的记录见实施报告。

## 改动与消费者

| 边界 | 真实实现与消费者 | 结论 |
| --- | --- | --- |
| 业务 Clients | `services/clients` → 所有原 `engineeringService` UI/hook 消费者；旧门面重导出 | 函数调用方迁移，类型/构建及真实浏览器通过 |
| 编辑上下文 | `useParameterEditor` 接收面板 durable；共享文档通过 `DocumentTaskPanel` 独立处理确认 | 不再读任意 active panel；保存、拒绝与草稿/选择保留通过 |
| WebSocket | 连接维护 → `sessionMessages` → 按 session/panel 定位的 Store actions | 有序重放、跨面板消息归属与真实 WS 集成通过 |
| HTTP/用例 | `document_requests`、`native_modification`、`document_operations`；CAD/监控共用认证服务 | 137 个路径、111 个 schema 与基线 OpenAPI 完全一致；DTO 无反向 Temporal 依赖 |
| 模型执行 | 显式 8 项 Handler 注册、独立操作契约、事务栅栏、UsageSink | 真实 Job/取消/代次/长等待/续跑与用量权限通过 |
| Activity | 旧 wire 包装 → 业务 Handler → Attempt/制品链 | 30 个搬移方法在归一依赖名/SQL 空白后函数体相同；另 2 个为显式工程/场景依赖装配，绑定与真实工程链验证 |
| 受理事务 | 文档入口使用 `create_document_workflow`；只读检查/场景/工程使用通用 `create_workflow` | 显式组合且同事务；故障注入无半条记录；幂等、CAS/ABA、撤权与分发专项通过 |
| 监控 | HTTP 管理员鉴权 → 共享认证；查询走独立可配置只读池 | 独立登录角色无法成为 runtime 或读取密码/正文/写表；双账号真实浏览器通过 |

关联功能域覆盖 01–20、23、24；22 的前端调用入口仅迁移 import，未声称外部 CAD 实机验收。

## 风险扫描与核验

执行 `scan_review_risks.py --base 62084d5 --head HEAD --worktree --json`，逐类复核报告中的线索：

- 搬移文件中的 `pass` 属已有清理/取消分支；原方法体比较与取消/失败真实回归未发现新增空实现。
- `status: completed` 是检查步骤完成，返回中保留实际 `outcome`、证据身份和报告，不等于固定“验证通过”。
- `fixture`、`controlled-provider` 出现在测试环境或兼容门面说明；浏览器主链未拦截 HTTP，真实 Provider 另有 5 项通过证据。
- 未发现新增生产 Mock、固定成功返回、输出 token 上限、模型等待总期限、权限绕过或编码损坏。

代码变动导致的 Handler 漏接 `backend` 已在进入本轮审查前修复，并增加全部 Activity 绑定测试；此处没有以单元通过代替真实工程验证。

## 测试证据

| 层级 | 实际命令/入口 | 结果 | 分类 |
| --- | --- | --- | --- |
| 通用 | `pytest backend/tests -m 'not docker and not llm and not fusion_e2e'` | 1535 passed，180 skipped，1 deselected；跳过不计通过 | LOGIC / CONTRACT |
| 前端 | `node --test --experimental-strip-types tests/*.test.ts`、lint/build | 154 passed；lint/build 通过 | LOGIC / BUILD |
| PostgreSQL | `tests/postgres` 除另列 3 文件 | 80 passed，无跳过 | SERVICE |
| 模型/监控/迁移 | `test_model_jobs`、`test_monitoring`、`test_upgrade_contract` | 12 / 6 / 1 项通过；包含 310 秒等待及 500 次以上续跑 | SERVICE |
| 核心集成 | CI 所列 6 文件，排除另列 Provider 案例 | 30 passed，5 deselected | SERVICE / RUNTIME |
| Provider | 4 个 Temporal 场景 + 旧 REST 真实生成 | 5 passed | REAL_ACCEPTANCE |
| 回放 | `replay_histories.py` | 6 历史 / 4 Workflow 家族，V2 patch 前后均覆盖 | SERVICE |
| 内核 | `native_contracts.sh` | 约束 8 案例、原生参考保存、FEA/CAM/发布通过 | RUNTIME |
| 浏览器 | candidate、rejection、layout、monitoring、CI browser contract | 真实候选/修改/保存/拒绝/缓存/布局/监控通过 | PRODUCT |
| 工程 | `cloud_engineering_acceptance.py`、`cloud_cam_browser.py` | 真实 HTTP 求解与浏览器刀路/导出、负例通过 | REAL_ACCEPTANCE |
| 静态 | `git diff --check`、边界检查、24 域映射校验、compileall | 通过；当前违规基线为空 | CONTRACT |

逐阶段日志、任务/版本/产物身份见 [实施与测试报告](../../architecture/decoupling-progress.md)。私有原始日志没有复制到仓库，避免混入凭据或带签名下载地址。

## 限制

1. 私有仓库分支保护 API 返回 403，当前套餐不支持；没有独立审批或失败 PR 阻止合并的服务端证据。CI 配置不能替代保护设置。
2. 提交前本地审查不等于远端合并 SHA 的 CI；远端运行须单独核对。
3. 未部署腾讯云，未重验云端旧/新监控镜像组合；本次无新 schema，应用兼容入口保留。
4. 未验证 Fusion/Onshape 实机、真实机床/打印机或手机实物适配。既有产品局限没有因解耦自动消失。
5. 导入边界不是全仓动态依赖/SQL 写表证明。保留部分 Store、路由与兼容门面是最小必要变更，不称作所有模块完全独立。

## 维护记录

实施阶段更新了 canonical feature map 的实际实现/测试归属，24 域、334 路径校验通过。未复制到其他 Agent 私有 Skill。审查结论为作者自查，有套餐/部署/实机边界，不能冒充独立验收团队结论。
