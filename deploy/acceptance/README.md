# 完整隔离栈发布验收

`compose.yml` 用于独立 Docker daemon 中的可丢弃验收栈。必须使用新项目名和独立凭据；禁止指向生产数据库、生产目录或生产对象存储。`docker compose down` 只停止该项目，升级/回退期间不能加 `-v`。

配置文件均放在仓库外、权限 0600：

- Compose 环境：`BACKEND_IMAGE`、`FRONTEND_IMAGE`、`MONITORING_IMAGE` 使用已核对源码的镜像摘要；设置 `RELEASE_ENV_FILE`、`MONITOR_ENV_FILE`、`POSTGRES_PASSWORD`、`MINIO_ROOT_USER`、`MINIO_ROOT_PASSWORD`、`WORK_ROOT`、`DOCKER_SOCKET`。
- 应用环境：数据库和迁移 URL 都指向 `postgres:5432/cad_acceptance`；对象存储为本栈 `minio:9000`、桶 `cad-release-acceptance`；Temporal 为 `temporal:7233`、namespace `cad-release-acceptance`；启用 durable control plane，设置独立认证密钥及管理员密码。真实 Provider 凭据单独注入，不复制生产认证凭据。
- 监控数据库 URL 使用独立 login，授予迁移创建的 `cad_agent_monitor` 角色。管理员身份仍由真实认证数据库读取。
- `SANDBOX_IMAGE` 使用当前验收版本不可变摘要；Docker daemon 能访问 `WORK_ROOT`，容器和 daemon 看到的路径相同。
- 本栈为回环 HTTP 的测试环境，使用 `APP_ENVIRONMENT=test`。它不证明生产 HTTPS、硬件资源或公网发布配置已验收。

```sh
docker compose -p cad-acceptance-<唯一标识> \
  --env-file /私有路径/compose.env \
  -f deploy/acceptance/compose.yml up -d
```

发布验收必须：

1. 固定旧、新源码及镜像摘要，验证镜像内源码字节和 schema。
2. 使用真实账号、HTTP/WebSocket 和模型服务提交任务；在数据库确认 Model Job 为 running 后终止 worker。
3. 重建整栈并切换新版，保留专用卷；验证同一个 Job 以更高 generation 恢复，payload_hash 不变，旧代次续租无效，真实产物可验证且能提交。
4. 反向切回旧版重复以上流程，验证先前账号、版本与产物摘要不变。
5. 重新升级到交付版本；浏览器检查已保存模型、刷新和抽屉；验证新旧监控镜像组合、管理员访问、普通账号拒绝和数据库只读角色。
6. 明确记录每次失败及环境差异；测试通过不代表 GitHub 分支保护已生效。

2026-09-23 的实际执行过程、镜像、日志和结论见 [验收报告](../../docs/qa/deployments/2026-09-23-acceptance-fixes/README.md)。该次在 ARM 主机仿真 AMD64 镜像，对专用沙箱容器提供额外内存，不能冒充腾讯云原生资源配置验证。
