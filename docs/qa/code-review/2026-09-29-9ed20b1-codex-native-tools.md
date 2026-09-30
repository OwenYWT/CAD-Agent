# CAD-Agent Review: 2026-09-29 9ed20b1 codex-native-tools

## Conclusion

- Status: **FAIL**。确认 2 项 P1、2 项 P2；本次既有回归通过不能替代新增反例。
- Reviewer agent: Codex `/root`，单代理审查。
- Review ID: `codex-native-tools`。
- Reviewed at: 2026-09-29 21:15 +08:00；机器时间见证据索引。
- Repository revision: `9ed20b1b22a3c6d55172ef5da58669564aab6d99`，`fix/native-engineering-reliability`。
- Review range: 本地跟踪默认分支的 merge-base `62084d5c84ead7bcd88030695fdfcd61112280ea..HEAD`；重点追踪最近提交 `09c3ccb..9ed20b1` 的 193 个文件。没有把本地 `origin/main` 当作已实时核对的 GitHub main。
- Working tree state: 开始时干净。结束时只新增本次审查报告、证据及审查记忆。
- Business code modified by reviewer: **No**。未修改应用测试、部署或数据库；未提交、推送、合并或部署。
- 本报告不宣称逐行审完范围内全部旧改动，也不宣称完成本版全栈发布验收。

## Change scope

本轮变更包含 Agent 工具、需求契约、原生操作、检查点、持久化 CAD Job、独立工程验收、少量草图编辑适配及测试证据。扫描范围还包含此前解耦和发布修复，不能把整个扫描范围都归因于最后一个提交。

按当前实现追踪的功能域：04 需求依据、05 Agent 协作、06 状态恢复、07 原生建模、08 特征参数、09 草图、10 场景、11 装配、12 检查证据、14 候选提交、15 历史、19 导出、21 能力目录、24 运行保障。重点缺陷落在 07、12、14、19。权限/数据库/外部连接的完整现实行为未由本轮重新验收。

功能清单是 2026-09-14 的历史基线，包含已变化的四区布局和用量统计描述；本轮以实际源码为准，没有将旧清单文字当成现状。技能映射校验通过只说明映射结构及引用路径有效，不说明新增功能已全面覆盖。

## Findings

### R01 / P1：径向壁厚只检查半径差，缺失材料仍然通过

- 位置：`backend/sandbox/feature_verification.py:538–564`，`measure_checks()` 的 `radial` 分支；圆柱完整性判断还依赖参数区间，见同文件 `308–377`。
- 复现：外半径 10 mm、内半径 8 mm、高 20 mm 的圆管。正例为完整圆管；负例一在壁内挖半径 0.5 mm 球形空腔，负例二横向钻 Ø2 通孔。
- 独立 Boolean 量测分别确认缺失材料 `0.5235987755982989 mm³` 和 `12.585299078394836 mm³`。两件仍为有效单实体，适合检验验收器是否识别实体内部缺陷。
- 实际结果：两负例都输出径向壁厚 `2.0 mm / passed`；重新导出、读取 STEP 后，`validate_geometry_files()` 总结仍为 `passed`。
- 交叉验证：使用真实 `AcceptanceContract` 规范化输入后重跑，再调用真实 `DurableGeometryReport`、`verify_geometry_evidence()` 和 `acceptance_outcome()`；契约摘要匹配，后端仍接受通过结论。并非审查脚本省略契约字段造成的假象。
- 根因：同轴、半径及轴向/角度参数范围只能证明边界关系，不能证明内外边界之间整层材料连续存在。表面被裁剪打孔时，底层参数范围仍可能覆盖一整圈。
- 影响：用户要求径向侧壁完整达到指定厚度时，隐藏空腔或穿孔可被误报符合要求。范围限定为 `radial`，不能据此否定已另有材料层证明的 `surface_normal` / `continuous_normal`。
- 建议：建立所声明测量域的完整材料层与裁剪边界证明，或明确返回无法验证；不能只扩大数值容差或增加针对球形空腔的特判。
- 验收负例：隐藏空腔、横向穿孔、局部切槽、分裂圆柱面；完整侧壁仍通过；最终 STEP 和服务端门禁均须复核。
- 证据：[几何反例](2026-09-29-9ed20b1-codex-native-tools/evidence/geometry-probe.json)、[规范化契约交叉验证](2026-09-29-9ed20b1-codex-native-tools/evidence/cross-validation.json)。属于 **RUNTIME + LOGIC**，没有冒称已通过 HTTP 提交为版本。

### R02 / P1：包围盒数值扩张被当成真实尺寸错误

- 位置：`backend/sandbox/feature_verification.py:453–463` 与 `569–574`，`measure_checks()`。
- 复现：同一 Ø20、高 20 mm 圆管，横向开孔但不改变外轮廓的轴向极值。圆管、球体、立方体、横向开孔立方体作为对照。
- 实际结果：前四个对照按名义尺寸检查通过；横向开孔圆管的包围盒为 `20.000000200000002 mm`，三个方向均失败。重新导出并读取 STEP 后仍失败。
- 根因：以 OCCT 包围盒跨度作为精确尺寸，但只允许单个 `Precision.Confusion`（`1e-7 mm`）误差；没有处理包围盒两端扩张和相应的测量不确定度。这里的 Boolean 切除不会把外形增大，误差来自量测数值而非需求尺寸改变。
- 影响：未提供制造公差的合法名义几何可能被拒绝，并触发无意义修复；削弱真实复杂模型成功率。
- 建议：区分几何极值、保守包围盒和测量误差，使用可解释的不确定度传播；不能让 AI 修改用户尺寸迎合检查，也不能固定加一个大公差掩盖问题。
- 验收：正例覆盖切孔、曲面 Boolean、变换和 STEP 往返；真实超尺寸负例必须继续失败。
- 证据：[尺寸反例与对照](2026-09-29-9ed20b1-codex-native-tools/evidence/dimensions-probe.json)。属于 **RUNTIME**。

### R03 / P2：工程量测结果在用户证据接口中被过滤

- 位置：`backend/app/services/task_evidence.py:12–17,37`，`project_evidence()`；消费者为 `backend/app/api/tasks.py:128` 和 `frontend/src/components/agent/TaskCard.tsx:10`。
- 复现：将真实内核产生的几何报告交给实际投影函数，使用正确报告摘要和已选入修订的 manifest/evidence 关联。
- 实际结果：`selected_for_revision=true`，但返回给用户的 `report` 去掉了 `acceptance` 和 `request_sha256`。因此原本存在的逐项量测值、检查方法、失败细节及契约绑定信息无法经该接口查看；前端的“量测与规则原文”也只得到过滤后的内容。
- 根因：新增报告契约未同步到既有公开字段白名单。内部保存和验证已接入，用户证据读取链没有闭合。
- 影响：用户看到检查状态，却不能核对新增孔深、壁厚等逐项证据。此项是新契约与旧消费者的集成缺口，过滤函数本身不是本提交新写的。
- 建议：设计安全的公开工程证据 DTO，携带规则/期望/实测/结果及来源绑定；同步前端和接口回归，继续隐藏运行时内部路径。
- 证据：[实际投影结果](2026-09-29-9ed20b1-codex-native-tools/evidence/projection-probe.json)。属于 **CONTRACT_ONLY / LOGIC**；未启动浏览器声称页面实测。

### R04 / P2：STL 单格式请求被受理，但缺少新验收必需的 STEP

- 位置：`backend/app/freecad/operation_generator.py:639–647` 的 `_required_formats()`；`backend/sandbox/geometry_validation.py:267–277`；请求契约见 `backend/app/models/workflow_requests.py:278,309–317`。
- 复现：FreeCAD 生成请求 `output_formats=('stl',)` 通过真实工作流请求模型校验，生成器要求的产物为 `fcstd, stl`，没有 STEP。
- 同一真实 20 mm 立方体：STL 无新增工程契约时通过；加入明确尺寸验收后变为 `indeterminate / measurement_requires_one_parseable_step`；改用 STEP 则通过。
- 交叉验证：规范化需求契约及真实服务端证据校验均确认结果，未通过 Mock 替换内核。调用链中几何门禁为 required，不确定结果不能形成可提交候选。
- 根因：用户交付格式与内部验收输入共用同一个格式列表；新增 BRep 验收强制依赖 STEP，但上游没有补充内部测量产物，也没有在请求入口说明限制。
- 影响：默认同时导出 STEP/STL 不触发；受支持的 STL 单格式生成任务会在完成建模后无法通过工程门禁。
- 建议：将内部验收产物与用户导出选择分离，或从经核验的原生文档执行等价独立测量；不能跳过工程检查让任务成功。
- 证据：[格式对照](2026-09-29-9ed20b1-codex-native-tools/evidence/formats-probe.json)、[请求及格式推导](2026-09-29-9ed20b1-codex-native-tools/evidence/projection-probe.json)、[服务端交叉校验](2026-09-29-9ed20b1-codex-native-tools/evidence/cross-validation.json)。属于 **RUNTIME + CONTRACT_ONLY**，不是新的完整 HTTP/Temporal 复现。

## Implementation trace

| 契约/符号 | 实现链与消费者 | 判断 |
| --- | --- | --- |
| 需求及工程验收 | Planner → AcceptanceContract → durable plan → geometry gate | 有真实约束和纠错；自然语言完整语义覆盖仍不能仅凭 Schema 判定 |
| 检查点 | Agent 工具 → 持久化源码 → execute_freecad → accepted manifest → checkpoint_context | 核对了租户/候选/工作流/基础版本、摘要、产物大小及执行模式；真实保存并重开原生文档的既有契约通过 |
| 最终几何 | 最终 STEP → measure_checks → report → verify_geometry_evidence → required gate | R01/R02/R04；报告完整性不能弥补量测算法错误 |
| 检查详情 | agent_validation_evidence → project_evidence → task validation API → TaskCard | R03；新增字段在旧白名单处丢失 |
| 版本与候选 | checkpoint 模式不允许作为最终候选；commit 使用候选证据 | 契约和源码有保护；本轮未重跑数据库竞争/真实用户提交 |
| 流程兼容 | workflow patches → 固定旧发布历史回放 | 12 份通过；不等于新任务中断恢复的现实验收 |

## Encoding and fake implementation review

风险扫描对范围内 456 条路径产生 130 条线索：18 条 unimplemented、45 条异常/空分支、12 条 static-success、52 条 mock/fixture、3 条 placeholder；无编码损坏线索。

- 18 条 TODO 均位于从 FreeCAD 导出的 API 文档字符串，不是 CAD-Agent 业务占位实现。
- 已检查的生产异常分支执行错误记录/重抛、取消清理或明确 indeterminate，不是静默成功。
- 检查 Activity 的 `status=completed` 表示检查已完成，实际 `outcome` 保留 failed/indeterminate；不能由该字符串认定假成功。
- 其余 fixture/mock/static 标记主要来自受控测试、历史报告、兼容门面及提示词中的禁止假结果说明，没有据此将 Mock 测试称为真实模型验收。
- R01 是实际量测算法的误通过，不是固定返回成功；两者需区分。
- `git diff --check` 对本轮工作树通过；最近提交范围的检查提示原始日志/STEP 尾部空白及 `agent_tools.py` 末尾空行。属于格式问题，保留证据，未在审查期间修正。

## Test evidence

证据根目录：`docs/qa/code-review/2026-09-29-9ed20b1-codex-native-tools/evidence/`。

| 层 | 本轮实际命令/检查 | 结果 | 证据性质 |
| --- | --- | --- | --- |
| 技能映射 | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | PASS：24 域、345 引用路径 | 静态 |
| 风险扫描 | `python .../scan_review_risks.py --base 62084d5 --head HEAD --worktree --json` | 已人工分类线索；无扫描器直接定罪 | 静态 |
| 架构边界 | `python scripts/architecture/check_boundaries.py` | PASS | 静态 |
| 后端 | `APP_ENVIRONMENT=test DURABLE_CONTROL_PLANE_ENABLED=false PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q --tb=short -m 'not docker and not llm and not fusion_e2e' --junitxml=.../backend.xml` | 1700 passed，204 skipped，1 deselected | LOGIC / CONTRACT_ONLY，不是完整服务验收 |
| 前端 | `node --test --experimental-strip-types tests/*.test.ts` | 155 passed | LOGIC |
| 前端静态/构建 | `npm run lint`；分别 `tsc -p tsconfig.app.json`、`tsc -p tsconfig.node.json`，buildinfo 写入报告目录；`vite build --outDir <报告目录>/frontend-dist` | PASS；保留原有包体积告警 | BUILD，未浏览器验收 |
| Temporal 兼容 | `PYTHONPATH=. python tests/e2e/replay_released_histories.py --output <报告目录>/released-replay.json` | 12 份固定旧历史通过 | REPLAY；无新服务写入 |
| 真实内核已有契约 | `run_runtime_review.py`：运行当前 `feature_verification_contract.py`、`curved_surface_contract.py`、`freecad_checkpoint_replay_contract.py` | 3 组通过；要求成功 marker 且无 Traceback | RUNTIME；检查点使用已记录模型源码，不是新模型调用 |
| 新几何反例 | `probe_geometry.py`、`probe_dimensions.py`、`probe_formats.py` | 确认 R01/R02/R04 | RUNTIME，真实 OCCT/BRep 与 STEP/STL |
| 服务端证据交叉核验 | `cross_validate_probes.py`、`probe_projection.py` | 确认 R01/R03/R04 | LOGIC + RUNTIME，非数据库/HTTP 验收 |
| 当前源码对应性 | 对比 666 文件记录；运行时核对 4 个直接使用的生产文件 SHA | 全部一致 | PROVENANCE |

Python 使用 `/Users/wentao/miniconda3/bin/python`。内核镜像 digest：`sha256:4003a8e12d80286a1ea8415d5c93b59115d500153cd77fcf767f818464e969ee`；隔离、无网络、只读根文件系统，测试临时数据位于容器 tmpfs。没有停止或改写既有测试服务。

`run_runtime_review.py` 第一次因审查脚本自身仓库根路径少一层而失败；修正该报告目录内脚本后重跑通过。没有修改业务代码或既有应用测试来解决这个错误。

## Gaps and blocked prerequisites

- **BLOCKED（本轮完整服务/产品闭环）**：未配置专用于本审查的数据库、对象存储、Temporal/API/Worker 和浏览器账号组合。现存旧测试栈的 backend 状态为 unhealthy；其旧 Worker 不代表本 SHA。未用旧服务结果代替最新版，未清理共享数据库或用户数据。
- 后端 204 个跳过包括 PostgreSQL、对象存储、真实服务、模型/设备/运行时条件，以及 2 个 chromadb 环境条件。详细原因可在 `backend.xml` 查看；跳过不计通过。
- 本轮没有新付费模型调用，不给出复杂任务成功率，没有重跑新版完整十题建模。
- 未重做云升级/回退、浏览器、多人撤权/竞争、在途任务跨 Worker 恢复、FEA/CAM/外部 CAD/设备验收。
- 能力目录不等于全部 FreeCAD 功能认证；当前可证明曲面域不等于任意曲面验收。本轮没有为这些已知未完成目标追加虚假的通过结论。
- R01/R02 是直接运行时问题，足以给出 FAIL，无需把未运行的端到端层包装成通过。

## Knowledge update

- 本报告及机器证据：[review-summary.json](2026-09-29-9ed20b1-codex-native-tools/evidence/review-summary.json)。
- 新增审查记忆：`.agents/skills/cad-agent-review/references/review-memory/2026-09-29-9ed20b1-codex-native-tools.md`。
- 未修改稳定功能映射或不变量；只记录已经由实际源码和新证据确认的观察。
- 后续优先处理 R01/R02，再补齐 R03/R04，增加反例并重跑所影响的完整链路。没有在本次审查内实施修复。
