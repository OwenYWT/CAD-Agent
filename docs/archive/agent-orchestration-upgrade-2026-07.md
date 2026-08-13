# Agent 编排升级方案（历史归档）

> 状态：已废弃的 2026-07 比较稿；最后复核：2026-08-13。

本文最初比较了 CADAM 风格的显式 Agent 状态与当时的进程内
`Orchestrator + SQLite + WebSocket` 链路。它促成了以下产品原则：

- 规划、工具调用、结果、修复和确认必须是可观察步骤；
- 消息、运行、步骤、产物和执行尝试需要稳定身份；
- 刷新、断线和服务重启后必须恢复任务与事件；
- Agent 不能直接拥有任务生命周期或绕过隔离执行。

原文中的当前架构、分阶段实施清单、SQLite 主链路和进程内恢复方案均已失效，不能用于开发或部署。当前实现统一为：

```text
REST / WebSocket
  -> FastAPI 控制平面
  -> PostgreSQL + Temporal
  -> WorkflowRun / StepRun / ExecutionAttempt
  -> ExecutionBackend
  -> 隔离 MCAD Runtime
  -> 不可变 Artifact + ProjectRevision
```

生成和修改使用 V2 Durable Agent 工作流，覆盖规划、建模、隔离执行、有限修复、几何/视觉/DFM 验证和候选版本封存；V1 仅保留既有历史兼容与执行/检查流程。生产环境没有进程内写回退，也没有双写开关。

当前事实来源：

- [Durable Agent Fusion Design](../superpowers/specs/2026-08-12-durable-agent-fusion-design.md)
- [MCAD M1/V2 真实链路验收](../qa/mcad-m1-report.md)
- [本地开发与交接](../development.md)
- [部署与上线检查](../../DEPLOY.md)

需要原始比较稿时请使用 Git 历史。
