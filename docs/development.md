# 本地开发与项目交接

本文是当前开发环境的唯一入口。生产部署见 [`DEPLOY.md`](../DEPLOY.md)，Fusion 360 工作站安装见 [`fusion360-installation.md`](fusion360-installation.md)。

## 环境要求

- Python 3.11+
- Node.js 20+ 与 npm
- Docker Desktop 或 Podman Desktop
- 可用的 LLM provider 凭据

Docker 与 Podman 二选一。基础生成链路依赖 `cad-agent-sandbox:latest`；没有容器时可以运行大部分单元测试和浏览非生成页面，但 `/ready` 会返回 degraded，真实 CAD 生成不可用。

## 仓库结构

```text
backend/                    FastAPI、LLM、生成/修改、存储、检查和任务接口
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

运行数据默认写入 `backend/data/`，包括 SQLite 数据库和生成文件；该目录不提交 Git。

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

## 构建沙箱

Docker：

```bash
docker build -t cad-agent-sandbox:latest backend/sandbox
```

Podman：

```bash
podman machine start
podman build -t cad-agent-sandbox:latest backend/sandbox
```

Podman 同时需要在 `backend/.env` 设置：

```env
SANDBOX_RUNTIME=podman
SANDBOX_COMMAND=podman
```

## 启动开发服务

后端：

```bash
cd backend
python -m pip install -r requirements-dev.txt
python -m uvicorn app.main:app --reload --port 8000
```

前端：

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

`/health` 是进程存活检查；`/ready` 还验证模型凭据、容器守护进程和沙箱镜像。

## Windows PowerShell

先复制并填写 `backend/.env`，尤其是 `AUTH_TOKEN_SECRET` 和 LLM 凭据。然后可使用：

```powershell
.\start-web.ps1 -Runtime docker -BuildFrontend
```

Podman 改为：

```powershell
podman machine start
.\start-web.ps1 -Runtime podman -BuildFrontend
```

该脚本构建沙箱、可选构建前端并在 `http://localhost:8000` 前台启动 FastAPI。它不是 Vite 双服务开发模式；需要热更新时仍应分别运行 Uvicorn 和 `npm run dev`。

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
npm run build
```

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
- WebSocket 的消息格式、鉴权和 session/panel 数据结构是现有兼容边界。
- 上传的 Python/JavaScript generator 不能直接在宿主机执行；没有隔离 runner 时保持阻塞。
- `third_party/cadskills` 是上游快照。更新必须同时修改 [`UPSTREAM.md`](../third_party/cadskills/UPSTREAM.md) 的版本/commit，并运行上游和本项目测试。
- Fusion 模拟/窄替身测试不能写成真实 Fusion E2E 通过；人工矩阵见 [`fusion360-installation.md`](fusion360-installation.md)。

## 常见问题

### 启动时报 `Unsafe auth configuration`

`AUTH_REQUIRED=true` 时，`AUTH_TOKEN_SECRET` 不能为空或使用公开 placeholder，`AUTH_DEV_EXPOSE_CODE` 也必须为 `false`。修正 `backend/.env` 后重启。

### `/ready` 返回 `503`

按响应的 `problems` 检查 LLM 凭据、Docker/Podman daemon 和 `cad-agent-sandbox:latest`。这不是前端问题。

### 前端无法连接

先确认后端 `http://localhost:8000/health` 可访问。开发模式依赖 Vite 的 `/api`、`/ws` 代理；非同源部署应使用反向代理，不要把模型密钥放进前端环境变量。

### Fusion 功能离线

基础 Web CAD 不依赖 Fusion。Fusion 状态、安装和 `local_runtime` 诊断分别见 [`fusion360-limitations.md`](fusion360-limitations.md) 与 [`fusion360-installation.md`](fusion360-installation.md)。
