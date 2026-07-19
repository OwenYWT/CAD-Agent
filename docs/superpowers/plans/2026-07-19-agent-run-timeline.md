# Agent Run Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CADAM / ForgeCAD-style run timeline that shows generation, validation, repair, inspection, and recovery as a visible staged process.

**Architecture:** Extend the backend `StepUpdate` contract additively so existing `step` and `message` clients keep working. Add a small backend helper for structured timed events, then build a focused React timeline component over existing `stepHistory`, `repair_history`, and `inspect_report`. Recovery actions reuse existing WebSocket operations instead of adding cancellation, pause/resume, or partial coroutine retry.

**Tech Stack:** FastAPI, Pydantic, async WebSocket events, React, TypeScript, Zustand, Vite, Pytest.

---

## Source Attribution

- CADAM references: `../CADAM/src/components/chat/ChatReasoning.tsx`, `../CADAM/src/hooks/useLoadingProgress.tsx`, `../CADAM/src/constants/spinnerVerbs.ts`.
- CADAM idea: make agent progress readable and visible instead of using only a silent spinner.
- ForgeCAD references: `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`, `../forgecad-public-kit/skills/forgecad-inspect-model/SKILL.md`.
- ForgeCAD idea: expose run, inspect, fix, retry, and evidence stages honestly.

## File Responsibilities

- `backend/app/models/schemas.py`: Add optional lifecycle fields and status validation to `StepUpdate`.
- `backend/tests/test_agent_run_timeline.py`: Add backend contract and helper tests.
- `backend/app/agent/run_steps.py`: Add lightweight helper functions for structured step creation and durations.
- `backend/app/agent/orchestrator.py`: Upgrade key generation, execution, repair, snapshot, completion, and failure step events.
- `backend/app/agent/multi_step.py`: Upgrade assembly and final execution step events.
- `backend/app/api/websocket.py`: Keep WebSocket passthrough; verify structured payloads in tests.
- `frontend/src/types/index.ts`: Extend `StepUpdate` and add `StepStatus`.
- `frontend/src/stores/sessionStore.ts`: Preserve completed timeline history and synthesize final status only when absent.
- `frontend/src/components/AgentRunTimeline.tsx`: Render timeline, evidence summary, and safe recovery actions.
- `frontend/src/App.tsx`: Wire timeline into the analysis tab.
- `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`: Record Adaptation 6 traceability.

## Stage Vocabulary

- Public statuses: `queued`, `running`, `success`, `warn`, `failed`, `skipped`.
- Preferred stages: `intent_detection`, `planning`, `retrieving_examples`, `generating_code`, `executing`, `validating_geometry`, `validating_vision`, `repairing_code`, `fixing_error`, `rendering_preview`, `exporting_files`, `snapshotting_version`, `assembly_part`, `complete`, `failed`.
- Compatibility rule: keep current stage names valid; add aliases only in frontend labels.

---

### Task 1: Backend StepUpdate Contract

**Files:**
- Modify: `backend/app/models/schemas.py`
- Create: `backend/tests/test_agent_run_timeline.py`

- [ ] **Step 1: Write failing schema tests**

Create `backend/tests/test_agent_run_timeline.py`:

```python
import pytest
from pydantic import ValidationError

from app.models.schemas import StepUpdate


def test_step_update_remains_backward_compatible():
    step = StepUpdate(step="planning", message="Planning model")
    payload = step.model_dump()

    assert payload["step"] == "planning"
    assert payload["message"] == "Planning model"
    assert payload["status"] == "running"
    assert payload["stage_id"] is None
    assert payload["attempt"] is None
    assert payload["started_at"] is None
    assert payload["duration_ms"] is None
    assert payload["detail"] is None


def test_step_update_accepts_structured_timeline_fields():
    step = StepUpdate(
        step="repairing_code",
        message="Repairing code after execution error",
        status="warn",
        stage_id="repairing_code:2",
        attempt=2,
        started_at="2026-07-19T09:01:02.003Z",
        duration_ms=2410,
        detail={"error_type": "NameError", "source": "execution"},
    )

    assert step.status == "warn"
    assert step.stage_id == "repairing_code:2"
    assert step.attempt == 2
    assert step.duration_ms == 2410
    assert step.detail == {"error_type": "NameError", "source": "execution"}


def test_step_update_rejects_unknown_status():
    with pytest.raises(ValidationError):
        StepUpdate(step="planning", message="Planning model", status="thinking")
```

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest backend\tests\test_agent_run_timeline.py -q`

Expected: fails because `StepUpdate` lacks the new timeline fields.

- [ ] **Step 3: Extend schema**

Replace `StepUpdate` in `backend/app/models/schemas.py` with:

```python
class StepUpdate(BaseModel):
    step: str
    message: str
    status: str = "running"
    stage_id: str | None = None
    attempt: int | None = None
    started_at: str | None = None
    duration_ms: int | None = None
    detail: dict | None = None
    part_name: str | None = None
    part_index: int | None = None
    total_parts: int | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        allowed = {"queued", "running", "success", "warn", "failed", "skipped"}
        if value not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}")
        return value
```

- [ ] **Step 4: Verify tests pass**

Run: `python -m pytest backend\tests\test_agent_run_timeline.py -q`

Expected: `3 passed`.

---

### Task 2: Backend Timeline Helper

**Files:**
- Create: `backend/app/agent/run_steps.py`
- Modify: `backend/tests/test_agent_run_timeline.py`

- [ ] **Step 1: Add helper tests**

Append to `backend/tests/test_agent_run_timeline.py`:

```python
from app.agent.run_steps import RunStageTimer, make_step


def test_make_step_creates_stable_stage_id_and_detail():
    step = make_step(
        "executing",
        "Running sandbox",
        status="running",
        attempt=1,
        detail={"source": "sandbox"},
    )

    assert step.step == "executing"
    assert step.status == "running"
    assert step.stage_id == "executing:1"
    assert step.started_at is not None
    assert step.detail == {"source": "sandbox"}


def test_run_stage_timer_completes_with_duration():
    timer = RunStageTimer("validating_geometry", "Checking model", attempt=1)
    started = timer.start()
    completed = timer.complete("Geometry validation complete", status="success")

    assert started.status == "running"
    assert completed.status == "success"
    assert completed.stage_id == "validating_geometry:1"
    assert completed.duration_ms is not None
    assert completed.duration_ms >= 0
```

- [ ] **Step 2: Verify helper tests fail**

Run: `python -m pytest backend\tests\test_agent_run_timeline.py -q`

Expected: fails with `ModuleNotFoundError` for `app.agent.run_steps`.

- [ ] **Step 3: Add helper module**

Create `backend/app/agent/run_steps.py` with `make_step()` and `RunStageTimer` exactly as specified by this plan: UTC ISO timestamps, stable `stage_id`, optional `attempt`, `detail`, part metadata, and `duration_ms` measured by `perf_counter()`.

- [ ] **Step 4: Verify helper tests pass**

Run: `python -m pytest backend\tests\test_agent_run_timeline.py -q`

Expected: `5 passed`.

---

### Task 3: Backend Structured Emissions

**Files:**
- Modify: `backend/app/agent/orchestrator.py`
- Modify: `backend/app/agent/multi_step.py`
- Modify: `backend/tests/test_deploy_websocket.py`

- [ ] **Step 1: Add WebSocket payload regression**

In the existing WebSocket generation test in `backend/tests/test_deploy_websocket.py`, assert the first `step_update` data includes:

```python
first_step = next(event["data"] for event in events if event["type"] == "step_update")
assert first_step["status"] in {"queued", "running", "success", "warn", "failed", "skipped"}
assert "stage_id" in first_step
assert "started_at" in first_step
assert "duration_ms" in first_step
assert "detail" in first_step
```

- [ ] **Step 2: Verify focused WebSocket tests**

Run: `python -m pytest backend\tests\test_deploy_websocket.py -q`

Expected: tests pass after Task 1 defaults, or fail only at the new assertion before Task 1 is complete.

- [ ] **Step 3: Use helper in orchestrator**

Add import to `backend/app/agent/orchestrator.py`:

```python
from app.agent.run_steps import RunStageTimer, make_step
```

Use this pattern for start-only stages such as planning, example retrieval, code generation, snapshotting, completion, and failure:

```python
await _call_step(
    on_step,
    make_step("planning", "正在理解需求并制定建模计划...", detail={"source": "planner"}),
)
```

Use this pattern for execution blocks:

```python
execution_timer = RunStageTimer("executing", "正在执行 CadQuery 代码...", attempt=attempt + 1, detail={"source": "sandbox"})
if on_step:
    await _call_step(on_step, execution_timer.start())

# existing sandbox execution remains here

if on_step:
    await _call_step(
        on_step,
        execution_timer.complete(
            "CadQuery 代码执行完成",
            status="success" if result.success else "failed",
            detail={"source": "sandbox", "success": result.success},
        ),
    )
```

Use this pattern for repair attempts:

```python
await _call_step(
    on_step,
    make_step(
        "repairing_code",
        f"正在自动修复第 {attempt + 1} 次错误...",
        status="warn",
        attempt=attempt + 1,
        detail={"source": "repair_loop"},
    ),
)
```

- [ ] **Step 4: Use helper in multi-step generator**

Add import to `backend/app/agent/multi_step.py`:

```python
from app.agent.run_steps import RunStageTimer, make_step
```

Use this pattern for assembly progress while preserving part metadata:

```python
await _call_step(
    on_step,
    make_step(
        "assembly_part",
        f"正在生成零件 {i + 1}/{len(build_plan.parts)}: {part.name}",
        attempt=i + 1,
        detail={"source": "assembly"},
        part_name=part.name,
        part_index=i + 1,
        total_parts=len(build_plan.parts),
    ),
)
```

- [ ] **Step 5: Run backend regression tests**

Run: `python -m pytest backend\tests\test_agent_run_timeline.py backend\tests\test_deploy_websocket.py backend\tests\test_repair_history.py backend\tests\test_inspect_report.py -q`

Expected: selected tests pass. Do not fix unrelated API-key wording failures in `backend/tests/test_codegen.py`.

---

### Task 4: Frontend Types And Store

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/stores/sessionStore.ts`

- [ ] **Step 1: Extend TypeScript type**

Replace `StepUpdate` in `frontend/src/types/index.ts` with:

```ts
export type StepStatus = "queued" | "running" | "success" | "warn" | "failed" | "skipped";

export interface StepUpdate {
  step: string;
  message: string;
  status?: StepStatus;
  stage_id?: string | null;
  attempt?: number | null;
  started_at?: string | null;
  duration_ms?: number | null;
  detail?: Record<string, unknown> | null;
  part_name?: string;
  part_index?: number;
  total_parts?: number;
}
```

- [ ] **Step 2: Add terminal helper functions**

In `frontend/src/stores/sessionStore.ts`, add near `StepHistoryEntry`:

```ts
function hasTerminalStep(history: StepHistoryEntry[]) {
  return history.some((entry) => entry.step === "complete" || entry.step === "failed" || entry.status === "failed");
}

function terminalStepFromResult(success: boolean): StepHistoryEntry {
  const timestamp = Date.now();
  return {
    step: success ? "complete" : "failed",
    message: success ? "Generation completed" : "Generation failed",
    status: success ? "success" : "failed",
    stage_id: success ? "complete" : "failed",
    started_at: new Date(timestamp).toISOString(),
    duration_ms: null,
    detail: { source: "frontend_result" },
    timestamp,
  };
}
```

- [ ] **Step 3: Preserve finished run timeline**

In generation result handling, stop clearing `stepHistory`. Return this shape:

```ts
const nextStepHistory = hasTerminalStep(p.stepHistory)
  ? p.stepHistory
  : [...p.stepHistory, terminalStepFromResult(Boolean(result.success))];

return {
  ...p,
  isGenerating: false,
  currentStep: null,
  result: enrichedResult,
  messages: updatedMessages,
  stepHistory: nextStepHistory,
};
```

- [ ] **Step 4: Clear only at new run start**

Keep `stepHistory: []` in actions that start a new generation, direct code execution, part modification, or restore replacement:

```ts
return {
  ...p,
  isGenerating: true,
  currentStep: null,
  generationStartTime: Date.now(),
  stepHistory: [],
  multiStepProgress: null,
};
```

- [ ] **Step 5: Verify frontend build**

Run: `cd frontend; npm.cmd run build`

Expected: build succeeds; Vite chunk warnings are acceptable.

---

### Task 5: AgentRunTimeline Component

**Files:**
- Create: `frontend/src/components/AgentRunTimeline.tsx`

- [ ] **Step 1: Create component**

Create `frontend/src/components/AgentRunTimeline.tsx` with these exact interface boundaries and helper functions:

```tsx
import type { GenerationResult, InspectReport, RepairStep, StepStatus, StepUpdate } from "../types";
import type { StepHistoryEntry } from "../stores/sessionStore";

interface AgentRunTimelineProps {
  steps: StepHistoryEntry[];
  isGenerating: boolean;
  result: GenerationResult | null;
  repairHistory?: RepairStep[] | null;
  inspectReport?: InspectReport | null;
  onRetryPrompt?: () => void;
  onRerunCode?: () => void;
}

const STATUS_STYLES: Record<StepStatus, string> = {
  queued: "bg-gray-100 text-gray-600 border-gray-200",
  running: "bg-indigo-50 text-indigo-700 border-indigo-200",
  success: "bg-emerald-50 text-emerald-700 border-emerald-200",
  warn: "bg-amber-50 text-amber-700 border-amber-200",
  failed: "bg-red-50 text-red-700 border-red-200",
  skipped: "bg-gray-50 text-gray-500 border-gray-200",
};

const STATUS_ICON: Record<StepStatus, string> = { queued: "○", running: "◐", success: "✓", warn: "!", failed: "×", skipped: "–" };

const STEP_LABELS: Record<string, string> = {
  planning: "Planning",
  retrieving_examples: "Examples",
  generating_code: "Code generation",
  executing: "Execution",
  validating_geometry: "Geometry check",
  validating_vision: "Visual check",
  repairing_code: "Repair",
  fixing_error: "Repair",
  snapshotting_version: "Snapshot",
  assembly_part: "Assembly part",
  complete: "Complete",
  failed: "Failed",
};

function normalizeStatus(step: StepUpdate, isLast: boolean, isGenerating: boolean): StepStatus {
  if (step.status) return step.status;
  if (step.step === "complete") return "success";
  if (step.step === "failed") return "failed";
  if (step.step === "fixing_error" || step.step === "repairing_code") return "warn";
  if (!isGenerating && !isLast) return "success";
  return "running";
}
```

Then render a compact card with:

- Header text: `Agent Run Timeline`.
- Subtitle text: `Visible CADAM-style progress with ForgeCAD-style inspect and repair evidence.`
- Per-step label, status badge, message, optional `attempt`, optional `duration_ms`, optional detail source/error, and optional part metadata.
- Evidence summary: `Repair evidence: N recorded repair step(s).` and `Inspect evidence: verdict X, printable Y.`
- Buttons: `Retry Prompt` only on failed runs, `Re-run Current Code` only when `result.code` exists.

- [ ] **Step 2: Verify frontend build**

Run: `cd frontend; npm.cmd run build`

Expected: build succeeds. If TypeScript reports an unused import, remove only that import.

---

### Task 6: Wire Timeline Into App

**Files:**
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Import component**

Add near existing component imports:

```ts
import AgentRunTimeline from "./components/AgentRunTimeline";
```

- [ ] **Step 2: Add safe action handlers**

Inside `App`, add after existing callbacks:

```ts
const handleRetryPrompt = useCallback(() => {
  const latestPrompt = panel.messages.filter((message) => message.role === "user").at(-1)?.content;
  if (latestPrompt && !panel.isGenerating) {
    sendMessage(latestPrompt);
  }
}, [panel.messages, panel.isGenerating, sendMessage]);

const handleRerunCurrentCode = useCallback(() => {
  if (result?.code && !panel.isGenerating) {
    executeCode(result.code);
  }
}, [executeCode, panel.isGenerating, result?.code]);
```

If needed, update the React import:

```ts
import { useCallback, useEffect, useMemo, useState } from "react";
```

- [ ] **Step 3: Render in analysis tab**

Render above `InspectReportPanel`:

```tsx
<AgentRunTimeline
  steps={panel.stepHistory}
  isGenerating={panel.isGenerating}
  result={result}
  repairHistory={result?.repair_history || null}
  inspectReport={result?.inspect_report || null}
  onRetryPrompt={handleRetryPrompt}
  onRerunCode={handleRerunCurrentCode}
/>
```

- [ ] **Step 4: Verify frontend build**

Run: `cd frontend; npm.cmd run build`

Expected: build succeeds.

---

### Task 7: Traceability Report

**Files:**
- Modify: `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`

- [ ] **Step 1: Add Adaptation 6 section**

Append:

```markdown
## Adaptation 6: CADAM / ForgeCAD-Style Agent Run Timeline

### Source

- CADAM: `../CADAM/src/components/chat/ChatReasoning.tsx`, `../CADAM/src/hooks/useLoadingProgress.tsx`, `../CADAM/src/constants/spinnerVerbs.ts`
- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`, `../forgecad-public-kit/skills/forgecad-inspect-model/SKILL.md`

### Borrowed Idea

CADAM shows agent work as visible progress rather than an opaque wait state. ForgeCAD makes build, inspect, repair, and retry stages explicit. CAD-Agent adapts both ideas into a timeline that explains what the agent did, which stages warned or failed, and which recovery actions are safe.

### CAD-Agent Changes

- Extended backend `StepUpdate` with additive lifecycle metadata: `status`, `stage_id`, `attempt`, `started_at`, `duration_ms`, and `detail`.
- Added lightweight backend timeline helpers in `backend/app/agent/run_steps.py`.
- Upgraded backend generation, execution, repair, assembly, snapshot, completion, and failure step events with structured status metadata.
- Extended frontend timeline types and preserved completed run history after generation results.
- Added `AgentRunTimeline` in the analysis tab with status badges, timing, evidence summaries, `Retry Prompt`, and `Re-run Current Code` actions.

### Non-Goals Kept

- No true backend cancellation was added.
- No pause/resume workflow was added.
- No hidden chain-of-thought or private model reasoning was exposed.
- No arbitrary mid-coroutine partial retry was added.

### Verification

- `python -m pytest backend\tests\test_agent_run_timeline.py backend\tests\test_deploy_websocket.py backend\tests\test_repair_history.py backend\tests\test_inspect_report.py -q`
- `cd frontend && npm.cmd run build`
```

- [ ] **Step 2: Verify report entry**

Run: `Select-String -Path docs\case-study-adaptations\CAD-Agent-adaptation-report.md -Pattern "Adaptation 6|Agent Run Timeline|CADAM|forgecad-public-kit"`

Expected: Adaptation 6 appears with both source projects and verification commands.

---

### Task 8: Final Verification

**Files:**
- Read only: test outputs

- [ ] **Step 1: Run focused backend suite**

Run: `python -m pytest backend\tests\test_agent_run_timeline.py backend\tests\test_deploy_websocket.py backend\tests\test_repair_history.py backend\tests\test_inspect_report.py -q`

Expected: selected tests pass.

- [ ] **Step 2: Run frontend build**

Run: `cd frontend; npm.cmd run build`

Expected: TypeScript and Vite build pass. Bundle-size warnings are acceptable.

- [ ] **Step 3: Manual smoke test**

Run backend and frontend in separate terminals:

```powershell
cd backend
python -m uvicorn app.main:app --reload --port 8000
```

```powershell
cd frontend
npm.cmd run dev
```

Expected:

- Chat inline progress still appears during generation.
- The analysis tab shows `Agent Run Timeline` after a run.
- Timeline shows planning, code generation, execution, repair, and validation stages when available.
- Failed runs show `Retry Prompt`.
- Runs with code show `Re-run Current Code`.
- Existing `InspectReportPanel`, `VersionHistoryPanel`, `RepairHistory`, and `MultiStepProgress` remain visible.

---

## Self-Review Notes

- Spec coverage: visible progress, statuses, timing, retry attempts, repair/inspect links, safe actions, and traceability are all mapped to tasks.
- Compatibility: new backend fields are optional or defaulted, so older clients that use only `step` and `message` continue working.
- Non-goals: no true cancellation, pause/resume, job queue, chain-of-thought exposure, or arbitrary partial retry.
- Test strategy: backend contract uses Pytest; frontend safety uses the existing `npm.cmd run build` script because no frontend test script exists.
- Traceability: Adaptation 6 must be recorded in the comprehensive report before handoff.
