# CAD-Agent 代码代理说明

本文件只定义本仓库的工作边界。产品事实和命令分别以 [README.md](README.md)、[docs/development.md](docs/development.md) 和 [DEPLOY.md](DEPLOY.md) 为准。

## 修改原则

- 先读取相关实现、测试和文档，再提出或执行修改。
- 保留现有 REST、WebSocket、鉴权、session/panel、文件归属和数据结构兼容性。
- 不用 Mock、静态成功响应或仅前端状态冒充后端/外部服务能力。
- 后端调用集中在 service/adapter/hook 层；不要在新组件中散落 `fetch`。
- 缺少外部运行时或凭据时，返回明确的 blocked/error 状态，不做隐式 fallback。
- 工作区可能包含用户未提交改动。只修改任务直接涉及的文件，不 stash、reset、clean 或重排无关代码。

## 事实边界

- 基础 Web MCAD：React/Vite/Three.js -> FastAPI 控制平面 -> PostgreSQL/Temporal -> ExecutionBackend -> Docker/Podman 隔离 Worker -> S3 兼容不可变产物。
- 核心任务事实是 `WorkflowRun -> StepRun -> ExecutionAttempt`、持久事件、`ProjectRevision` 和 `Artifact`；WebSocket 只订阅/回放事件，不能承担生命周期。
- 业务层不能直接调用 Docker、Podman、Kubernetes 或宿主 shell；修改必须携带 `expected_base_revision_id`，外部执行必须幂等。
- CAD Skills：产品 adapter 在 `backend/app/capabilities`；`third_party/cadskills` 是固定上游快照，不直接写产品逻辑。
- Fusion 360：`backend/app/fusion360` 和 `fusion_addin` 使用 typed action、Preview、Approval 和 verified result。模拟/facade 测试不能写成真实 Fusion E2E。
- 生产配置：`backend/.env.example` 是字段模板，不是可直接上线的配置；认证密钥、邀请码、CORS、模型凭据和设备凭据必须由部署方设置。

## 常用验证

```bash
cd backend
python -m pytest

cd ../frontend
npm run lint
npm run build

cd ..
python scripts/fusion360/generate_schema.py --check
git diff --check
```

真实 LLM/sandbox 评测按 [backend/benchmark/README.md](backend/benchmark/README.md) 单独运行。Fusion 实机验收按 [docs/fusion360-installation.md](docs/fusion360-installation.md) 执行。

## 文档规则

- `README.md` 只放概览和最短路径；本地开发写入 `docs/development.md`；生产运维写入 `DEPLOY.md`。
- 所有一方文档必须能从 [docs/README.md](docs/README.md) 或根 README 发现。
- 计划、调研和测试替身必须标注状态/日期，不能和当前实现混写。
- 修改接口、配置、脚本或部署方式时，同步更新对应文档并检查本地链接。
