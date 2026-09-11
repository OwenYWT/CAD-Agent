# WordsWave CAD Agent

面向硬件工程的 AI Engineering Workspace。用户通过自然语言创建或修改 CAD 设计，系统通过 REST 与 WebSocket 展示任务进度、模型、参数、工程检查、历史记录和可下载文件。

当前产品以浏览器工作区为主，不依赖桌面 CAD 插件完成基础生成流程。Autodesk Fusion 360 Connector 是独立的可选集成；其自动化合同和模拟链路已验证，真实 Windows/macOS Fusion 验收仍是上线前置条件。

## 当前能力

- 云文档特征树、参数编辑、已提交版本同步，以及带权限控制的共享审阅、评论与在线状态。实现范围与调用链见 [云文档](docs/cloud-documents.md)。
- 特征意图与按需内核查询、编辑租约和独立参数重放、实例化/LOD 网格缓存、文档分支与审查合并、草图尺寸预览及原生约束提交。
- 原生有限元静力分析与 2.5D 外轮廓加工、结果可视化及修订绑定的 Agent 工程上下文，见 [工程计算](docs/engineering-compute.md)。
- 命名发布、真实原生 BOM 与 CAD/工程证据打包，以及可配对、撤销、恢复的本地文件 Bridge，见 [发布与本地交付](docs/releases-and-local-bridge.md)。
- 自然语言生成、修改和执行 CadQuery / ezdxf 设计。
- Three.js 3D 预览、2D 预览、参数调整、DFM/几何检查和文件下载。
- 装配零件清单、零件级修改、模型快照与版本差异；恢复旧版本会重新进入 Durable 审查流程。
- 登录鉴权、邀请码、项目历史、任务进度、文件归属和 WebSocket 实时状态。
- STEP、STL、DXF、SVG、PNG 等输出，具体格式取决于生成类型和实际产物。
- 11 项固定版本的 CAD Skills 工作流，以及显式的运行依赖/阻塞状态。
- 可选 Fusion 360 typed action、Preview、Approval、执行报告和导出链路。
- 独立三层工具插件运行时，支持插件元数据、会话级启停、权限范围、限流、结构化确认和审计。

未配置的外部运行时不会被伪装成可用：缺少容器、切片器、Gazebo/MoveIt、Implicit CAD 依赖、隔离执行器或打印机授权时，对应能力返回明确的 `blocked` 状态。

本轮融合内容、完整回归、真实调用链证据及本机运行版本见 [2026-09-09 融合验收与交接](docs/qa/cloud-cad-fusion-2026-09-09.md)。

## 架构

```text
React / TypeScript / Three.js
        │ REST + WebSocket
        ▼
FastAPI 控制平面
   │
   ├── PostgreSQL：项目、文档、版本、任务、协作、持久派发与产物元数据
   ├── Temporal V1：执行、检查与既有 Workflow 历史
   ├── Temporal V2 Agent：规划 → 建模 → 隔离执行 → 修复 → 几何/视觉/DFM → 候选版本封存
   ├── S3 兼容对象存储：不可变 CAD 产物、日志与检查报告
   ├── Agent Tools：basic / capability / business 插件、权限与确认策略
   └── ExecutionBackend
          └── Docker / Podman 隔离 FreeCAD / CadQuery / Gmsh / CalculiX Worker
```

所有生成、修改、执行和 WebSocket 写请求都进入 Durable 主链路，不存在进程内回退。WebSocket 负责提交请求、订阅和回放持久任务事件，不承担任务生命周期。浏览器断线、API 重启或 Worker 重试不会覆盖已有运行记录；修改通过 `expected_base_revision_id` 防止并发覆盖。配置的 LLM provider、CAD Skills adapter 和可选 Fusion 360 / APS Connector 位于上述控制平面边界之外。

主要技术栈：Python 3.11+、FastAPI、React 19、TypeScript、Vite、Tailwind CSS、Three.js、Zustand、PostgreSQL、Temporal、S3/MinIO、Docker/Podman。

## 本地启动

最短可复现路径使用 Docker Compose。前置条件：Python 3.11+、Node.js 20+、npm、Docker Engine 与 Compose。

1. 创建后端配置，并至少设置模型凭据和认证密钥：

```bash
cp backend/.env.example backend/.env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

把输出写入 `backend/.env` 的 `AUTH_TOKEN_SECRET`，再按所选 provider 填写 `MOONSHOT_API_KEY`、Azure OpenAI 或其他 OpenAI-compatible 配置。生产环境还必须通过 `DEFAULT_INVITE_CODES` 配置私有邀请码并限制 CORS；完整要求见 [部署指南](DEPLOY.md)。

2. 从仓库根目录构建 CAD 沙箱：

```bash
docker build -f backend/sandbox/Dockerfile -t cad-agent-sandbox:dev .
```

本地使用 Podman 时把命令中的 `docker` 替换为 `podman`，并在 `backend/.env` 设置 `SANDBOX_RUNTIME=podman`、`SANDBOX_COMMAND=podman`。生产环境必须把 Runtime 推送到 Registry，并使用 `registry/path@sha256:<digest>`。

3. 为 Compose 设置数据库和对象存储凭据，再启动完整控制平面：

```bash
export POSTGRES_PASSWORD='<strong-database-password>'
export DATABASE_URL='postgresql+asyncpg://cad_agent:<url-encoded-password>@postgres:5432/cad_agent'
export MINIO_ROOT_USER='<object-store-access-key>'
export MINIO_ROOT_PASSWORD='<strong-object-store-secret>'
docker compose config --quiet
docker compose up -d --build
```

访问 `http://localhost:8080`。`GET /health` 只表示 API 进程在线；`GET /ready` 返回 `ready` 才表示 PostgreSQL、对象存储、Temporal、Workflow/Activity poller、模型凭据和 MCAD Runtime 均可用。

需要 Vite 热更新或 Podman 本地开发时，按 [本地开发与交接](docs/development.md) 分别启动基础设施、数据库迁移、Workflow Worker、Uvicorn 和 Vite。只启动 Uvicorn 不能执行当前持久化 MCAD 工作流。

## CAD Skills

11 项上游工作流固定在 `third_party/cadskills`，版本为 `0.3.9`、commit 为 `fdbb4b4fb62d95ae298cfe9a46fdc7092bdaf423`。产品 adapter 位于 `backend/app/capabilities`，主要接口为：

- `GET /api/capabilities`
- `POST /api/capability-artifacts`
- `POST /api/capability-actions/{capability_id}/{action_id}`
- `GET /api/capability-artifacts/{scope}/{request_id}/{filename}`

上游来源、许可和更新流程见 [third_party/cadskills/UPSTREAM.md](third_party/cadskills/UPSTREAM.md)。不要直接在 vendored 目录写产品逻辑。

## 文档

- [文档索引](docs/README.md)
- [本地开发与交接](docs/development.md)
- [部署与上线检查](DEPLOY.md)
- [评测基线](backend/benchmark/README.md)
- [Fusion 360 文档入口](docs/README.md#fusion-360-connector)

一次性实施计划、重复的中英文使用说明和旧 Sprint 文档不再作为当前事实来源。历史产品想法只保留在明确标注的 [ForgeCAD 路线归档](docs/archive/forgecad-roadmap-2026-07.md) 中。
