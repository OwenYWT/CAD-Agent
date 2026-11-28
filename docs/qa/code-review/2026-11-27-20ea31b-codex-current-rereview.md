# CADTest 修复复审

## 结论

**FAIL，不能确认本轮修复已经彻底闭环。** 本轮修复解决了上一份报告中的部分问题：原生校验器现在显式加入 `/code`，输入 sidecar 不再写入 artifact 输入目录，三文件 bundle 不再静默降级为 STEP 单文件评分，并且原生校验失败会影响 bundle 状态。相关专项测试通过。

但是仍存在可直接复现的 P1 证据完整性漏洞，以及上一份报告中未修复的二维评分和 Worker 配置问题。由于当前 Podman 连接失败，不能把真实 FreeCAD/容器运行判定为通过。

## Findings

### P1: 观察结果旁路仍可伪造 benchmark passed

`backend/app/evaluation/integration.py:1002` 的 `attach_cad_test_observation()` 仍然直接对调用者提供的 `result` 调用 `store.attach_cad_test_result(run_id, result)`，没有检查 benchmark source、case/dataset identity、`cad_test_seal`、封存 artifact 的 SHA-256 或实际被评分文件。该入口仍可把调用者构造的 `status=pass`、完整 score 和 `delivery_verified=true` 规范化为 `status=passed`。

独立反例：创建 benchmark session，设置 candidate revision，但不写 `cad_test_seal`，直接调用 `attach_cad_test_observation()`，结果仍为：

```json
{
  "status": "passed",
  "verdict": true,
  "reason": "cad_test_verified",
  "identity": {
    "workflow_match": true,
    "candidate_revision_match": true,
    "artifact_match": null
  }
}
```

这说明新增的 seal 保护只覆盖 `attach_cad_test_result()` / `attach_cad_test_artifacts()` 的评分路径，不能保护 observation 旁路。当前 Worker 的失败记录和 user-task native 记录确实使用该函数，但它不是可信评分入口；benchmark 结果不能允许该函数落盘为通过。

影响：同一 case、不同 artifact 的伪造结果仍可能进入 live report，违背“评分文件身份必须由 trusted admission/seal 独立绑定”的核心不变量。

### P1: `cad_test_live` 对 unknown/unavailable/缺失结果仍返回成功

`backend/benchmark/cad_test_live.py:102` 只在状态为 `blocked`、`error`、`failed` 时返回退出码 1。`unknown`、`unavailable`、缺少 `cad_test` 状态时返回 0。

因此评分未完成、报告缺失或证据不完整时，依赖退出码的流水线仍可能放行。上一轮报告要求“缺失证据不能成为 pass”，本轮只修复了 batch `score` 路径，没有闭合 live attach CLI 的退出码契约。至少应把非明确 verified/passed 的状态视为非成功，并区分 disabled、not_applicable 和 blocked 的业务语义。

### P2: 2D scorer 仍把任意毫米制非空 DXF 当作通过

`backend/benchmark/cad_test_engine/score_dxf.py:15-34` 只检查 `entities > 0` 和 `document.units == ezdxf.units.MM`，固定写入一个 `dxf.readable.v1` 要求。它没有读取 case 真值、题目 checks、尺寸、孔数量、位置或拓扑。

独立反例：生成仅包含一条直线的毫米制 DXF，当前 scorer 输出 `passed=true`、`requirements_passed=1`、`requirements_total=1`。这与上一份报告中“只有一条直线的负例也得 1/1”一致，说明 F04/P2 尚未修复，且不是单个 case 的问题，而是二维评测路线缺少 case-bound truth comparison。

### P2: Worker 的题库和报告目录仍未形成端到端配置闭环

`backend/app/evaluation/cad_test_observer.py:26-37` 只从 Worker 进程环境读取 `CAD_TEST_DATASET_ROOT` / `CAD_TEST_OUTPUT_ROOT`；`backend/app/evaluation/integration.py:29` 的 `default_report_root()` 仍固定到源码目录下的 `benchmark/reports`。`benchmark.cad_test_batch live --dataset-root` 和 `--report-root` 只配置 CLI 进程，不能自动配置独立 Worker。

因此 CLI 指定题库或报告目录后，Worker 可能仍记录到自己的默认路径，CLI 在指定路径找不到 finished report，或者 benchmark observe 得到 `cad_test_configuration_unavailable`。这与上一份报告 F05/F06 的复现结论一致，本轮未看到对应的 Worker 配置传递改动。

### P2: 终态重建会丢失 `quality_matrix.cad_test`

`backend/app/evaluation/store.py:195-206` 的 `finish()` 每次调用 `build_quality_matrix(report)`，而 `backend/app/evaluation/complexity_profile.py:222-238` 构建矩阵时没有保留已有的 `cad_test` 项。

独立反例：先挂接一个 `passed` CADTest，再调用 `finish()`，顶层 `report["cad_test"].status` 仍是 `passed`，但 `report["quality_matrix"].get("cad_test")` 变为 `None`。这会使终态报告的质量矩阵与顶层 CADTest 不一致，上一份报告 F10 仍未修复。

## 已确认修复

- `backend/app/evaluation/cad_test_runner.py:307-411`：原生校验启动命令显式加入 `/code`；输出 sidecar 写到 `/out`；非零退出码和 JSON `passed` 不一致时不再报告通过。
- `backend/app/evaluation/cad_test_runner.py:546-610`：3D bundle 要求完整 `step/fcstd/stl` 集合，缺件和多余 artifact 不再静默降级；native delivery 失败会把 bundle 变为 `fail` 或 `error`。
- `backend/app/evaluation/integration.py:762-837`：sealed artifact 评分前校验候选 revision、seal 中的 SHA-256/size，并复制到临时目录评分；不能把被测文件自身摘要当作期望摘要。
- `backend/app/evaluation/cad_test_observer.py:62-90`：装配 artifact 不再根据 `combine-*` 文件名前缀猜测 primary，要求显式 `is_primary`。
- `backend/benchmark/cad_test_batch.py:123-148`：只传 STEP 保持单文件评分；只要传入 FCStd 或 STL 就进入完整 bundle 路径，缺件返回非零。

这些结论有单元/集成测试支持，但原生项还缺真实容器证据。

## 验证记录

- `python -m pytest backend/tests/test_cad_test_pass_gates.py backend/tests/test_cad_test_runner.py backend/tests/test_cad_test_integration.py backend/tests/test_cad_test_observer.py backend/tests/test_cad_test_evidence.py -q`：`47 passed`。
- `python -X utf8 -m pytest tests/test_native_delivery_selection.py tests/test_cad_test_report_html.py tests/test_live_evaluation_benchmark.py tests/test_cad_test_self_contained.py -q`：`42 passed`。
- `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py`：通过，24 domains / 433 paths。
- `python scripts/architecture/check_boundaries.py`：通过。
- `git diff --check`：通过；仅有 Windows 换行提示。
- `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base 7508b94d9f4e1cf0a306508d66ab27729db2a18e --head HEAD --worktree`：扫描到 27 条线索，其中生产代码异常分支需人工审查；本报告中的 P1/P2 已由独立反例确认。
- `python -X utf8 -m pytest -m "not docker and not llm and not fusion_e2e" -q`：`3062 passed, 136 failed, 431 skipped, 2 errors`。失败集中包含数据库连接/事件循环、Fusion 安装脚本和大量 API/auth 依赖环境问题；不能作为本轮 CADTest 逻辑全部失败的证据，但也不能称为全量回归通过。
- Podman 真实运行：`podman ps` 被 `D:\tmp\cad_agent_podman_fix` 权限错误阻断，因此未完成 FreeCAD 1.1.4 正反例、native bundle 和真实对象存储链路验收。
- 独立二维反例：仅一条线的毫米制 DXF 当前 scorer 输出 `passed=true`。
- 独立 seal 旁路反例：无 seal 直接调用 `attach_cad_test_observation()` 当前输出 `status=passed`。
- 独立终态反例：`finish()` 后顶层 CADTest 保留，但 `quality_matrix.cad_test` 丢失。

## 正常功能影响

本轮没有发现这些修复直接破坏普通单 STEP 评分、3D bundle 缺件拒绝、装配显式 primary 选择或现有 live report HTML 的专项回归；相关 42 项测试通过。但由于全量后端测试受环境依赖影响大面积失败，且 Podman 无法启动，不能对正常 Worker/API/Temporal/FreeCAD 端到端流程做无条件保证。

## 后续必须补齐

1. 将 benchmark 的所有结果写入统一可信入口；`attach_cad_test_observation()` 对 benchmark 至少必须要求 admission identity、dataset identity、candidate seal 和独立 artifact identity，缺失时只能 `blocked/unknown`。
2. 统一 batch/live CLI 的退出码契约：只有明确满足 CADTest 验收合同才返回 0；unknown、unavailable、missing evidence 和验证错误不能返回 0。
3. 为 2D case 迁移并实现独立真值几何检查，至少覆盖尺寸、实体类型、数量、位置和拓扑，并增加合格件/可读负例对照。
4. 把 dataset/report root 作为受信任的运行配置传递到 Worker，增加 CLI 与 Worker 使用不同目录时的端到端控制。
5. 让 `finish()` 保留并重新合并 `quality_matrix.cad_test`，增加“挂接前、挂接后、终态后”一致性测试。
6. 恢复 Podman 连接后，重新运行真实 FreeCAD 正例、放大 FCStd 反例、错 case/换件/改件/缺件/歧义装配控制。

## 范围声明

本次只读审查未修改业务代码、应用测试、部署代码或 Git 历史。工作区中原有未提交修改和其他未跟踪 QA/设计文档均未删除、未提交、未改写。
