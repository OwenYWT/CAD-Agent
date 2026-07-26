# Agent 编排升级方案

> 目标：借鉴 CADAM 的可恢复工具闭环，把当前 CAD-Agent 的后端流水线升级为可追踪、可恢复、可分支、可自动续跑的工程化 agent 编排系统。

## 背景

当前项目已经具备完整的 CAD 生成链路：用户通过 WebSocket 发起请求，后端 `Orchestrator` 完成计划、代码生成、沙箱执行、验证、修复和结果推送。这个架构适合 CadQuery / ezdxf / DFM / 文件归属等后端重型能力。

CADAM 的优势不在具体技术栈，而在编排形态：它把模型输出、工具调用、工具结果、自动续跑、消息分支和中断恢复都建模成显式状态。我们应保留现有 `FastAPI + WebSocket + CadQuery sandbox + SQLite` 基础，只吸收这些编排原则。

## CADAM 可借鉴点

### 1. 工具契约明确

CADAM 把关键动作声明成 schema 化工具：

- `build_parametric_model`：生成或更新完整 OpenSCAD artifact。
- `answer_user`：给用户最终答复。
- `create_mesh`：生成创意网格资产。

工具输入输出都有结构化 schema，模型不能把核心结果藏在自然语言里。对应参考：`D:\project\selfprojectcad\CADAM\shared\chatAi.ts:42`。

### 2. 自动循环由工具结果驱动

CADAM 的参数化链路是：

```text
用户请求
  -> 模型调用 build_parametric_model
  -> 浏览器编译 OpenSCAD 并生成多视图预览
  -> 工具结果写回数据库
  -> 满足自动续跑条件后继续让模型检查/修复
  -> 模型调用 answer_user 结束
```

关键点不是“在哪里执行 CAD”，而是“工具结果落盘后才允许下一步继续”。对应参考：`D:\project\selfprojectcad\CADAM\src\components\chat\ChatSession.tsx:580`。

### 3. 单一事实源

CADAM 服务端不信任客户端提交的完整消息上下文，只接收 `conversationId` 和模型选择，然后从数据库当前 leaf 回溯分支生成模型上下文。对应参考：`D:\project\selfprojectcad\CADAM\src\server\aiChat.ts:971`。

这能避免重试、恢复、编辑消息时出现“客户端上下文和服务端上下文不一致”。

### 4. 中断恢复

CADAM 会扫描卡在 `input-streaming` 或 `input-available` 的工具调用，把它们改写成可展示、可继续操作的失败状态。对应参考：`D:\project\selfprojectcad\CADAM\src\components\chat\ChatSession.tsx:690`。

我们当前已有 retry 和 failure taxonomy，但缺少“服务重启 / 浏览器断线 / 执行中断后恢复 run 状态”的机制。

### 5. 主结果与侧带事件分离

CADAM 用 transient data parts 推送标题、建议等 side-channel 信息，不污染主消息 parts。我们可以在 WebSocket 中保留类似分离：`generation_result` 是主结果，`run_title`、`suggestions`、`diagnostics`、`timeline_update` 是辅助事件。

## 我们当前架构对照

| 维度 | CADAM | 当前 CAD-Agent | 升级方向 |
|---|---|---|---|
| 编排入口 | Server route + AI SDK stream | `backend/app/agent/orchestrator.py` | 保留 Python 后端编排 |
| 实时传输 | SSE / AI SDK UI stream | WebSocket | 保留 WebSocket，细化事件类型 |
| 工具执行 | 浏览器 OpenSCAD WASM | 后端 CadQuery/ezdxf sandbox | 保留后端沙箱，改为工具结果事件驱动 |
| 状态源 | Supabase messages + leaf | 内存 `ConversationContext` + 文件 | 增加 SQLite run/step/history 表 |
| 自动续跑 | 工具结果落盘后触发 | 后端内部 retry loop | 显式 step 状态机 + resumable retry |
| 恢复能力 | stuck tool recovery | WebSocket 重连仅提示失败 | 增加 pending run recovery |

## 目标架构

```text
Frontend WebSocket
  -> POST/WS user_message
  -> RunManager 创建或恢复 AgentRun
  -> AgentStateMachine 执行下一步
      -> plan_design
      -> generate_cad_code
      -> execute_cad_code
      -> inspect_geometry
      -> repair_code / finalize_result
  -> 每个 step 输入/输出/status 持久化
  -> WebSocket 推送 step_update / artifact_update / diagnostics / generation_result
```

### 核心原则

- 每一次用户请求对应一个 `AgentRun`。
- 每一个关键动作对应一个 `AgentStep`。
- 每个 step 必须有稳定的 `status`：`pending`、`running`、`succeeded`、`failed`、`blocked`、`cancelled`。
- 每个 step 的 `input_json` 和 `output_json` 都可审计。
- 下一步只读取已落盘的上一步结果。
- 失败修复也作为 step 记录，不再只是内存里的 retry。

## 数据模型建议

### `agent_runs`

```sql
CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  panel_id TEXT,
  user_prompt TEXT NOT NULL,
  capability TEXT NOT NULL DEFAULT 'auto',
  status TEXT NOT NULL,
  current_step_id TEXT,
  result_request_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
```

### `agent_steps`

```sql
CREATE TABLE IF NOT EXISTS agent_steps (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  step_index INTEGER NOT NULL,
  step_type TEXT NOT NULL,
  status TEXT NOT NULL,
  input_json TEXT NOT NULL,
  output_json TEXT,
  error_json TEXT,
  started_at TEXT,
  completed_at TEXT,
  FOREIGN KEY(run_id) REFERENCES agent_runs(id)
);
```

### `agent_artifacts`

```sql
CREATE TABLE IF NOT EXISTS agent_artifacts (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  artifact_type TEXT NOT NULL,
  path TEXT NOT NULL,
  metadata_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES agent_runs(id),
  FOREIGN KEY(step_id) REFERENCES agent_steps(id)
);
```

这些表可以先落在现有 SQLite 存储层旁边，不需要引入 Supabase。

## 工具契约建议

用 Pydantic 定义内部 agent tool contract。第一阶段不需要接 AI SDK，只需要让后端各步骤统一输入输出。

```python
class PlanDesignInput(BaseModel):
    prompt: str
    manufacturing_profile: ManufacturingProfile | None = None
    previous_code: str | None = None

class PlanDesignOutput(BaseModel):
    intent: Literal['generate', 'modify', 'execute_code', 'blocked']
    design_brief: DesignBrief
    output_formats: list[str]
    blocked_reason: str | None = None

class GenerateCadCodeInput(BaseModel):
    prompt: str
    design_brief: DesignBrief
    previous_code: str | None = None
    examples: list[dict] = []

class GenerateCadCodeOutput(BaseModel):
    code: str
    language: Literal['cadquery', 'ezdxf']
    parameters: list[ParamConfig] = []

class ExecuteCadCodeInput(BaseModel):
    code: str
    output_formats: list[str]
    mode: Literal['3d', '2d'] = '3d'

class ExecuteCadCodeOutput(BaseModel):
    success: bool
    request_id: str
    files: list[str]
    error_message: str | None = None

class InspectGeometryInput(BaseModel):
    request_id: str
    prompt: str
    code: str

class InspectGeometryOutput(BaseModel):
    passed: bool
    validation: ValidationResult | None = None
    inspect_report: dict | None = None
    repair_hint: str | None = None

class RepairCodeInput(BaseModel):
    original_prompt: str
    code: str
    failure_type: str
    error_message: str
    repair_history: list[RepairStep]

class RepairCodeOutput(BaseModel):
    code: str
    reason: str
```

## WebSocket 事件建议

保留当前 `step_update` 和 `generation_result`，新增更细粒度的事件，避免把诊断和最终结果混在一起。

```ts
type RunCreatedEvent = {
  type: 'run_created';
  data: { run_id: string; panel_id: string };
};

type AgentStepEvent = {
  type: 'agent_step';
  data: {
    run_id: string;
    step_id: string;
    step_index: number;
    step_type: string;
    status: 'pending' | 'running' | 'succeeded' | 'failed' | 'blocked' | 'cancelled';
    message: string;
    panel_id: string;
  };
};

type ArtifactEvent = {
  type: 'artifact_update';
  data: {
    run_id: string;
    step_id: string;
    artifact_type: 'code' | 'step' | 'stl' | 'dxf' | 'svg' | 'png' | 'inspect_report';
    url?: string;
    path?: string;
    panel_id: string;
  };
};

type DiagnosticsEvent = {
  type: 'diagnostics';
  data: {
    run_id: string;
    severity: 'info' | 'warning' | 'error';
    message: string;
    details?: unknown;
    panel_id: string;
  };
};
```

## 分阶段实施计划

### Phase 1：只做持久化 run/step，不改变生成行为

**目标：** 当前功能不变，但每次生成都有可追踪运行记录。

**涉及文件：**

- 新增：`backend/app/agent/run_store.py`
- 修改：`backend/app/agent/orchestrator.py`
- 修改：`backend/app/storage/database.py` 或现有 SQLite 初始化文件
- 修改：`backend/tests/test_agent_run_timeline.py`

**步骤：**

- [ ] 新增 `agent_runs`、`agent_steps`、`agent_artifacts` 表初始化。
- [ ] 新增 `RunStore.create_run()`、`start_step()`、`complete_step()`、`fail_step()`。
- [ ] 在 `Orchestrator.generate()` 入口创建 run。
- [ ] 在 plan/codegen/execute/validate/repair 关键节点写 step。
- [ ] 保持现有 WebSocket 消息不变，避免前端同步改造。
- [ ] 添加测试：生成请求执行后能查到完整 step 顺序。

**验收：**

- `python -m pytest backend/tests/test_agent_run_timeline.py`
- 一次成功生成至少包含 `plan_design`、`generate_cad_code`、`execute_cad_code`、`inspect_geometry`、`finalize_result`。
- 一次失败生成至少包含失败 step 的 `error_json`。

### Phase 2：显式状态机替代散落的 step callback

**目标：** 把当前 `_execute_with_retry()` 内部循环升级为可恢复状态机。

**涉及文件：**

- 新增：`backend/app/agent/state_machine.py`
- 修改：`backend/app/agent/orchestrator.py`
- 修改：`backend/app/agent/run_steps.py`
- 新增：`backend/tests/test_agent_state_machine.py`

**步骤：**

- [ ] 定义 `AgentStepType` 和 `AgentStepStatus` 枚举。
- [ ] 新增 `AgentStateMachine.run_next(run_id)`。
- [ ] 把 plan、codegen、execute、validate、repair 包装成独立 step handler。
- [ ] 每个 handler 只读取上一步落盘输出，不依赖临时局部变量。
- [ ] 把 retry 历史写入 `agent_steps.output_json`，不只写在最终响应。
- [ ] 添加测试：执行失败后进入 `repair_code`，修复成功后进入 `finalize_result`。

**验收：**

- 单元测试能模拟 executor 首次失败、二次成功。
- run 最终状态为 `succeeded`。
- repair step 中保留失败类型、原错误和修复理由。

### Phase 3：WebSocket 事件细化

**目标：** 前端能展示稳定的运行时间线、诊断和 artifact 更新。

**涉及文件：**

- 修改：`backend/app/api/websocket.py`
- 修改：`backend/app/models/schemas.py`
- 修改：`frontend/src/types/index.ts`
- 修改：`frontend/src/hooks/useWebSocket.ts`
- 修改：`frontend/src/stores/sessionStore.ts`

**步骤：**

- [ ] 增加 `run_created`、`agent_step`、`artifact_update`、`diagnostics` 类型。
- [ ] 后端在 step 状态变化时推送 `agent_step`。
- [ ] 后端生成文件时推送 `artifact_update`。
- [ ] 前端 session store 增加 `runsByPanelId` 或等价结构。
- [ ] UI 先复用现有进度列表展示，不新增复杂页面。

**验收：**

- 生成过程中刷新 WebSocket 消息能看到 step 状态从 `running` 到 `succeeded`。
- 老的 `generation_result` 仍然兼容。
- 断线重连不会把已完成 step 标成失败。

### Phase 4：中断恢复

**目标：** 服务重启、浏览器断线或 worker 中断后，run 不再无限卡住。

**涉及文件：**

- 新增：`backend/app/agent/recovery.py`
- 修改：`backend/app/main.py`
- 修改：`backend/app/api/websocket.py`
- 新增：`backend/tests/test_agent_recovery.py`

**步骤：**

- [ ] 启动时扫描 `running` 且 `updated_at` 超过阈值的 run。
- [ ] 如果 step 是纯 LLM/codegen 阶段，标记为 `failed` 并给出可重试诊断。
- [ ] 如果 step 是 execute 阶段，检查 work_dir / output files 是否存在。
- [ ] 有完整 artifact 时补写 `succeeded`，否则补写 `failed`。
- [ ] WebSocket reconnect 时按 panel 查询最近 run 状态并回放时间线。

**验收：**

- 测试能构造一个过期 `running` step，并被恢复为 `failed`。
- 前端重连后能展示明确的失败信息和重试入口。

### Phase 5：工具结果驱动的自动续跑

**目标：** 把内部 retry loop 改成“工具完成 -> 持久化 -> 下一步”的事件驱动模式。

**涉及文件：**

- 修改：`backend/app/agent/state_machine.py`
- 修改：`backend/app/agent/orchestrator.py`
- 修改：`backend/app/agent/failure_taxonomy.py`
- 修改：`backend/tests/test_repair_history.py`

**步骤：**

- [ ] `execute_cad_code` 完成后只写结果，不直接在局部循环里继续。
- [ ] 状态机读取 execute 输出，决定进入 `inspect_geometry` 或 `repair_code`。
- [ ] `repair_code` 完成后写新代码版本，再进入下一次 `execute_cad_code`。
- [ ] 设置最大修复次数和重复错误熔断。
- [ ] 最终 `finalize_result` 从 run/step/artifact 表组装 `GenerateResponse`。

**验收：**

- 修复历史在数据库中可逐步回放。
- 相同错误重复出现时 run 进入 `blocked` 或 `failed`，不继续烧模型调用。
- 最终响应内容与当前 API 兼容。

## 风险与取舍

- **不要照搬 CADAM 的浏览器执行模型。** 我们的 CadQuery、文件导出、DFM 和安全隔离更适合后端沙箱。
- **不要一次性重写 Orchestrator。** 先持久化 run/step，再抽状态机，降低回归风险。
- **不要引入 Supabase。** 当前 SQLite 足够支撑本地和单服务部署。
- **不要让 side-channel 变成主协议。** 最终 CAD 产物仍以 `generation_result` 为准。
- **不要让恢复机制自动重跑高成本 LLM。** 默认标记失败并给重试入口，只有明确安全的执行检查可以自动补全。

## 推荐落地顺序

1. Phase 1：新增 run/step 持久化，建立可观测基础。
2. Phase 3：细化 WebSocket 事件，让前端先看到稳定时间线。
3. Phase 4：做 pending run recovery，解决中断卡死。
4. Phase 2：抽状态机，减少 `orchestrator.py` 的隐式流程。
5. Phase 5：把 retry loop 改成事件驱动自动续跑。

这个顺序能保证每一步都有用户可见收益，并且不会中途破坏现有生成能力。

## 最小首个 PR 建议

首个 PR 只做 Phase 1，范围控制在后端：

- 新建 `RunStore`。
- 新建三张 SQLite 表。
- 在 `generate()` 和 `_execute_with_retry()` 关键节点写 step。
- 增加一个测试覆盖成功链路和失败链路。
- 不改 WebSocket 协议，不改前端 UI。

首个 PR 合入后，我们就能基于真实运行记录继续做状态机和恢复机制，而不是直接重构大文件。
