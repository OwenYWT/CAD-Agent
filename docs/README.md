# 文档索引

本目录只收录当前开发所需的专题文档和明确标注的历史归档。项目入口、运行方式和事实边界以本页链接的文档为准，不再维护重复的中英文副本。

## 项目入口

| 文档 | 受众 | 作用 |
| --- | --- | --- |
| [`qa/continuation-recovery-2026-09-15.md`](qa/continuation-recovery-2026-09-15.md) | 产品、测试与研发 | 旧“继续”任务丢失目标的根因、恢复确认流程及验证记录 |
| [`functional-inventory-2026-09-14.md`](functional-inventory-2026-09-14.md) | 产品、测试与研发 | 按实际代码整理的完整功能清单，含入口、实现范围、角色权限、可选集成和未完成项 |
| [`README.md`](../README.md) | 所有人 | 产品能力、架构、最短启动路径和文档入口 |
| [`development.md`](development.md) | 开发与交接人员 | 环境、目录、双服务启动、测试、安全边界和排障 |
| [`DEPLOY.md`](../DEPLOY.md) | 运维 | Docker Compose、生产配置、上线检查和更新流程 |
| [`CLAUDE.md`](../CLAUDE.md) | 代码代理 | 本仓库工作边界、验证命令和文档维护规则 |
| [`backend/benchmark/README.md`](../backend/benchmark/README.md) | 模型/生成链路维护者 | 真实 LLM + sandbox 的手动评测基线 |

## MCAD 架构验收

| 文档 | 作用 |
| --- | --- |
| [`qa/task-state-2026-09-13.md`](qa/task-state-2026-09-13.md) | 手机壳反馈的五状态整改、原需求重试、候选/草稿边界、真实浏览器与独立量测证据及剩余限制 |
| [`qa/native-coediting-2026-09-13.md`](qa/native-coediting-2026-09-13.md) | 最新方案差异、共同编辑实现、端云与 Agent 架构、T01–T19 真实验证及开发阶段额度 / CI 状态；后续上线见部署报告 |
| [`qa/acceptance-remediation-2026-09-10.md`](qa/acceptance-remediation-2026-09-10.md) | 独立验收发现的问题、当前整改进展、真实复验证据与尚未通过的范围 |
| [`qa/cloud-cad-fusion-2026-09-09.md`](qa/cloud-cad-fusion-2026-09-09.md) | 历史融合测试及交接记录；整体完成结论已被独立验收推翻 |
| [`cloud-documents.md`](cloud-documents.md) | 云文档版本权威、自动派发、特征协作、组件场景、分支与草图的真实调用链及边界 |
| [`engineering-compute.md`](engineering-compute.md) | 真实有限元、外轮廓 CAM、不可变修订证据、Agent 上下文与运行边界 |
| [`qa/cloud-document-p0.md`](qa/cloud-document-p0.md) | 2026-09-08 早期 P0 融合的真实服务、浏览器、内核和回归证据 |
| [`qa/mcad-m0-report.md`](qa/mcad-m0-report.md) | 固定 Runtime、统一 ExecutionBackend 与隔离执行的 M0 证据 |
| [`qa/mcad-m1-report.md`](qa/mcad-m1-report.md) | PostgreSQL、Temporal、不可变 Artifact、Revision、WebSocket 和真实 Podman 的 M1 验收结果 |

## Fusion 360 Connector

这些文档各自承担不同职责，不互相复制完整内容：

| 文档 | 作用 |
| --- | --- |
| [`fusion360-research.md`](fusion360-research.md) | Autodesk 官方 API 事实和来源索引 |
| [`fusion360-architecture.md`](fusion360-architecture.md) | 系统边界、信任模型、状态机和验收拓扑 |
| [`fusion360-api-contract.md`](fusion360-api-contract.md) | Web/Backend/Runtime/Add-in 的稳定字段和接口合同 |
| [`fusion360-installation.md`](fusion360-installation.md) | 安装、配置、调试、卸载和真实 Fusion 验收矩阵 |
| [`fusion360-web-example.md`](fusion360-web-example.md) | Cloud Agent 与兼容 Runtime 的调用示例 |
| [`fusion360-limitations.md`](fusion360-limitations.md) | 已实现能力、已知限制和后续边界 |

Fusion 自动化测试与真实桌面验收必须分开表述。没有完成 Windows/macOS 实机矩阵时，只能声明合同、服务、Palette/controller 和窄 facade 测试通过。

## 上游与生成资料

- [`third_party/cadskills/UPSTREAM.md`](../third_party/cadskills/UPSTREAM.md) 是 11 项 CAD Skills 的版本、commit、许可和刷新说明。
- `third_party/cadskills/skills/**` 是固定的上游运行资料，供能力注册和运行时使用，不属于本项目的一方产品文档，不做语言或结构性清理。
- `schemas/fusion360/**` 是由 `backend/app/fusion360/contract.py` 等源码生成的 wire schema，不手工编辑。
- `.gstack/qa-reports/**`、`frontend/qa/**`、测试截图和 benchmark reports 是测试证据或 harness，不是产品文档入口；其中 `.gstack` 与 benchmark 输出已被 Git 忽略。

## 当前架构改造

- [`superpowers/plans/2026-09-12-native-coediting.md`](superpowers/plans/2026-09-12-native-coediting.md)：新版方案差异、起始代码与实施记录；P0/P1 实现及本地回归已推进，真实 Provider 剩余用例待完成；最终提交 CI 与部署见下方上线报告。

- [`releases-and-local-bridge.md`](releases-and-local-bridge.md)：命名发布、原生 BOM、同修订工程证据和本地文件交付；安装方式、权限、恢复行为与外部环境边界。

- [`superpowers/plans/2026-09-08-cloud-cad-expansion.md`](superpowers/plans/2026-09-08-cloud-cad-expansion.md)
  2026-09-08 继续融合的模块顺序与验证进度；未勾选项尚未完成验收。

- [`superpowers/specs/2026-07-25-commercial-mcad-execution-design.md`](superpowers/specs/2026-07-25-commercial-mcad-execution-design.md)
  定义 M0 本地 MCAD、M1 持久控制平面和 M2 隔离计算扩展的已批准目标、边界与真实验收要求。
- [`superpowers/specs/2026-08-12-durable-agent-fusion-design.md`](superpowers/specs/2026-08-12-durable-agent-fusion-design.md)
  定义已落地的 V2 Durable Agent 规划、建模、修复、几何/视觉/DFM 验证和候选版本封存边界。

## 历史归档

- [`archive/forgecad-roadmap-2026-07.md`](archive/forgecad-roadmap-2026-07.md)：从旧 `NEXT.md` 提炼的产品研究和候选路线。它不是当前实现清单、排期或接口合同。
- [`archive/agent-orchestration-upgrade-2026-07.md`](archive/agent-orchestration-upgrade-2026-07.md)：2026-07 的进程内 Agent 编排比较稿；旧架构和实施清单已废弃。
- [`archive/cadam-orchestration-borrowed-advantages-2026-08.md`](archive/cadam-orchestration-borrowed-advantages-2026-08.md)：Durable Agent 融合 CADAM 编排优点的历史决策记录；当前事实以实现、设计规范和 M1 验收报告为准。

未从本索引列出的 `superpowers/plans/` 文件只保留实施追溯价值，不是当前事实来源。旧 Sprint、重复使用说明和旧交接副本不再保留；更早内容使用 Git 历史追溯。

## 维护规则

最新腾讯云上线记录见 [`qa/tencent-deployment-2026-09-13.md`](qa/tencent-deployment-2026-09-13.md)，
运行配置见 [`../deploy/tencent/README.md`](../deploy/tencent/README.md)。该报告区分部署验证与完整产品验收。

1. `README.md` 只保留项目概览和最短路径；开发细节进入 `development.md`，生产运维进入 `DEPLOY.md`。
2. 新文档必须从本索引或 `README.md` 可发现，并明确受众与事实来源。
3. 带日期的调研/验收结论要写清核对日期；环境限制不能用“当前开发机”这类会迅速失效的表述。
4. 不把 Mock、计划、测试替身或尚未接通的外部能力写成已实现。
5. 删除或移动文档前先搜索文件名、Markdown 链接、脚本和 CI 引用，并在修改后执行本地链接检查。
