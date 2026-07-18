# 文档索引

本目录只收录当前开发所需的专题文档和明确标注的历史归档。项目入口、运行方式和事实边界以本页链接的文档为准，不再维护重复的中英文副本。

## 项目入口

| 文档 | 受众 | 作用 |
| --- | --- | --- |
| [`README.md`](../README.md) | 所有人 | 产品能力、架构、最短启动路径和文档入口 |
| [`development.md`](development.md) | 开发与交接人员 | 环境、目录、双服务启动、测试、安全边界和排障 |
| [`DEPLOY.md`](../DEPLOY.md) | 运维 | Docker Compose、生产配置、上线检查和更新流程 |
| [`CLAUDE.md`](../CLAUDE.md) | 代码代理 | 本仓库工作边界、验证命令和文档维护规则 |
| [`backend/benchmark/README.md`](../backend/benchmark/README.md) | 模型/生成链路维护者 | 真实 LLM + sandbox 的手动评测基线 |

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

## 历史归档

- [`archive/forgecad-roadmap-2026-07.md`](archive/forgecad-roadmap-2026-07.md)：从旧 `NEXT.md` 提炼的产品研究和候选路线。它不是当前实现清单、排期或接口合同。

已完成的一次性 implementation plan/spec、旧 Sprint、重复使用说明和旧交接副本已删除。需要追溯时使用 Git 历史，不再把它们放在当前文档树里。

## 维护规则

1. `README.md` 只保留项目概览和最短路径；开发细节进入 `development.md`，生产运维进入 `DEPLOY.md`。
2. 新文档必须从本索引或 `README.md` 可发现，并明确受众与事实来源。
3. 带日期的调研/验收结论要写清核对日期；环境限制不能用“当前开发机”这类会迅速失效的表述。
4. 不把 Mock、计划、测试替身或尚未接通的外部能力写成已实现。
5. 删除或移动文档前先搜索文件名、Markdown 链接、脚本和 CI 引用，并在修改后执行本地链接检查。
