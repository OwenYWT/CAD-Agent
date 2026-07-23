# ForgeCAD 路线研究归档（2026-07）

> 状态：历史产品研究，不是当前实现说明、承诺或排期。
> 来源：原根目录 `NEXT.md`，已移除其中失效的文件行号、不存在的插件路径、重复纠偏记录和竞品原文摘录。
> 使用方式：任何候选项进入开发前，都必须重新对照当前代码、用户需求和接口能力编写独立规格。

## 当时的产品判断

目标是把一次性的 `prompt -> CAD code -> sandbox -> file` 生成流程，演进为可检查、可修复、可迭代、可回滚的工程工作区：

```text
requirements -> design -> execute -> inspect evidence -> revise -> version -> export
```

优先级原则是先建立结果可信度，再增加项目化和装配能力，最后评估 CLI、Agent skill 与桌面插件生态。

## 候选方向

### 1. 检查与证据

- 为每次生成建立 evidence bundle，包括 manifest、标准视角、截面和检查项关联。
- 增加 collision、connectivity/floating、section view 等确定性检查。
- 明确区分“已产出模型”和“工程验收通过”，禁止只用 `success=true` 声称设计正确。
- 检查失败要进入目标明确的修复和复验流程，保留实际证据与残留风险。

### 2. 参数稳定性

- 对参数范围进行采样，记录通过率、失败组合和失败类型。
- 从显式约束或保守规则推断采样范围，不盲目扩大尺寸。
- 修复后重复同一组探针，证明稳定性确实提升。

### 3. 项目与版本

- 把 session/history 升级为有明确项目边界的 workspace。
- 每次生成、修改、参数执行和回滚形成不可覆盖的 revision。
- Change Set / Diff 同时展示参数、代码、文件和检查状态变化。
- 结构化编辑必须在失败时保留上一版本。

### 4. 装配

- 使用 connector/frame 和结构化连接关系描述装配，不让 LLM 猜世界坐标。
- 由确定性 solver 计算位置，并验证类型兼容、间隙、碰撞和运动范围。
- 单零件修改后重建装配，避免无关零件被整组重写。
- 在开展装配检查前，确认执行链不会把需要独立检查的实体无条件融合。

### 5. 规格与验收纪律

- 先形成可展示的需求 brief / design spec，再生成模型。
- 最终回复必须引用实际检查、视角、文件和残留告警，不能只写“已验证”。
- 可评估对照 brief 的自动评分，但它不能替代真实几何检查和用户反馈。

### 6. 生态与独立产品线

- CLI 与 Agent skill：在核心 API 稳定、证据语义一致后再开放。
- 逆向建模：从 STEP/STL/图片形成可编辑参数模型，属于独立入口，需单独验证需求。
- Kinematics/MuJoCo：只在机器人/机构用户成为明确目标后投入。
- AutoCAD/SolidWorks 等插件：原研究曾误把不存在的 `plugins/**` 当成现有基础，实际应视为从零产品线。

## 与当前实现的关系

当前仓库已有浏览器工程工作区、生成/修改/执行、历史、参数、检查、文件、WebSocket、CAD Skills 和 Fusion 360 typed connector 等基础能力。它们不等于上述候选方向已经全部完成。

判断当前事实时使用：

- 项目入口：[`../../README.md`](../../README.md)
- 当前开发边界：[`../development.md`](../development.md)
- Fusion 能力与限制：[`../fusion360-limitations.md`](../fusion360-limitations.md)
- 实际 API 与类型：`backend/app/**`、`frontend/src/**`

本归档不保留旧文件行号。代码变更后行号会失效，新的实施计划必须引用符号、测试和可运行命令。
