# CAD-Agent PR #27 · CADTest 问题复验

## 结论

- `FAIL`，建议暂缓合并。三项 P1 均成立，当前评测成绩及批量命令退出码不宜作为 CI 放行依据。
- Codex / `codex-cadtest-findings-validation`，2026-11-27 10:17（UTC+08:00，工作站时间）。
- 分支 `feature/live-cadtest-evaluation`，范围 `7508b94d9f4e1cf0a306508d66ab27729db2a18e...20ea31b5d0d5faf766be2b178c33c374e7559154`，52 个差异路径。审查开始时无已跟踪改动，8 个既有未跟踪文档未动。
- 对照工作区同级原报告 `2026-11-26-20ea31b-codex-pr27-recheck.md`；同级附件缺失。未改业务代码、应用测试、部署文件或 Git 历史。

## 调用链和影响范围

`benchmark/live.py` → `services/durable_submission.py` 绑定题目、题库和路线 → `workflows/activities.py` 封存后调用 `cad_test_observer.py` → `cad_test_runner.py` → `evaluation/integration.py` 与 `store.py` 落盘 → JSON / HTML。离线 `cad_test_batch.py` 和手工补录 `cad_test_live.py` 是另两个入口。涉及功能域 05、06、07、11、12、14、19、24；没有前端、WebSocket 契约、迁移或租户权限差异。评测报告不等于封存或工程 Head。

## Findings

| 问题 | 复验结果与修复方向 |
| --- | --- |
| P1-1 原生启动 | 真实 Podman / FreeCAD 1.1.4 运行生产 `-c` 启动表达式，`backend/benchmark/cad_test_engine/check_native_delivery.py:8` 同目录导入失败，退出 1。诊断补 `/code` 到搜索路径后，方块匹配控制的双向差集 0 mm³、退出 0；FCStd 单轴放大 20% 后差集约 200 mm³、退出 1。修复 `backend/app/evaluation/cad_test_runner.py:357`，然后从默认入口跑几何正反例。 |
| P1-2 多格式假通过 | 受控 STEP 通过与 native 失败，真实 `backend/benchmark/cad_test_batch.py:126` 写出 `status=pass`、`delivery_verified=false`，退出 0；只给 STEP 和 FCStd 也静默退出 0。`backend/app/evaluation/cad_test_runner.py:577` 未合成总判定。显式区分 STEP-only 和 bundle；bundle 缺件或任一必要证据失败必须非零退出。 |
| P1-3 补录制品身份 | 固定 case、dataset、workflow、candidate，在真实 `backend/app/evaluation/integration.py:853` 调用和报告落盘中，只隔离 scorer/native 后重现 `passed / cad_test_verified / artifact_match=null`。手工路径缺独立封存制品核对；自动观察路径已校验对象大小与 SHA-256。以可信封存清单逐份对照，缺失、有歧义或同题换件时在评分前拒绝。 |
| P2 / F04 二维及原有题集 | 真实毫米制法兰和单直线 DXF 均评分通过、`1/1`，归一化均 `unknown`；同一标为通过的 2D 行由 HTML 严格判定为 false。`backend/benchmark/cad_test_engine/score_dxf.py:15` 只查可读性，`backend/benchmark/live.py:326` 与 `backend/benchmark/cad_test_report.py:256` 的路线判定不同。应验证题目几何，并统一评分、落盘与展示门禁。内置 X01/X02 默认也要求 CADTest，历史跨基线服务对照未重跑。 |
| P2 文件诊断 | 本机复现 `artifact_not_found` 经 `backend/app/evaluation/cad_test_evidence.py:170` 被覆盖成题库摘要不一致。保留已确认题库身份及原始错误，不放宽真实身份冲突。 |
| F03 Boolean 历史 | 选择器专项测试通过；历史保留及扁平几何对照未独立重放，P1-1 仍阻断默认入口。 |
| F05 Worker 题库 | 代码确认：CLI `--dataset-root` 不配置另一进程的 Worker；后者读 `CAD_TEST_DATASET_ROOT` / `CAD_TEST_ROOT`（`backend/app/evaluation/cad_test_observer.py:26`）。应在提交前校验 Worker 的题库和摘要。 |
| F06 报告目录 | 代码确认：`backend/benchmark/live.py:414` 改读路径，`backend/app/evaluation/integration.py:26` 落盘仍用 Worker 默认路径。统一配置或显式拒绝未对齐的路径。 |
| F07 装配选件 | 本机复现：`combine` 正例选对，`final` 无法选择，`final` + `combine-bracket` 三种格式均选错；歧义记录后访问 `None.items()`。`backend/app/evaluation/cad_test_observer.py:83` 应依据封存最终步骤选整套交付，歧义后返回。 |
| F08 封存等待 | `backend/app/workflows/activities.py:118` 在业务封存后等待评测，封存时限 300 秒，两个串行子过程默认各 180 秒。代码确认等待关系；实际 Temporal 超时和重试仍是条件风险。应把评测移出封存关键路径。 |
| F09 输入覆写 | 本机复现：即使子过程失败，`backend/app/evaluation/cad_test_runner.py:334` 仍覆盖调用方输入目录的 `delivery-inputs.json`；只读负例未重放。sidecar 应写入运行器自有目录。 |
| F10 终态质量明细 | 本机文件存储复现：补录后存在 `quality_matrix.cad_test`，`backend/app/evaluation/store.py:176` 终态重建后消失，顶层 `cad_test` 保留。终态投影应保留合法评分。 |

## 验证和边界

- `git diff --check` 和 `git diff --cached --check` 通过；范围检查仅两份历史 QA 文档 EOF 空行。
- `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py`、`python scripts/architecture/check_boundaries.py` 均通过；风险扫描 `--base 7508b94 --head HEAD --worktree` 共 60 路径 / 26 线索。异常分支已核对，真正的错误传播和假通过见 Findings，历史文档的 mock/乱码描述不是新业务实现。
- `backend` 下运行 `python -m pytest -q -p no:cacheprovider --basetemp ../docs/qa/code-review/.pr27-review-pytest-20ea31b`，选择 `test_cad_test_{evidence,integration,observer,report_html,runner,self_contained}`、`test_native_delivery_selection`、`test_live_evaluation_{benchmark,contracts,store,writer}`、`test_cad_evaluation_executor`、`test_eval_{cases,metrics}`：186 passed、1 warning。首轮临时路径传错导致初始化错误，纠正命令后通过。
- 原生测试用 `localhost/cad-agent-sandbox:latest`，`podman run --rm --pull=never --network none`，只读挂载引擎到 `/code`，调用 `/opt/freecad/bin/FreeCADCmd -c`；几何文件在容器临时挂载中。是原生故障和对照证据，不是修复后默认入口验收。
- 未独立重放原报告的 PostgreSQL/MinIO 换件对照、Temporal 超时、外部 100 题题集、付费模型和全量 CI。未修改稳定映射或门禁，现有不变量已覆盖这些问题。

先修默认原生启动、bundle 假通过和补录制品身份三个合并阻塞项，再统一路线判定、配置和报告投影。合并前需覆盖同题换件、跨题/题库、篡改、缺证据、摘要冲突、非零退出和歧义选件，不得把本次专项通过替代真实链路验收。
