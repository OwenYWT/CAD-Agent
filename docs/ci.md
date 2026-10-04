# CAD-Agent CI

每个 PR 合入 main 前必须通过全部确定性回归。并行拆分只改变执行顺序，快速 push 检查不能替代 `Required regression gate`。真实付费模型评测仅在有明确预算时手动选用，不定时运行，也不作为每次发布的前置要求。

## 触发与合并门禁

- `ci-quick.yml`：所有分支 push，执行后端离线测试、前端 lint／类型检查／测试／构建、架构检查。
- `ci.yml`：PR 创建、更新、重新打开、转为可审查，以及 main／master push、merge queue、手动触发，执行完整确定性回归。
- 两者按“工作流＋事件＋PR 或分支”分组取消旧运行，push 与 PR 不互相取消。没有路径跳过规则。
- `ci-live.yml`：仅手动触发，只执行受信任的 main 提交。独立环境、凭据、费用上限，不参与 PR 门禁，不自动消耗模型 token。

完整门禁始终运行，要求以下八个 job 全部成功，再核对同一提交、workflow run 和 attempt 的 62 份必需证据。失败、取消、跳过、缺少 job、旧报告均不能通过。

| 必需 job | 保留／新增的检查 |
| --- | --- |
| backend | 原后端离线集合：1,945 个测试 ID，209 个明确记录的隔离环境／付费依赖／向量回退跳过；另运行 Fusion 离线 189 项、CI 门禁与预算／资源保护 39 项、核心 lint 和类型基线 |
| frontend | 原 lint、159 项测试、TypeScript、生产构建，交付镜像使用本次构建的 dist |
| architecture-contract | 原架构边界和功能地图检查，新增必测集合、工作流权限及 Action SHA 检查 |
| build-images | 构建并验证 sandbox、backend、frontend、object-store；记录源码 SHA、镜像 ID 和归档 SHA；执行真实运行时探针 |
| mcad-core | 原持久化/API 55 项、原生运行时 16 项、动态历史重放四类工作流及 Model Job 补丁前后分支、12 份固定发布历史、原生脚本 22 项 |
| model-lifecycle | 原 PostgreSQL 99 项拆为事务 80、Model Job 12 及其真实历史重放、监控 6、迁移 1；补充原 CI 漏掉的集成 22 项；保留旧提交 `62084d5c84ead7bcd88030695fdfcd61112280ea` 的源码／数据库跨版本协议演练 |
| browser-e2e | 保留原生候选提交、参数联动、拒绝与刷新、布局、监控权限、约束失败反馈、保存后内核测量的全部浏览器检查 |
| deploy-smoke | 实际交付 Compose、生产 Dockerfile 镜像、前端及 TLS Nginx；候选接受／提交／刷新；18 项鉴权权限断言、WebSocket、私有 S3 SigV4、监控 HTTP 与只读数据库权限 |

离线阶段的跳过不是通过。需要服务的确定性检查在对应重测试 job 中必须实际运行且零跳过。两个 Chroma 回退测试只允许既有的确切回退原因；现有五个以 `ANTHROPIC_API_KEY` 为开关的历史付费测试继续单列跳过，不将其计为当前模型评测。真实 Fusion 桌面端也不由这些离线测试证明。

原生约束链使用修复前冻结的有效 L 形基线与受控模型响应，执行真实 FreeCAD，保留错误现场、局部补丁、欠约束处理、22 项参数探针、候选提交、FCStd 保存后重开及独立 25 mm／单实体测量。原自交历史样本仍需被拒绝；没有修改历史任务或冻结验收条件。

## 本地入口与隔离

Python `3.11.16`、Node `22.17.0`。安装 `backend/requirements-ci.lock` 和 `npm ci --prefix frontend`；后端生产镜像安装独立的 `requirements.lock`。锁文件保存已有生产依赖版本，新增 CI 工具单独固定版本。

```bash
bash scripts/ci/run.sh quick backend
bash scripts/ci/run.sh quick frontend
bash scripts/ci/run.sh quick architecture

# 每次使用全新 scope、私有 env 文件和报告目录；不要引用生产配置。
export CAD_CI_SCOPE=cad-ci-core-local-001
export CAD_CI_ENV_FILE=/tmp/cad-ci-core-local-001.env
export CAD_CI_REPORT_ROOT=/tmp/cad-ci-core-local-001-reports
export SANDBOX_IMAGE=<本次构建并验证的沙箱镜像>
export CAD_CI_OBJECT_STORE_IMAGE=<本次构建并验证的对象存储镜像>
bash scripts/ci/services.sh up
source "$CAD_CI_ENV_FILE"
bash scripts/ci/run.sh core
bash scripts/ci/services.sh down

# 独立服务 scope 上使用同样入口。
bash scripts/ci/run.sh lifecycle
bash scripts/ci/run.sh browser
# lifecycle 另需 CAD_CI_RELEASE_BASELINE 指向上述旧提交的独立 worktree。
# deploy 另需本次 BACKEND_IMAGE、FRONTEND_IMAGE、MONITORING_IMAGE 等交付镜像。
bash scripts/ci/run.sh deploy
```

Actions 负责安装环境、构建镜像、创建服务和上传证据；测试选择由这些脚本统一维护。完整运行前须准备 Playwright Chromium。每个重测试 job 有自己的 PostgreSQL、Temporal、任务队列、MinIO 和对象前缀／bucket；生命周期子组另有独立数据库。

普通服务清理核对创建时的随机 owner 标记；交付测试先证明 Compose 项目和卷不存在，只删除自身新建资源。不会使用生产 `cad-native`、生产配置或 `down -v`，也不做全局 prune。失败时保留日志、JUnit、浏览器 trace 和失败计划；测试失败钩子在 fixture 清理前采集现场。现场采集有 128 项／64 MiB 预算，采集错误不能使原测试成功。

原生库导入和建模测试明确使用 1 GiB 内存预算，OOM 检查仍必须拒绝超限分配；不扩大建模等待超时。原生测试根据 `SANDBOX_RUNTIME` 选择真实 Docker／Podman，保留原断言。

lint 首先覆盖约束修复核心七个模块的语法及未定义名称检查，严格类型检查覆盖 identity／projects 两个已有类型域。Pydantic 动态约束模型中的两个字段名字符串有 Ruff `F821` 基线例外，范围仅限该文件。没有宣称全仓严格 lint／类型检查已经完成。

Python／npm 缓存按锁文件失效；BuildKit 分镜像缓存，缓存写入故障不会隐藏构建故障。各重测试 job 下载同次运行构建的镜像归档并核对源码及字节身份。缓存不作为成功证据；首次无缓存的完整 AMD64 构建仍须由 GitHub 运行证明。

原生镜像和重测试准备阶段只在 `RUNNER_ENVIRONMENT=github-hosted` 的一次性 Linux runner 上释放未使用的 .NET／Android／Haskell／CodeQL SDK，并要求至少 24 GiB 空闲磁盘。本机与 self-hosted 调用会直接拒绝清理。镜像归档完成后只回收本 job 的独立 Buildx builder；各测试 job 校验并加载归档后删除下载副本。没有扩大超时、修改运行时依赖或减少必测项。

## 必测集合与失败证据

`docs/architecture/ci-tests.json` 固定测试 ID 和允许跳过的确切原因；`modules.json` 固定 job、报告、原生步骤及重放集合。CI 不从自己的运行结果生成预期清单。新增测试应同时更新清单；删除必测项需要明确审查。

`require_test_report.py` 拒绝非空但无关的报告、遗漏参数化用例、重复 ID、失败及意外跳过。`require_runtime_evidence.py` 还核对原生标记、实际执行命令／日志 hash、重放家族及冻结历史、真实运行时操作和浏览器 trace。`require_regression.py` 最后核对所有 job 与本次运行身份。

本地报告目录默认全新创建，拒绝复用既有 job 证据。失败摘要包含步骤、日志片段和本地复现入口；复现重测试时须先按上面的隔离服务配置准备环境。Actions 的环境安装／镜像构建故障另见对应步骤日志。付费模型密钥不进入 PR；默认 `contents: read`，第三方 Actions 固定提交 SHA，禁用 `pull_request_target` 执行待审代码。

## 可选真实模型评测

独立 `cad-evaluation` GitHub environment 配置以下变量和测试凭据，未配置则在模型调用前失败。

- 变量：`CAD_EVAL_CURRENCY`（CNY／USD）、`CAD_EVAL_RUN_LIMIT`、`CAD_EVAL_MONTH_LIMIT`、`CAD_EVAL_MAX_CALLS`、`CAD_EVAL_MODEL`、`CAD_EVAL_CONTEXT_TOKENS`、`CAD_EVAL_OUTPUT_TOKENS`、输入／输出每百万 token 单价 `CAD_EVAL_INPUT_PRICE_PER_MILLION`／`CAD_EVAL_OUTPUT_PRICE_PER_MILLION`。
- 地址：HTTPS `CAD_EVAL_PROVIDER_URL`（含 `/v1` 的 OpenAI 兼容地址）；持久月度账本的 `CAD_EVAL_LEDGER_ENDPOINT`、`CAD_EVAL_LEDGER_BUCKET`。
- 独立 secrets：`CAD_EVAL_PROVIDER_KEY`、`CAD_EVAL_LEDGER_ACCESS_KEY`、`CAD_EVAL_LEDGER_SECRET_KEY`。账本使用专用前缀和允许条件写入的 S3 凭据，与 CAD 用户产物分开。

网关在每次请求前按最大上下文／输出及配置单价预留费用，核对模型、调用次数和每次运行上限；实际 usage 完整时才释放未用预留。月度 S3 账本用条件写入避免并发超支；中断、缺少 usage 或账本结算失败都保留预留。单价是管理员配置，不等于供应商账单。

当前固定 11 项 live 用例覆盖原有八个受排除的真实 provider 用例、原生自然语言生成与 L2 检查修改两项、有效冻结基线的真实约束修复一项。仍不等于自然语言建模整体成功率，也不证明制造条件。

需要验证实际模型表现时，可手动运行评测，再下载该运行证据并验证：

```bash
python scripts/ci/require_live_evidence.py <解压后的证据目录> \
  --sha <待发布的完整提交SHA> --model <配置的模型ID>
```

该可选验证要求实际调用、11 项全部通过、费用与月度结算证据完整、来源匹配且在 24 小时内。它仅验证一次实际评测的证据；PR、定时任务与发布都不会自动要求或启动付费评测。普通 CI 使用离线预算测试和隔离 S3 条件写入验证费用控制流程，不能将这些流程验证描述为真实模型能力评测通过。

## GitHub 主分支保护

YAML 的汇总 job 不会自动启用保护。main 仍需设置：通过 PR 合并、必需状态检查 `Required regression gate`（限定 GitHub Actions）、合并前与最新 main 兼容、管理人员同样受规则约束且不保留日常绕过，并禁用强推和删除。

GitHub Free 的公开仓库支持保护；私有个人仓库需要 GitHub Pro 等支持套餐。改为公开后仍须配置规则，同时会公开源码和历史。本轮没有改变仓库可见性或远端保护设置。[GitHub 官方说明](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)。

本轮验证范围和实际结果见 `docs/verification/ci-regression-20261003/implementation.md`。GitHub 合入前结果以对应提交的 `Required regression gate` 为准，本轮没有调用付费模型。
