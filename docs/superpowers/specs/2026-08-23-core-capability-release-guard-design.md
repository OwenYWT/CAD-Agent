# 核心能力与发布保护设计

## 状态

已于 2026-08-23 在对话中确认。本设计解决团队协作时新代码在没有文本冲突的情况下覆盖旧行为的问题，并建立可执行的核心功能清单、分层发布门禁和回滚边界。本文不修改业务功能，也不把 Mock、静态占位或仅前端展示视为通过证据。

## 问题与目标

Git 能发现同一行代码的文本冲突，但不能发现以下语义回归：

- 接口仍能调用，但字段、鉴权或状态机含义发生变化；
- 新实现绕过 Durable Workflow、Change Set、Revision 或审计链路；
- 页面仍能展示，但真实数据库、对象存储、CAD Runtime 或外部服务未接通；
- 数据库迁移、Temporal Workflow 或产物格式使旧版本无法继续运行；
- 合并时测试的是旧 SHA，进入 `main` 的却是另一组代码。

目标是让每项商业能力都有明确所有者、机器可读契约、分层验证证据和回滚策略；任何核心能力回归都不能进入 `main` 或生产环境。

## 设计原则

1. `main` 只接收经过 Merge Queue 验证的提交，禁止直接推送和强推。
2. 核心功能清单必须机器可读，并驱动 CI；Markdown 只负责解释。
3. 测试证据必须标明验证层级。单元测试或替身不能冒充真实依赖、真实 MCAD 或真实 Provider E2E。
4. 发布物和 CAD 产物不可变。回滚切换引用或创建新 Revision，不覆盖历史。
5. 数据库、事件、接口和 Temporal Workflow 至少在迁移窗口内保持向后兼容。
6. 外部 Connector 可独立降级；鉴权、持久化和 MCAD 主链路失败必须阻止或回滚整体发布。
7. 每次生产事故都必须沉淀为永久回归测试并绑定到对应能力。

## 核心能力注册表

新增一个版本控制、机器可读的能力注册表，建议路径为 `config/core-capabilities.yaml`。注册表是发布门禁的唯一能力索引，不复制测试实现。

每项能力至少包含：

```yaml
id: mcad.native.generate
name: 真实 MCAD 生成
criticality: P0
owners: [mcad-runtime, durable-agent]
status: supported
paths:
  - backend/app/workflows/**
  - backend/app/execution/**
contracts:
  - execution-spec-v1
tests:
  pr: [hermetic-agent-contract]
  main: [real-mcad-durable-e2e]
  staging: [mcad-release-regression]
  production: [mcad-canary-smoke]
dependencies: [postgresql, temporal, object-store, mcad-runtime]
feature_flag: native_mcad_enabled
rollback: application_and_worker_release
```

字段约束：

- `criticality=P0`：商业主链路能力，相关门禁失败时禁止合并或部署。
- `criticality=P1`：已启用的独立能力；自身失败时禁止发布该能力，但可通过 Feature Flag 隔离，不拖垮 P0 主链路。
- `status` 只能是 `supported`、`limited` 或 `unavailable`。`limited` 和 `unavailable` 必须在 API 与 UI 中如实呈现。
- `paths` 用于自动识别影响范围，开发者在 PR 中的声明只能增加，不能缩小 CI 推导出的范围。
- `tests` 引用稳定测试套件 ID，不能引用“人工已检查”等无证据状态。
- 每个 `supported` 能力必须有至少一个真实链路测试和可执行回滚策略。

## 初始核心能力目录

| 领域 | 核心能力 | 级别 | 最低真实证据 |
|---|---|---:|---|
| 身份与安全 | 登录、用户、租户、权限、文件所有权、RLS | P0 | 真实 PostgreSQL 集成测试与越权负例 |
| 项目与版本 | 项目、分支、Revision、历史、并发 CAS | P0 | PostgreSQL 集成测试与恢复 E2E |
| Durable Agent V2 | 规划、Tool Calling、建模、修复、确认、取消、恢复 | P0 | Temporal + PostgreSQL 持久工作流 E2E |
| 执行与沙箱 | ExecutionSpec、幂等、隔离 Runtime、重试和取消 | P0 | 固定镜像的真实容器执行 |
| 原生 MCAD | 生成、Preview、参数修改、重新计算 | P0 | CadQuery/build123d/OCP 真实执行与可读产物 |
| 工程验证 | 几何、视觉、DFM 证据和失败状态 | P0 | 真实几何/渲染/规则执行；零假成功 |
| 变更管理 | Change Set 接受、拒绝、请求修改、提交、回滚 | P0 | 真实 Revision、Artifact 和审计链路 |
| 产物与导出 | STEP、STL、DXF、SVG、PNG、项目包 | P0 | 文件可读、Hash 正确、来源可追踪 |
| 实时任务 | WebSocket 进度、断线重连、事件回放 | P0 | 持久事件 + 重连集成测试 |
| 数据基础设施 | PostgreSQL、对象存储、Temporal、Artifact 不可变性 | P0 | 真实依赖集成测试与重启恢复 |
| Onshape | OAuth、账户连接、资源同步、预览和受控修改 | P1 | Staging 真实账户 Provider E2E |
| Fusion | Connector、Add-in 协议、授权和受控修改 | P1 | 支持平台上的真实 Fusion E2E |
| 前端工作区 | 主页面、路由、Preview、参数、检查、变更、导出 | P0 | 浏览器主链路和控制台零错误 |
| 响应式与文案 | 桌面、平板、移动端和中文状态一致性 | P1 | 规定视口的浏览器回归 |
| 未接通能力 | ECAD 等能力状态诚实、不出现进行中或成功假象 | P1 | API 能力状态与 UI 映射契约测试 |
| 部署与运维 | 配置校验、健康检查、密钥边界、生产冒烟 | P0 | Release 候选环境和生产 Canary |

## 防覆盖合并机制

### 分支保护

- 禁止直接推送、强推和删除 `main`。
- 所有修改通过短生命周期 PR；使用 squash 或线性历史保持变更边界清晰。
- 启用 Merge Queue 和“分支必须为最新”规则。真正进入 `main` 的 Merge SHA 必须是通过门禁的 SHA。
- 使用 `CODEOWNERS` 绑定 Agent、MCAD Runtime、数据、Connector 和部署目录。涉及契约删除、能力降级或 P0 逻辑时必须由对应负责人审批。
- 删除或弱化测试、接口、Schema、事件、工作流定义和能力注册项需要显式高风险审批。

### 语义兼容门禁

CI 不只检查代码测试，还要比较当前 PR、最新 `main` 和当前生产 Release：

- OpenAPI 路径、方法、鉴权、请求和响应字段；
- PostgreSQL Schema、迁移顺序、RLS 和数据约束；
- Durable Event、ExecutionSpec、ExecutionResult、Artifact Manifest 等协议；
- Temporal Workflow/Activity 名称、任务队列和版本兼容范围；
- 前端 Adapter 与后端字段、状态机和错误码映射；
- 已支持能力的状态是否被静默改为占位、固定返回或仅前端展示。

破坏性变化必须先引入兼容版本和迁移窗口，不能在同一发布中直接覆盖旧契约。

## 四层发布门禁

| 阶段 | 必须验证 | 失败结果 |
|---|---|---|
| PR | Lint、类型检查、单元测试、接口/Schema/事件契约、受影响能力回归、安全静态检查 | 禁止进入 Merge Queue |
| `main` 候选 | 真实 PostgreSQL、对象存储、Temporal、MCAD Runtime、WebSocket、Change Set、恢复与回滚 E2E | 不生成 Release Candidate |
| Staging | 完整主链路、真实 Onshape/Fusion、数据库迁移、旧任务兼容、安全、浏览器与响应式回归 | 禁止生产部署或关闭对应 P1 能力 |
| Production Canary | 登录、项目创建、Durable Agent、MCAD、Preview、导出、历史恢复，以及错误率、任务成功率和延迟 | 关闭单项 Feature Flag 或切回稳定 Release |

门禁规则：

- P0 失败始终阻断当前阶段。
- P1 的代码或配置发生变化时必须运行自身门禁；真实外部服务故障可阻止该 Connector 上线或触发独立降级，但不能伪造成功来通过。
- 被标记为 flaky 的测试仍然失败；只能在有负责人、问题单、截止时间且不影响 P0 的情况下临时隔离。
- 每次测试保存 Git SHA、依赖版本、Runtime Image Digest、测试套件版本和原始结果，避免复用其他提交的通过记录。

## 发布与回滚

### Release Manifest

每个候选版本生成不可变 Manifest，记录：

- Git Commit 和构建来源；
- API、前端、Worker 和 MCAD Runtime 镜像 Digest；
- 数据库 Schema/Migration 版本；
- Temporal Workflow/Worker 兼容版本；
- 配置指纹和启用的 Feature Flags；
- 核心能力测试结果与 Artifact 链接。

Manifest 不含密钥。生产只允许部署已签名或受信任流水线生成的候选版本。

### 应用和 Worker

- 保留当前和上一稳定 Release，不以 `latest` 作为发布身份。
- 新 Release 先进入 Canary，健康和核心冒烟通过后再提升流量。
- P0 指标越过发布阈值时切回上一稳定 Release；P1 Connector 异常时优先关闭自身 Feature Flag。
- 回滚不能重新启用被淘汰的直接写链路。旧 Workflow Worker 在其历史全部终止前继续注册和运行。

### 数据库

- 使用 expand-contract：先增加兼容结构，再迁移/回填，最后在后续发布删除旧结构。
- 应用回滚依赖向后兼容 Schema，而不是自动执行危险的数据库降级。
- 数据迁移必须可重入、有校验、可暂停，并在切换前验证行数、约束和租户隔离。

### CAD 版本与产物

- Artifact 路径不可变并记录 SHA-256、来源 Revision、WorkflowRun 和 Runtime Digest。
- 产品回滚创建新的 `ProjectRevision` 或 Change Set 操作，并通过 CAS 更新 Branch Head；不覆盖旧 Revision 或固定 `current.step`。
- Onshape/Fusion 的外部修改保持独立工作流和审计，未知提交状态进入 reconciliation，不能假定未发生。

### 当前单机部署过渡

在现有单机资源无法同时承载完整双环境时，第一阶段使用不可变 Release 目录、镜像 Digest、发布前真实冒烟和可重复的上一版本切换命令。数据库仍必须先满足向后兼容。具备双实例容量后再启用自动 Canary 流量切换；不能把单机脚本包装成已经具备高可用能力。

目标恢复时间为 5 分钟内切回上一稳定应用版本，且用户数据、已提交 Revision 和运行中 Durable 任务不丢失。

## 团队工作流

1. 开发者在 PR 模板中声明影响的能力、契约、迁移和回滚方式。
2. CI 根据路径和契约 Diff 自动补充影响能力；人工声明不能缩小范围。
3. 运行受影响能力门禁并由 CODEOWNERS 审批。
4. Merge Queue 在最新 `main` 上重新验证实际 Merge SHA。
5. 通过 `main` 真实依赖测试后生成 Release Manifest。
6. Staging 验证迁移、外部 Provider 和完整主链路。
7. Production Canary 执行只使用隔离测试租户和可清理产物的冒烟任务。
8. 指标异常时执行能力降级或应用回滚，并保存发布事件证据。
9. 根因修复必须新增或加强永久回归测试，再解除发布冻结。

## 可观测性与失败处理

每个发布至少按 Release、能力、租户和 Workflow 类型观察：

- API 5xx、鉴权失败和契约错误；
- Workflow/Step/ExecutionAttempt 成功率、超时、重试和积压；
- MCAD 真实产物成功率、假成功计数和验证失败类型；
- WebSocket 重连和事件回放失败；
- 数据库迁移、对象存储和 Artifact 完整性错误；
- Connector OAuth、限流、权限和 reconciliation 状态；
- 前端未捕获异常、路由失败和关键交互失败。

阈值必须由首轮稳定生产数据建立并版本化，不能在本设计中虚构固定数值。唯一固定的零容忍条件是跨租户访问、产物完整性破坏、重复提交、错误 Branch Head、假成功和不可恢复的数据丢失。

## 验收标准

- 核心能力注册表覆盖上述目录，且每个 `supported` 能力有 Owner、真实测试和回滚策略。
- `main` 无法被直接推送，Merge Queue 只合并通过全部 Required Checks 的 Merge SHA。
- 一个删除接口字段、绕过 Durable 写链路、移除 STL 门禁或破坏 Revision CAS 的测试 PR 会被自动阻止。
- 一个真实 MCAD 主链路失败的候选版本无法进入生产。
- Onshape/Fusion 故障可以被单独禁用，原生 MCAD 仍能正常运行。
- 应用可在 5 分钟内切回上一稳定 Release，数据库和旧 Temporal Workflow 仍兼容。
- 回滚后历史、Revision、Artifact、审计和事件均可查询且未被覆盖。
- 所有通过记录都能追踪到实际部署的 Git SHA 和镜像 Digest。

## 非目标

- 不在本阶段自建新的 CI 平台或工作流引擎。
- 不要求每个 PR 调用真实 Onshape、Fusion 或付费 LLM。
- 不把 ECAD 尚未实现的能力包装为已支持。
- 不以增加测试数量代替真实链路和明确契约。
- 不在没有容量和监控证据时宣称已经实现高可用或零停机发布。
