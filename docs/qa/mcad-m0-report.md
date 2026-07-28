# M0 真实 MCAD 发布门禁报告

## 结论

记录时间：2026-07-28（Asia/Shanghai）

**M0 工程实现与内部回归通过，但当前不允许发布。**

阻塞原因不是前端占位或本地 MCAD Runtime 故障，而是 Moonshot 账号余额不足。候选评测的 150 次真实模型调用全部收到 HTTP 429 `exceeded_current_quota_error`，所以发布门禁按预期返回非零状态，并报告 3 个硬回归和净回归。没有把未执行、缺少产物或外部服务失败包装为成功。

恢复模型服务额度后，必须使用本报告“重新放行”中的原命令重跑 50 × 3 候选评测；门禁通过前不得将 M0 标记为可发布。

## 测试范围

- 统一 `ExecutionBackend`、Podman 隔离执行、错误分类与业务层解耦
- CadQuery、build123d、OCP/STEP、STL、DXF、SVG、PNG、inspect、implicit CAD
- REST、WebSocket 进度/断线重连、SQLite 历史、版本父子关系、回滚
- 鉴权、文件所有权、未授权拒绝、缺失文件与过期版本
- 前端真实后端映射、参数校验、Change Set、导出、刷新恢复
- 1440、1280、1024、768、390 像素响应式布局与浏览器控制台
- 后端 hermetic 回归、Fusion 契约、前端 lint/test/typecheck/build
- 50 个固定 MCAD 用例，每个 3 次，基线/候选严格可比性与产物门禁

## 自动化回归结果

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 后端完整 hermetic | `cd backend && python3.12 -m pytest -m "not docker and not llm and not fusion_e2e" -q` | 920 passed，14 skipped，1 deselected |
| Fusion schema | `python3.12 scripts/fusion360/generate_schema.py --check` | 通过 |
| Fusion Python 语法 | `python3.12 -m compileall -q backend/app/fusion360 fusion_addin/CADAgentFusionConnector scripts/fusion360` | 通过 |
| Fusion shell 语法 | `bash -n scripts/fusion360/*.sh`（逐文件） | 通过 |
| Fusion 契约 | `cd backend && PYTHONPATH=.. python3.12 -m pytest tests/fusion360 -m "not fusion_e2e" -q` | 189 passed，1 deselected |
| 发布门禁单元测试 | `cd backend && python3.12 -m pytest tests/test_eval_cases.py tests/test_eval_metrics.py -q` | 53 passed |
| 前端 lint | `cd frontend && npm run lint` | 通过 |
| 前端契约/交互测试 | `cd frontend && node --test --experimental-strip-types tests/*.test.ts` | 21 passed |
| 前端 TypeScript | `cd frontend && npx tsc --noEmit -p tsconfig.app.json` | 通过 |
| 前端生产构建 | `cd frontend && npx vite build` | 通过；仅保留 Viewer3D 907.56 kB 的体积警告 |

## 真实 Runtime 与调用链验证

### 隔离 MCAD Runtime

使用 `localhost/cad-agent-sandbox:m0-unified` 在以下约束下重新运行真实探针：

- `--network none`
- `--read-only`
- `--cap-drop ALL`
- `--security-opt no-new-privileges:true`
- 512 PID、内存、CPU 与临时目录限制

从当前源码重新构建 `localhost/cad-agent-sandbox:ci` 后，探针连续 3 次均为
`status=success`。真实生成并验证：

- STEP 20,235 bytes
- STL 26,084 bytes
- DXF 15,536 bytes
- SVG 11,474 bytes
- CAD PNG 18,976 bytes
- implicit STL 583,284 bytes
- implicit PNG 10,145 bytes
- build123d STEP 16,386 bytes
- inspect JSON 390 bytes

主要 Runtime 版本：Python 3.12.10、CadQuery 2.8.0、build123d 0.11.1、cadquery-ocp 7.9.3.1.1、ezdxf 1.4.2、Playwright 1.60.0、Three.js 0.160.0。

详细的固定镜像、隔离策略和先前恶意输入探针见 [mcad-m0-runtime.json](./mcad-m0-runtime.json)。

### API、WebSocket、历史与权限

真实 Podman 建模回归：

- WebSocket 客户端在任务执行中断开，任务继续运行。
- 重连并请求恢复后收到 `running` 状态及最终成功结果。
- 任务 `54b2bbdb-223c-4104-b283-d8a2346c27d2` 生成 STEP 63,328 bytes、STL 473,084 bytes，两个文件均通过真实 HTTP 下载。

真实鉴权回归：

- 请求 `c1c5ce6b-a68a-4f52-a076-9ef290d124b6` 成功，模型体积 1,296 mm³。
- 已鉴权历史查询返回 200，并返回版本 2、1。
- 已鉴权 STEP 下载返回 200，文件 15,422 bytes。
- 同一文件未携带身份时返回 401。

真实版本与回滚回归：

- 连续执行生成宽度 20 mm 与 24 mm 的两个真实模型，体积为 1,000 与 1,200 mm³。
- 新版本 9 的 `parent_snapshot_id` 正确指向版本 8。
- POST 恢复版本 8 返回该版本的代码、参数、STEP/STL、验证及 inspect 结果；恢复结果宽度 20 mm、体积 1,000 mm³。

异常链路已在本轮 M0 集成中真实验证：

- 不安全导入：`ValidationError`，零产物。
- 未生成 CAD 对象：`InvalidCode`，零产物。
- 真实超时：`ExecutionTimeout`，零产物。
- 不存在快照：404。
- 不存在文件：404。
- Runtime 不可用：readiness 503，且不会触发 LLM 修复。
- LLM 额度不足：用户界面明确显示“模型服务额度不足”，不进入机械设计成功状态。

## 浏览器与响应式验证

真实打开 `http://127.0.0.1:5173/` 并验证 1440 × 900、1280 × 800、1024 × 768、768 × 1024、390 × 844：

- 首屏 Prompt、制造方式、示例、历史项目入口布局正常。
- 390 像素下 Prompt、示例和项目工作区为单列，顶部操作保持可访问。
- 在真实页面提交“设计一个 10 mm 的实心立方体”，后端返回额度错误后，机械设计阶段显示“存在问题”，机械入口、检查和导出保持禁用。
- ECAD 正常可见并明确标记后端未接通，没有伪造完成状态。
- 所有检查视口均无浏览器控制台错误。

截图证据保存在本机临时目录：

- `/tmp/task9-desktop.png`
- `/tmp/task9-1280.png`
- `/tmp/task9-1024.png`
- `/tmp/task9-768.png`
- `/tmp/task9-390.png`
- `/tmp/task9-llm-quota.png`
- `/tmp/task9-workspace-390.png`

## 50 × 3 候选评测

### 可比配置

基线与候选使用相同的：

- 模型：`kimi-k2.7-code`
- Provider：`moonshot`
- temperature：0.2
- RAG：开启
- 用例：50 个，`case_set_hash=ceea5cb4e4eb`
- 重复：每例 3 次
- 并发：4
- 执行器：Podman
- 基准 Runtime digest：`sha256:5fa2d755fce931ac2325d53ce6aa84fd800aa31f522b7eba11159a728eb1c642`
- 架构：arm64

基准 Runtime 被保留用于苹果对苹果的发布比较；当前统一 Runtime 另由上面的独立真实探针验证。原始证据：

- [基线报告](./mcad-m0-baseline.json)
- [候选报告](./mcad-m0-candidate.json)

### 比较结果

| 指标 | 基线 | 候选 | 变化 |
| --- | ---: | ---: | ---: |
| pass@1 | 11.3% | 0.0% | -11.3% |
| pass@k | 20.0% | 0.0% | -20.0% |
| pass^k | 6.0% | 0.0% | -6.0% |
| verdict pass | 2.7% | 0.0% | -2.7% |
| printable | 11.3% | 0.0% | -11.3% |
| one-shot | 13.3% | 0.0% | -13.3% |

候选的 150 次调用全部在 Planner LLM 阶段因 Moonshot HTTP 429 结束，实际 MCAD 执行次数为 0。门禁检测到：

- 3 个硬回归：P01、P06、P08 从 100% 降为 0%。
- 4 个显著破坏用例：P01、P05、P06、P08。
- 固定用例 0 个，形成净回归。
- 零 false success。
- 元数据和用例集合完全可比。

`python3.12 -m benchmark.compare ../docs/qa/mcad-m0-baseline.json ../docs/qa/mcad-m0-candidate.json` 返回 1。`benchmark.eval --baseline` 同样传播门禁失败状态，不能被 CI 忽略。

说明：基线 pass@1 标准差为 26.61%，高于基线均值，因此“均值减一个标准差”的数学下限被截断为 0%。候选虽然未触发该单项下限，但硬回归和净回归仍可靠地阻止发布。当前基线成功率本身偏低，应作为后续产品质量工作的首要指标，而不是商业上线目标。

## 本轮发现与修复

1. 原比较器只检查净回归/硬回归，未验证模型、temperature、RAG、用例集、并发、执行器、镜像 digest 和架构是否一致。
   - 已加入严格元数据可比性检查。
2. 原比较器未对“成功但 STEP/STL 不可读、几何为空、少于四视图”的候选结果执行发布阻断。
   - 已加入 3D/2D 真实产物证据门禁和 false-success 检测。
3. `benchmark.eval --baseline` 原来只打印失败，不向 shell/CI 返回失败。
   - 已传播非零退出状态。
4. CI 原来只有无 Docker、无 LLM 的 hermetic 测试，无法证明 MCAD Runtime 镜像可构建、可运行。
   - 已增加独立 `mcad-runtime` Job，在生产级隔离参数下构建并运行真实探针。
5. 新增 Runtime CI Job 最初使用 256 PID；真实重建后探针出现 1/3 次 Chromium
   `SIGSEGV`，而同一镜像重跑可成功，属于资源阈值抖动。
   - 已将 CI 对齐到既有验证基线的 512 PID；修复后连续 3/3 次完整探针通过，
     网络禁用、只读根文件系统、capability drop 和 `no-new-privileges` 均保持不变。
6. 外部模型额度不足会让完整生成链路无法继续。
   - 产品已准确降级并禁用后续成功操作；此问题需要补充 Provider 额度或切换到已配置的真实 Provider，不能在代码中伪造成功。

## 仍存在的风险

- **发布阻塞：** Moonshot 账号余额不足，完整自然语言生成/修改无法通过发布门禁。
- **质量风险：** 最近可用基线 pass@1 仅 11.3%，距离可规模化付费产品仍有明显差距。
- **统计风险：** 当前基线方差较大，“均值减一个标准差”下限退化为 0；硬回归、用例级门禁必须继续保留。
- **性能风险：** 前端 Viewer3D 生产 chunk 为 907.56 kB（gzip 243.46 kB），构建通过但需要在 M1 做按需加载/拆包基准。
- **架构边界：** M0 仍是 SQLite + 进程内编排，具备断线持久状态和诚实重启失败语义，但不等于 Temporal 级持久工作流。
- **外部 Connector：** ECAD、Onshape、Fusion 等保持独立边界；未配置的能力不会被标记为完成。

## 重新放行

补充 Moonshot 余额或配置另一个真实 Provider 后：

```bash
cd backend
SANDBOX_RUNTIME=podman \
SANDBOX_IMAGE=localhost/cad-agent-sandbox:m0-baseline-3629b54 \
python3.12 -m benchmark.eval \
  --n 3 \
  --cases all \
  --concurrency 4 \
  --baseline ../docs/qa/mcad-m0-baseline.json
```

只有命令返回 0，并同时满足零硬回归、无净回归、无 false success、所有成功产物可读且几何/四视图门禁通过，M0 才可转为发布候选。
