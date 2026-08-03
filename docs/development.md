# 本地开发与项目交接

本文是当前开发环境的唯一入口。生产部署见 [`DEPLOY.md`](../DEPLOY.md)，Fusion 360 工作站安装见 [`fusion360-installation.md`](fusion360-installation.md)。

## 环境要求

- Python 3.11+
- Node.js 20+ 与 npm
- Docker Desktop 或 Podman Desktop
- PostgreSQL 16、S3 兼容对象存储和 Temporal（可由 Compose 启动）
- 可用的 LLM provider 凭据

Docker 与 Podman 二选一。当前 MCAD 产品链路依赖独立 API、Workflow Worker、PostgreSQL、对象存储、Temporal 和 `cad-agent-sandbox:dev`；缺少其中任一执行依赖时仍可运行大部分单元测试和浏览非生成页面，但 `/ready` 返回 degraded，真实 CAD 任务不可用。

## 仓库结构

```text
backend/                    FastAPI 控制平面、工作流、执行契约、检查和任务接口
backend/alembic/            PostgreSQL schema migrations
backend/app/execution/      ExecutionBackend、ExecutionSpec 与 ExecutionResult
backend/app/workflows/      Temporal Workflow 定义
backend/app/workers/        独立 Workflow/Activity Worker
backend/app/capabilities/   CAD Skills 产品 adapter 与依赖检查
backend/app/fusion360/      Fusion typed contract、Runtime、Agent 与 APS adapter
backend/benchmark/          需要真实模型与沙箱的手动评测工具
backend/sandbox/            CadQuery/ezdxf 隔离执行镜像
frontend/                   React/Vite/Three.js 工程工作区
fusion_addin/               Fusion 360 用户级 Add-in
schemas/fusion360/          由代码生成并校验的 Fusion JSON Schema
scripts/fusion360/          Fusion 安装、升级、Runtime 和 schema 脚本
third_party/cadskills/      固定版本的上游运行资料，不放产品逻辑
docs/                       当前开发、Fusion 专题和历史归档
```

核心运行数据保存在 PostgreSQL 和 S3 兼容对象存储：数据库保存项目、Revision、任务、事件、审计与 Artifact 元数据，对象存储保存不可变 CAD 字节和检查报告。`backend/data/` 仍可能包含未迁入核心工作流的可选 Connector/辅助能力状态，不得把这些本地文件当作核心任务事实来源。

## 首次配置

```bash
cp backend/.env.example backend/.env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

必须先把随机值写入 `AUTH_TOKEN_SECRET`。然后选择一种模型配置：

- 默认 Moonshot：填写 `MOONSHOT_API_KEY`。
- Azure OpenAI：设置 `LLM_PROVIDER=azure`、endpoint、key、API version 和部署名。
- 其他 OpenAI-compatible：设置 `LLM_PROVIDER=openai_compatible`、base URL、key 和 model。

本地也不应使用空或公开的认证密钥。需要完全跳过登录时只能显式设置 `AUTH_REQUIRED=false`，且只允许在本机回环开发环境使用。

## 构建 MCAD Runtime

Docker：

```bash
docker build -f backend/sandbox/Dockerfile -t cad-agent-sandbox:dev .
```

Podman：

```bash
podman machine start
podman build -f backend/sandbox/Dockerfile -t cad-agent-sandbox:dev .
```

Podman 同时需要在 `backend/.env` 设置：

```env
SANDBOX_RUNTIME=podman
SANDBOX_COMMAND=podman
SANDBOX_IMAGE=cad-agent-sandbox:dev
```

Runtime 的 Python、CadQuery、build123d、OCP、Node 和上游 CAD Skills 版本由 `backend/sandbox/runtime-lock.json` 及固定依赖锁定。生产镜像必须使用 OCI digest；本地 tag 只允许开发环境。

## 启动开发服务

### 方式 A：完整 Compose

这是验证部署拓扑的最短路径。先按 [DEPLOY.md](../DEPLOY.md) 设置 Compose 所需的数据库/对象存储环境变量，然后：

```bash
docker compose config --quiet
docker compose up -d --build
```

访问 `http://localhost:8080`。

### 方式 B：基础设施 + 热更新进程

先启动本地基础设施并执行 migration：

```bash
export POSTGRES_PASSWORD='<local-password>'
export MINIO_ROOT_USER='<local-access-key>'
export MINIO_ROOT_PASSWORD='<local-secret>'
export DATABASE_URL='postgresql+asyncpg://cad_agent:<url-encoded-password>@postgres:5432/cad_agent'
docker compose up -d postgres minio minio-init temporal

cd backend
export DATABASE_URL='postgresql+asyncpg://cad_agent:<url-encoded-password>@127.0.0.1:55432/cad_agent'
export DURABLE_CONTROL_PLANE_ENABLED=true
export DURABLE_API_CUTOVER_ENABLED=true
export OBJECT_STORE_ENDPOINT_URL='http://127.0.0.1:59000'
export OBJECT_STORE_ACCESS_KEY="$MINIO_ROOT_USER"
export OBJECT_STORE_SECRET_KEY="$MINIO_ROOT_PASSWORD"
export OBJECT_STORE_BUCKET='cad-agent-artifacts'
export TEMPORAL_TARGET='127.0.0.1:57233'
alembic upgrade head
```

把 `cd backend` 之后的 host-mode 变量同步写入本地 `backend/.env`（不要提交），或在启动 Uvicorn 和 Worker 的每个终端重复导出；两者必须使用完全相同的数据库、对象存储、Temporal 和 task queue。一个终端启动 Worker：

```bash
cd backend
python -m app.workers.workflow_worker
```

另一个终端启动 API：

```bash
cd backend
python -m pip install -r requirements-dev.txt
python -m uvicorn app.main:app --reload --port 8000
```

第三个终端启动前端：

```bash
cd frontend
npm ci
npm run dev
```

访问 `http://localhost:5173`。Vite 将 `/api` 代理到 `http://localhost:8000`，将 `/ws` 代理到 `ws://localhost:8000`。

检查：

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

`/health` 是 API 进程存活检查；`/ready` 还验证 PostgreSQL、对象存储、Temporal、Workflow/Activity poller、模型凭据、容器守护进程和 Runtime 镜像。仅启动 Uvicorn 时 Worker probe 会失败，这是正确的不可接流量状态。

## Windows PowerShell

`start-web.ps1` 只启动旧的单进程辅助路径，不包含 PostgreSQL、对象存储、Temporal 和独立 Worker，不能验收当前持久 MCAD 链路。Windows 开发应先按上述方式启动基础设施与 Worker，再分别运行 Uvicorn 和 Vite。该脚本仅用于不执行持久任务的辅助页面调试。

## 验证命令

后端回归：

```bash
cd backend
python -m pytest
```

前端静态检查和构建：

```bash
cd frontend
npm run lint
npx tsc --noEmit -p tsconfig.app.json
npm run build
node --test --experimental-strip-types tests/*.test.ts
```

需要 PostgreSQL、MinIO、Temporal 和 Podman/Docker 的真实 M1 回归：

```bash
cd backend
CAD_AGENT_TEST_DATABASE_URL='postgresql+asyncpg://...' \
CAD_AGENT_TEST_OBJECT_STORE=1 \
CAD_AGENT_TEST_TEMPORAL=1 \
TEMPORAL_TARGET='127.0.0.1:57233' \
TEMPORAL_TASK_QUEUE='cad-agent-m1-test' \
OBJECT_STORE_ENDPOINT_URL='http://127.0.0.1:59000' \
SANDBOX_RUNTIME=podman \
SANDBOX_COMMAND=podman \
SANDBOX_IMAGE='cad-agent-sandbox:dev' \
python -m pytest -q \
  tests/integration/test_temporal_mcadd_workflow.py \
  tests/integration/test_websocket_replay.py \
  tests/integration/test_change_set_api.py \
  tests/integration/test_postgres_project_files.py \
  tests/integration/test_api_compatibility_matrix.py
```

测试队列必须与持续运行的开发 Worker 队列不同；否则已有 Worker 的长轮询可能抢占测试 Workflow/Activity，造成无法复现的超时。并行运行多组真实集成测试时，每组还应使用不同的测试队列名。

真实 LLM 测试还需显式设置 `CAD_AGENT_TEST_LLM=1` 和有效 provider 凭据；不设置时会明确跳过，不用固定返回替代。最近一次完整 M1 验收见 [`qa/mcad-m1-report.md`](qa/mcad-m1-report.md)。

Fusion 专项：

```bash
cd backend
PYTHONPATH=.. python -m pytest tests/fusion360

cd ..
python scripts/fusion360/generate_schema.py --check
python -m compileall -q backend/app/fusion360 fusion_addin/CADAgentFusionConnector scripts/fusion360
```

评测 harness 需要真实 LLM key 和沙箱，不进入普通 PR CI，按 [`backend/benchmark/README.md`](../backend/benchmark/README.md) 执行。

## 数据与安全边界

- 组件内不要新增散落的后端请求；前端 API 调用集中在 service/adapter/hook 层。
- 业务层只能通过 `ExecutionBackend` 提交 `ExecutionSpec`，不得直接调用 Docker、Podman、Kubernetes 或宿主 shell。
- WebSocket 的消息格式、鉴权和 session/panel 数据结构是兼容边界；它只订阅持久事件。
- 核心写操作必须携带幂等键；修改必须携带 `expected_base_revision_id`。
- Artifact 必须使用不可变对象路径并记录 SHA-256、来源 Workflow/Attempt、Runtime 版本和创建时间。
- 上传的 Python/JavaScript generator 不能直接在宿主机执行；没有隔离 runner 时保持阻塞。
- `third_party/cadskills` 是上游快照。更新必须同时修改 [`UPSTREAM.md`](../third_party/cadskills/UPSTREAM.md) 的版本/commit，并运行上游和本项目测试。
- Fusion 模拟/窄替身测试不能写成真实 Fusion E2E 通过；人工矩阵见 [`fusion360-installation.md`](fusion360-installation.md)。

## 常见问题

### 启动时报 `Unsafe auth configuration`

`AUTH_REQUIRED=true` 时，`AUTH_TOKEN_SECRET` 不能为空或使用公开 placeholder，`AUTH_DEV_EXPOSE_CODE` 也必须为 `false`。修正 `backend/.env` 后重启。

### `/ready` 返回 `503`

按响应的 `problems` 检查 PostgreSQL、对象存储（含主机时钟）、Temporal、Worker poller、LLM 凭据、Docker/Podman daemon 和 Runtime digest/tag。这不是前端问题。

### 前端无法连接

先确认后端 `http://localhost:8000/health` 可访问。开发模式依赖 Vite 的 `/api`、`/ws` 代理；非同源部署应使用反向代理，不要把模型密钥放进前端环境变量。

### Fusion 功能离线

基础 Web CAD 不依赖 Fusion。Fusion 状态、安装和 `local_runtime` 诊断分别见 [`fusion360-limitations.md`](fusion360-limitations.md) 与 [`fusion360-installation.md`](fusion360-installation.md)。
