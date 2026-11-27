# CAD-Agent PR27 复审报告审计

## 结论

结论：`FAIL` 方向成立，建议继续暂缓合并。外部报告提出的前三项 P1 与当前 `20ea31b` 代码的关键行为一致；其中 P1-2 可在本机用无容器的控制复现，P1-3 需要限定在手工补录入口理解。P2 的 DXF 评分粒度和错误原因覆盖也有代码依据。

本次审计没有修改业务代码、应用测试、部署文件或 Git 历史。工作树中已有的未跟踪 QA/计划文档未改动。

## 审计范围

- 当前分支：`feature/live-cadtest-evaluation`
- 精确 HEAD：`20ea31b5d0d5faf766be2b178c33c374e7559154`
- merge-base：`7508b94d9f4e1cf0a306508d66ab27729db2a18e`
- 目标报告：`D:\project\projectcad\2026-11-26-20ea31b-codex-pr27-recheck.md`
- 报告引用的 sidecar 证据目录不在 `D:\project\projectcad` 顶层，因此其中的真实 FreeCAD、PostgreSQL、MinIO 数值不能在本次审计中独立重放；代码路径和可复现控制仍单独核对。

## Findings

### P1-1：默认 FreeCAD native verifier 的模块导入路径缺口，认可

`backend/benchmark/cad_test_engine/check_native_delivery.py:8` 从 `native_delivery_selection` 导入模块；模块位于同一 `/code` 挂载目录。`backend/app/evaluation/cad_test_runner.py:357-364` 通过 `FreeCADCmd -c` 执行编译后的脚本，但没有把 `/code` 加入 `sys.path`，而 `backend/sandbox/Dockerfile:171` 将工作目录设为 `/tmp`。因此报告指出的 `ModuleNotFoundError` 是合理的默认启动回归，且上层只得到 `native_delivery_result_missing` 的传播链与代码一致。

报告中的 FreeCAD 1.1.4 正反例体积数字因 sidecar 缺失未独立复核，但这不影响对启动路径缺陷的判断。该问题属于真实 runtime contract，而不是模型生成质量问题。

### P1-2：多文件 bundle/native 失败仍可得到顶层 pass，确认

`run_cad_test_bundle()` 在 `backend/app/evaluation/cad_test_runner.py:577-601` 先保留 STEP 结果，再附加 `native_delivery` 和 `delivery_verified`，没有用 native 结果重算顶层 `status`。`backend/benchmark/cad_test_batch.py:_score()` 只根据顶层 `result["status"]` 决定 CLI 退出码。

本机控制（替换 scorer/native 调用，不依赖 Podman）得到：

```text
{"status":"pass", "native_delivery":{"status":"failed","passed":false}, "delivery_verified":false}
```

因此报告关于 `status=pass`、退出码可能为 0、native 失败未传播的结论成立。现有测试覆盖了正例、缺件和非零 verifier 进程，但没有覆盖“STEP 通过 + native 明确失败”这一必须的负例。

### P1-3：手工补录可在缺少独立 expected artifact identity 时记为通过，部分确认

自动观察器在 `backend/app/evaluation/cad_test_observer.py:187-201,273-287` 下载对象时会校验对象的大小和 SHA-256，这条自动路径不能简单归类为“完全无 artifact 校验”。

但 `backend/benchmark/cad_test_live.py:62-76` 调用 `attach_cad_test_result()` / `attach_cad_test_artifacts()` 时，`backend/app/evaluation/integration.py:759-809,835-887` 没有从候选封存清单取得并强制传入 expected artifact SHA。`normalize_cad_test_result()` 在 `expected_artifact_sha256=None` 时允许 `artifact_match=None`，而只要 score、geometry、delivery 和 requirements 均通过仍可返回 `passed`。本机控制结果为：

```text
status=passed, verdict=true, reason=cad_test_verified, artifact_match=null
```

所以报告的风险成立，但影响面应准确表述为“手工/开发者补录入口可绕过候选封存 artifact identity”；自动对象存储观察路径已有独立校验。要完全确认报告中的“换件后仍通过”的真实数据库对照，需要其缺失的 sidecar 和服务环境。

### P2：DXF 评分、存储和 HTML 展示的验收条件不一致，认可为代码风险

`backend/benchmark/cad_test_engine/score_dxf.py:15-27` 只验证 DXF 可读、毫米单位和实体数大于零，没有题目几何验收。`backend/benchmark/live.py:326-333` 对 2D route 放宽 native delivery，而 `backend/benchmark/cad_test_report.py:256-262` 的 `_strict_run_passed()` 仍无条件要求 native delivery。报告关于“底层可将一条直线计为通过、JSON 与 HTML 最终状态可能不同”的判断与当前代码结构相符。

### P2：缺失 artifact 的原始错误可能被 dataset identity mismatch 覆盖，认可为错误优先级风险

`run_cad_test()` 对缺失文件可返回 `artifact_not_found`，但 `attach_cad_test_result()` 后续按 source dataset identity 规范化；当结果没有 dataset hash 时，`normalize_cad_test_result()` 的 `dataset_match=False` 分支会优先返回 `cad_test_dataset_identity_mismatch`。这会损失原始文件错误，报告的建议合理。

报告列出的 F05-F10（worker/report-root 配置边界、assembly 文件名前缀选择、封存等待时限、输入 sidecar 覆盖、终态 quality matrix 重建）均有相应代码线索；本次没有把报告 sidecar 缺失时无法完整复放的项目升级为新的 P1，建议在修复后分别补充可执行负例。

## 为什么上一轮没有发现

这不是“之前检查证明代码没问题”而是审查边界和证据层级不足：

1. 上一轮 `docs/qa/code-review/2026-11-26-bbe0ae4-codex-rereview.md` 的 focused matrix 为 `111 passed`，但它明确把 FreeCAD/Podman runtime acceptance 标为 `BLOCKED`。当前 `bbe0ae4` 与 `20ea31b` 的 CADTest 业务路径相同；两者差异主要是 4 个部署/镜像相关文件。因此 P1-1 并非只由本次新业务代码引入，而是 unit-only 复审没有触发的 runtime 回归。
2. 之前测试验证了 native 正例、缺件、非零进程和身份冲突等局部契约，却没有“STEP pass + native fail 必须让 bundle/CLI fail”的组合负例；所以 P1-2 需要把结果 JSON、顶层 status、CLI exit code 三个通道一起核对。
3. 之前重点修复的是跨 case、dataset hash 和 observer 自动下载校验；没有把“同 case/同 candidate 下手工传入另一份文件”作为独立 artifact provenance 控制。`expected_artifact_sha256` 可选且未由候选封存清单强制提供，正是 P1-3 的遗漏边界。
4. `111 passed` 证明选定的 Python 逻辑测试通过，不等于证明 FreeCADCmd 的导入环境、Podman 镜像、CLI 退出码和真实对象存储中的 artifact provenance。此次报告把这些边界纳入真实控制；但由于 sidecar 不在本地，本审计仍将真实数值标为历史报告证据而非当前重放结果。

## Checks

| 检查 | 结果 | 证据分类 |
| --- | --- | --- |
| `git diff --check origin/main...HEAD` | 通过业务代码；历史 QA 文档有 EOF 空行提示 | LOGIC |
| `validate_feature_map.py` | 通过，24 domains / 433 paths | CONTRACT_ONLY |
| `check_boundaries.py` | 通过 | CONTRACT_ONLY |
| `scan_review_risks.py` | 26 leads，生产代码异常分支已按评估隔离语义人工核对 | LOGIC |
| CADTest focused pytest matrix | `111 passed, 1 warning` | LOGIC/SERVICE-CONTRACT |
| FreeCAD/Podman、隔离 PostgreSQL/MinIO sidecar | 当前工作区不可用 | BLOCKED / historical evidence only |

最终判断：认可报告的 `FAIL` 和暂缓合并方向；认可 P1-1、P1-2，认可 P1-3 但限定为手工补录路径；P2 结论有代码依据。此前未发现的根因是缺少真实 runtime、组合负例和独立 artifact provenance 控制，而不是大模型或单纯测试数量不足。
