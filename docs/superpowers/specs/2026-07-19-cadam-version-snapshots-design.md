# CADAM-Style Version Snapshots Design

## Background

CAD-Agent is being improved through traceable adaptations from CADAM and forgecad-public-kit. The next CADAM-inspired improvement is versioned model history: users should be able to recover a previous successful CAD result after later prompts or parameter edits make the model worse.

## Source Attribution

- Source project: `CADAM`
- Borrowed idea: conversations are durable creative sessions, and each important model-producing interaction is recoverable through conversation/message state rather than being overwritten by the latest result.
- Local references studied:
  - `../CADAM/src/contexts/ConversationContext.tsx`
  - `../CADAM/src/services/messageService.ts`
  - `../CADAM/src/services/conversationService.ts`
  - `../CADAM/src/components/Sidebar.tsx`
  - `../CADAM/src/components/history/ConversationCard.tsx`

## CADAM Pattern To Adapt

CADAM uses a conversation-centric model:

- A conversation owns durable state and metadata.
- Messages are stored as persistent records.
- Message parentage can represent a branch or continuation path.
- A current leaf pointer identifies the active conversation state.
- The UI exposes recent creations so users can return to prior work.

CAD-Agent should not copy CADAM's full branching system yet. The practical adaptation is to introduce explicit model snapshots inside each CAD-Agent panel.

## Current CAD-Agent State

CAD-Agent already has a useful foundation:

- `backend/app/storage/history.py` stores sessions, panels, messages, and `current_code`.
- `backend/app/api/history.py` exposes sessions, panels, messages, and session deletion.
- `backend/app/api/websocket.py` saves user and assistant messages, updates panel code, and restores backend context.
- `frontend/src/stores/sessionStore.ts` keeps active panels, messages, generation results, and current code.
- `frontend/src/components/HistorySidebar.tsx` can restore a whole historical session.

Current limitations:

- Only the latest panel code is stored as `current_code`.
- Successful generations are buried inside assistant message results.
- Parameter re-execution can overwrite the active state without creating an obvious checkpoint.
- Users cannot list model versions for one panel.
- Users cannot restore a known good printable model with one click.
- There is no structured version status such as printable, warning, failed, or repaired.

## Goal

Add CADAM-style model version snapshots to CAD-Agent.

A snapshot is a durable checkpoint for a panel-level CAD result. It stores enough data to restore a previous generated model without asking the LLM again.

The feature should let users:

- See previous successful model versions in the current panel.
- Identify whether each version was printable, warning, or failed according to `inspect_report`.
- Restore a selected version's code and result into the active panel.
- Keep parameter-edit results as separate versions when execution succeeds.
- Preserve a simple parent link for future branching without building branch UI now.

## Non-Goals

- Do not implement a full branching conversation tree UI.
- Do not implement side-by-side geometry diff or visual comparison.
- Do not delete old versions automatically.
- Do not create a separate project/workspace concept.
- Do not rerun the LLM when restoring a snapshot.
- Do not change the existing sessions/panels/messages API contract in a breaking way.

## Proposed Scope: B, Recommended Version

### Backend Storage

Add a `model_snapshots` table managed by `backend/app/storage/history.py`.

Suggested columns:

- `id TEXT PRIMARY KEY`
- `panel_id TEXT NOT NULL REFERENCES panels(id) ON DELETE CASCADE`
- `parent_snapshot_id TEXT REFERENCES model_snapshots(id) ON DELETE SET NULL`
- `version INTEGER NOT NULL`
- `source TEXT NOT NULL` with values such as `generation`, `execute_code`, `parameter_edit`, `modify_part`
- `prompt TEXT NOT NULL DEFAULT ''`
- `code TEXT NOT NULL`
- `result TEXT NOT NULL` as JSON payload matching `GenerationResult` or `GenerateResponse`
- `files TEXT NOT NULL DEFAULT '{}'` as JSON
- `params TEXT`
- `parameters TEXT`
- `validation TEXT`
- `inspect_report TEXT`
- `repair_history TEXT`
- `status TEXT NOT NULL` with values `pass`, `warn`, `fail`, or `unknown`
- `created_at TEXT NOT NULL`

Add indexes:

- `idx_model_snapshots_panel_version ON model_snapshots(panel_id, version)`
- `idx_model_snapshots_panel_created ON model_snapshots(panel_id, created_at)`

### Backend Storage Functions

Add focused functions in `backend/app/storage/history.py`:

- `create_model_snapshot(panel_id, result, source, prompt='', parent_snapshot_id=None) -> dict`
- `list_model_snapshots(panel_id) -> list[dict]`
- `get_model_snapshot(snapshot_id) -> dict | None`
- `restore_model_snapshot(snapshot_id, user_id=None) -> dict`

Behavior:

- Compute `version` as one greater than the current max version for the panel.
- Compute `status` from `result.inspect_report.verdict` when present.
- Fall back to `fail` if `result.success` is false.
- Fall back to `unknown` if no inspect report exists.
- Store full result JSON so the frontend can restore analysis, downloads, parameters, and repair history.
- On restore, update `panels.current_code` and `panels.current_params` from the snapshot.

### Backend API

Extend `backend/app/api/history.py` with new endpoints:

- `GET /api/history/panels/{panel_id}/snapshots`
- `GET /api/history/snapshots/{snapshot_id}`
- `POST /api/history/snapshots/{snapshot_id}/restore`

Security:

- Reuse `panel_belongs_to_user` or add `snapshot_belongs_to_user`.
- Return 404 for unauthorized snapshots to avoid leaking existence.

### WebSocket Integration

In `backend/app/api/websocket.py`, create snapshots after successful result-producing operations:

- `user_message` when assistant generation succeeds and has code.
- `modify_part` when modification succeeds and has code.
- `execute_code` when manual execution succeeds and has code.

The snapshot source should be:

- `generation` for ordinary generation.
- `modify_part` for assembly part modification.
- `execute_code` for manual code or parameter-panel re-execution.

The response payload may include `snapshot_id` and `version` after creation, but older clients should still work without them.

### Frontend Types

Extend `frontend/src/types/index.ts` with:

```ts
export interface ModelSnapshotSummary {
  id: string;
  panel_id: string;
  parent_snapshot_id?: string | null;
  version: number;
  source: "generation" | "execute_code" | "parameter_edit" | "modify_part" | string;
  prompt: string;
  status: "pass" | "warn" | "fail" | "unknown";
  created_at: string;
  inspect_verdict?: "pass" | "warn" | "fail" | null;
  available_exports?: string[];
}

export interface ModelSnapshotDetail extends ModelSnapshotSummary {
  code: string;
  result: GenerationResult;
}
```

Optionally extend `GenerationResult` with:

```ts
snapshot_id?: string;
version?: number;
```

### Frontend UI

Add a compact version panel:

- File: `frontend/src/components/VersionHistoryPanel.tsx`
- Location: right-side `analysis` tab or a new `versions` tab if the existing analysis tab becomes crowded.
- Recommended initial placement: analysis tab below `InspectReportPanel` and above detailed design analysis.

Panel behavior:

- Fetch snapshots for the active panel when the analysis tab opens or when a new successful result arrives.
- Display version number, status badge, source, timestamp, and exports.
- Highlight the current active snapshot if `result.snapshot_id` matches.
- `Restore` button calls `POST /api/history/snapshots/{snapshot_id}/restore`.
- After restore, update local panel messages/current result/current code through the session store.
- Send existing `restore_context` WebSocket message with restored code so backend conversation context is aligned.

### Session Store

Extend `frontend/src/stores/sessionStore.ts` minimally:

- Add `restorePanelResult(panelId, result, code)` or reuse existing hydration logic if suitable.
- Preserve messages rather than replacing the whole session.
- Set the active panel's latest assistant result to the restored snapshot result so viewer, parameters, downloads, inspect report, and code tabs update together.

## Snapshot Creation Rules

Create a snapshot only when:

- `success` is true.
- `code` is non-empty.
- The operation produced or preserved a meaningful model result.

Do not create snapshots for:

- Validation failures before execution.
- LLM planning failures.
- Canceled operations.
- Empty code execution.

Status mapping:

- `inspect_report.verdict == "pass"` -> `pass`
- `inspect_report.verdict == "warn"` -> `warn`
- `inspect_report.verdict == "fail"` -> `fail`
- No inspect report but success true -> `unknown`
- success false -> `fail`

## Acceptance Criteria

- Each successful generation creates a versioned snapshot for the active panel.
- Each successful manual execution creates a versioned snapshot for the active panel.
- Snapshot version numbers increase monotonically per panel.
- Users can list snapshots for the active panel.
- Users can restore a snapshot without an LLM call.
- Restored snapshot updates code, parameters, downloads, inspect report, and active frontend result.
- Existing session history still loads as before.
- Deleting a session cascades snapshots through panels.
- Adaptation 5 is recorded in `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`.

## Testing Strategy

Backend tests:

- Storage test for creating snapshots and version increments.
- Storage test for cascading delete through session/panel deletion.
- API test for listing snapshots with panel ownership checks.
- API test for restoring snapshot updates panel `current_code`.
- WebSocket test for snapshot creation after successful generation and execute_code.

Frontend validation:

- TypeScript build must pass.
- Component should handle empty snapshot list, loading, restore success, and restore failure states.
- If no component test harness exists, rely on `npm.cmd run build` for type safety and manual browser validation.

## Documentation

Update the ongoing report with:

- Adaptation 5 title: `CADAM-Style Version Snapshots`
- Source references from CADAM.
- Local implementation details.
- Verification commands and results.
- User decision: recommended version B.

## Risks And Mitigations

- Risk: Snapshot table stores large result JSON payloads.
  - Mitigation: store only panel-level successful results for now; add pruning later if needed.
- Risk: Restoring old file URLs may point to files deleted from storage.
  - Mitigation: restore code and result first; if files are missing, user can re-execute restored code.
- Risk: Confusion between chat history and model versions.
  - Mitigation: label UI as model versions and show source/status, not raw messages.
- Risk: Future branching needs a different data model.
  - Mitigation: include `parent_snapshot_id` now, but keep UI linear in this phase.
