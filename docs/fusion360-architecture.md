# Fusion 360 Connector 架构

> 默认生产拓扑：Fusion Add-in / Palette 直连 HTTPS Cloud Agent
> 兼容拓扑：loopback Local Runtime 仅用于离线/诊断部署
> 日期：2026-07-16
> API 事实依据：[`fusion360-research.md`](fusion360-research.md)

## 1. 目标链路

```text
Fusion Desktop
  -> local HTML Palette（输入、状态、Preview、Approval、结果）
  -> Add-in 主线程读取当前文档、选择、装配/特征摘要
  -> worker outbound HTTPS（纯 JSON；不调用 adsk）
  -> Cloud Fusion Agent（Pydantic / JSON Schema）
  -> 单个 typed CadAction proposal
  -> CustomEvent 回到 Fusion 主线程
  -> native no-write PreviewData
  -> Palette 用户批准（high-risk 再经 Fusion 原生确认）
  -> Dispatcher -> FusionApiFacade -> adsk native API
  -> computeAll + Feature health + target re-resolution + assertion + Diff
  -> structured result / authorized artifact report over HTTPS
```

业务层继续只依赖统一 `CadAdapter`，不 import `adsk`，不执行 Agent 生成的 Python/命令，
也不认识 Fusion Component/Feature 实例。新增其他 CAD Adapter 不需要改变 Action 生命周期。

## 2. 仓库审计与复用

| 现有模块 | 处理 |
| --- | --- |
| `contract.py` 的 `CadAdapter`、Context/Action/Verify/Result/Error | 复用，仍是业务边界和唯一 CAD Action schema |
| `Dispatcher` + `ExecutionJournal` | 复用，承担静态 action allowlist、at-most-once、补偿和 indeterminate |
| `FusionApiFacade` | 复用真实 `adsk` context/action/export/verify 映射，并修复静态验证声明 |
| `policy.py` 的 `CAD-C14N-1` intent hash / 风险 | 云 Agent 和 Palette approval 共用语义 |
| FastAPI auth、rate limit、LLM provider | 复用 Cloud Agent HTTP 边界；不复用任意代码生成 pipeline |
| Local Runtime SQLite queue/lease | 保留为 `local_runtime` 兼容 transport；不再是默认生产必经层 |
| APS `cloud.py` / encrypted token store | 只负责用户授权的 Hub/Project/Version 读取，不负责当前桌面建模 |

新增模块：

```text
backend/app/fusion360/
  agent_contract.py   Agent turn/plan/report/artifact Pydantic models
  agent_planner.py    natural language + ContextData -> one typed CadAction
  agent_store.py      heartbeat/status、idempotent plan/result audit（不存 token/完整 CAD）
  agent_api.py        authenticated HTTPS Agent endpoints

fusion_addin/CADAgentFusionConnector/
  agent_transport.py stdlib HTTPS worker, TLS/retry/limits/redaction
  palette.py         main-thread state, native preview, approval binding
  palette.html       local UI shell with restrictive CSP
  palette.js         local-only async Qt Web Browser bridge
```

## 3. 信任边界

### 3.1 Palette 是不可信 UI

Palette JavaScript 不能调用 Fusion API。它只能发送有限事件：提交 prompt、批准/拒绝当前
proposal、请求刷新和显式 artifact/F3D 授权。HTML 使用本地资源和严格 CSP；所有 prompt、
Agent 文本、Diff 和错误用 `textContent` 渲染。Approval event 只能携带 Add-in 生成的 opaque
nonce，不能回传或修改 Action。

### 3.2 Network worker 不持有 Fusion 对象

worker 只处理不可变 JSON、HTTPS、timeout、Retry-After、有限重试和 Queue。除
`Application.fireCustomEvent` 外，不得调用 Application/UI/Palette/Design 或任何 `adsk` API。
context 采集、Action 再校验、native preview、approval 消费、execute、verify 和
`Palette.sendInfoToHTML` 全在 Fusion 主线程。

### 3.3 Cloud Agent 只提议类型化 Action

Agent output 先解析为 JSON，再经 `CAD_ACTION_ADAPTER` 严格验证。生产代码没有 `exec`、
`eval`、动态 import、subprocess、shell 或反射式属性 dispatch；执行入口使用完整静态 action
map。服务端覆盖模型给出的 request ID、connector ID、execution mode 和 approval ID，并把
proposal 强制设为 preview。

## 4. Direct Agent 合同

### 4.1 Turn

`AgentTurnRequest` 包含：

- Agent contract version 与完整 `CadCapabilities`；
- `request_id`、`connector_instance_id`；服务端从 bearer credential 派生 authenticated subject，
  不信任 body 自报 owner；
- bounded natural-language prompt；
- `ContextData` 与 versioned `context_fingerprint`；
- request-scoped artifact upload consent；F3D 是额外的独立 consent。

Application 的 Fusion 用户名只在本地 `get_context` 结果中可见；Add-in 构造云端 turn 时将
`application.user_name` 置为 null。Cloud Agent 不需要也不持久化该身份字段。

上下文至少包含 Application/Document/Design、Root/Components/Occurrences、当前 selection、
parameter、material/mass、Sketch/Feature/Body/assembly/cloud 摘要。所谓“特征树”由装配层级、
各 Component 的 Feature/Sketch/Body 和 parametric Timeline 摘要组成，不声称 1:1 复刻 Fusion
UI Browser。

### 4.2 Plan

v1 每个 turn 最多返回一个 Action：

- `proposed`：恰好一个严格 `CadAction`；
- `needs_clarification`：没有 Action，说明缺失信息；
- `no_action`：没有 Action，说明为何不能安全操作。

每个 ID-bearing 字段都必须出现在非截断 context 中，并满足 ownership：parameter/feature/
profile/edge/point/entity 属于声明的 component/document。上下文不足时不能猜 token、名称或
timeline index。多步任务必须等上一步真实 result，再重新读取 context 开启下一 turn。

### 4.3 Report 和 Artifact

`AgentExecutionReport` 回传 request/proposal/connector、intent hash、最终公开 `CadResult` 与
context fingerprint。preview/approval 的真实性由 Add-in 本地绑定与 Dispatcher 执行顺序保证，
不会把 opaque nonce 或内部 snapshot evidence 发给云端。plan 按 request ID、report 按 report ID
幂等；相同 ID 不同 canonical payload 是冲突。

默认只上传结构化摘要。Artifact 只有在明确 `cad.export`、用户批准且 request-scoped upload
claim 存在时才可上传；路径必须是本地 staging 内 basename，云端验证 size/SHA-256/格式。
F3D 是完整 CAD archive，除 export approval 外还需要独立的 full-model-upload 授权。

## 5. Context fingerprint 与 Stale 防护

fingerprint 使用 versioned canonical JSON，覆盖：document ID、saved/modified/read-only、云
version/project/folder/read-only、selection/Feature 的 ID/kind/component/health、Parameter 的
ID/component/createdBy/expression，以及 bounded Timeline 的完整字段。display name、user、mass、
prompt 和 vendor extension 不进入指纹。流程中至少三次采集/核对：

1. 发送 Agent turn 前；
2. native preview 前；
3. 用户批准后的 execute 前。

document、credential subject、selection、parameter/feature 或 fingerprint 变化都会返回
`STALE_CONTEXT`，不执行。Entity token 仍通过 `Design.findEntityByToken` 解析，不能只比较
字符串。

## 6. Preview 与 Approval

Agent 文本不是 Preview。proposal 必须先进入 Dispatcher 的 `execution_mode=preview`，由真实
Fusion context 生成 `PreviewData.planned_changes` 和 verification plan，且不得调用写 API。

Approval 记录绑定：authenticated subject、connector、document、`CAD-C14N-1` intent hash、
context fingerprint、proposal/preview/request IDs、risk、expiry 和 Add-in nonce。执行入口原子
消费；重复、过期、任一字段变化全部拒绝。`save`/`saveAs` 等 high-risk action 还需要 Fusion
原生 confirmation。Agent 和 Palette 都不能自行签发 approval。

## 7. Dispatcher、快照和恢复

Dispatcher 是唯一执行入口：

```text
validate -> context precondition -> snapshot -> durable journal started
 -> static action handler -> compute/verify/diff -> durable completed -> report
```

- 已 started 但没有 durable completed 的 mutation 永不自动重放，状态为 `indeterminate`。
- completed report 在 HTTPS 失败后可以原样重发。
- 参数/名称/材料保存旧表达式/属性用于补偿；新 Feature 保存对象引用，失败时尝试
  `deleteMe` 并重新计算；保存操作不可回滚。
- snapshot ID 必须指向真实 mutation 前 baseline，不能用无关联随机 ID 伪装。

## 8. 结果验证

成功 mutation 必须同时满足：

1. `Design.computeAll()` 确实完成；其 true 不能替代健康检查；
2. 修改前后完整 Feature health delta 没有新增 Error；
3. 目标或新实体重新解析成功；
4. action-specific assertion 成功（参数 expression/value、属性/material、创建 Feature 类型、
   保存状态或导出 evidence）；
5. `changes[]` 是真实 before/after Diff；
6. export 文件位于 staging、存在、非空、头部/格式合理且 size/hash 一致。

Preview 不声称 compute 完成。Standalone verify 必须自行调用 compute；静态
`compute_completed=true` 被视为缺陷。

## 9. 错误、取消、超时和断线

所有失败使用 `CadError`：稳定 code/category、safe message、retryable、受控 details；公网结果
不含 traceback、token、prompt 原文或绝对路径。

- HTTPS timeout/429：plan/report 可按服务端语义有限重试。worker 启动时先协商 exact contract/
  action capabilities；heartbeat 5s/TTL 15s，TTL 过期的 `fusion_running` 为 unknown。
- cancel：在 snapshot、mutation、verify 等安全边界协作检查；单个 Fusion API 调用不能强杀。
- deadline：未开始为 timeout；started mutation 无确定结果为 indeterminate。
- stop：先停止接收，join worker，再注销 handler/CustomEvent/Palette；late response 丢弃。
- queue：有界且一次只允许一个 mutation，防止 Fusion 主线程被远程并发占用。
- mutation 的 exact public execution report 在本地 journal durable 后才入网；ack 前断线或
  Fusion 重启只重发同 report ID/payload，不重新执行 CAD Action。Artifact upload 本身不自动
  重放，需按 receipt/云端记录 reconciliation。

## 10. Local Runtime 兼容模式

`mode=local_runtime` 保留现有 loopback HTTP + SQLite queue + lease/heartbeat/cancel/idempotency，
用于离线、企业代理或诊断。它只监听 `127.0.0.1`/`::1`，不能直接暴露公网；共享 artifact
root 语义也只适用于同机。`CadAdapter`、Action、Dispatcher 和 Fusion API 不随 transport
改变。

## 11. APS 边界

- Desktop API：当前活动文档、选择、未保存状态、native edit/rebuild、viewport。
- APS Data Management：三方 OAuth 后的 Hub/Project/Folder/Item/Version 元数据。
- Fusion Automation：显式输入文件的 headless/batch WorkItem，不读取用户当前桌面会话。
- Cloud Fusion Agent：本项目的 typed planner，不是 APS 产品，也不获得 Fusion 登录 token。

默认不上传完整 CAD。APS token 加密存储在 Backend，Add-in Cloud Agent credential 与 APS OAuth
彼此隔离且遵循最小权限。

## 12. 部署与验收

默认安装只复制用户级 Add-in、写不含实际 secret 的配置，并引用用户权限受控 token file；
不需要本地 Python Runtime。远端 endpoint 必须 HTTPS；loopback HTTP 只允许显式开发模式。
Windows/macOS 分别处理路径、ACL/0600、系统 CA/企业代理和 credential provider。

自动测试分为 schema/unit、Adapter contract、HTTPS integration、Palette/controller simulated、
facade narrow substitutes 和 regression。它们不能冒充真实 Fusion。只有 Windows 和 macOS 都
完成以下 live 链路，才能声明 production-ready：

```text
Palette prompt -> current context -> real HTTPS Agent -> typed proposal
 -> native no-write preview -> approval -> native mutation
 -> compute + health + target + Diff verification -> HTTPS report/artifact
```

截至 2026-07-17 尚无真实 Fusion Desktop 执行证据，交付状态必须写成“代码与模拟验证完成，真实平台验收待执行”。
