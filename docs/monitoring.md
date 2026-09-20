# 独立账号使用监控

监控应用与 CAD 界面分开运行，使用已有平台管理员账号登录。项目 owner/editor 不等于平台管理员；普通用户即使直接请求监控 API，也会得到 403。CAD 前端不增加监控入口。

## 记录范围

- 按账号汇总任务数量、成功、失败、处理中、取消及最近使用时间。同一账号在不同工作区的记录合并汇总。
- 每次实际模型 API 调用独立记录，包括适配器重试和 Temporal Activity 重试。关联提交账号、工作区、任务、活动阶段与活动尝试次数。
- 记录服务与实际返回模型、输入/输出/总 Token、服务提供的推理 Token、耗时、终止原因、错误类型、HTTP 状态、响应 ID 和请求/响应哈希。
- 未返回用量保持 NULL，页面显示“未知”；总量是已知用量之和。推理 Token 已包含在服务的 completion/total 中，不重复累加。
- 任务成功与 API 调用成功分别显示；截断、失败、取消、未结束均有独立状态。未结束记录不能作为成功，也可能表示进程中断。
- 不保存密码、API Key、完整提示词、模型回复或未经清理的服务错误正文；不根据未知价格估算费用。
- 历史任务直接查询既有任务数据库；上线前的模型调用不补造 Token 记录。

## 运行

使用与 API/Worker 相同的 PostgreSQL 及账号配置，先迁移，再更新 API、Worker 和监控服务：

```sh
cd backend
alembic upgrade head
uvicorn app.monitoring:app --host 127.0.0.1 --port 8092
```

打开 `http://127.0.0.1:8092/`。需要现有管理员 `admin` 的密码；不会自动提升测试账号权限。进程使用独立应用 `app.monitoring:app`，只提供登录、只读监控和静态面板。数据库读取切换到 `cad_agent_monitor`，该角色仅能读取监控必要列，无账号密码、任务需求正文或写入权限。

腾讯云入口为 https://www.wordswave.ai/monitor/ 。管理员凭据保存在部署私有配置中，不写入仓库。

腾讯云 Compose 已增加 `monitoring` 服务，端口仅绑定 `127.0.0.1:8092`。`MONITORING_IMAGE` 可独立指定监控镜像，未指定时使用 `BACKEND_IMAGE`。可通过 SSH 隧道访问，或在已有 HTTPS 站点增加独立反向代理位置：

```nginx
location = /monitor { return 301 /monitor/; }
location ^~ /monitor/ {
    proxy_pass http://127.0.0.1:8092/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

迁移版本为 `0028_llm_monitoring`。对使用独立数据库登录角色的部署，需由数据库管理员将 `cad_agent_monitor` 授予监控进程登录角色；不得将它授予普通终端用户。现有迁移 owner 方式会自动获得该角色。

## 输出策略

所有模型调用不再传入 `max_tokens`、`max_completion_tokens`、`max_output_tokens`；适配器也过滤旧调用者和 `extra_body` 中的这些参数。`PLANNER_MAX_TOKENS` 已废弃。输出受模型服务的实际能力约束，应用不能承诺无限输出。

全部模型调用使用已有适配器流式接收，收齐后仍按完整结果处理。Token 用量取服务返回的 usage，无应用字符数截断。SDK 与 HTTP 客户端不设置模型等待超时；V2 模型调用通过持久化任务执行，工作流等待结果，不设置生成总等待期限。主动取消、连接错误和真实完整性检查继续保留；短控制活动与 CAD 沙箱的独立资源策略不等于模型等待期限。模型截断不会被修补成成功。

## 验证与运维

- `tests/test_uncapped_llm.py` 验证旧参数不能重新引入输出上限。
- `tests/test_llm_stream_integrity.py` 验证流终止、断流及服务用量。
- `tests/postgres/test_monitoring.py` 使用迁移后的独立数据库，覆盖真实认证、权限、跨工作区聚合、并发隔离和 NULL 用量。使用 `CAD_MONITOR_TEST_DATABASE_URL` 指向测试库。
- `tests/e2e/monitoring_browser.py` 使用真实服务和账号验证页面；通过 `CAD_MONITOR_E2E_PRIVATE` 传入私有账号 JSON，`CAD_MONITOR_E2E_URL` 指定入口，`CAD_MONITOR_E2E_REPORT` 指定新建证据目录。
- 监控写入异常记录 `LLM_USAGE_WRITE_FAILED`，不会覆盖 CAD 的真实执行结果；缺少账号上下文记录 `LLM_USAGE_UNATTRIBUTED`。数据库写入异常可能造成监控缺口，因此不将其作为无缺口的财务账单。
- 回退代码时保留监控表与历史记录。面板可独立停止，不影响 CAD 前端。
