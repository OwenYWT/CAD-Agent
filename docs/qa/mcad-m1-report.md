# MCAD M1 / Durable Agent V2 真实链路验收报告

真实链路验收日期：2026-08-13

最新合并回归日期：2026-08-14

验收范围：本地单机 M1 控制平面、`PodmanExecutionBackend` 与 Durable Agent V2

候选版本：分支 `codex/m1-durable-control-plane` 的 Task 12 发布候选

## 结论

当前 MCAD 写链路只有 Durable 实现：

```text
REST / conversational WebSocket
  -> FastAPI 控制平面
  -> PostgreSQL WorkflowRun / StepRun / ExecutionAttempt
  -> Temporal V1（execute / check / 既有历史）
     或 Temporal V2 Agent（generate / modify）
  -> ExecutionBackend
  -> 隔离 MCAD Runtime
  -> MinIO 不可变产物 + ProjectRevision / Change Set
```

V2 Agent 已真实覆盖规划、检索、建模、隔离执行、有限修复、几何验证、视觉验证、DFM、候选版本封存和审查。生产入口不存在进程内 Agent 回退、双写或运行时切换开关；WebSocket 进度来自持久事件。

生产代码没有 Mock、固定成功返回或静态 CAD 产物。普通单元/故障测试仍使用明确命名的测试替身；真实 PostgreSQL、MinIO、Temporal、Podman 和 provider 门槛单独运行，不把替身结果当成外部链路证据。

## 测试环境

- macOS / Apple Silicon，Podman machine
- PostgreSQL 16
- MinIO（S3 API）
- Temporal Server 1.29
- Runtime：`localhost/cad-agent-sandbox:m1-validation`
- FastAPI、独立 Temporal V1/V2 Worker、React/Vite 前端

测试凭据只由本地环境注入，不记录在本文或仓库。

## 自动化结果

### 完整代码回归

| 范围 | 结果 |
| --- | --- |
| 后端完整测试 | `1132 passed, 122 skipped` |
| PostgreSQL 模型快照与零件差异链路 | `1 passed`，使用真实 PostgreSQL |
| 前端 lint | 通过 |
| 前端生产构建 / TypeScript | 通过；仅保留 Viewer3D chunk 体积警告 |
| 前端契约测试 | `40 passed, 0 failed` |

普通测试中的 skip 均由显式依赖或平台门槛控制，不计入已验证能力。

### 真实基础设施回归

以下测试在同一真实 PostgreSQL、MinIO、Temporal 和 Podman 环境运行，并使用独立 V1/V2 task queue：

- `test_agent_candidate_seal.py`
- `test_temporal_mcadd_workflow.py`
- `test_websocket_replay.py`
- `test_change_set_api.py`
- `test_api_compatibility_matrix.py`

排除四个需额外 provider 开关的专项用例后，结果为 `33 passed, 4 deselected in 197.92s`，无 skip。真实覆盖：

- V1 执行、检查、取消、重试、Worker 恢复与 fenced Attempt；
- V2 规划确认、简单/复杂/装配建模、部分失败和取消；
- user-code、几何和视觉失败后的有限修复与重新验证；
- staging manifest、不可变验证证据、幂等候选版本封存与 orphan 清理；
- Revision、Change Set、stale base、分支 compare-and-swap；
- REST / WebSocket 鉴权、提交、断线续传、游标回放和 API 兼容；
- 对象存储下载、SHA-256、Artifact 归属和工程检查报告。

### 真实模型 Provider 门槛

四个受控真实 provider 场景均已通过：

1. 真实视觉模型判断，持久化 provider、model、response ID 和请求/响应哈希；
2. 首次 user-code 失败后调用真实修复模型，新源码和新的 ExecutionAttempt 成功；
3. 真实 Planner / Retriever / Codegen 后通过 Podman 生成 STEP/STL，并持久化完整 provenance；
4. 真实 `POST /api/generate` 经 FastAPI、V2 Temporal、真实 Planner/Codegen、Podman、PostgreSQL 和 MinIO 返回可下载的 STEP/STL；最终单项结果 `1 passed in 43.49s`。

API 门槛使用已单独验证的确定性视觉通过器，以避免重复调用视觉 provider；它没有替代真实规划、代码生成、CAD 执行、几何/DFM、持久化或对象存储。

## 本轮发现并修复的问题

| 问题 | 修复与复验 |
| --- | --- |
| REST / WebSocket 测试仍通过旧进程内 Orchestrator，无法证明生产主链路 | 测试全部迁到 Durable 提交与持久投影边界；生产 generate/modify/execute/batch/async/WebSocket 不再回退 |
| `DURABLE_API_CUTOVER_ENABLED`、`DURABLE_AGENT_FUSION_ENABLED` 已不控制行为，却仍暗示双链路 | 删除配置、Compose、环境模板、文档和旧测试中的行为开关 |
| V2 Worker 未就绪时，幂等重放也会先被 readiness 拒绝 | 幂等请求先重进持久提交边界并修复 DB→Temporal 崩溃窗口；只有新任务要求 V2 readiness |
| snapshot restore 写接口仍可触达旧状态模型 | 统一返回 `410 Gone`，不再产生非 Durable 修改 |
| 旧请求可省略项目/分支/基线身份 | 核心写请求强制完整 Durable identity，缺失返回 `422` |
| 零宽限 orphan 清理依赖应用与 MinIO 秒级时钟完全一致 | `grace_seconds=0` 不再比较跨系统时间；非零宽限仍按对象时间保护 |
| V2 验证证据文件混入产品导出 `files` 字段 | API 兼容投影只暴露 STEP/STL/DXF/SVG 等产品导出；验证证据仍保留在任务快照和事件中 |
| 真实修复测试仍预期最终失败，但工作流已正确修复成功 | 改为断言成功结果，同时继续校验失败 Attempt、修复源码链和 provider provenance |
| MinIO 返回 `RequestTimeTooSkewed` | 定位到 Podman VM 漂移约 16 分钟；同步 VM 时钟后 readiness 与全部真实链路恢复 |

所有上述代码问题修复后均已重新执行对应聚焦测试；随后完成全量后端、前端和真实基础设施回归。

## 尚存风险与后续门槛

1. 一次真实视觉 provider 调用使同步 `/api/generate` 超过 180 秒响应期限；Durable Workflow 不会丢失并可继续查询，但同步客户端可能收到 `504`。交互主链路应继续使用 WebSocket/任务订阅，并为生产 provider 建立延迟 SLO。
2. Headless 环境无 GPU/WebGL；发布前仍需在 Chrome、Safari 和 Edge 实机检查模型旋转、适应视图、爆炸、剖切与测量。
3. Podman VM、S3 签名和 TLS 对系统时间敏感；开发机和生产节点都需 NTP 与漂移监控。
4. 当前执行器适合 M1 单机受信节点，不是多租户强隔离终态。Kubernetes/gVisor 与 Private Worker 仍需按同一 `ExecutionBackend` 契约实现和验收。
5. Compose 的 Temporal auto-setup 只适用于本地/单机验证；生产需托管 Temporal 或运维管理的高可用集群。
6. Fusion 360 自动化仍缺少 Windows/macOS 真实 Fusion Desktop 验收，不能与浏览器 MCAD 主链路的通过结论混写。
7. PostgreSQL 与对象存储必须联合备份、恢复演练和一致性校验；S3 Versioning 不能替代 `ProjectRevision`。
