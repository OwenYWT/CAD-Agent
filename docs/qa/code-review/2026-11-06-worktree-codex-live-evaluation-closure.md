# CAD-Agent Review: 2026-11-06 beb2da39f515 live-evaluation-closure

## Conclusion

- Status: `PASS_WITH_LIMITATIONS`
- Reviewer agent: Codex
- Review ID: `live-evaluation-closure`
- Reviewed at: 2026-11-06 Asia/Shanghai
- Review range: `HEAD` plus current worktree, focused on live model-operation observation and Change Set report closure
- Repository revision: `beb2da39f515`
- Working tree state: dirty; 55 changed or untracked paths were scanned and no cleanup or reset was performed
- Business code modified by reviewer: `No`

## Change scope

- Change class: developer-only evaluation observation and report lifecycle integration
- User-described goal: record real Agent v2 operation evidence and close live reports after authoritative Change Set transitions without affecting ordinary CAD behavior
- Changed paths: `backend/app/evaluation/integration.py`, `backend/app/workers/workflow_worker.py`, `backend/app/services/event_relay.py`, `backend/app/api/changes.py`, and focused tests
- Functional inventory domains: 05 Agent modeling collaboration; 06 Task state and recovery; 12 Inspection, validation, and evidence; 14 Candidate review and commit; 15 History, rollback, branches, and merge; 24 Reliability, deployment, and operations

## Implementation trace

| Changed symbol or contract | Implementation path | Consumers | Impact judgment |
| --- | --- | --- | --- |
| `observe_model_operation` | `backend/app/evaluation/integration.py` | real `ModelJobWorker.model_operations` registrations | Preserves result identity, original exceptions, cancellation, and durable operation names; observer failures are isolated. |
| V2 operation wrapping | `backend/app/workers/workflow_worker.py` | durable Agent v2 model-job worker | Wraps every existing `DURABLE_OPERATIONS` entry after the registration equality guard; no operation is added or removed. |
| `change_set_workflow_id` | `backend/app/services/event_relay.py` | Change Set review, commit, and rollback API paths | Returns authoritative correlation identity after permission checks; signal eligibility is no longer conflated with identity lookup. |
| `_signal_review` active-state allowlist | `backend/app/api/changes.py` | accept, reject, and request-change endpoints | Signals only pending, planning, running, or waiting-confirmation workflows; closed and cancelling workflows remain unsignaled. |
| `record_change_set_transition` | `backend/app/evaluation/integration.py` and `backend/app/api/changes.py` | accepted, committed, rejected, changes-requested, and rolled-back transitions | Mirrors successful service results best-effort; commit closes the report as pass, while other terminal review actions close it without fabricating validation success. |

## Findings

### Encoding and fake implementation review

- Scanner result: 62 heuristic leads across the dirty worktree; the feature map validated with 24 domains and 424 referenced paths.
- Confirmed findings: none in the reviewed phase.
- False-positive leads dismissed with context: broad exception isolation is required by the observe-mode non-interference contract; static success payloads occur only in tests; explicit UTF-8 reads show the Chinese test strings are valid and not mojibake; no model, FreeCAD, validator, candidate, or revision result is synthesized by the observer.

### Compatibility review

- Frontend/backend contract: no user-facing API response or frontend evaluation surface changed; frontend production build passed.
- WebSocket/event contract: no WebSocket event schema or task projection contract changed.
- Persistence/migration/RLS: no schema or migration changed; Change Set workflow identity still passes existing tenant and project permission checks.
- Workflow/retry/cancellation: operation registration is unchanged; return object identity, business exceptions, cancellation, and Temporal signal eligibility are covered by focused tests.
- Revision/candidate/draft/artifact identity: report identity is copied only from authoritative payloads or service results; missing evidence remains absent or unknown.
- Permissions and external boundaries: developer observation defaults to disabled; generated reports and `backend/.env` remain ignored by Git.

## Test evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Logic | focused live evaluation, CAD evaluation, Agent v2, durable API, Change Set evidence, and retry tests | `131 passed, 1 warning` | Passed | Warning is the existing Pydantic `model_kind` protected-namespace warning. |
| Service | `python -X utf8 -m pytest tests/postgres/test_model_jobs.py -q` | `13 skipped` | Not executed | Required PostgreSQL/service fixture conditions were not active; this is not a pass. |
| Static | risk scanner, feature-map validator, `git diff --check`, and `compileall` | Passed | Passed | Diff check emitted only line-ending conversion warnings. |
| Product | `npm.cmd run build` | Passed | Passed | Vite emitted existing Browserslist-age and chunk-size warnings. |
| Runtime | fresh Temporal, PostgreSQL, Podman, and frontend create/modify/restore flow after Worker restart | Not run | Limitation | Existing report files predate the new Worker wrapper and cannot prove this change. |

## Gaps and blocked prerequisites

- Restart the non-reloading Worker in `observe` mode before manual validation; the API reloader alone cannot load the new model-operation wrapper.
- Run a fresh create -> accept -> commit, modify -> accept -> commit, and restore -> accept -> commit sequence against real Temporal, PostgreSQL, Podman, and FreeCAD services.
- Confirm each new report contains real operation and validation events, authoritative revision identities, a final `session_finished` event, `status=finished`, and non-null `finished_at`.
- Concurrent same-report writes across API and Worker processes have sequential coverage but no stress test; treat this as residual report-integrity risk until a real flow and concurrency test are completed.

## Knowledge update

- Review report: `docs/qa/code-review/2026-11-06-worktree-codex-live-evaluation-closure.md`
- Review memory: none
- Stable map/invariant update: none
- Reason and evidence: the current implementation follows existing documented invariants; runtime acceptance remains pending rather than being promoted to stable knowledge.
