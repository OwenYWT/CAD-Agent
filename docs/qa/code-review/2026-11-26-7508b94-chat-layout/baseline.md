# 聊天工作台改造基线

2026-11-26，从远端重新获取 main 后，原工作区 HEAD、origin/main、FETCH_HEAD 与布局分支起点均为 `7508b94d9f4e1cf0a306508d66ab27729db2a18e`。原工作区的未跟踪历史报告没有纳入比较。上一轮界面原型位于独立工作区，以下基线取自未修改的 main。本轮调整之前运行了基线前端全套测试，183 项全部通过。

| 业务链 | 前端入口与数据 | 后端权威事实及比对项 |
| --- | --- | --- |
| 身份与历史 | App、auth、ProjectSidebar、PanelTabs、历史分页与恢复 | 密码登录和 token、历史列表、线程消息、已保存任务与文档归属。刷新、换线程后读取原有记录 |
| 创建与需求依据 | ProjectStart → RequirementCard → useWebSocket。制造方式、原始目标、尺寸、来源、用途、配合说明、概念确认组成 requirement-basis.v1 | POST /api/tasks/requirements/preview 只返回建议。用户确认后由 WS /ws/{session} 发送 user_message、panel_id、idempotency_key、operation_intent、制造方式及依据。每次提交只受理一次 |
| 规划、建模、验收 | task_submitted 回执，任务 snapshot、有序 events、服务端计划确认和恢复 | durable_submission → workflow_dispatch → Temporal Agent V2 → Model Job 和模型服务 → 沙箱 FreeCAD → 几何量测、检查和封存。PostgreSQL 保存工作流和审阅状态，S3 保存实际文件与证据 |
| 对话状态与协同 | Zustand sessionStore、pendingSubmissions、taskState、useDocumentView、useCloudDocument、draftGuard | 用户、会话、线程、任务、文档、修订、选中特征、手工草稿互相隔离。失败、取消及过期事件不能推进分支。恢复、重试、取消和权限检查沿用原入口 |
| 模型与审阅 | MechanicalWorkspace、原生场景和选择、候选审阅、修改建议、接受/提交/回滚 | change_sets / revisions / branches。明确审核步骤、预期基线、状态版本、必需验证、权限决定能否提交。候选产生时分支头不动。查看、输入与展开均不提交 |
| 专业能力 | 项目流程、属性、模型树、草图、检查、版本、发布和导出、账户与设置 | 手动参数变更使用租约和基线，保存后更新当前版本。版本、历史恢复、FEA、CAM、DFM、BOM、发布和交付沿用各自客户端、服务及证据标识 |
| 文件 | 打开已有 FCStd / STEP，2D SVG、3D 场景或 STL，导出及下载 | 验证真实 MIME/大小、SHA-256、对象存储版本、修订归属及重新读取。FCStd、STEP 的几何结果可由独立 FreeCAD 进程重开测量 |

功能边界以 `docs/functional-inventory-2026-09-14.md` 及当前实现为准。可选外接设备、第三方账户与外部短信等能力需要各自真实环境；逐项范围与结论记录在最终报告。回归采用独立测试数据库、版本化对象桶、Temporal 队列及测试账户，基线与改版使用相同后端。
