# CAD-Agent 复审：PR #27 外部报告对照

## 结论

- **FAIL，建议修复剩余问题后再合并。** 外部报告的三项 P1 在当前未提交工作区均未复现；封存 Activity 等评分、旧题库 live 口径及部分二维报告不一致仍存在。
- 审查者：Codex；编号：`codex-pr27-external-recheck`；日期：2026-11-27。
- 外部报告：`D:\project\projectcad\2026-11-26-20ea31b-codex-pr27-recheck.md`。报告中同名的相邻附件目录未提供；下文不以历史附件或 CI 结论替代现测。
- 范围：`7508b94d9f4e1cf0a306508d66ab27729db2a18e...20ea31b5d0d5faf766be2b178c33c374e7559154` 加当前工作区；分支 `feature/live-cadtest-evaluation`。已有 17 个受跟踪修改、两份新增测试及未跟踪 QA/计划文件，均保留。结论不等价于仅检出 `20ea31b` 的结果。
- 范围涉及功能域 05、06、07、11、12、14、19、24；追踪受信任提交、封存、STEP/DXF 评分、报告和 CLI。该差异未修改前端、WebSocket 契约、数据库迁移或权限规则。本次未修改业务代码和应用测试。

## 仍存在的问题

### P2 · 封存 Activity 等待可选评分（F08）

`backend/app/workflows/activities.py:118` 在业务封存回调返回后直接等待评分，再记录终态。用受控回调及 `asyncio.Event` 暂停观察器时，事件为 `seal_finished → observer_started`，Activity 仍未返回；释放后才出现 `observer_finished → terminal_recorded`。`backend/app/workflows/agent_v2.py:196` 的非执行 Activity 默认 300 秒，STEP 和原生检查可串行各占 180 秒，另有下载时间。**已证实等待，未实测 Temporal 超时或实际重试。** 建议独立调度评分并记录 pending/终态，按封存身份幂等写入；业务封存完成不依赖评分器退出。

### P2 · 旧内建题与新题库的 live 兼容性未恢复（F04）

`backend/benchmark/eval.py:509` 的旧 live CLI 构造上下文时未提供题集身份；`backend/benchmark/live.py:326` 默认要求所有题有 CADTest pass。X01/X02 一类旧题仅有业务交付证据时，当前行仍不通过；`7508b94` 同路径只要求已完成且 DXF 可读。读取当前两组数据，内建 50 个 ID 与外部 100 个 ID 交集为 0。旧报表语义与新严格评测需显式区分：要么支持独立校验合同及完整映射，要么明确标记旧指标并声明兼容范围；不要用跳过严格证据替代新 CADTest 通过。

### P2 · 二维新版正常路径一致，但旧/精简报告的 JSON 和 HTML 仍分歧（F04）

`backend/benchmark/live.py:327` 对二维免除 3D native 门禁；`backend/benchmark/cad_test_report.py:256` 无条件要求 `native_delivery_verified=true`。受控报告仅有 `cad_test.status=passed` / `verdict=true` 且 DXF 可读时，JSON 为通过、HTML 为失败。新版评分写入 `delivery.status=passed`，使真正容器正例的 JSON/HTML 都通过、孔位偏移例都失败，因此不能把精简输入反例说成新版生产路径必现。建议生成和渲染复用有路线语义的最终判定，并明确旧报告缺失字段时应如何展示。

### P2 · 实际 STEP scorer 未回传所读文件摘要，审计链仍有缺口

`backend/benchmark/cad_test_engine/score_step.py:26` 的结果没回传 `artifact_sha256`；`backend/app/evaluation/cad_test_runner.py:276` 只在 scorer 提供摘要时判断不一致。此次真实三文件正例 `scorer_artifact_sha256=null`，与 DXF 的严格输出合同不一致。封存校验和只读暂存已阻断报告中的同题换件，并不因此恢复原 P1；但上报结果没有独立确认子进程读到的文件。建议真实 scorer 返回 SHA-256，主运行器拒绝缺失或不符的生产评分结果。

## 外部报告其余事项

| 外部编号 | 当前判断与证据 |
| --- | --- |
| P1-1 原生默认启动导入 | **关闭（RUNTIME）**：默认 wrapper 入口能导入 `/code` 模块；FreeCAD 1.1.4 对真实参考件明确通过，双向差集为 0 mm³。 |
| P1-2 三文件状态 / CLI | **关闭（RUNTIME）**：`ctb-00000007` 固定 STEP/STL，正常 FCStd 退出 0；放大 FCStd 差集约 274603.77 mm³，`status=fail`、退出 1；缺 STL 返回 `required_delivery_artifacts_missing`、退出 1。 |
| P1-3 同题换件 | **当前逻辑关闭**：以真实文件写受控报告 seal，替换同一候选的 FCStd 后，在评分前得到 `blocked/cad_test_artifact_identity_mismatch`；专项测试另覆盖逐种格式换件和无 seal。此次未重跑 PostgreSQL/MinIO 封存服务。 |
| 二维可读即通过、落盘 unknown | **当前路线关闭（RUNTIME）**：真实 Podman 运行新版 DXF scorer；匹配件明确 `passed`、CLI 0，孔位偏移件明确 `failed`、CLI 1，验证了封存摘要。单线、尺寸、位置、实体数、开口和缺真值还经过本机对照。 |
| 缺失文件误判为题库不匹配 | **关闭（LOGIC）**：真实题库及有效受控 seal 下，缺 STEP 实测 `blocked/artifact_not_found`，`dataset_match=true`。 |
| F03 布尔历史 | **选择器逻辑通过**：相关回归通过；本轮真实 FreeCAD 正例是扁平模型，未把选择器夹具算作原生布尔运行。 |
| F05–F06 Worker 路径 | **共享配置合同关闭；实际多进程未复验**：新 live CLI 拒绝目录参数与本地共享环境不符，说明要求已启动进程使用相同环境/挂载。未证明能探测独立 Worker 被运维配置错的情况。旧 `benchmark.eval --live-observe` 仍显式读取 `REPORTS_DIR`，不会跟随 `CAD_EVALUATION_REPORT_ROOT`，属于旧入口兼容性边界。 |
| F07 装配选件 | **逻辑关闭**：显式 primary 替代前缀猜测；歧义直接返回，已有专项覆盖。未复验真实生产装配。 |
| F09 评分改写输入目录 | **关闭**：实际原生调用读挂载 `/input`，sidecar 落评分输出目录；真实对照与源码一致。 |
| F10 终态质量字段 | **关闭**：专项断言 `finish()` 前后投影一致，真实容器 DXF 正反例终态均保留。 |

## 测试与边界

| 层级 | 当前证据 |
| --- | --- |
| LOGIC | 后端 `pytest -q -p no:cacheprovider -m 'not docker and not llm and not fusion_e2e'` 对 CADTest、live evaluation、native selection、既有题集和 metrics 等 15 个模块：197 passed、2 deselected，1 条既有 Pydantic 警告。另用真实文件和本地报告存储验证换件及缺文件。 |
| RUNTIME | `pytest -q -p no:cacheprovider -m docker tests/test_cad_test_review_closure.py`：2 passed。三文件复验使用 `cad-agent-sandbox:latest` 中的 FreeCAD 1.1.4，证据在 `.external-pr27-review-native/summary.json`；二维保存报告在 `.external-pr27-review-dxf/`。 |
| Dataset | `benchmark.cad_test_batch dataset-check`：100 题验证通过；SHA-256 `77c6c137722f3b2fe57059f587aaba7c0d4f37c7f8e06d3180e8553646c089c9`。这是题集完整性，不是模型成绩。 |
| Static | Feature map 24 域 / 433 路径、架构边界与工作区 `git diff --check` 通过。PR 提交范围的空白检查仍有两个历史 QA 文档 EOF 多空行。风险扫描 30 条线索：异常分支及报告文字不自动认定为占位实现；实际等待、合同分歧见 Findings。 |
| SERVICE / PRODUCT | 未重跑 PostgreSQL/MinIO 封存服务、Temporal 超时与重试、付费模型、线上或浏览器全链路；外部报告的服务/CI 数字仅是历史。 |

未修改稳定功能地图或不变量；可复用证据记入同名 review memory。
