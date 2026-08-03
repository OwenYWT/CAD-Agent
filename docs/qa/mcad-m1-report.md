# MCAD M1 真实链路验收报告

验收日期：2026-08-03

验收范围：本地单机 M1 控制平面与 `PodmanExecutionBackend`

候选版本：`fb845e5` 及其之前的 M0/M1 提交

## 结论

当前 MCAD 核心写链路已统一到：

```text
REST / conversational WebSocket
  -> FastAPI 控制平面
  -> WorkflowRun / StepRun / ExecutionAttempt
  -> Temporal Workflow + Activity
  -> PodmanExecutionBackend
  -> 隔离 MCAD Runtime
  -> PostgreSQL 元数据 + MinIO 不可变产物
```

在本报告记录的环境中，执行、修改、人工确认、取消、检查、导出、Revision 提交、断线回放、API/Worker 重启恢复和幂等重放均使用真实 PostgreSQL、MinIO、Temporal 与 Podman 完成，没有用 Mock、固定成功返回或仅前端状态替代外部链路。

## 测试环境

- macOS / Apple Silicon，Podman machine
- PostgreSQL 16
- MinIO（S3 API）
- Temporal Server 1.29
- 固定依赖的统一 MCAD OCI Runtime
- FastAPI、独立 Temporal Worker、React/Vite 前端
- 浏览器自动化视口：1440×900、1024×768、375×812

测试凭据只在本地环境注入，不记录在本文或仓库。

## 自动化结果

### 代码回归

| 范围 | 命令/结果 |
| --- | --- |
| 后端完整测试 | `python -m pytest -q`：`1040 passed, 89 skipped` |
| 前端 lint | `npm run lint`：通过 |
| TypeScript | `npx tsc --noEmit -p tsconfig.app.json`：通过 |
| 前端生产构建 | `npm run build`：通过 |
| 前端契约测试 | `node --test --experimental-strip-types tests/*.test.ts`：`39 passed` |

89 个普通测试 skip 来自按依赖/平台显式隔离的专项场景，不被统计为已验证能力。生产构建仍报告 Three.js Viewer chunk 大于 500 kB；它已动态分包，不影响正确性，但属于后续性能优化项。

### 真实基础设施回归

以下五个集成文件在同一真实 PostgreSQL、MinIO、Temporal 和 Podman 环境中运行：

- `test_temporal_mcadd_workflow.py`
- `test_websocket_replay.py`
- `test_change_set_api.py`
- `test_postgres_project_files.py`
- `test_api_compatibility_matrix.py`

使用独立于持续运行开发 Worker 的 Temporal task queue，并显式启用真实 provider 测试。结果：`15 passed in 142.95s`，无 skip。

其中自然语言用例真实完成了 Planner/Codegen → Temporal Workflow/Activity → Podman → PostgreSQL/MinIO 全链路，并返回可读取的 STEP、STL、生成代码、计划、持久事件和成功执行记录；没有以确定性代码、Mock 或固定成功返回替代模型调用。

真实回归覆盖：

- 规划/建模/验证/确认/导出步骤及事件顺序；
- 用户拒绝确认时不推进 branch head；
- 任务取消停止执行且不产生成功 Artifact；
- 用户代码失败保持失败，不伪造成功；
- stale `expected_base_revision_id` 在执行前持久失败；
- Worker 进程崩溃后创建新的 fenced `ExecutionAttempt` 并恢复；
- WebSocket 鉴权、断线重连、游标回放、慢消费者与 retention；
- Change Set 接受、拒绝、请求修改、取消及 Revision 提交；
- 产物本地临时文件删除后仍可从对象存储下载；
- REST execute、幂等 replay、下载、review 和 stale-base 兼容矩阵；
- 工程检查独立 Workflow、报告下载、SHA-256 与幂等；
- conversational WebSocket 只提交一次并在断线后回放。

## 手动真实链路

使用确定性 CadQuery 代码创建了 32×24×12 mm 立方体：

- 生成 STEP 和 STL；
- PostgreSQL 持久化三层任务记录、事件、Revision 和 Artifact 元数据；
- MinIO 保存不可变字节，下载内容和元数据一致；
- 接受 Change Set 后推进 branch head；
- 停止并重启 FastAPI 与 Worker 后，任务快照、历史消息和文件仍可查询；
- 重启后再次运行工程检查，STEP 精确分析得到 6 个面和 9216 mm³ 体积；
- 重复相同检查请求复用同一 Workflow/Attempt/Artifact，没有重复写入。

`GET /ready` 在服务恢复后返回 200，并同时确认 PostgreSQL、对象存储、Temporal、Workflow poller、Activity poller、LLM 配置和 Runtime。`/health` 不替代该检查。

## 前端与响应式验证

- 初始 Prompt 页、历史项目、项目流程、机械设计和工程检查弹窗均通过真实浏览器操作。
- 机械工作区真实请求 STL：`GET /api/files/{workflow}/model-result.stl` 返回 200 和实际二进制。
- 浏览器运行环境没有 WebGL 时，界面明确显示“STL 文件已成功读取，但 WebGL 不可用”，没有伪造模型画布。
- 工程检查由 UI 发起真实 `POST /api/analyze/{workflow}`，约 3.1 秒返回并展示真实 DFM 结果。
- 1440、1024 和 375 宽度下没有控制台错误或失败网络请求。
- 移动端项目恢复后侧栏状态实测已关闭；一次批量截图中的打开侧栏来自测试工具保留历史面板状态，不是应用缺陷。

截图证据保存在被 Git 忽略的 `.gstack/qa-reports/screenshots/`，不作为产品运行依赖。

## 验收中发现并修复的问题

| 问题 | 修复 |
| --- | --- |
| 工程检查仍在 API 进程内运行，结果没有三层任务与不可变报告 | 新增 `McadCheckWorkflow` 和真实 Activity；验证源 Artifact 哈希，通过 `ExecutionBackend` 做 STEP 分析，并提交不可变 JSON 报告 |
| Compose 只有 API，没有独立 Worker 和 migration gate | 新增 `migrate`、`workflow-worker`，API/前端按健康依赖启动 |
| Backend 镜像不含 Alembic 配置和 migration | Dockerfile 纳入 `alembic.ini` 与 `alembic/`，镜像内实际执行 migration 通过 |
| `/ready` 只验证 Temporal Server，无法发现没有消费者 | 增加 task queue 的 Workflow/Activity poller 检查和 90 秒新鲜度门槛 |
| 生产仍可关闭 durable API cutover | 非开发环境对两个 durable 开关 fail closed |
| MinIO 曾返回表面上的 403 | 定位为 Podman VM 时钟漂移导致 `RequestTimeTooSkewed`；同步时钟后恢复，并由对象存储 readiness 阻止错误接流量 |
| 崩溃恢复测试第一次被常驻开发 Worker 抢占 | 隔离测试 task consumer 后失败单项及完整整组均重新通过；产品代码无需规避正确的多 Worker 消费行为 |
| 二维 SVG 预览使用正则清洗后写入 `dangerouslySetInnerHTML`，未加引号的事件属性仍可穿透 | 改为 Blob URL 的 `<img>` 隔离上下文渲染，保留鉴权加载和缩放，并增加禁止可执行 DOM 注入及 URL 回收的回归测试 |
| 真实 LLM 用例仍断言不存在的旧事件名 `attempt.succeeded` | 与运行状态服务和前端事件适配器统一为持久事件 `attempt.completed`，并继续断言数据库 Attempt 状态为 `succeeded`；真实链路重跑通过 |
| 暂停开发 Worker 后首个测试仍可能被其未完成的长轮询抢占 | 真实集成测试改用独立 Temporal task queue；失败用例由 60 秒超时恢复为 10.10 秒通过，完整 15 项随后全部通过 |

## 尚存风险与后续门槛

1. Headless QA 环境无 WebGL；发布前仍需在有 GPU/WebGL 的 Chrome、Safari 和 Edge 检查模型旋转、适应视图、爆炸、剖切与测量。
2. 当前执行器为单机 Docker/Podman socket，适合 M1 和受信节点；它不是多租户强隔离终态。Kubernetes/gVisor 与 Private Worker 仍需按统一 ExecutionBackend 契约实现和验收。
3. Compose 的 Temporal auto-setup 不是正式生产集群方案；生产需托管 Temporal 或运维管理的高可用部署。
4. Podman VM、对象存储签名和 TLS 对系统时间敏感，应配置 NTP/时钟漂移监控。
5. 生产必须使用 Registry image digest，不能把本地可变 tag 带入 staging/production。
6. PostgreSQL 与对象存储必须联合备份、恢复演练和一致性校验；S3 Versioning 不能替代 ProjectRevision。
