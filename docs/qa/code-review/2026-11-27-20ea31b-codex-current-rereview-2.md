# CAD-Agent Review

## 结论

**PASS_WITH_LIMITATIONS。** 本次复审没有复现上一轮报告中的两个 P1：benchmark 观察旁路不能再写入可信的 `passed`，并且 `cad_test_live` 对未知、缺失、不可用或错误结果不会返回成功退出码。2D DXF 评分也已从“可读取即通过”改为基于受控 case reference 的几何比对，`finish()` 后的 `quality_matrix.cad_test` 投影保持一致。

仍不能给出无条件通过：当前 Windows 环境的 Podman socket 使用 `D:\tmp\cad_agent_podman_fix` 时返回 `Access is denied`，所以本轮没有完成真实 FreeCAD 原生交付正反例；全量后端回归还受到现有数据库、asyncio、Fusion 脚本和运行环境问题影响。专项逻辑测试和可运行的容器标记测试通过，但不能替代完整生产链路验收。

本次审查未修改业务代码、应用测试、部署代码或 Git 历史；只新增本报告。

## 范围

- 分支：`feature/live-cadtest-evaluation`
- HEAD：`20ea31b` (`fix: register CADTest evaluation boundaries`)
- 对照：`origin/main` `7508b94`
- 工作区：包含一批待审查的 CADTest/live-evaluation 修改，以及既有 QA/设计文档和复验产物；未清理、回滚或改写这些用户文件。
- 重点调用链：`durable submission -> seal_candidate -> observation workflow -> sealed artifact scorer -> live report -> batch JSON/HTML`。

## Findings

### 无 P1/P2 缺陷复现

#### P1 控制：benchmark observation 不能伪造通过

`backend/app/evaluation/integration.py:1005` 的 `attach_cad_test_observation()` 对 benchmark source 不接受调用方提交的 `pass`/`passed` 结果，而是降级为 `blocked`，只有 user-task 的 `not_applicable` 观察允许走此证据入口。可信 benchmark 评分走 `backend/app/evaluation/integration.py:765` 的 `_score_sealed_artifacts()`，先核对 admission source、candidate revision、seal 中每个 artifact 的 SHA-256/size，再复制到临时只读输入目录后调用 scorer。

独立控制覆盖：无 seal、已有不匹配 seal、直接提交伪造 score、同 case 替换 STEP/FCStd/STL、缺少 artifact、错误 case 和错误 dataset。专项测试结果均为预期的 blocked/error，而不是 passed。

#### P1 控制：结构化结果和进程退出码必须一致

`backend/benchmark/cad_test_live.py:31` 现在只在报告中同时出现 `status=passed` 且 `verdict=true` 时返回 `0`；报告缺失、JSON 损坏、CADTest 为 `unknown`/`unavailable`/`blocked`/`error`/`failed`/`not_applicable` 均返回非零。`backend/app/evaluation/cad_test_runner.py` 同时检查 scorer/native verifier 的 JSON、独立 artifact hash 和子进程返回码，非零返回码不会被 JSON 中的 `passed=true` 覆盖。

#### P2 控制：2D scorer 使用 case 真值

`backend/benchmark/cad_test_engine/score_dxf.py:49` 读取受控 case 的 `reference.dxf` 和 `checks.json`，检查毫米单位、平面几何、实体类型、实体数量、位置、半径/角度、折线闭合性和容差内的双射匹配。仅一条直线、移动、缩放、额外实体、开口折线和缺失真值的控制均未通过；匹配 reference 的控制通过。结果中 artifact hash 由 scorer 对实际输入文件独立计算。

#### P2 控制：终态质量投影保持一致

`backend/app/evaluation/store.py:195` 的 `finish()` 重建质量矩阵后重新写入 CADTest 投影；挂接前、挂接后和 finish 后的顶层 `cad_test` 与 `quality_matrix.cad_test` 保持一致。

#### 观察任务与正常封存解耦

`backend/app/workflows/cad_test_observation.py:8` 使用独立 Temporal workflow，`backend/app/workflows/activities.py:136` 通过独立 activity 执行观察。`seal_candidate` 先返回并记录业务终态，观察调度失败不会改变封存结果；worker 注册了新的 workflow/activity。该链路的单元和集成控制通过。

## 验证结果

### 通过

- `python -X utf8 -m pytest tests/test_cad_test_review_closure.py tests/test_cad_test_external_recheck.py tests/test_cad_test_pass_gates.py tests/test_cad_test_runner.py tests/test_cad_test_integration.py tests/test_cad_test_observer.py tests/test_cad_test_report_html.py tests/test_live_evaluation_benchmark.py tests/test_cad_test_self_contained.py -q`：`122 passed, 1 skipped`。
- `python -X utf8 -m pytest tests/test_agent_v2_worker.py tests/test_live_evaluation_admission.py tests/test_live_evaluation_store.py tests/test_cad_test_evidence.py tests/test_native_delivery_selection.py -q`：`29 passed`。
- `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py`：通过，24 domains / 433 referenced paths。
- `python scripts/architecture/check_boundaries.py`：通过，legacy exceptions remain explicit。
- `git diff --check`：通过；仅有 Windows 换行转换提示。
- `podman ps`：本地 Podman 容器列表可读取。

### 受限或失败

- `python -X utf8 -m pytest tests/test_cad_test_review_closure.py -m docker -q`：`2 passed, 25 deselected`。这些测试中的 scorer 调用被测试替换为本地控制执行，不能视作真实 FreeCAD 接受证据。
- 当前工作区代码实际调用 `run_native_delivery_check()` 时，Podman 返回：`unable to connect to Podman socket ... D:\\tmp\\cad_agent_podman_fix ... Access is denied`，随后得到 `native_delivery_result_missing`。因此真实 FreeCAD 正常模型、放大 FCStd、缺失 artifact 和装配模型控制本轮均为 `BLOCKED`，不能声称 runtime acceptance。
- `python -X utf8 -m pytest -m "not docker and not llm and not fusion_e2e" -q`：`3104 passed, 136 failed, 432 skipped, 3 deselected, 10 warnings, 2 errors`。失败集中在当前 Windows/外部运行环境的 Fusion shell dry-run、数据库/asyncio/TestClient、认证/持久化、FreeCAD result channel 等既有环境依赖；本轮 CADTest 专项测试未失败。但由于全量未通过，正常功能不能被表述为全量无回归。
- `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base 7508b94d9f4e1cf0a306508d66ab27729db2a18e --head HEAD --worktree`：34 条线索。生产代码中的异常隔离分支、评分器将异常转换为非通过结果的分支已人工核对；扫描器输出不是自动缺陷结论。历史 QA 文档和复验产物也产生了若干文本线索。

## 正常功能影响

专项范围内未发现业务封存结果被 CADTest 观察失败阻断、worker 缺少新注册项、报告终态投影丢失、单 STEP 评分回归或 HTML/JSON 判定不一致。当前仍需在 Podman socket 修复后复验：

1. FreeCAD 真实正常 FCStd/STEP/STL bundle；
2. 放大 FCStd、替换单个 bundle artifact、缺少 STL、多个 assembly primary 和保留 Boolean 历史；
3. 真实 Temporal worker dispatch、重试、重复 workflow ID 和最终报告落盘；
4. 生产数据库/MinIO/对象存储环境下的完整批量测试。

## 后续建议

1. 先修复或切换 Podman connection，重新运行真实 FreeCAD 正反例；没有这些证据，不应把本次结果升级为 `PASS`。
2. 将当前全量测试失败按环境依赖与代码回归分组，先恢复数据库/asyncio/TestClient 和 Fusion shell 的基线，再重新执行全量回归。
3. 保持现有负例作为合并门禁，尤其不能允许任何绕过 seal 的 helper 直接写入 benchmark `passed`。
