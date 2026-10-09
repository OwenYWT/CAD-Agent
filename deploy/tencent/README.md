# Tencent Cloud 独立部署

目标服务器：`129.226.74.100`，新入口：`https://www.wordswave.ai`。
原 `https://wordswave.ai` 及其 `cad-agent` 容器、数据库和文件存储保留。

线上旧版的 Alembic 分支是 `0013_guest_project_lifecycle`，本地新版为
`0027_document_scene_tasks`，两者在 `0010_agent_candidate_seal_links` 后分叉。
本配置使用独立的 `cad-native` Compose 项目、网络、PostgreSQL、MinIO 和 Temporal，
没有修改旧库迁移标记，也没有把旧版游客/Onshape 数据强行导入新版。
新旧站账号和项目分别存储；新版 bootstrap 管理员沿用现有部署的管理员密码。

## 文件与启动

本次服务器发布目录：`/www/releases/cad-20260915-continuation-a8cd956`。
Compose 文件及私密配置位于该目录的 `deploy/tencent/`。
部署应用对应 main 合并提交 `8fdbec1c566653babe9c5d6aba28dde2b1222514`，
与构建源码 `a8cd9569e35d67354b56dcb8259244503aca7f00` 的 Git tree 完全一致。
实际来源与固定镜像摘要记录在 `deploy/tencent/release.json`；备份位置见下文回退说明。
`source-manifest.json` 记录源文件和前端产物的 SHA-256。

以下旧发布路径是历史记录；当前发布及验收结果见 [2026-09-30 报告](../../docs/qa/acceptance-fixes-20260930/README.md)。

运行需要三个仅由部署方持有、权限为 `0600` 的文件：

- `.env`：`POSTGRES_PASSWORD`、`MINIO_ROOT_USER`、`MINIO_ROOT_PASSWORD`、
  `BACKEND_IMAGE` 和 `FRONTEND_IMAGE`；镜像使用已经验证的 OCI 摘要。
- `backend.env`：实际应用配置。生产模式、认证开启、验证码回显关闭、邀请注册；
  使用真实模型凭据、独立 PostgreSQL URL、HTTPS S3 入口和独立 Temporal 队列。
  `SANDBOX_IMAGE` 必须是已通过真实探测的 `repository@sha256:digest`，
  不能使用裸 `sha256:digest` 或可变标签；生产启动会拒绝不完整引用。
- `monitor.env`：监控服务独立配置，包含认证所需连接及 `MONITOR_DATABASE_URL`。
  查询 URL 使用只被授予 `cad_agent_monitor` 的独立登录，不授予超级用户、
  BYPASSRLS、`cad_agent_runtime` 或 `cad_agent_auth`。升级时必须保留此文件，
  不能用 `backend.env` 替代；验收应核对 `current_user`、`session_user` 和越权拒绝。

服务器上执行：

```bash
cd /www/releases/cad-20260915-continuation-a8cd956/deploy/tencent
docker compose config -q
docker compose run --rm --no-deps backend python -c \
  'from app.config import settings; settings.assert_auth_config_safe(); settings.assert_sandbox_config_safe(); settings.assert_durable_control_plane_config_safe()'
docker compose up -d --no-build
docker compose ps
curl --fail https://www.wordswave.ai/ready
```

数据库迁移先于应用运行；API 和 Worker 共享相同的绝对沙箱工作目录
`/tmp/cad-native-work`。该目录供宿主机创建的兄弟计算容器读取真实输入与输出。
数据库、Temporal 和 MinIO 控制台不发布公网端口。
API `8010`、前端 `8091`、MinIO S3 `59100` 仅绑定服务器回环地址。

## HTTPS 与存储

宿主机 Nginx 配置为 `/etc/nginx/conf.d/cad-native.conf`，对应本目录 `nginx.conf`。
`www.wordswave.ai` 的 443 入口代理新版前端；`/cad-native-artifacts` 桶路径代理新版
MinIO，并保留原始 URI 和含端口的 Host，以便校验 S3 SigV4。
桶为私有且开启版本管理，代理并不绕过 MinIO 的签名验证。

证书由现有 `/root/.acme.sh/acme.sh` 定时续期。HTTP ACME 路径指向
`/www/wwwroot/wordswave/dist`，续期后使用 `nginx -t && systemctl reload nginx`。
新增证书安装在 `/www/server/panel/vhost/cert/www.wordswave.ai/`。
不需要开放 8443；排查中临时增加的该端口规则已撤销。

## 构建与验证

2026-09-13：Docker Hub 的 MinIO 拉取返回 access denied；当前配置使用已验证可读取的
官方 `quay.io/minio/minio` / `quay.io/minio/mc` 同版本镜像。既有部署的数据卷保持不变。
前端预构建打包使用 `Dockerfile.frontend.dockerignore`，显式包含 `dist/`；
完整 Node 构建仍使用 `frontend/.dockerignore`。

后端、FreeCAD 沙箱必须构建为服务器的 `linux/amd64`。本地 ARM 镜像不能直接使用。
标准构建文件为 `backend/Dockerfile` 和 `backend/sandbox/Dockerfile`。
沙箱从官方 FreeCAD 1.1.3 AppImage 构建，并校验仓库中记录的架构对应 SHA-256。

前端可在开发机运行 `npm ci`、`npm run lint`、`npm run build`，再使用
`Dockerfile.frontend` 打包平台无关的 `dist/`。传输时只包含普通应用文件，
排除 macOS AppleDouble (`._*`) 和扩展元数据。发布前比较容器中的实际静态文件哈希。

`verify-images.py` 使用真实镜像读取文件字节，核对后端、内核入口和 vendored
CAD Skills 是否与源码 manifest 一致。它不加载应用凭据，也不替换任何工具响应。

运行时必须在限制网络、内存、权限且根文件系统只读的真实容器中执行
`/opt/cad-agent/runtime_probe.py --json`，检查所有操作结果。仅成功导出镜像清单或
返回镜像 ID 不足以证明它已完整解包且可以运行。

真实业务验收使用 `backend/tests/e2e/` 中的 Cloud Document、浏览器与工程计算脚本，
测试账号和会话保存在私密目录。普通 PostgreSQL pytest 会清理 fixture 数据，
不得对线上业务数据库运行。

## 回退与资源边界

切换应用镜像前先暂停写入入口，保留当前 worker 处理已受理任务，并使用目标镜像检查实际持久派发载荷，并重放这些任务的真实 Temporal 历史：

```bash
python3 deploy/tencent/check-inflight.py --source-container <当前后端容器> --target-image <目标后端镜像摘要>
```

该命令只读，不修改任务或部署；必须具备检查所有租户任务的数据库身份，受 RLS 限制而看不到完整集合时直接失败。退出码 2 表示存在不兼容请求、未结束的 Model Job 或仍在规划／执行的工作流：保留当前版本，完成这些任务后重查，不要将新请求交给旧 worker。检查通过表示已读取的请求和历史可由目标版本处理，不能代替同一旧、新镜像对的后续真实 CAD 演练；恢复写入前还须验证模型、报告、鉴权、监控和存储。只有请求和真实历史均兼容且等待用户确认的工作流可以跨版本继续；规划／执行中的任务必须先完成，避免检查之后又创建 Model Job。切换期间保持写入暂停并结束已进入 API 的写请求；在最终检查前停止同一队列的所有旧 worker，避免已确认的信号在检查后创建 Model Job。最终检查不通过时启动原容器继续处理，不能按指向新版的 Compose 默认值启动另一镜像。检查通过后切换应用、核验并恢复写入。

固定发布基线 `df8fdbb` 无法重放含新版 `agent-v2-native-source-solid-count-v1` patch 的历史，必须保留新版处理完已受理任务再回退。旧版不支持新版的 `rule_configuration`，不能接手尚未完成的冻结 DFM 检查。已完成报告在旧 API 中可能不显示这项新增元数据，但原始报告哈希必须保持不变，恢复新版后必须能读取完整配置。不要降级数据库或覆盖既有 `.env`、`backend.env`、`monitor.env` 来消除不兼容。

统一 `bash scripts/ci/run.sh deploy` 必须执行 `scripts/ci/release_image_contract.py`，在独立测试 Compose 中保留同一数据库、Temporal、对象存储和卷切换应用镜像。默认从固定提交 `df8fdbb84f34164d9ad18e9101f881de4f0fa17a` 自动构建旧版，只有运行时构建输入完全一致时复用已验证依赖层，并清空再复制全部应用源码；依赖变化时使用该提交的原 Dockerfile 构建。也可明确提供一整组 `CAD_CI_BASELINE_BACKEND_IMAGE`、`CAD_CI_BASELINE_FRONTEND_IMAGE` 和 `CAD_CI_BASELINE_SANDBOX_IMAGE` 检查其他版本对。报告记录实际镜像 ID、同一在途任务、执行中拒绝切换、新候选审查提交、原生体积、历史不变和不兼容回退拒绝。Required regression gate 要求镜像演练报告，缺失或失败不能通过。

原发布保留在 `/www/releases/0209a4b-20260822-2345`，原站持续提供服务。
2026-09-10 的旧库与配置备份仍保留。2026-09-13 切换前的联合备份位于
`/www/backups/cad-20260913-task-state-7d1dd5e`，仅 root 可读；包含 PostgreSQL、Temporal、MinIO 和 cad_data。
本次应用更新前的 PostgreSQL 与旧配置备份位于 `/www/backups/cad-20260915-continuation-a8cd956`。
上一版 native 配置目录 `/www/releases/cad-20260913-task-state-7d1dd5e/deploy/tencent` 及更早发布仍保留。
停止新版只需在新版目录执行 `docker compose stop`；不要删除卷、覆盖旧配置或执行
`down -v`。恢复新版使用同一配置和固定镜像摘要。

服务器为 2 GB 内存、4 GB swap 的共享单机，沙箱并发配置为 1。
这是低并发单机部署，未提供高可用、负载均衡或容量压测保证。
本次沿用版本固定的 Temporal auto-setup 单机发行方式；扩容为正式集群时应采用
独立维护 schema 的 Temporal server 或托管服务。

部署验证不等于完整产品方案验收。硬件实机适配和外部企业系统集成仍以
`docs/qa/acceptance-remediation-2026-09-10.md` 中列出的真实完成状态为准。

本次上线、两个持久测试账户的角色、真实测试与限制见
[2026-09-13 部署报告](../../docs/qa/tencent-deployment-2026-09-13.md)。账号密码仅保存在私密交接文件中。

五状态改造与本次发布的实际验证、原账户保留情况及手机壳失败限制见
[2026-09-13 五状态与发布报告](../../docs/qa/task-state-2026-09-13.md)。
2026-09-13 发布复用已经验证的 AMD64 运行时层；后端应用、沙箱入口及前端静态文件
通过逐文件 SHA-256 校验，当次线上另执行真实候选生成、审查、保存与刷新验收。

2026-09-15 的旧“继续”任务恢复修复、CI、真实本地候选生成和线上原任务恢复验证见
[恢复修复与发布报告](../../docs/qa/continuation-recovery-2026-09-15.md)。本次线上没有替用户确认手机壳建模或适配依据。
