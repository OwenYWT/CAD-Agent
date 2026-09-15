# 本地开发与项目交接

本文是当前开发环境的唯一入口。生产部署见 [`DEPLOY.md`](../DEPLOY.md)，Fusion 360 工作站安装见 [`fusion360-installation.md`](fusion360-installation.md)。

2026-09-13 新版方案的本机隔离联调入口为 `http://127.0.0.1:8100/`；正式镜像无源码挂载的独立验证入口为 `http://127.0.0.1:8101/`，后者只包含该轮新建模型。进展、测试和未关闭门禁见 [本轮实施报告](qa/native-coediting-2026-09-13.md)。2026-09-09 的 `8087` 环境保留为历史验收环境，不代表新版。通用首次安装仍按下方 Compose/热更新步骤执行。

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
backend/app/freecad/        原生 typed operation、语义状态、检查与工程任务契约
backend/app/services/       云文档、协作、分支、工程证据、发布及持久派发
backend/app/workflows/      Temporal Workflow 定义
backend/app/workers/        独立 Workflow/Activity Worker
backend/app/capabilities/   CAD Skills 产品 adapter 与依赖检查
backend/app/tools/          三层 Agent 工具插件、会话工具池、权限、确认与审计执行边界
backend/app/fusion360/      Fusion typed contract、Runtime、Agent 与 APS adapter
backend/benchmark/          需要真实模型与沙箱的手动评测工具
backend/sandbox/            FreeCAD/CadQuery/ezdxf、Gmsh/CalculiX 与 CAM 隔离执行镜像
frontend/                   React/Vite/Three.js 工程工作区
fusion_addin/               Fusion 360 用户级 Add-in
schemas/fusion360/          由代码生成并校验的 Fusion JSON Schema
scripts/fusion360/          Fusion 安装、升级、Runtime 和 schema 脚本
scripts/local_bridge.py     本地发布交付客户端入口（与 API 下载的客户端复用实现）
third_party/cadskills/      固定版本的上游运行资料，不放产品逻辑
docs/                       当前开发、Fusion 专题和历史归档
```

核心运行数据保存在 PostgreSQL 和 S3 兼容对象存储：数据库保存项目、Revision、任务、事件、审计与 Artifact 元数据，对象存储保存不可变 CAD 字节和检查报告。`backend/data/` 仍可能包含未迁入核心工作流的可选 Connector/辅助能力状态，不得把这些本地文件当作核心任务事实来源。

### 验收修复后的接口约束

- 同步生成、异步生成和修改接口的 `prompt` 去除首尾空白后必须为 1–4000 字符；非法请求在 API 边界返回 422。批量请求仍逐项返回失败，不因单项无效而丢弃其他合法项。
- DXF 建模以毫米为契约，必须显式设置 `doc.units = ezdxf.units.MM`（`$INSUNITS=4`）；错误或未声明单位不会通过生成/几何门禁。修改 `backend/sandbox` 后必须重新构建执行镜像，重启 API 本身不会更新沙箱源码。
- 能力目录的 `dependencies[].available` 为 `true`（已满足）、`false`（已知缺失）或 `null`（执行时检查）。目录 GET 不访问外部服务，未探测网络不会永久禁用 action；设备配置、授权和执行时检查仍然生效。
- 原生模型的 `validation.gates` 与旧网格测量是不同证据。缺失 `is_watertight` 不代表不闭合，必需门禁通过也不代表视觉/DFM 参考检查通过。
- 历史版本差异优先比较不可变 Artifact 的 SHA-256 与结构化参数的稳定名称、值和单位，不以下载 URL 变化代表文件内容变化。原生产物差异以 `类型:文件名` 为稳定标识，新增同类型报告不会改变旧文件的比较标识。没有源码或缺少文件/参数/零件证据时，对应差异字段省略，前端显示无法比较，不能将缺失数据当作“无变化”；检查摘要使用实际门禁，不用任务成功代替检查通过。
- 原生历史恢复通过会话 WebSocket 的 `restore_revision` 提交 `source_revision_id` 和完整 Durable identity（含当前 `expected_base_revision_id`、幂等键）。后端只接受当前工程/面板内的历史版本，自己解析不可变 FCStd 及 SHA-256；历史来源与当前分支头分别保存。恢复沿用 V2 计划确认、沙箱打开/重算/导出、必需几何检查、候选审查及提交流程，不自动修复或重写历史模型，不覆盖历史文件，不直接移动分支头。无 FCStd 的源码历史仍沿用代码执行；无文件也无源码的空版本明确拒绝。旧的直接恢复 REST 接口继续返回 410。
- Geometry IR 与验证目标共享稳定特征编号：Durable 计划沿用 `step_key`，旧版描述生成确定性编号；长编号及派生草图/验证编号需满足 120 字符上限。通用拓扑契约与 FreeCAD 原生选择器并存，不能用其中一套覆盖另一套。
- 前端待确认请求按账号、会话、面板隔离；切换面板或明确接收新的请求预填会重置尚未发送的组件内草稿。历史补证优先核对不可变版本/任务身份，只有无持久身份的旧记录可按相同代码回退。带 Change Set 的结果不能单独推进分支头，以快照和提交状态为准。
- 新增 `0026`/`0027` 迁移分别固定候选原始代次和持久场景任务。手动参数/草图草稿固定首次编辑基线并受离开保护；受理响应不确定时沿用原键查询/重试。自然语言指代须携带可信版本目标，子元素只支持现有已实测圆角/倒角范围。统一版本视图、异步 scene 和恢复演练边界见 [云文档调用链](cloud-documents.md)。

## 首次配置

```bash
cp backend/.env.example backend/.env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

必须先把随机值写入 `AUTH_TOKEN_SECRET`。然后选择一种模型配置：

- 默认 Moonshot：填写 `MOONSHOT_API_KEY`。
- Azure OpenAI：设置 `LLM_PROVIDER=azure`、endpoint、key、API version 和部署名。
- 其他 OpenAI-compatible：设置 `LLM_PROVIDER=openai_compatible`、base URL、key 和 model。

`PLANNER_MAX_TOKENS` 控制需求规划和装配拆解的单次输出上限，默认 8192，允许 2048–32768。装配拆解最多调用两次；校验失败时保留原需求并反馈具体原因，耗尽重试后 Durable 任务失败，不用单零件占位结果代替装配方案。若详情显示 `assembly planner output was truncated`，检查实际配置及模型服务支持的输出上限；增加额度不能保证任意需求都能生成有效方案。

CadQuery 外观修复的分析和代码回复也使用该上限，并通过已有模型适配器流式接收，避免长回复生成期间一直等不到响应头。空回复或截断回复明确失败；流中断不会当作完整代码，网络无数据超时和 Temporal Activity 总时限仍然生效。

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

Runtime 的 Python、FreeCAD、CadQuery、build123d、OCP、Gmsh、CalculiX、Node 和上游 CAD Skills 版本由 `backend/sandbox/runtime-lock.json`、Dockerfile 及固定依赖锁定。生产镜像必须使用 OCI digest；本地 tag 只允许开发环境。

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
export OBJECT_STORE_ENDPOINT_URL='http://127.0.0.1:59000'
export OBJECT_STORE_ACCESS_KEY="$MINIO_ROOT_USER"
export OBJECT_STORE_SECRET_KEY="$MINIO_ROOT_PASSWORD"
export OBJECT_STORE_BUCKET='cad-agent-artifacts'
export TEMPORAL_TARGET='127.0.0.1:57233'
export TEMPORAL_TASK_QUEUE='cad-agent-mcad'
export TEMPORAL_AGENT_V2_TASK_QUEUE='cad-agent-mcad-v2'
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

访问 `http://localhost:5173`。Vite 将 `/api` 代理到 `http://localhost:8000`，将 `/ws` 代理到 `ws://localhost:8000`；`/api` 同时支持云文档 WebSocket。独立验收环境可使用 `CAD_API_TARGET=http://127.0.0.1:8017 npm run dev -- --port 5179` 指向隔离 API。

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
APP_ENVIRONMENT=test DURABLE_CONTROL_PLANE_ENABLED=false \
python -m pytest -m "not docker and not llm and not fusion_e2e" -q
```

这组命令与 CI 的隔离回归环境一致，只在测试进程中关闭外部控制平面依赖。实际启动和下方真实服务验收必须启用 Durable 控制平面；不能用这一测试配置启动产品。

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
TEMPORAL_AGENT_V2_TASK_QUEUE='cad-agent-m1-agent-v2-test' \
OBJECT_STORE_ENDPOINT_URL='http://127.0.0.1:59000' \
OBJECT_STORE_ACCESS_KEY='<test-access-key>' \
OBJECT_STORE_SECRET_KEY='<test-secret>' \
OBJECT_STORE_BUCKET='cad-agent-artifacts' \
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

V1 和 V2 测试队列必须彼此不同，也必须与持续运行的开发 Worker 队列不同；否则已有 Worker 的长轮询可能抢占测试 Workflow/Activity，造成无法复现的超时。并行运行多组真实集成测试时，每组还应使用不同的队列名。

API 真实生成门槛需显式设置 `CAD_AGENT_TEST_LLM=1`；V2 provider 专项分别使用 `CAD_AGENT_TEST_REAL_LLM=1` 和 `CAD_AGENT_TEST_REAL_VISION=1`。三者都要求有效 provider 凭据；不设置时会明确跳过，不用固定返回替代。完整数据库与服务回归应执行 `tests/postgres tests/integration`，并使用独立测试数据库、bucket 和队列。测试 fixture 会清理测试数据，不得指向日常项目数据库。

端到端脚本位于 `backend/tests/e2e`。`cloud_document_acceptance.py` 创建真实测试账号和原生文档，后续浏览器脚本读取它保存的权限为 `0600` 的私有会话文件。通过 `CAD_NATIVE_E2E_URL`、`CAD_NATIVE_E2E_WEB`、`CAD_NATIVE_E2E_ENV`、`CAD_NATIVE_E2E_PRIVATE` 和报告路径指定隔离环境。私有会话包含随机测试凭据，不能提交或贴入日志。

五状态验收使用 `task_state_browser.py`（`CAD_TASK_STATE_MODE=candidate` 或 `generation`）、`task_state_controls.py`、`task_state_coedit.py`、`task_state_boundaries.py`，另指定新的私有 `CAD_TASK_STATE_REPORT_DIR`。Controls 需要真实 `CAD_QUOTA_WORKFLOW` 和已封存的 `CAD_EVIDENCE_WORKFLOW`；共同编辑需要 `CAD_EVIDENCE_WORKFLOW`；边界测试需要空文档失败的 `CAD_PHONE_WORKFLOW` 与已保存的 `CAD_SAVED_WORKFLOW`。实际 Provider 生成默认等待 900 秒，可用 `CAD_TASK_STATE_TIMEOUT` 配置；失败后默认记录真实结果，只有显式 `CAD_TASK_STATE_RETRY_FAILED_GENERATION=1` 才再次调用服务。不能通过伪造额度错误或候选来满足前置条件。当前实测与限制见 [五状态整改报告](qa/task-state-2026-09-13.md)。

`POST /api/tasks/{workflow_run_id}/retry` 接受 `idempotency_key`，保留原任务输入并重新检查权限、原始版本及选择。只支持终态 Agent 生成/自然语言修改；参数租约和版本恢复须返回对应入口。基线冲突为 409，重复幂等请求返回同一任务的实际状态。`GET /api/tasks/{workflow_run_id}/validations/{evidence_id}` 返回经权限和哈希核验的检查报告；仅当对象清单与证据被修订封存才提供版本绑定，否则明确为中间产物。生成请求的可选 `requirement_basis` 存于服务端 `operation_context`，尺寸来源是用户提供的依据，不表示独立验证。

`cloud_engineering_controls.py` 通过 `CAD_NATIVE_E2E_WORKER` 指定要停启的测试 Worker。`cloud_proxy_restart.py` 及 Bridge 重启验收需要显式指定 `CAD_NATIVE_E2E_API` 和 `CAD_NATIVE_E2E_FRONTEND`；它们会重启该 API，并验证 Nginx 未重启时的 HTTP、原会话与文档 WebSocket 恢复。`CAD_NATIVE_E2E_REQUIRE_ADDRESS_CHANGE=1` 要求实际发生容器 IP 变化，防止没有覆盖地址缓存问题却判为通过。只有隔离验收容器可用于这些故障测试。

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
- WebSocket 的消息格式、鉴权和 session/panel 数据结构是兼容边界；写请求进入 Durable Workflow，进度只来自持久事件。
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
