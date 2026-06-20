# CAD Agent Web 交接说明

## 1. 项目简介

CAD Agent Web 是一个浏览器版 CAD 生成工具。用户在网页输入自然语言需求，后端调用大模型生成 CAD 脚本，再通过 Podman/Docker 沙箱执行脚本并产出 STEP、STL、DXF、SVG 等文件。

当前交接版本是纯网页版，不需要安装任何 CAD 插件。

## 2. 交接包目录

```text
backend/                FastAPI 后端、LLM 调用、沙箱执行、文件下载接口
backend/sandbox/        Podman/Docker 沙箱镜像构建文件
frontend/               React + Vite 前端
frontend/dist/          已构建好的网页产物，可由后端直接托管
backend/.env.example    后端环境变量模板
frontend/.env.example   前端环境变量模板
HANDOFF.md              交接说明
USAGE.md                使用方法
使用方法.md             中文文件名副本
交接说明.md             中文文件名副本
start-web.ps1           Windows 启动辅助脚本
setup.sh                Bash 启动辅助脚本
```

## 3. 环境要求

- Windows 10/11 + PowerShell
- Python 3.11+
- Node.js 20+ / npm
- Podman Desktop 或 Docker Desktop 二选一
- Azure OpenAI Key，推荐模型部署名：`gpt-5`

## 4. 安装 Podman（不能安装 Docker 时使用）

Podman 在 Windows 上通过 Podman Machine 运行容器。推荐安装 Podman Desktop。

### 4.1 图形化安装

1. 打开 `https://podman-desktop.io/`
2. 下载 Windows 安装包并安装。
3. 打开 Podman Desktop。
4. 按界面提示初始化 Podman Machine。
5. 打开 PowerShell 验证：

```powershell
podman --version
podman machine list
podman machine start
podman info
```

`podman info` 能正常输出信息，就说明 Podman 可用。

### 4.2 winget 安装

如果电脑支持 `winget`，可执行：

```powershell
winget install -e --id RedHat.Podman-Desktop
```

安装完成后执行：

```powershell
podman machine init
podman machine start
podman info
```

如果提示 Podman Machine 已存在，忽略 `init`，直接执行：

```powershell
podman machine start
```

### 4.3 Podman 常见问题

- `podman` 不是内部或外部命令：重启 PowerShell，或确认 Podman 已加入 PATH。
- `podman machine start` 失败：确认 BIOS/系统虚拟化、WSL2 或 Hyper-V 可用。
- 拉取镜像失败：检查公司网络/代理。本项目沙箱基础镜像已改为 DaoCloud 镜像源。
- 第一次构建慢：沙箱会安装 Conda/CAD 依赖，首次耗时较长，后续会复用缓存。

## 5. Docker 安装与使用（可替代 Podman）

如果同事电脑可以安装 Docker，也可以直接使用 Docker Desktop。Docker 和 Podman 二选一即可，不需要同时安装。

### 5.1 安装 Docker Desktop

方式 A：官网下载

1. 打开 Docker Desktop 官网：`https://www.docker.com/products/docker-desktop/`
2. 下载 Windows 安装包并安装。
3. 安装完成后启动 Docker Desktop。
4. 等待左下角或状态栏显示 Docker Engine 正常运行。
5. PowerShell 验证：

```powershell
docker --version
docker info
```

方式 B：使用 winget

```powershell
winget install -e --id Docker.DockerDesktop
```

安装完成后重启 PowerShell，再验证：

```powershell
docker --version
docker info
```

### 5.2 后端 `.env` 使用 Docker 的配置

如果选择 Docker，把 `backend/.env` 中沙箱相关配置改成：

```env
SANDBOX_RUNTIME=docker
SANDBOX_COMMAND=
SANDBOX_IMAGE=cad-agent-sandbox:latest
```

保留 Azure 配置不变：

```env
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://secalgo-azure-openai.openai.azure.com/
AZURE_OPENAI_API_KEY=<AZURE_OPENAI_API_KEY>
AZURE_OPENAI_API_VERSION=2025-03-01-preview
LLM_MODEL=gpt-5
LLM_REASONING_EFFORT=minimal
```

### 5.3 用 Docker 构建沙箱镜像

```powershell
cd "项目根目录\backend\sandbox"
docker build -t cad-agent-sandbox:latest .
docker images cad-agent-sandbox
```

### 5.4 用 Docker 启动项目

后端：

```powershell
cd "项目根目录\backend"
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

前端开发模式：

```powershell
cd "项目根目录\frontend"
npm.cmd ci
npm.cmd run dev
```

浏览器打开 `http://localhost:5173`。

### 5.5 Docker 常见问题

- `Docker daemon unavailable`：Docker Desktop 没启动，或 Docker Engine 还没运行完成。
- `docker` 不是内部或外部命令：重启 PowerShell，或确认 Docker Desktop 已安装并加入 PATH。
- `Sandbox image 'cad-agent-sandbox:latest' not found`：还没执行 `docker build -t cad-agent-sandbox:latest .`。
- 公司网络拉镜像失败：检查代理。当前沙箱基础镜像已使用 DaoCloud 镜像源。
## 6. 环境配置

### 6.1 后端配置：`backend/.env`

后端配置大模型、沙箱和存储。第一次启动前复制模板：

```powershell
cd "项目根目录"
Copy-Item backend\.env.example backend\.env
```

编辑 `backend/.env`，至少确认以下配置：

```env
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://secalgo-azure-openai.openai.azure.com/
AZURE_OPENAI_API_KEY=<AZURE_OPENAI_API_KEY>
AZURE_OPENAI_API_VERSION=2025-03-01-preview
LLM_MODEL=gpt-5
LLM_REASONING_EFFORT=minimal
SANDBOX_RUNTIME=podman
SANDBOX_COMMAND=podman
SANDBOX_IMAGE=cad-agent-sandbox:latest
```

说明：

- `AZURE_OPENAI_API_KEY` 必须由接收同事自己填写真实 Key。
- `LLM_MODEL` 在 Azure OpenAI 中通常是“部署名”，不是模型家族名；如果 Azure 部署名不是 `gpt-5`，这里要改成实际部署名。
- 不要把真实 `backend/.env` 发到群里或提交 Git。
- 不要把 Azure 配置放到 `frontend/.env`，前端不读取这些变量。

### 6.2 前端配置：`frontend/.env`

开发模式通常不需要前端配置，因为 Vite 已代理：

- `/api` -> `http://localhost:8000`
- `/ws` -> `ws://localhost:8000`

如果需要创建前端配置：

```powershell
Copy-Item frontend\.env.example frontend\.env
```

内容保持这样即可：

```env
VITE_API_BASE=
VITE_API_TOKEN=
```

只有当前后端不在同一个地址且没有代理时，才设置：

```env
VITE_API_BASE=http://localhost:8000
```

## 7. 构建沙箱镜像

Podman：

```powershell
cd "项目根目录\backend\sandbox"
podman machine start
podman build -t cad-agent-sandbox:latest .
podman images cad-agent-sandbox
```

Docker：

```powershell
cd "项目根目录\backend\sandbox"
docker build -t cad-agent-sandbox:latest .
docker images cad-agent-sandbox
```

## 8. 启动方式 A：开发模式

### 8.1 启动后端

```powershell
cd "项目根目录\backend"
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

检查：

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/ready
```

- `/health` 返回 `ok`：服务在线。
- `/ready` 返回 `ready`：Key 和沙箱检查通过。
- `/ready` 返回 `degraded`：按 `problems` 提示处理。

### 8.2 启动前端

另开一个 PowerShell：

```powershell
cd "项目根目录\frontend"
npm.cmd ci
npm.cmd run dev
```

浏览器打开：

```text
http://localhost:5173
```

## 9. 启动方式 B：单服务模式

适合演示或交给同事直接使用：前端先构建，然后只启动后端。

```powershell
cd "项目根目录\frontend"
npm.cmd ci
npm.cmd run build

cd "项目根目录\backend"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

浏览器打开：

```text
http://localhost:8000
```

## 10. 快速脚本

Windows：

```powershell
cd "项目根目录"
.\start-web.ps1 -Runtime podman -BuildFrontend
```

Bash：

```bash
cd "项目根目录"
bash setup.sh
```

## 11. 常见错误

### 11.1 `DASHSCOPE_API_KEY is required for LLM operations`

说明后端没有正确读取 Azure 配置。检查：

1. 配置是否写在 `backend/.env`，不是 `frontend/.env`。
2. `backend/.env` 第一行是否是 `LLM_PROVIDER=azure`。
3. 后端是否已重启。
4. 当前版本代码是否包含 Azure 修复。

可验证：

```powershell
cd "项目根目录\backend"
$env:PYTHONPATH="."
python -c "from app.config import settings, make_llm_client; print(settings.normalized_llm_provider, settings.has_llm_credentials, type(make_llm_client().raw_client).__name__)"
```

正常输出应包含：

```text
azure True AsyncAzureOpenAI
```

### 11.2 `Incorrect API key` / `401`

说明 Azure Key、Endpoint、API Version 或部署名不匹配。检查 `backend/.env`：

```env
AZURE_OPENAI_ENDPOINT=...
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_API_VERSION=2025-03-01-preview
LLM_MODEL=gpt-5
```

### 11.3 `Sandbox image 'cad-agent-sandbox:latest' not found`

执行：

```powershell
cd "项目根目录\backend\sandbox"
podman build -t cad-agent-sandbox:latest .
```

### 11.4 `连接未就绪，请稍后重试`

通常是前端未连上后端：

- 确认 `http://localhost:8000/health` 可访问。
- 确认后端启动端口是 `8000`。
- 开发模式确认前端 Vite 代理生效。
- 非代理部署时设置 `frontend/.env` 的 `VITE_API_BASE`。

### 11.5 `npm audit` 提示漏洞

这是依赖审计提示，不代表安装失败。需要处理时单独执行：

```powershell
npm.cmd audit
npm.cmd audit fix
```

## 12. 交付注意事项

不要移交：

- `backend/.env`
- `frontend/.env`
- `.git/`
- `node_modules/`
- `backend/data/`
- 任何真实 API Key

交付时优先让同事阅读：

1. `HANDOFF.md`
2. `USAGE.md`
3. `backend/.env.example`

