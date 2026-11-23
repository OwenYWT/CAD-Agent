# CADTest 批量报告审查

- Reviewer: Codex
- Review ID: `cadtest-report-20261123`
- Date: 2026-11-23
- Scope: `backend/benchmark/reports/cad_test/20261123T090009Z_3de005e8/report.json` and its `report.html`
- Review mode: evidence-only; no application code or application tests modified

## Conclusion

这份报告不能直接作为模型质量结论。报告中至少有两类确定的评测链路问题，会把实际运行结果显示成错误或不完整；另外有一条高度疑似几何验收器轴向误判。其余失败目前有原始工作流证据支持，不能直接归因于源码 Bug。

## Findings

### High: 缺失 CADTest 被当成已存在的失败门禁

- Evidence: 6 次运行的 live 状态为 `finished`，且 `step_readable`、`stl_readable`、`geometry_nonempty`、`visual_rendered` 全部为 `true`，但 `cad_test` 是空字典，`artifact_gate.passed` 被写成 `false`。
- Affected runs: `ctb-00000007/run-1`, `ctb-00001411/run-1..2`, `ctb-00003219/run-1..2`, `ctb-00003247/run-1`。
- Root cause: `backend/benchmark/live.py` 的 `_live_cad_test_gate` 只判断 `cad_test` 是否为 `Mapping`。空字典也满足该条件，于是状态被填成 `unknown`，随后 `_artifact_gate_from_live_report` 又强制要求 CADTest `passed` 和 native delivery `true`。`benchmark_row_from_live_report` 同时把空字典写入行数据。
- Impact: 这些运行被报告为最终失败，但真实 live 过程和基础交付门禁已通过。这是确定的报告/门禁误判；正确状态应区分“CADTest 未记录/不可用”和“CADTest 已执行但失败”。

### High: 批量流程没有保证每次运行产生 CADTest 记录，且失败被静默吞掉

- Evidence: 这份报告 20 次运行中只有 1 次有完整 CADTest 结果，另外 19 次为 `unavailable`；其中多次工作流已经 `finished`，原始 live 报告没有 `cad_test_finished` 事件。
- Root cause candidates confirmed in code: `backend/app/evaluation/cad_test_observer.py` 依赖 worker 进程环境中的 `CAD_TEST_DATASET_ROOT`/`CAD_TEST_ROOT`；批量命令的 `--dataset-root` 只用于批量进程加载数据，没有传递给已经运行的 worker。配置缺失或文件/评分器异常时 observer 会直接 `return`，`backend/app/workflows/activities.py` 还会吞掉 observer 异常。
- Impact: 报告无法区分“未配置、未执行、下载失败、评分器失败、记录失败”，导致 CADTest 覆盖率和最终门禁不可信。唯一成功的 `ctb-00000007/run-2` 不能证明其余运行已按同一流程完成 CADTest。

### Medium: `WorkflowPending` 覆盖了真实终态错误

- Evidence: `ctb-00001615/run-1` 和 `ctb-00001817/run-2` 的批量行错误为 `WorkflowPending`，但对应 live 原始报告最终状态已经是 `failed`，并且包含完整的 geometry/planner 失败事件。
- Root cause: `backend/benchmark/live.py` 先保存 `wait_for_compatibility_response` 返回的响应错误，再读取 live report；`benchmark_row_from_live_report` 没有在 live report 已到达终态时用终态错误替换超时阶段的 `WorkflowPending`。
- Impact: 失败原因被错误地显示成“仍在运行”，开发人员无法从批量报告直接判断真正失败点。这里的底层工作流失败本身有证据，不应把它归因于 Temporal 或等待服务 Bug；Bug 在报告错误归因。

### Medium: `ctb-00003247/run-2` 很可能是几何验收器轴向误判

- Evidence: 用户需求只规定 length `0.3`、width `0.7`、height `0.3`，生成物 bounds 为 `[70, 30, 30]` mm。CADTest 数据集的 `reference-validation.json` 以 `[0.3, 0.7, 0.3]` 为参考，并按最大/中间/最小尺寸验证，未要求 length 必须绑定到世界 X 轴。
- Current behavior: durable acceptance 将目标绑定为 `x=30, y=70, z=30`，通过 `brep_extent_on_explicit_axis` 比较 X/Y，所以把几何尺寸正确但绕 Z 轴旋转的长方体判为 `dimension_mismatch`。
- Assessment: 这是高度疑似验收契约不一致，需确认产品是否明确要求世界坐标轴方向。按当前 CADTest 数据集语义，应允许等价轴向排列；在未确认前不应把它归为模型生成失败。

## Failures supported by the artifact evidence

- `ctb-00001490/run-1`: FreeCAD 草图约束冗余，后续 constraint patch 被拒绝；更像生成/修复结果失败。
- `ctb-00001490/run-2`: 几何验证明确报告圆柱外轮廓不是单一同轴恒半径边界；更像生成几何不满足要求。
- `ctb-00001615/run-1`: 原始 live timeline 有 `subtractive_feature_no_effect`、`NameError: Body is not defined`、参数越界等真实执行失败；报告只把它显示成 `WorkflowPending`，属于上面的归因问题。
- `ctb-00001615/run-2`: planner 输出 opening bounds 非正体积；属于规划输出/输入约束失败。
- `ctb-00001817/run-1`: 验收对象选择器找到了 4 个相对面，而要求唯一面组；属于当前验收能力限制或需求绑定问题，证据不足以认定平台 Bug。
- `ctb-00002221`, `ctb-00002298`, `ctb-00003763`: planner 计划校验失败，包括 `source_quote` 缺失、2D/solid 分类错误、opening bounds 或 requiredness 不合法；当前只能归为 planner 输出或规则绑定失败。

## Encoding issue

报告中 `WorkflowPending` 的 message 已经出现 `�`/`鎸...` 形式的乱码。源码 `backend/app/services/durable_submission.py` 中的默认错误文案本身已是损坏字符串，因此这是确定的文案编码缺陷，但不改变工作流失败原因。

## Verification

- `python -m pytest tests/test_eval_cases.py tests/test_eval_metrics.py tests/test_live_evaluation_benchmark.py tests/test_cad_test_report_html.py -q`: `83 passed, 1 warning`。
- `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py`: passed。
- `git diff --check`: passed; only报告了工作区已有文件的 LF/CRLF 警告。
- `scan_review_risks.py --base HEAD --head HEAD --worktree`: found only the two unrelated untracked planning-document leads; no application-code finding was introduced by this review.

## Limitations

- 没有重新执行完整 CADTest 批量任务，也没有修改或重放原始工作流。
- CADTest observer 的具体运行时配置缺失原因需要结合当时 worker 的环境和日志进一步确认；当前可以确定的是报告链路没有记录该原因。
- 没有在本次审查中修复代码，符合 `cad-agent-review` 的只审查边界。

