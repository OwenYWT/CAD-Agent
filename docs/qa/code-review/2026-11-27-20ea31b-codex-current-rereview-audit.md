# CADTest 当前复审报告审计

## 结论

- **状态：`FAIL`。** 报告提出的五项未闭环问题均有当前代码或本机复现支持；其中两项 P1 仍会使 live evaluation 接受未经证明的通过结果，或使未通过的人工补录命令返回成功。
- 审查人：Codex；审查 ID：`codex-current-rereview-audit`；时间：2026-11-27 11:48（Asia/Shanghai）。
- 报告及基线：审计 `docs/qa/code-review/2026-11-27-20ea31b-codex-current-rereview.md`，代码 HEAD `20ea31b5d0d5faf766be2b178c33c374e7559154`，范围为 `7508b94d9f4e1cf0a306508d66ab27729db2a18e...20ea31b5d0d5faf766be2b178c33c374e7559154`，并检查当前工作树。
- 当前工作树含 14 个已修改应用/测试/文档文件和一个新测试文件（上一轮修复尚未提交），另有先前未跟踪审查文档。此次审计没有修改业务代码、应用测试或 Git 历史；只新增本报告。

## Findings

| 优先级 | 报告主张 | 独立审计判断与证据 |
| --- | --- | --- |
| P1 | `attach_cad_test_observation()` 可绕开 seal | **确认。** 本机创建 benchmark session、candidate revision，但不记录 `cad_test_seal`，直接附加完整 pass payload。当前报告变为 `status=passed`、`verdict=true`、`artifact_match=null`，seal 仍为空。`backend/app/evaluation/integration.py:1002` 归一化只检查调用者给出的 workflow/revision，再直接落盘；未校验 benchmark admission、dataset、seal 或实评分文件。审计未发现此函数对应的外部 API，但 Worker 内有多处调用；它仍是 benchmark 结果持久化边界，应对 benchmark success 强制独立 provenance，未满足时只能 blocked/unknown。报告对内部入口的证据完整性风险成立，不能据此声称已证明远程用户可直接调用该 Python 函数。 |
| P1 | `cad_test_live` 对未知结果返回成功 | **确认。** 通过真实 `main()` 配合受控报告文件调用，`unknown`、`unavailable`、`not_applicable`、无 `cad_test` 字段均返回 0；只有 `blocked` 返回 1。位置 `backend/benchmark/cad_test_live.py:102`。文件不存在时当前实现抛读文件异常，进程非零，不是报告文字所称的“缺报告返回 0”；应将这一特定例证收窄，但退出码契约缺口仍成立。仅有明确 `passed` 且必要证据完整时才应返回 0，disabled / not-applicable 应定义为不同退出语义。 |
| P2 | 二维 scorer 把非空毫米 DXF 当通过 | **确认。** 独立生成只含一条线的毫米制 DXF，调用当前 scorer 得到 `passed=true, requirements_passed=1, requirements_total=1`。`backend/benchmark/cad_test_engine/score_dxf.py:15` 未读取 case 真值，故不具备题目几何验收能力。 |
| P2 | Worker dataset / report root 未闭环 | **代码确认，范围需限定。** `cad_test_batch --dataset-root/--report-root` 只创建 CLI 的 `LiveBenchmarkContext`；准入传 dataset hash 而非 Worker 路径。Worker 从独立进程环境读取 `CAD_TEST_DATASET_ROOT` / `CAD_TEST_ROOT`，默认 report 写到源码下 `benchmark/reports`。因此独立配置 Worker 与 CLI 不一致时，CADTest 可不可用，或 CLI 在自定义 report root 等不到 Worker 报告。默认 report root 一致的场景不受此差异影响。调用链见 `backend/benchmark/cad_test_batch.py:178-204`、`backend/benchmark/live.py:367-414`、`backend/app/evaluation/cad_test_observer.py:26-37`、`backend/app/evaluation/integration.py:29`。 |
| P2 | 终态重建丢失质量矩阵 CADTest | **确认。** 当前工作树下执行“挂接 passed → `finish()`”：顶层 `cad_test.status` 保持 `passed`，`quality_matrix.cad_test` 从 `passed` 变成缺失。`backend/app/evaluation/store.py:195-206` 重建整个矩阵，`backend/app/evaluation/complexity_profile.py:222-238` 未合并 CADTest 事实。 |

## 对报告证据的校准

- 报告记录的 `47 passed`、`42 passed`、全量测试 `3062 passed / 136 failed / 431 skipped / 2 errors` 是其自己的历史运行结果；审计未重放全量套件，不把它们当成当前结果。
- 本次复跑相关 10 个 CADTest / Live evaluation 测试模块：`98 passed`、1 条既有 Pydantic warning。它们未覆盖上述四个新缺口，不能推翻独立反例。
- 报告称 Podman 当时被权限错误阻断。审计时 Podman 可用；本机 FreeCAD 1.1.4 通过修复后的真实 `run_native_delivery_check()` 对照：匹配 STEP/FCStd/STL 通过，双向差集 `0.0 mm³`；放大 FCStd 反例失败，差集约 `200 mm³`。因此“本次审计仍无法进行原生验证”不是当前状态；但该正反例不覆盖生产模型、多格式任务或对象存储端到端流程。
- `validate_feature_map.py`（24 域 / 433 路径）、`check_boundaries.py`、`git diff --check` 通过。风险扫描 75 路径、27 条线索；生产 exception 命中需逐项结合调用语义判断，不能作为缺陷结论。

## 影响与建议

影响域：05 Agent 协作、06 任务状态、07 原生建模、12 验收证据、19 文件与导出、24 运维。此次工作树的原生入口和 bundle 收紧已有真实 FreeCAD 正反例；复审报告正确指出剩余旁路和 CLI 门禁，不应据此回滚已验证的修复。

1. 首先关闭 `attach_cad_test_observation()` 的 benchmark pass 旁路：成功只能从受信任 scorer/封存路径产生；错误观察器仍可记录失败，但不能让任意 payload 自授通过。加无 seal、错 case/dataset/revision、换件、空摘要的负例。
2. 收紧 `cad_test_live` CLI 退出码；覆盖 `passed / failed / error / blocked / unknown / unavailable / not_applicable / missing` 状态表。
3. 建立 case-bound DXF 真值 scorer，并以尺寸、数量、位置和拓扑正反例验证；当前单直线控制表明不能把现有可读性分数称为 2D 通过。
4. 明确 Worker 与 CLI 的题库及报告共享配置；默认 root 对齐不证明自定义 root 正确。
5. `finish()` 重建质量矩阵时保留 CADTest，增加挂接前、挂接后、终态后的一致性用例。

未更新 review memory 或稳定规则：此报告记录的是当前审查工作区的复验和缺陷，不需复制已存在的证据完整性不变量。
