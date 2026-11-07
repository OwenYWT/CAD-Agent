# CAD-Agent Review: 2026-11-04 8866b7c8 codex-history-restore-confirmation

## Conclusion

- Status: `PASS_WITH_LIMITATIONS`
- Reviewer agent: Codex (`/root`)
- Review ID: `codex-history-restore-confirmation`
- Reviewed at: 2026-11-04 Asia/Shanghai
- Review range: working tree changes against `HEAD` (`8866b7c8d4552f71190d9c24de99ad80171f465c`)
- Repository revision: `8866b7c8d4552f71190d9c24de99ad80171f465c`
- Working tree state: shared worktree contains unrelated user changes; review scope was restricted to the seven paths listed below.
- Business code modified by reviewer: `No`

## Change scope

- Change class: historical restore queue failure handling and durable-plan confirmation permission projection.
- User-described goal: prevent history restore from hanging behind an existing document operation and allow users with review permission to confirm a plan while viewing an immutable history revision.
- Changed paths:
  - `backend/app/workflows/document_queue.py`
  - `backend/tests/test_document_queue.py`
  - `frontend/src/adapters/taskState.ts`
  - `frontend/tests/task-state.test.ts`
  - `frontend/src/components/agent/AgentPanel.tsx`
  - `frontend/src/components/workspace/EngineeringWorkspace.tsx`
  - `frontend/tests/reference-workspace-content.regression-1.test.ts`
- Functional inventory domains: 02, 05, 06, 14, 15, 16, 23, 24.

## Implementation trace

| Changed symbol or contract | Implementation path | Consumers | Impact judgment |
| --- | --- | --- | --- |
| `wait_for_document` busy restore handling | `backend/app/workflows/document_queue.py` | V1/V2 workflows; `agent_v2.run` finally releases queued operation | Restore-only fast failure; ordinary generation/modification queue behavior remains unchanged. |
| `document_operation_in_progress` task error | `frontend/src/adapters/taskState.ts` | Task state card and activity indicator | Terminal failure title is readable and clears the running state. |
| Durable confirmation control | `AgentPanel` and `EngineeringWorkspace` | Server confirmation buttons and document capability projection | Uses `can_review` for UI enablement while preserving `can_edit` for mutations. |
| Confirmation authorization | `backend/app/api/tasks.py` | `POST /api/tasks/{workflow_run_id}/confirmation` | Server still enforces `Permission.REVIEW_CHANGE`; frontend does not grant authority. |

## Findings

### Encoding and fake implementation review

- Scanner result: 19 leads across 33 changed paths. No lead was in the seven in-scope paths; reported leads were unrelated benchmark code, plans, and prior QA documentation.
- Confirmed findings: none.
- False-positive leads dismissed with context: fixture/mock and placeholder wording belongs to unrelated benchmark/tests/docs and is not product-path behavior.

### Compatibility review

- Frontend/backend contract: `CloudDocument.can_review` is populated by `document_snapshot`; the confirmation route independently checks `REVIEW_CHANGE`.
- WebSocket/event contract: task reducer receives the durable terminal error code and maps it to a non-running failed state; existing confirmation replay tests remain green.
- Persistence/migration/RLS: no schema or persistence changes. Queue acquire/finish remains tenant- and principal-scoped by existing activities.
- Workflow/retry/cancellation: restore requests fail immediately after a `waiting` acquire result; `agent_v2.run` retains its existing `finally` release path. Other requests still poll and honor cancellation/timeout.
- Revision/candidate/draft/artifact identity: history view remains non-editable through `canModify={canEdit}`; only confirmation uses `can_review`.
- Permissions and external boundaries: no authorization bypass found. The fallback `can_review ?? can_edit` supports older document payloads but may disable review-only users until the capability is returned.

## Test evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Logic | `pytest -q backend/tests/test_document_queue.py backend/tests/test_agent_v2_worker.py` | `2 passed` | PASS | Covers fail-fast queue behavior and worker wiring. |
| Logic | `node --experimental-strip-types --test tests/task-state.test.ts tests/history-restore-commit-regression.test.ts tests/reference-workspace-content.regression-1.test.ts` | `31 passed` | PASS | Covers terminal task projection, restore replay, and confirmation permission wiring. |
| Contract | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | PASS | PASS | `24 domains, 424 referenced paths`. |
| Hygiene | `git diff --check` on seven scoped paths | PASS | PASS | No whitespace errors. |
| Runtime | Full deployed multi-user restore/confirmation flow | Not run | BLOCKED | Requires external service/test data; local unit and contract evidence passed. |

## Gaps and blocked prerequisites

- No full browser acceptance against a live PostgreSQL/Temporal/object-storage deployment was run in this review.
- Frontend tests emit expected Zustand storage-unavailable warnings in the Node test environment; all assertions pass.
- Older API payloads without `can_review` use the existing `can_edit` fallback. This is a compatibility limitation, not a server-side permission change.

## Knowledge update

- Review report: this file.
- Review memory: not added; no new reusable invariant beyond existing feature logic was established.
- Stable map/invariant update: none.
- Reason and evidence: current implementation, permission route, focused tests, feature-map validation, and risk scan were reviewed without modifying business code.
