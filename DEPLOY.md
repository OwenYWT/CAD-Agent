# 部署与上线检查

本文描述当前仓库已验证的单机部署边界。开发环境见 [docs/development.md](docs/development.md)，Fusion 360 工作站安装见 [docs/fusion360-installation.md](docs/fusion360-installation.md)。

## 部署拓扑

`docker-compose.yml` 提供以下服务：

- `postgres`：保存租户、项目、分支、Revision、WorkflowRun、StepRun、ExecutionAttempt、事件、审计和产物元数据。
- `minio` / `minio-init`：保存不可变 CAD 产物、日志和检查报告，并为 bucket 启用底层 Versioning。
- `temporal`：本地/单机验证用的持久工作流服务。
- `migrate`：在 API 和 Worker 启动前执行 `alembic upgrade head`。
- `backend`：FastAPI 控制平面，只在 Compose 网络暴露 `8000`。
- `workflow-worker`：独立 Temporal Worker；同时注册 V1 与专用 V2 Agent task queue，通过统一 `ExecutionBackend` 提交隔离 MCAD 执行。
- `frontend`：Nginx 托管 Vite 产物，对外监听 `8080`，并代理 `/api`、`/health`、`/ready` 和 `/ws`。

当前单机执行后端通过宿主机 `/var/run/docker.sock` 启动一次性沙箱，因此只能部署在受信执行节点。Compose 中的 `temporalio/auto-setup` 只用于本地和单机验证；正式生产应改为托管 Temporal 或由运维管理 schema 的受支持 Temporal 部署。后续 Kubernetes Job 或企业私有 Worker 必须实现同一 `ExecutionSpec` / `ExecutionResult` 契约，业务 API 不应直接依赖容器技术。

## 1. 准备生产配置

```bash
cp backend/.env.example backend/.env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

把随机值写入 `AUTH_TOKEN_SECRET`，并检查以下配置：

- 设置真实 `MOONSHOT_API_KEY`，或完整配置 Azure/OpenAI-compatible provider。
- 保持 `AUTH_REQUIRED=true`、`AUTH_DEV_EXPOSE_CODE=false` 和 `DURABLE_CONTROL_PLANE_ENABLED=true`。MCAD 写请求只有 Durable 链路，不再存在可切换的旧写链路。
- 设置 `DATABASE_URL=postgresql+asyncpg://...`，数据库密码如含特殊字符必须 URL 编码。
- 设置对象存储 endpoint、access key、secret 和 bucket；非本地环境必须使用 HTTPS。
- 设置 Temporal target、namespace、`TEMPORAL_TASK_QUEUE` 和不同名的 `TEMPORAL_AGENT_V2_TASK_QUEUE`；API 与 Worker 必须完全一致。
- 私测邀请码模式保持 `AUTH_CODE_FLOWS_ENABLED=false`；删除模板邀请码，改成实际发放的单次邀请码。
- 如启用验证码流程，设置 `AUTH_CODE_FLOWS_ENABLED=true`，并完整配置腾讯云 SMS 或自有 webhook；不得使用 `disabled/log` provider。
- `CORS_ORIGINS` 只包含真实 HTTPS 域名。
- `CADSKILLS_ENABLE_BAMBU_LAN=false` 默认保持关闭。
- `CADSKILLS_ISOLATED_EXECUTOR=[]` 表示禁止上传的 Python/JavaScript generator 在宿主机执行。只有部署受控隔离 runner 后才能配置命令前缀。
- 不要把模型密钥、数据库密码、对象存储密钥、Fusion token、Bambu access code、短信凭据或真实 `backend/.env` 提交到仓库或日志。

认证、持久控制平面或生产沙箱配置不安全时进程会直接终止。依赖暂时不可达时 `/ready` 返回 `503`，不能接收流量。

Compose 插值还需要由部署环境提供：

```bash
export APP_ENVIRONMENT=production
export POSTGRES_PASSWORD='<strong-database-password>'
export DATABASE_URL='postgresql+asyncpg://cad_agent:<url-encoded-password>@postgres:5432/cad_agent'
export MINIO_ROOT_USER='<object-store-access-key>'
export MINIO_ROOT_PASSWORD='<strong-object-store-secret>'
export SANDBOX_IMAGE='registry.example.com/wordswave/mcad-runtime@sha256:<digest>'
```

## 2. 构建并启动

从仓库根目录构建、验证并推送 MCAD Runtime。生产部署不能使用 `latest` 或其他可变 tag：

```bash
docker build -f backend/sandbox/Dockerfile -t registry.example.com/wordswave/mcad-runtime:<version> .
docker run --rm --entrypoint python \
  registry.example.com/wordswave/mcad-runtime:<version> \
  /opt/cad-agent/runtime_probe.py
docker push registry.example.com/wordswave/mcad-runtime:<version>
docker buildx imagetools inspect registry.example.com/wordswave/mcad-runtime:<version>
```

把 Registry 返回的 digest 写入 `SANDBOX_IMAGE`，然后启动：

已验证的后端依赖镜像可用于受限网络下的应用更新：从仓库根目录运行 `docker build -f backend/Dockerfile.reuse-dependencies --build-arg VERIFIED_BACKEND_IMAGE=已验证的后端镜像引用 -t 新后端镜像 .`。该入口会比较两份 `requirements.txt` 的完整内容并执行 `pip check`；依赖清单改变时拒绝复用。它只重建应用层，必须记录所用基础镜像 digest，并对新镜像执行正常回归。干净安装依赖仍使用默认 `backend/Dockerfile`。

```bash
docker compose config --quiet
docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps
```

`migrate` 和 `minio-init` 应显示成功退出；`postgres`、`minio`、`temporal`、`backend`、`workflow-worker`、`frontend` 应处于运行状态，`backend` 最终应健康。API 会等数据库迁移完成，前端会等 `/ready` 通过。

API/Worker 镜像从仓库根目录构建，以包含 `third_party/cadskills`；镜像内同时包含 Docker CLI 和 SDK。`SANDBOX_WORK_DIR` 默认 `/tmp/cad-agent-work`，必须在宿主机、API、Worker 中以相同绝对路径挂载，`TMPDIR` 也必须相同，内核兄弟容器才能读取输入和写回结果。macOS/Podman 使用共享路径时应采用实际绝对路径（例如 `/private/tmp/cad-agent-work`）。

Podman 的 Docker-compatible socket 需要单独映射至 `/var/run/docker.sock`；使用 SELinux 的 Podman 虚拟机还需为该 socket 的控制平面容器配置适当权限。本轮本机验证对 API/Worker 使用 `--security-opt label=disable`，内核任务仍保持 network none、只读根文件系统、cap drop 和资源限制。不要仅修改 `SANDBOX_RUNTIME` 而遗漏 socket、路径共享或权限配置。

云文档流位于 `/api/documents/{id}/stream`；Nginx 的 `/api/` 代理也必须允许 WebSocket Upgrade，不能只配置旧 `/ws/` 路径。

前端镜像启动时使用官方 Nginx entrypoint 从 `/etc/resolv.conf` 读取容器 DNS，并将 `nginx.conf` 模板生成为实际配置。代理通过变量解析 `backend`，DNS 缓存为 5 秒，API 容器重建或地址变化后无需重启前端。不要把模板直接复制为运行配置，也不要将 Docker 的 `127.0.0.11` 写死到 Podman 环境。机制依据：[Nginx resolver](https://nginx.org/en/docs/http/ngx_http_core_module.html#resolver)、[变量 proxy_pass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass) 和 [官方镜像 DNS entrypoint](https://github.com/nginx/docker-nginx/blob/master/entrypoint/15-local-resolvers.envsh)。

发布和 Local Bridge 需要迁移至 `0024_local_bridge` 或更新版本，并部署对应 API/Worker、前端和原生沙箱。原生有限元使用实际 Gmsh/CalculiX，发布包使用 FreeCAD `Assembly::BomObject`；这些依赖已纳入沙箱 Dockerfile。Bridge 客户端从 API 下载，Python 3.11+、macOS/Linux，不增加服务端端口。安装与边界见 [发布与本地交付](docs/releases-and-local-bridge.md)。

工程任务每个沙箱上限 1 GiB。`SANDBOX_MAX_CONCURRENT` 限制每个 API/Worker 进程的内核并发，多个进程的限制会叠加。4 GiB 的共享本地虚拟机应采用低并发（例如各进程为 1），避免同时运行多套验收部署；该配置不能替代生产节点的总容量调度。

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

`/health` 通过但 `/ready` 为 `503` 时，根据 `problems` 检查：

- PostgreSQL 连接和 migration head；
- 对象存储访问、bucket、主机时钟和 TLS；
- Temporal namespace、V1/V2 task queue；
- V1/V2 Workflow 与 Activity poller 是否在 90 秒有效窗口内；
- LLM provider 凭据；
- Docker daemon 和 digest 固定的 MCAD Runtime。

验证 WebSocket 代理能到达后端鉴权层：

```bash
curl --include --no-buffer --max-time 5 \
  -H 'Connection: Upgrade' \
  -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  "$BASE_URL/ws/deploy-check"
```

未带有效认证时，`400`/`401`/`403` 表示请求已到 WebSocket 层；`404`/`502` 表示路由或代理异常。

最后用已登录账号执行一次真实 MCAD 任务，至少验证：

1. 生成/修改创建 V2 Agent `WorkflowRun`，并持久化规划、建模、执行、验证、修复与封存对应的 `StepRun` / `ExecutionAttempt`；
2. WebSocket 显示的状态能在刷新后通过 REST/事件回放恢复；
3. STEP/STL 可下载，字节数和 SHA-256 与产物元数据一致；
4. 工程检查生成独立检查 Workflow 和不可变 JSON 报告；
5. 修改使用正确的 `expected_base_revision_id`，旧 Revision 写入返回冲突；
6. 接受 Change Set 后才推进分支 head，拒绝/取消不产生成功产物。

不能只以 HTTP 200 或 `success=true` 作为工程质量结论。

## 4. CAD Skills 检查

使用已登录凭据调用 `GET /api/capabilities`。11 项能力会分别报告依赖状态：

- G-code 需要 OrcaSlicer、PrusaSlicer 或 CuraEngine CLI，以及明确的 profile JSON。
- Implicit CAD 使用统一 Runtime 内固定的 Node/npm 依赖和隔离执行器。
- URDF/SRDF/SDF 的仿真或验证可能需要 Gazebo、MoveIt 等外部工具。
- Bambu LAN 需要部署开关、设备网络、请求确认和短期 access code。

部分能力为 `blocked` 不应拖垮整个服务，也不能被写成“全部可执行”。部署验收应记录每项实际依赖状态。

## 5. 更新、备份与恢复

更新前至少备份：

- PostgreSQL 逻辑备份和数据库版本；
- MinIO/S3 bucket、Versioning 状态及对象清单；
- `backend/.env` 和部署平台 secret；
- Runtime image digest 与 `runtime-lock.json`；
- `cad_data` volume 中仍由可选 Connector/辅助能力使用的本地状态。

S3 Versioning 只防止底层误删除/误覆盖，不能替代 `ProjectRevision`。恢复时必须同时保持数据库中的 Artifact 元数据和对象存储字节一致。

```bash
cd /path/to/CAD-Agent
git pull --ff-only
test -s backend/.env

docker compose config --quiet
docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps
```

确认 `migrate` 成功、Worker poller 出现在 `/ready` 后，再重复第 3、4 节并恢复流量。正式域名应由 HTTPS 终止层转发到 `127.0.0.1:8080`。

## 6. 辅助启动脚本的边界

`setup.sh`、`start-web.ps1` 是旧的本地单进程辅助路径，不会启动 PostgreSQL、MinIO、Temporal 或独立 Workflow Worker，不能作为当前持久 MCAD 产品路径的生产部署器。需要热更新时按 [开发文档](docs/development.md) 启动完整依赖；生产始终使用受管部署配置。

## 7. Fusion 360 上线状态

Fusion Connector 的 schema、HTTP、Palette controller、Dispatcher 和纯 Python facade 测试不能替代真实桌面 Fusion。完成 [Windows/macOS 验收矩阵](docs/fusion360-installation.md#8-windowsmacos-真实-fusion-验收门槛) 前，不得把该集成标记为 production-ready。

## 8. 腾讯云独立部署（2026-09-11）

新版入口为 https://www.wordswave.ai，原 https://wordswave.ai 保留。
新旧数据库独立，旧账号和项目没有自动迁移。
实际受管配置、固定镜像、证书与备份路径见 [腾讯云部署说明](deploy/tencent/README.md)；
真实测试结果及未验证范围见 [部署验证报告](docs/qa/tencent-deployment-2026-09-11.md)。
