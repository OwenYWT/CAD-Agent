# Live Execution CAD Evaluation Implementation Plan

> **Execution rule:** implement one task at a time, run its focused tests, and stop at each checkpoint if the business path or evidence contract is not proven.

**Goal:** Replace the current report-after-the-fact CAD evaluation path with a developer-only live evaluation session that records evidence while the real user or benchmark workflow executes.

**Architecture:** Add an application-owned evaluation runtime with `disabled|observe|experimental` modes, a session/event contract, an allowlisted local JSON writer, and adapters at existing workflow/service boundaries. The observer receives real activity and Change Set results, runs CADTests when authoritative evidence becomes available, and finalizes one report per session. It never owns modeling, validation, candidate, revision, or user-facing state transitions.

**Runtime constraint:** API and Temporal Worker run in separate processes. A Uvicorn-only flag cannot enable observation inside Worker activities. The implementation must provide one explicit mode source for API, Worker, and benchmark processes, with `disabled` as the default in every process. No report is considered live unless the process executing the relevant workflow/activity is in `observe` mode.

## Scope Guard

This plan implements the first live execution phases from the approved design:

- process-safe mode selection and launcher support;
- `EvaluationSession` and ordered live event contracts;
- local atomic developer report writing under the ignored benchmark report directory;
- failure isolation from business workflow behavior;
- live evidence adapters for the real Agent v2 activities;
- live candidate, Change Set, revision, restore, and modification linkage;
- benchmark reuse of the same production execution path;
- deterministic unit and integration coverage before live environment verification.

This plan does not implement candidate ranking, reward integration, automatic repair selection, report database storage, frontend evaluation UI, or a second CAD execution path.

## File Map

### Phase 1: runtime and contracts

Create:

- `backend/app/evaluation/__init__.py`: public evaluation runtime exports.
- `backend/app/evaluation/mode.py`: strict mode resolution shared by API, Worker, and benchmark processes.
- `backend/app/evaluation/contracts.py`: session, event, evidence, lifecycle, and report contracts.
- `backend/app/evaluation/session.py`: in-memory session lifecycle, identity propagation, event sequencing, and test dispatch.
- `backend/app/evaluation/writer.py`: allowlisted UTF-8 JSON report serialization and atomic local writes.
- `backend/app/run.py`: application-owned launcher that parses `--evaluation-mode` before importing the FastAPI app.
- `backend/app/workers/run.py`: Worker launcher that parses the same mode option before creating the Worker.

Modify:

- `backend/app/main.py`: construct application state from the resolved evaluation mode without changing ordinary routes or startup checks.
- `backend/app/workers/workflow_worker.py`: initialize the evaluation runtime for the Worker process using the same explicit mode source.
- `.gitignore`: only if needed to make the live report subdirectory rule explicit; do not unignore generated reports.
- `CAD-Agent-local-startup-runbook.md`: document matching API and Worker mode commands after implementation is verified.

Tests:

- `backend/tests/test_live_evaluation_contracts.py`
- `backend/tests/test_live_evaluation_mode.py`
- `backend/tests/test_live_evaluation_writer.py`
- `backend/tests/test_live_evaluation_session.py`
- `backend/tests/test_live_evaluation_launcher.py`

### Phase 2: real Agent v2 evidence

Create:

- `backend/app/evaluation/adapters.py`: allowlisted adapters for requirements, plans, operations, artifacts, geometry, visual, DFM, BOM, and candidate results.
- `backend/app/evaluation/integration.py`: best-effort emission helpers used by activity/service boundaries.

Modify additively:

- `backend/app/workflows/activities.py`: emit evaluation events around real Agent v2 activity results without changing activity return payloads.
- `backend/app/workflows/agent_v2.py`: propagate an evaluation session identity or event correlation fields through existing payloads only where the current workflow contract permits it; do not add an evaluator activity dependency.
- `backend/app/services/durable_submission.py`: create or attach session identity at real task admission when observe mode is active.
- `backend/app/services/event_relay.py`: expose no new ordinary-user evaluation data; only preserve correlation needed by developer report finalization if required by existing lifecycle results.

Tests:

- `backend/tests/test_live_evaluation_adapters.py`
- `backend/tests/test_live_evaluation_integration.py`
- extend the relevant Agent v2 activity contract tests with observe-disabled and observe-enabled assertions.

### Phase 3: candidate and revision lifecycle

Modify additively:

- `backend/app/services/artifact_commit.py`: emit artifact, candidate, and Change Set evidence from existing committed results.
- `backend/app/services/change_sets.py`: emit review, commit, reject, and rollback events after authoritative transitions succeed.
- `backend/app/services/durable_submission.py`, `backend/app/api/websocket.py`, `backend/app/api/history.py`, and the existing Change Set/revision services identified by the restore path: emit restore and revision identity transitions after authoritative transitions succeed.
- `backend/app/services/durable_submission.py`: finalize failed, cancelled, and successful sessions using actual task outcomes.

Tests:

- `backend/tests/test_live_evaluation_candidate_lifecycle.py`
- extend Change Set and revision integration tests to assert evaluation isolation and identity linkage.

### Phase 4: benchmark unification

Modify:

- `backend/benchmark/eval.py`: start an explicit live observe session and retain the report ID alongside the existing benchmark result.
- `backend/benchmark/README.md`: document live reports as the primary path and the old offline `cad_eval` command as compatibility-only.
- `backend/benchmark/cad_eval.py`: keep existing offline behavior; do not make it the live execution entry point.

Tests:

- `backend/tests/test_live_evaluation_benchmark.py`
- existing benchmark metric, compare, and evaluation tests.

## Task 1: Freeze the process mode contract

1. Write failing tests for strict `disabled`, `observe`, and reserved `experimental` resolution.
2. Test that missing mode resolves to `disabled` in a fresh environment.
3. Test that invalid values fail before application startup.
4. Test that API launcher and Worker launcher accept the same mode vocabulary.
5. Test that a mode supplied by a request payload or frontend field cannot override the process mode.
6. Implement the shared mode module.
7. Implement launcher parsing without importing `app.main` before arguments are applied.
8. Preserve direct `uvicorn app.main:app` compatibility with default disabled behavior.
9. Verify with focused tests and Python compile checks.

Expected checkpoint:

```text
python -m app.run --help
python -m app.workers.run --help
```

Both show the same mode options, and no existing application module is imported for help output or invalid arguments.

## Task 2: Implement the session, event, and report contracts

1. Write failing serialization tests for session identity, event sequence, status, evidence IDs, and terminal state.
2. Implement immutable or validation-backed contracts using the repository's established Python conventions.
3. Require an explicit `evaluation_run_id` and preserve nullable business IDs for early events.
4. Assign event sequence numbers inside the session, not from timestamps or logs.
5. Add terminal status precedence and incomplete-session representation.
6. Add allowlisted payload and provenance fields; reject or omit secret-shaped fields, `.env` content, credentials, and arbitrary settings.
7. Ensure all serialized text is UTF-8 and JSON-compatible.
8. Run focused contract tests.

Expected checkpoint:

```text
one deterministic session serializes with monotonic events and no fabricated IDs;
missing evidence remains missing or explicitly incomplete.
```

## Task 3: Add the atomic local writer and failure isolation

1. Write failing tests for successful atomic writes, temporary-file cleanup, UTF-8 output, and report path naming.
2. Write a failure-injection test where the writer raises while the business callback still returns its original result.
3. Implement `writer.py` under `backend/benchmark/reports/live/`.
4. Write only bounded report data and never artifact bytes.
5. Finalize reports for success, failure, cancellation, timeout, blocked, and evaluator error when a stable ID exists.
6. Ensure writer exceptions are captured in the session and do not become Temporal activity failures or retries.
7. Confirm generated reports remain ignored by Git.
8. Run focused writer and failure-isolation tests.

Expected checkpoint:

```text
disabled -> no session and no file;
observe -> one atomic report;
writer failure -> business result unchanged and evaluator error recorded.
```

## Task 4: Attach the session at real task admission

1. Trace the durable submission path and identify the authoritative task/workflow ID returned at admission.
2. Add a session only when the resolved process mode is `observe` or when benchmark explicitly requests live observation.
3. Emit `session_started` with the original requirement and safe request provenance.
4. Do not store the session in user document history or Change Set tables in this phase.
5. Ensure duplicate/idempotent submissions do not create duplicate sessions for the same evaluation identity.
6. Test normal generation, modification, restore, and benchmark admission with mode disabled.
7. Test observe admission with stable request/task/workflow correlation.
8. Verify the API response and ordinary frontend state are byte-for-byte contract-compatible where the existing tests require it.

Expected checkpoint:

```text
real task admission creates a developer session only in observe mode;
ordinary task state remains unchanged.
```

## Task 5: Connect Agent v2 activity evidence

1. Map the existing result contracts for requirements, decomposition, plan, source/operation generation, execution, geometry, visual, DFM, BOM, and seal activities.
2. Write adapter tests using actual result-shaped fixtures, not invented simplified payloads.
3. Emit `requirement_parsed` and `obligations_bound` only from actual requirement/obligation results.
4. Emit `plan_created`, `operation_started`, `operation_finished`, and `operation_failed` around existing activity boundaries.
5. Emit `artifact_created` only when the existing result supplies an artifact identity, path/object key, size, or hash that can be safely referenced.
6. Emit geometry, visual, DFM, and BOM events with the validator's actual outcome and evidence IDs.
7. Run CADTests as evidence becomes authoritative; do not rerun FreeCAD, the model, or a validator.
8. Keep all observer calls best effort and preserve the original activity result and retry behavior.
9. Test missing, partial, and failed activity results as `unknown`, `blocked`, `fail`, or `error` according to the contract.
10. Run focused Agent v2 tests and the live integration fixture.

Expected checkpoint:

```text
one deterministic Agent v2 fixture produces a timeline containing real requirement,
plan, operation, artifact, and geometry evidence without a second execution.
```

## Task 6: Connect candidate, Change Set, revision, and restore transitions

1. Identify the authoritative post-transition return values in artifact commit and Change Set services.
2. Emit `candidate_sealed` and `change_set_created` only after those existing operations succeed.
3. Emit review, accept, commit, reject, rollback, and restore events only after the authoritative service transition succeeds.
4. Link base revision, candidate revision, head revision, artifact IDs, and Change Set ID from existing results.
5. Finalize a session on all terminal workflow outcomes without converting business status to evaluation status.
6. Add tests for first generation, part modification, history restore, and post-restore modification.
7. Add failure-injection tests proving report failure cannot make a successful Change Set commit fail.
8. Run existing Change Set and revision integration tests plus the live lifecycle tests.

Expected checkpoint:

```text
generate -> modify -> review -> commit -> restore -> modify -> commit
```

produces coherent report linkage while business statuses remain authoritative.

## Task 7: Unify benchmark execution with live sessions

1. Add an explicit benchmark live-observe option or make live observation the benchmark default without changing ordinary application defaults.
2. Pass a benchmark source identity and case ID into the same task/workflow admission path.
3. Retain existing benchmark summary and compare output unchanged.
4. Add a link from the benchmark row to the live report ID and report path.
5. Keep `benchmark.cad_eval` available for compatibility fixtures and previously generated reports.
6. Run the deterministic benchmark fixture tests.
7. Run one real `P01` case only after local Podman, model, Worker, and API readiness are confirmed.

Expected checkpoint:

```text
P01 live report is produced from actual workflow events;
no final-log parsing is required to reconstruct operation or validation evidence.
```

## Task 8: End-to-end verification and documentation

1. Run the focused live evaluation suite.
2. Run existing Agent v2, Change Set, revision, benchmark, and frontend build checks.
3. Start API and Worker with matching `observe` mode commands and verify they both report the mode explicitly.
4. Run one simple real generation and inspect the report timeline.
5. Run one modification and one history restore followed by modification; inspect candidate/base/head linkage.
6. Verify that normal mode creates no live reports and has no frontend evaluation surface.
7. Scan report files for secrets, non-UTF-8 text, fabricated IDs, and generated artifact bytes.
8. Update the local startup runbook with the exact disabled and observe commands, including the requirement that API and Worker use the same mode.
9. Do not commit generated reports, `.env`, Podman state, or artifacts.

## Verification Commands

Run from `backend` unless noted:

```powershell
python -X utf8 -m pytest tests/test_live_evaluation_*.py -q
python -X utf8 -m pytest tests/test_cad_evaluation_*.py -q
python -X utf8 -m pytest tests/test_agent_v2_*.py tests/test_change_set_api.py tests/test_agent_change_set_evidence.py tests/test_task_retry.py -q
python -X utf8 -m compileall -q app benchmark tests
```

Build the frontend from `frontend`:

```powershell
npm.cmd run build
```

Normal startup:

```powershell
python -m app.run --port 8000 --evaluation-mode disabled
python -m app.workers.run --evaluation-mode disabled
```

Developer observation startup:

```powershell
python -m app.run --port 8000 --evaluation-mode observe
python -m app.workers.run --evaluation-mode observe
```

The exact Worker arguments may preserve the existing worker options; the implementation must add the mode without removing current queue, namespace, or connection configuration.

## Exit Criteria

The implementation is complete only when:

1. API and Worker mode selection is explicit, matched, and defaults to disabled independently.
2. Observe mode records real workflow events and authoritative evidence rather than parsing final logs.
3. One report is finalized for successful and unsuccessful terminal paths.
4. The report links real task, workflow, candidate, artifact, Change Set, and revision IDs where available.
5. Missing evidence remains unknown/blocked/skipped and is never promoted to pass.
6. Evaluation failures cannot change ordinary business state or Temporal retry behavior.
7. Benchmark uses the same execution and evidence adapters as real tasks.
8. Existing offline evaluator remains compatible until live integration is proven.
9. Focused tests, relevant regression tests, Python compilation, and frontend build pass.
10. No generated report, artifact, `.env`, secret, or Podman state is included in Git changes.
