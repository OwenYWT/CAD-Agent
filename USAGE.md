# CAD Agent Web 使用方法

## 1. 先看配置放哪里

- 后端配置写到 `backend/.env`。
- 前端配置写到 `frontend/.env`，但开发模式通常可以不写。
- Azure OpenAI、模型名、Podman/Docker 这些都是后端配置，不要写到 `frontend/.env`。

## 2. 安装 Podman

如果不能安装 Docker，安装 Podman Desktop：

1. 打开 `https://podman-desktop.io/`
2. 下载并安装 Windows 版本。
3. 打开 Podman Desktop，按提示初始化 Podman Machine。
4. PowerShell 验证：

```powershell
podman --version
podman machine start
podman info
```

也可以用 winget：

```powershell
winget install -e --id RedHat.Podman-Desktop
podman machine init
podman machine start
podman info
```

## 3. 如果使用 Docker

如果电脑可以安装 Docker，也可以不用 Podman，直接使用 Docker Desktop。

### 3.1 安装 Docker Desktop

官网下载：`https://www.docker.com/products/docker-desktop/`

或者使用 winget：

```powershell
winget install -e --id Docker.DockerDesktop
```

安装后启动 Docker Desktop，并验证：

```powershell
docker --version
docker info
```

### 3.2 Docker 环境配置

如果使用 Docker，`backend/.env` 中沙箱配置改为：

```env
SANDBOX_RUNTIME=docker
SANDBOX_COMMAND=
SANDBOX_IMAGE=cad-agent-sandbox:latest
```

Azure 配置仍然放在 `backend/.env`，不要放到 `frontend/.env`。

### 3.3 Docker 构建沙箱镜像

```powershell
cd "项目根目录\backend\sandbox"
docker build -t cad-agent-sandbox:latest .
```

后续启动后端、前端的命令和 Podman 方案相同。
## 4. 配置后端环境

```powershell
cd "项目根目录"
Copy-Item backend\.env.example backend\.env
```

编辑 `backend/.env`：

```env
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://<your-azure-openai-resource>.openai.azure.com/
AZURE_OPENAI_API_KEY=<AZURE_OPENAI_API_KEY>
AZURE_OPENAI_API_VERSION=2025-03-01-preview
LLM_MODEL=gpt-5
LLM_REASONING_EFFORT=minimal
SANDBOX_RUNTIME=podman
SANDBOX_COMMAND=podman
SANDBOX_IMAGE=cad-agent-sandbox:latest
```

如果 Azure 里的部署名不是 `gpt-5`，把 `LLM_MODEL` 改成实际部署名。

## 5. 构建沙箱镜像

```powershell
cd "项目根目录\backend\sandbox"
podman machine start
podman build -t cad-agent-sandbox:latest .
```

## 6. 启动后端

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

## 7. 启动前端

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

## 8. 单服务启动

如果希望只开后端一个服务：

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

## 9. 常见问题

### `DASHSCOPE_API_KEY is required for LLM operations`

检查 Azure 配置是否在 `backend/.env`，并重启后端。验证命令：

```powershell
cd "项目根目录\backend"
$env:PYTHONPATH="."
python -c "from app.config import settings, make_llm_client; print(settings.normalized_llm_provider, settings.has_llm_credentials, type(make_llm_client().raw_client).__name__)"
```

正常应输出：`azure True AsyncAzureOpenAI`。

### `Incorrect API key` / `401`

检查 `AZURE_OPENAI_ENDPOINT`、`AZURE_OPENAI_API_KEY`、`AZURE_OPENAI_API_VERSION` 和 `LLM_MODEL` 是否匹配。

### 沙箱镜像不存在

```powershell
cd "项目根目录\backend\sandbox"
podman build -t cad-agent-sandbox:latest .
```

### 前端连接失败

确认后端 `http://localhost:8000/health` 正常，开发模式下前端通过 Vite 代理访问后端。

