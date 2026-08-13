# CADAM 编排优点借鉴档案（2026-08）

> 状态：归档说明，不代表当前所有能力都已完全闭环。  
> 范围：总结近期提交中从 CADAM 借鉴到 CAD-Agent 的编排优点、落点和收益。

## 归档目的

这份档案记录的是：CAD-Agent 在近期提交 `aabb59a3056258895d4b4fe3782dc5e7c9418f0b` 中，吸收了 CADAM 哪些“编排层”的优点，以及这些优点具体落到了哪里。

重点不是复刻 CADAM 的技术栈，而是复用它在**状态建模、恢复能力、事件分层、结果可追踪性**上的方法。

## 借鉴到的优点

### 1. 工具契约显式化

CADAM 把关键动作做成 schema 化工具，避免把核心结果藏在自然语言里。

**借鉴价值**

- 模型输出边界清晰，主结果更可控。
- 工具输入/输出天然可校验，便于测试与调试。
- 后续扩展新能力时，协议演进更稳。

**在 CAD-Agent 的体现**

- 把生成、执行、修复、恢复拆成明确的 step/事件。
- 前后端通过结构化 `WSMessage` 传递状态，而不是只靠文本串联。

### 2. 工具结果驱动下一步

CADAM 的关键经验是：工具结果先落盘、状态先稳定，再决定是否继续下一步。

**借鉴价值**

- 自动续跑不依赖“记忆里的临时状态”。
- 失败重试、恢复和续跑可以共用同一套判断逻辑。
- 任务链路更接近真正的状态机，而不是递归式脚本。

**在 CAD-Agent 的体现**

- `backend/app/agent/step_runner.py` 根据已持久化 step 决定下一步。
- `backend/app/agent/state_machine.py` 将验证、执行、修复、终结显式化。

### 3. 单一事实源

CADAM 不把完整上下文信任给客户端，而是以服务端持久化数据为准回放分支。

**借鉴价值**

- 避免客户端缓存和服务端真实状态不一致。
- 重连、刷新、恢复时可以稳定重建现场。
- 对审计和排错更友好。

**在 CAD-Agent 的体现**

- 新增 `agent_runs`、`agent_steps`、`agent_artifacts` 三张表作为事实源。
- WebSocket 重连时会从数据库 replay 最新 run，而不是只依赖前端内存。

### 4. 中断恢复可操作化

CADAM 会把卡住的工具调用转成可见、可恢复的状态，而不是简单报错结束。

**借鉴价值**

- 服务重启、浏览器断线、执行中断后，任务还能被识别和接续。
- 失败状态不再是“死局”，而是可进入恢复分支。
- 用户能明确知道下一步是“继续”还是“重新开始”。

**在 CAD-Agent 的体现**

- `backend/app/agent/recovery.py` 会识别 running / stale run。
- 可恢复的 run 会被标成 `blocked`，并生成 `resume_available` step。
- 前端据此展示“继续上次任务”按钮。

### 5. 主结果与侧带事件分离

CADAM 会把标题、建议等 side-channel 信息与主消息分开，避免污染主结果。

**借鉴价值**

- 主流程更稳定，UI 事件更清晰。
- 后续加诊断、提示、阶段性进度时，不必改主结果格式。
- 前端可以按事件类型分别消费。

**在 CAD-Agent 的体现**

- WebSocket 事件拆分为 `run_created`、`step_update`、`agent_step`、`artifact_update`、`generation_result`。
- 前端 `useWebSocket.ts` 和 `sessionStore.ts` 分别处理不同事件层。

### 6. 恢复状态可回放

CADAM 的思路不是“恢复一个进度条”，而是恢复一整套可解释状态。

**借鉴价值**

- 恢复后能看见 step 历史、artifact 历史和当前运行态。
- 不会出现“页面能看见结果，但后台不知道怎么来的”。

**在 CAD-Agent 的体现**

- `_replay_latest_run` 会补发 run、step、artifact 事件。
- 前端时间线组件能直接展示恢复后的链路。

## 这次借鉴带来的整体收益

- **更可恢复**：任务不再只靠一次性执行，能从断点继续。
- **更可审计**：run / step / artifact 都能追溯。
- **更可扩展**：后续增加诊断、分支、更多工具时，协议已经有骨架。
- **更贴近工程化 agent**：从“脚本式执行”升级为“状态机式编排”。

## 关键落点文件

- `docs/agent-orchestration-upgrade.md`
- `backend/app/storage/history.py`
- `backend/app/agent/run_store.py`
- `backend/app/agent/recovery.py`
- `backend/app/agent/state_machine.py`
- `backend/app/agent/step_runner.py`
- `backend/app/api/websocket.py`
- `frontend/src/hooks/useWebSocket.ts`
- `frontend/src/stores/sessionStore.ts`
- `frontend/src/components/AgentRunTimeline.tsx`

## 结论

这次提交借鉴到的核心优点，可以概括为一句话：

**把 agent 编排从“内存里的自动重试”升级成“以数据库为事实源、以状态机为核心、以事件分层为接口、以恢复机制为兜底”的工程化流程。**
