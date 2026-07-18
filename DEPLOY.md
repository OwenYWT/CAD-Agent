# 部署与上线检查

本文只描述当前仓库支持的单机部署方式。开发环境见 [docs/development.md](docs/development.md)，Fusion 360 工作站安装见 [docs/fusion360-installation.md](docs/fusion360-installation.md)。

## 部署拓扑

`docker-compose.yml` 提供两个服务：

- `frontend`：Nginx 托管 Vite 产物，对外监听 `8080`，并代理 `/api`、`/health`、`/ready`、`/ws`。
- `backend`：FastAPI，仅在 Compose 网络暴露 `8000`；通过宿主机 `/var/run/docker.sock` 启动 CAD 沙箱容器。

沙箱镜像 `cad-agent-sandbox:latest` 不在 Compose build 中生成，必须单独构建。后端挂载 Docker socket，等同于拥有较高的宿主机控制权限，只能部署在受信主机；更严格的多租户环境应改用受限远程 Docker API、gVisor/Kata 或独立执行节点。

## 1. 准备生产配置

```bash
cp backend/.env.example backend/.env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

把随机值写入 `AUTH_TOKEN_SECRET`，并检查以下配置：

- 设置真实 `MOONSHOT_API_KEY`，或完整配置 Azure/OpenAI-compatible provider。
- 保持 `AUTH_REQUIRED=true` 和 `AUTH_DEV_EXPOSE_CODE=false`。
- 私测邀请码模式保持 `AUTH_CODE_FLOWS_ENABLED=false`；删除模板邀请码，改成实际发放的单次邀请码。
- 如启用验证码流程，设置 `AUTH_CODE_FLOWS_ENABLED=true`，并完整配置腾讯云 SMS 或自有 webhook；不得使用 `disabled/log` provider。
- `CORS_ORIGINS` 只包含真实 HTTPS 域名。
- `CADSKILLS_ENABLE_BAMBU_LAN=false` 默认保持关闭。确需局域网打印时，由运维显式开启；请求侧仍需执行和确认字段。
- `CADSKILLS_ISOLATED_EXECUTOR=[]` 表示禁止上传的 Python/JavaScript generator 在宿主机执行。只有部署了受控隔离 runner 后才能配置命令前缀。
- 不要把模型密钥、Fusion token、Bambu access code、短信凭据或真实 `backend/.env` 提交到仓库或日志。

启动时认证配置不安全会直接终止进程，不会降级运行。模型凭据、Docker 守护进程或沙箱镜像缺失时进程仍可启动，但 `/ready` 返回 `503`，生成能力不可用。

## 2. 构建并启动

```bash
docker build -t cad-agent-sandbox:latest backend/sandbox
docker compose config --quiet
docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps
```

`docker compose build` 只构建 Backend 和 Frontend，不会替代第一条沙箱镜像命令。

## 3. 上线前验证

```bash
BASE_URL=http://127.0.0.1:8080

curl --fail --silent --show-error "$BASE_URL/health"
curl --fail --silent --show-error "$BASE_URL/ready"
curl --fail --silent --show-error "$BASE_URL/" | grep -q '<div id="root"></div>'

curl --fail --silent "$BASE_URL/" \
  | grep -oE '/assets/[^" ]+\.(js|css)' \
  | sort -u \
  | while read -r asset; do
      curl --fail --silent --output /dev/null "$BASE_URL$asset"
    done

test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  "$BASE_URL/api/auth/me")" = "401"
```

`/health` 通过但 `/ready` 为 `503` 时，根据 `problems` 修复模型凭据、Docker 守护进程或沙箱镜像，不要开放流量。

验证 WebSocket 代理能到达后端鉴权层：

```bash
curl --include --no-buffer --max-time 5 \
  -H 'Connection: Upgrade' \
  -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  "$BASE_URL/ws/deploy-check"
```

未带有效认证时，`400`/`401`/`403` 表示请求已经到达 WebSocket 层；`404`/`502` 才表示路由或代理异常。

完成一次真实生成，确认只读根文件系统、临时目录和文件下载链路正常：

```bash
curl -X POST "$BASE_URL/api/generate" \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <LOGIN_TOKEN>' \
  -d '{"prompt":"一个 60x40x30mm 的收纳盒，壁厚 1.5mm"}'
```

响应至少应有真实生成结果和文件/验证字段。不能只用 `success=true` 作为工程质量结论，还要检查 validation、inspect report 和实际下载文件。

## 4. CAD Skills 检查

使用已登录凭据调用 `GET /api/capabilities`。11 项能力会分别报告依赖状态：

- G-code 需要 OrcaSlicer、PrusaSlicer 或 CuraEngine CLI，以及明确的 profile JSON。
- Implicit CAD 需要 Node.js、npm 依赖和隔离执行器。
- URDF/SRDF/SDF 的仿真或验证可能需要 Gazebo、MoveIt 等外部工具。
- Bambu LAN 需要部署开关、设备网络、请求确认和临时 access code。

部分能力为 `blocked` 不应拖垮整个服务，也不能被写成“全部可执行”。部署验收应记录每项实际依赖状态。

## 5. 更新现有单机

更新前备份 `backend/.env` 和 `cad_data` volume，禁止用模板覆盖线上密钥。

```bash
cd /path/to/CAD-Agent
git pull --ff-only
test -s backend/.env

docker build -t cad-agent-sandbox:latest backend/sandbox
docker compose config --quiet
docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps
```

重复执行第 3、4 节检查后再恢复流量。正式域名应由 HTTPS 终止层转发到 `127.0.0.1:8080`，腾讯云安全组只开放实际需要的端口。

## 6. `setup.sh` 的准确边界

`setup.sh` 是本地单服务辅助脚本，不是 Compose 生产部署器。它默认使用 Podman，并执行：

1. 检查 Python、Node.js、npm 和容器命令。
2. 缺少 `backend/.env` 时复制模板后立即退出，等待人工填写配置。
3. 在镜像不存在时构建 `cad-agent-sandbox:latest`。
4. 安装后端依赖，执行 `npm ci && npm run build`。
5. 前台启动 FastAPI `http://localhost:8000`，由 FastAPI 托管已构建前端。

它不会启动 `:5173` 的 Vite 开发服务器，也不会创建隧道。需要 Docker 时使用 `SANDBOX_RUNTIME=docker bash setup.sh`。

## 7. Fusion 360 上线状态

Fusion Connector 的 schema、HTTP、Palette controller、Dispatcher 和纯 Python facade 测试不能替代真实桌面 Fusion。完成 [Windows/macOS 验收矩阵](docs/fusion360-installation.md#8-windowsmacos-真实-fusion-验收门槛) 前，不得把该集成标记为 production-ready。
