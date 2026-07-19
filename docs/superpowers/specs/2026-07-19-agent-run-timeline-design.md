# CADAM / ForgeCAD-Style Agent Run Timeline Design

## Background

CAD-Agent is being improved through traceable adaptations from CADAM and forgecad-public-kit. The previous adaptations added editable parameters, visible repair history, printable example retrieval, inspect reports, and version snapshots. The next improvement is to make every generation feel inspectable while it is running, not only after it finishes.

Users of a web CAD agent wait through several opaque phases: intent detection, planning, example retrieval, code generation, sandbox execution, validation, repair, rendering, and export. CADAM reduces waiting anxiety with visible reasoning/loading states. ForgeCAD emphasizes an explicit run -> inspect -> fix -> retry workflow. CAD-Agent already emits basic `step_update` messages, so this adaptation should productize that stream into a durable run timeline.

## Source Attribution

- Source project: `CADAM`
- Borrowed idea: show model work as an explicit, user-readable process instead of a silent spinner.
- Local references studied:
  - `../CADAM/src/components/chat/ChatReasoning.tsx`
  - `../CADAM/src/hooks/useLoadingProgress.tsx`
  - `../CADAM/src/constants/spinnerVerbs.ts`

- Source project: `forgecad-public-kit`
- Borrowed idea: CAD generation should follow visible run, inspect, repair, and retry stages with honest evidence.
- Local references studied:
  - `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`
  - `../forgecad-public-kit/skills/forgecad-inspect-model/SKILL.md`

## Current CAD-Agent State

CAD-Agent already has the minimum infrastructure for process visibility:

- `backend/app/models/schemas.py` defines `StepUpdate`.
- `backend/app/api/websocket.py` sends `step_update` WebSocket events.
- `backend/app/agent/orchestrator.py` emits steps such as `planning`, `retrieving_examples`, `generating_code`, `executing`, `fixing_error`, `assembly_part`, `complete`, and `failed`.
- `frontend/src/stores/sessionStore.ts` stores `currentStep`, `stepHistory`, `generationStartTime`, and `multiStepProgress`.
- `frontend/src/components/MultiStepProgress.tsx` displays assembly-style multi-step progress.
- `frontend/src/components/RepairHistory.tsx` displays post-run automatic repair records.
- `frontend/src/components/InspectReportPanel.tsx` displays post-run model evidence.

Current limitations:

- `StepUpdate` is mostly a message string and lacks explicit lifecycle metadata.
- The frontend cannot reliably distinguish started, running, completed, warning, failed, skipped, or retried stages.
- Step timing is client-derived and not tied to backend stage lifecycle.
- Repair and inspect evidence appear separately from the live execution flow.
- The cancel button is intentionally non-interactive during generation because cooperative cancellation is not implemented.
- There is no safe failed-run action beyond asking the user to send another prompt manually.

## Goal

Add a CADAM / ForgeCAD-style Agent Run Timeline to CAD-Agent.

The feature should let users:

- See what the CAD agent is doing during generation.
- Understand where time is spent.
- See retry attempts and repair stages in the same flow as generation.
- Understand why a run failed or produced warnings.
- Retry safely after failure using existing safe operations.
- Re-execute current code without another LLM call when that is the right recovery action.

## Non-Goals

- Do not implement true backend task cancellation in this phase.
- Do not add pause/resume or concurrent run orchestration.
- Do not introduce a separate job queue or task manager.
- Do not implement arbitrary step-level retry that resumes from the middle of a Python coroutine.
- Do not expose chain-of-thought or hidden LLM reasoning.
- Do not replace `repair_history`, `inspect_report`, or `model_snapshots`; instead, link them into the timeline UI.

## Proposed Scope: B, Recommended Version

### Backend Contract

Extend `StepUpdate` additively with lifecycle metadata:

```python
class StepUpdate(BaseModel):
    step: str
    message: str
    status: str = "running"  # "queued" | "running" | "success" | "warn" | "failed" | "skipped"
    stage_id: str | None = None
    attempt: int | None = None
    started_at: str | None = None
    duration_ms: int | None = None
    detail: dict | None = None
    part_name: str | None = None
    part_index: int | None = None
    total_parts: int | None = None
```

Compatibility rule:

- Existing clients that only read `step` and `message` continue to work.
- Backend code may continue constructing `StepUpdate(step=..., message=...)`; defaults fill the new fields.

### Backend Emission Helpers

Add a small helper layer in `backend/app/agent/orchestrator.py` or a new `backend/app/agent/run_steps.py` module.

Recommended helper concepts:

- `run_stage(step, message, status="running", attempt=None, detail=None)` creates a structured update.
- `emit_step(on_step, update)` safely calls the callback.
- `complete_stage(...)` emits the same stage with `status="success"` and `duration_ms` when timing is available.
- `fail_stage(...)` emits `status="failed"` with error details.

Keep this lightweight. The goal is better event structure, not a full workflow engine.

### Stage Vocabulary

Normalize step names while preserving current values:

- `intent_detection`
- `planning`
- `retrieving_examples`
- `generating_code`
- `executing`
- `validating_geometry`
- `validating_vision`
- `repairing_code`
- `rendering_preview`
- `exporting_files`
- `snapshotting_version`
- `complete`
- `failed`

Existing step names such as `fixing_error` can still be accepted by frontend mapping, but new emissions should prefer `repairing_code`.

### Frontend Types

Extend `StepUpdate` in `frontend/src/types/index.ts`:

```ts
export interface StepUpdate {
  step: string;
  message: string;
  status?: "queued" | "running" | "success" | "warn" | "failed" | "skipped" | string;
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

### Frontend Timeline UI

Add `frontend/src/components/AgentRunTimeline.tsx`.

The component should:

- Render current and historical step updates as a compact vertical timeline.
- Group repeated steps by `stage_id` when present, otherwise by arrival order.
- Display status badges: queued, running, success, warning, failed, skipped.
- Display attempt number when present.
- Display duration when present, otherwise client-side elapsed time for the current running step.
- Show `repair_history` summary inline when a repair step appears.
- Show inspect verdict summary after completion when `inspect_report` exists.
- Hide itself when there are no steps and no active generation.

Recommended placement:

- In the right-side `analysis` tab above `InspectReportPanel`.
- Keep `RepairHistory` below it for detailed repair records.
- Keep `MultiStepProgress` for assembly-specific part progress until the timeline fully subsumes it in a later phase.

### Store Behavior

Update `frontend/src/stores/sessionStore.ts` so step history behaves like a run timeline:

- Preserve all step updates for the current run.
- Clear `stepHistory` only when a new run starts or when final result is accepted.
- When a `generation_result` arrives, append a synthetic `complete` or `failed` step if backend did not emit one.
- Do not lose `stepHistory` immediately on `setResult`; keep it visible with the final result until the next run starts.

This is important because users need to inspect what happened after completion.

### Safe User Actions

Add safe recovery actions without building true partial retry:

1. `Retry Prompt`
   - Available after a failed result when the latest user message exists.
   - Sends the same user prompt again through the existing WebSocket `user_message` path.
   - This may call the LLM again.

2. `Re-run Current Code`
   - Available when current code exists.
   - Sends code through the existing WebSocket `execute_code` path.
   - This does not call the LLM.

3. `Restore Version`
   - Already provided by Adaptation 5 through `VersionHistoryPanel`.
   - The timeline can link users to the version panel but should not duplicate restore logic.

Cancellation note:

- Keep the existing cancel behavior as an acknowledgement only.
- UI copy should not promise true cancellation until a future backend task manager exists.

### Documentation

Update `docs/case-study-adaptations/CAD-Agent-adaptation-report.md` with Adaptation 6:

- Source: CADAM + forgecad-public-kit
- Borrowed idea: visible process / run-inspect-repair-retry workflow
- Implementation scope: structured step updates, AgentRunTimeline, safe retry actions
- Verification commands and results
- Decision: recommended version B, without true backend cancellation

## Acceptance Criteria

- `StepUpdate` serializes new lifecycle fields with defaults.
- Existing step update messages still work without providing new fields.
- Frontend builds with the extended `StepUpdate` type.
- During generation, the analysis tab shows a live timeline of agent stages.
- After generation completes, the timeline remains visible with completion or failure state.
- Repair attempts are visible in the timeline and still available in `RepairHistory`.
- Failed runs offer safe retry actions: retry prompt and re-run current code where applicable.
- No UI copy claims true cancellation.
- Adaptation 6 is recorded in the comprehensive report.

## Testing Strategy

Backend tests:

- Schema test for `StepUpdate` defaults and serialization.
- WebSocket test proving enriched `step_update` payloads remain accepted by the client contract.
- Orchestrator-focused test for a repair path emitting attempt metadata if practical without real LLM/Docker.

Frontend validation:

- TypeScript build must pass.
- If component tests are not configured, rely on `npm.cmd run build` and manual browser validation.
- Manual browser checks should cover: fresh run, successful completion, execution error, repair history present, and retry button behavior.

## Risks And Mitigations

- Risk: Timeline duplicates `RepairHistory` and `InspectReportPanel`.
  - Mitigation: timeline shows high-level flow; existing panels remain detailed evidence views.
- Risk: Users may think retry is a true partial retry from the failed stage.
  - Mitigation: label actions as `Retry Prompt` and `Re-run Current Code`, not `Retry This Stage`.
- Risk: Step history can grow too large during long runs.
  - Mitigation: cap frontend run timeline to the latest run and optionally trim to a reasonable count such as 100 events.
- Risk: Backend timing metadata may be incomplete at first.
  - Mitigation: support optional timing fields and use client arrival timestamps as fallback.
