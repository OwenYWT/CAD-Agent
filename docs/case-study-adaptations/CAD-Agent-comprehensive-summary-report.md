# CAD-Agent 改造综合总结报告

更新时间：2026-07-19

## 1. 总体结论

本轮改造以 CADAM 和 forgecad-public-kit 两个成熟开源项目为参照，围绕“自然语言到可 3D 打印 CAD 模型”的核心链路，完成了 14 个可追溯改造点。整体方向是：让 CAD-Agent 不只生成模型，而是逐步具备需求理解、参数化交互、制造约束、校验修复、版本回溯、工程交付和用户恢复路径。

详细逐项留痕见：`docs/case-study-adaptations/CAD-Agent-adaptation-report.md`。

## 2. 改造清单

| 序号 | 借鉴来源 | 改造主题 | CAD-Agent 当前收益 |
| --- | --- | --- | --- |
| 1 | CADAM | 参数交互 | 从只看结果升级为可编辑关键参数，支持用户在前端调整模型尺寸。 |
| 2 | forgecad-public-kit | 校验与修复循环 | 生成后增加验证、失败分类和自动修复，使模型更接近可打印交付。 |
| 3 | forgecad-public-kit | 可打印案例 RAG | 让生成链路参考已有可打印样例，提升提示词到建模代码的稳定性。 |
| 4 | forgecad-public-kit | Inspect Report | 把水密性、体积、包围盒、壁厚和打印风险整理为工程检查结果。 |
| 5 | CADAM | 版本快照 | 支持保存和恢复生成结果，减少用户反复生成时的上下文丢失。 |
| 6 | CADAM / ForgeCAD | Agent 运行时间线 | 前端展示规划、检索、生成、执行、修复、导出等阶段证据。 |
| 7 | forgecad-public-kit | Engineering Brief | 在建模前形成设计意图、假设、关键尺寸和验收标准。 |
| 8 | CADAM | 动态建议按钮 | 根据当前状态生成下一步建议，降低用户继续修改的输入成本。 |
| 9 | CADAM | 工程参数面板 | 将关键尺寸优先展示，并和代码参数编辑联动。 |
| 10 | forgecad-public-kit | 设计确认门 | 对不明确需求先请求确认，避免直接生成错误模型。 |
| 11 | forgecad-public-kit | 轻量 Artifact Manifest | 下载 JSON 清单，记录来源、文件、参数、简报、检查和修复历史。 |
| 12 | forgecad-public-kit | 制造配置 Profile | 把 FDM/SLA、材料、喷嘴、层高、成型空间传入生成和交付链路。 |
| 13 | CADAM | 结构化恢复动作 | 把失败、警告、待确认状态转成可点击的恢复提示。 |
| 14 | CADAM | 参考附件元数据 | 前端可选择图片/CAD 参考文件，并将文件元数据写入 prompt 和 manifest。 |

## 3. 架构层面变化

- 后端 Agent 链路：从单次生成扩展为“规划 → 检索 → 生成 → 执行 → 校验 → 修复 → 汇总”的可观测流程。
- 前端交互层：从基础聊天页扩展为参数面板、检查报告、运行时间线、版本历史、设计简报、恢复动作和参考附件。
- 工程交付层：从单独下载 STL/STEP 扩展为可下载 Manifest 清单，便于复盘、归档和后续自动化。
- 制造约束层：增加制造配置和可打印性检查，使生成结果更接近 3D 打印使用场景。
- 质量保障层：新增多组 backend/frontend 测试，覆盖确认门、制造配置、恢复动作、manifest、参考附件和乱码回归。

## 4. 本次前端中文与乱码修复

本次额外完成前端中文化和乱码回归治理：

- 修复 `RepairHistory` 中的 `????` 展示，改为“自动修复记录、校验阶段、代码执行、已修复”等中文文案。
- 修复 `AgentRunTimeline` 中的问号图标、英文阶段名和英文按钮，改为中文阶段状态和中文操作按钮。
- 修复 `ParameterPanel` 中的 `mm?`、尺寸分隔符和 `?` 图标，改为稳定的中文/ASCII 展示。
- 修复 `VersionHistoryPanel` 中的版本加载、恢复、空状态、导出文件等英文或乱码文案。
- 修复 `DesignBriefPanel`、`InspectReportPanel`、`DownloadPanel`、`ChatPanel`、`manufacturingProfiles`、`suggestions` 中的主要用户可见英文和乱码残留。
- 新增 `frontend/tests/frontendText.test.ts`，扫描重点前端文件，防止 `????`、错误问号分隔符和乱码占位再次回归。

## 5. 当前验证记录

已执行并通过：

```bash
cd CAD-Agent/frontend
node --test --experimental-strip-types tests/frontendText.test.ts tests/referenceAttachments.test.ts tests/artifactManifest.test.ts tests/manufacturingProfiles.test.ts tests/parameterMapping.test.ts
npm.cmd run build
```

结果：

- Frontend Node tests：11 passed。
- Frontend production build：passed。
- Vite 仍有既有的大 chunk 提示，不影响构建成功。

## 6. 后续建议

- 优先做一次浏览器人工验收，重点查看聊天首页、输入区、参数面板、分析页、下载页、版本历史和运行时间线。
- 后续新增中文文案时，避免通过 PowerShell 管道向 Python 写入中文；推荐使用 UTF-8 文件直接编辑，或用 `.NET WriteAllText` 明确指定 UTF-8。
- 如果要继续产品化，下一阶段建议聚焦“生成质量评测集”和“模型可打印性评分闭环”，不要继续堆 UI 功能。
## 7. 追加修复：思考过程/进度流乱码

用户复验时发现“思考过程”仍有乱码。本次补充修复了前端进度展示和后端 WebSocket/Agent step_update 文案：

- 修复后端 `orchestrator.py` 中规划、检索、生成、执行、DFM、修复、装配体等阶段的 step_update 乱码。
- 修复后端 `websocket.py` 中保存到历史消息的“需要确认、生成成功、生成失败”文案乱码。
- 修复前端 `sessionStore.ts` 中生成终态、确认终态、恢复版本等默认消息。
- 修复前端 `MultiStepProgress.tsx` 的多步构建标题显示方式。
- 新增 `frontend/tests/progressText.test.ts`，防止思考/进度流文案再次出现 `????` 或典型 mojibake 残留。

追加验证：

```bash
cd CAD-Agent/frontend
node --test --experimental-strip-types tests/progressText.test.ts tests/frontendText.test.ts tests/referenceAttachments.test.ts tests/artifactManifest.test.ts tests/manufacturingProfiles.test.ts tests/parameterMapping.test.ts
npm.cmd run build
cd ..
python -m pytest backend\tests\test_deploy_websocket.py backend\tests\test_deploy_pipeline_matrix.py -q
```

结果：前端 12 passed，前端构建通过，后端 31 passed。
## 8. 追加修复：需求确认卡片英文提示

复验时发现需求确认卡片仍有英文文案。本次补充修复：

- 将前端确认卡片标题改为“需要确认需求”。
- 将确认说明改为“请先回答待确认问题，再继续生成 CAD 模型”。
- 将“View brief”按钮改为“查看设计简报”。
- 将后端结构化恢复动作的标签、提示词和原因中文化。
- 前端对恢复动作按钮增加基于 `action_type` 的中文兜底映射，避免旧历史数据中的英文 label 继续显示。
- `frontend/tests/frontendText.test.ts` 已纳入确认卡片英文残留检查。

追加验证：前端 12 passed，前端构建通过，后端确认/恢复/WebSocket 相关测试 20 passed。