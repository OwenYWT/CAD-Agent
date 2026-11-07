# Live Execution CAD Evaluation Design

> Status: Draft for review
>
> Date: 2026-11-04
>
> Scope: Developer-only evaluation evidence collected from the real CAD execution chain.

## 1. Decision Summary

The existing v0 evaluator is an offline adapter. It reads a completed benchmark JSON report and evaluates the evidence already present in that report. That remains useful as a compatibility tool, but it is not the long-term evaluation path.

The next version will collect evaluation evidence during the real execution chain. A single evaluation session will follow a real user task or benchmark case from requirement intake through modeling, validation, candidate sealing, Change Set review, and revision commit. The session will produce a developer-only JSON report from events and authoritative evidence emitted by the chain itself.

The first runtime switch is process-scoped:

```text
disabled (default)
    ordinary application behavior; no evaluation session or report

observe
    real tasks emit evaluation events and produce local developer reports

experimental (reserved)
    future candidate ranking or bounded repair experiments; not implemented now
```

The observe mode is non-blocking. Evaluation code may record `error`, `unknown`, or `blocked`, but it may not change task, workflow, candidate, revision, Change Set, or user-facing acceptance behavior.

## 2. Goals

1. Record what actually happened in the production workflow, rather than reconstructing it from final logs.
2. Associate every engineering result with its task, workflow, candidate, revision, artifact, operation, and evidence identity where available.
3. Preserve intermediate modeling and validation states so that failures and repairs can be located in the timeline.
4. Use the same evidence producers for ordinary user tasks and benchmark cases.
5. Make the report opt-in for a local backend process and mandatory for benchmark runs.
6. Keep reports outside ordinary user documents, revision history, and frontend user workflows.
7. Keep the existing offline evaluator as a compatibility path until live reports are proven stable.

## 3. Non-Goals

This phase does not:

- change generation, repair, candidate ranking, or acceptance policy;
- make a CADTest failure block a normal task;
- expose developer evaluation details in the ordinary frontend;
- add a report database or report query API;
- rebuild the Podman or FreeCAD environment;
- implement candidate search, reward integration, or automatic repair selection;
- infer engineering proof from planner labels or missing evidence;
- replace the existing production validators.

## 4. Runtime Modes and Entry Points

### 4.1 Normal application

The default remains disabled:

```powershell
python -m app.run --port 8000
```

The normal path must also remain compatible with the current Uvicorn invocation where practical:

```powershell
python -m uvicorn app.main:app --port 8000
```

The custom launcher is responsible for parsing the evaluation option before starting Uvicorn. Uvicorn must not be passed an application-specific option that it cannot parse.

### 4.2 Developer observe mode

```powershell
python -m app.run --port 8000 --evaluation-mode observe
```

The process creates a live evaluation session for eligible real tasks. The report is written under:

```text
backend/benchmark/reports/live/live_<evaluation_run_id>.json
backend/benchmark/reports/live/live_<evaluation_run_id>.md
```

The report directory is already covered by the repository ignore rule for benchmark reports. Generated reports and artifacts must not be committed automatically.

### 4.3 Benchmark mode

The benchmark runner invokes the same production workflow with an explicit observe session and marks the source as `benchmark`. It must not implement a second modeling or validation path.

The benchmark result may contain both:

- the existing benchmark summary used by `compare.py`; and
- the live execution report containing the event timeline and evidence references.

### 4.4 Configuration boundary

The evaluation mode is selected at process startup and injected into application-owned services. Request payloads, frontend fields, planner output, and model output cannot enable evaluation or change its mode.

## 5. Runtime Architecture

The runtime is a one-way observation branch from the existing workflow:

```text
real workflow and activities
        |
        +--> existing business state transitions
        |
        +--> EvaluationSession event sink (observe mode only)
                         |
                         +--> evidence normalization
                         +--> CADTest execution against live evidence
                         +--> atomic developer report writer
```

The observer must not be called as a required activity from the Temporal workflow. Evaluation persistence belongs in activity-side or service-side boundaries that already own the corresponding authoritative result. If an observer write fails, the business activity returns its original result and the session records an evaluation error when possible.

## 6. EvaluationSession Contract

The session is an in-memory runtime object with a durable JSON report assembled at completion. It must have a stable identity and explicit lifecycle:

```text
created -> running -> finished
                    -> failed
                    -> blocked
                    -> cancelled
                    -> error
```

The session stores:

- `evaluation_run_id`;
- mode and source kind (`user_task` or `benchmark`);
- request, task, workflow, document, base revision, candidate revision, and Change Set IDs;
- original requirement and source provenance;
- obligation records and binding outcomes;
- operation records and attempt metadata;
- artifact references and SHA-256 values;
- validation and CADTest results;
- candidate, review, commit, reject, and rollback transitions;
- resource measurements that were actually observed;
- ordered timeline events;
- final report verdict and evaluator errors.

The contract must allow missing identifiers for early events, but later events must fill them when the business result supplies them. Missing identity is reported as incomplete evidence, not silently invented.

## 7. Live Event Contract

Each event contains:

```json
{
  "event_id": "uuid",
  "event_type": "geometry_validation_finished",
  "occurred_at": "UTC timestamp",
  "sequence": 12,
  "request_id": "uuid or null",
  "task_id": "uuid or null",
  "workflow_id": "string or null",
  "candidate_build_id": "uuid or null",
  "candidate_revision_id": "uuid or null",
  "change_set_id": "uuid or null",
  "status": "pass|fail|unknown|blocked|error|skipped",
  "evidence_ids": [],
  "payload": {}
}
```

The sequence is assigned by the session, not inferred from log order. Event payloads must contain only bounded, serializable, non-secret data. Provider secrets, database URLs, `.env` contents, and raw credentials are prohibited.

### 7.1 Required lifecycle events

The first live implementation supports these event types:

```text
session_started
requirement_parsed
obligations_bound
plan_created
operation_started
operation_finished
operation_failed
artifact_created
geometry_validation_finished
visual_validation_finished
dfm_validation_finished
candidate_sealed
change_set_created
change_set_reviewed
revision_committed
change_set_rejected
revision_rolled_back
session_finished
session_error
session_blocked
```

An event is emitted only when its real boundary executes. For example, a visual event cannot be emitted if visual validation was skipped or never reached; the report must then contain `skipped`, `unknown`, or `blocked` according to the actual reason.

## 8. Existing Code Integration Points

The implementation must reuse the current boundaries rather than create parallel business logic.

### 8.1 Agent workflow and activities

The primary integration path is the version-isolated `McadAgentWorkflowV2` and its registered activities:

- requirements and decomposition activities provide requirement and obligation evidence;
- plan activity provides the modeling plan;
- source, operations, repair, and execution activities provide operation and artifact transitions;
- `agent_v2.validate_geometry` provides geometry evidence;
- `agent_v2.render_visual` and `agent_v2.judge_visual` provide visual evidence;
- `agent_v2.validate_dfm` provides manufacturing evidence;
- `agent_v2.seal_candidate` provides candidate sealing evidence;
- existing Change Set and revision services provide review, commit, reject, and rollback transitions.

The observer must consume returned activity payloads and existing service results. It must not duplicate geometry validation, artifact hashing, Change Set creation, or revision commit logic.

### 8.2 Candidate and artifact ownership

Candidate and artifact identity must come from the existing candidate and artifact services. The evaluator may copy IDs, hashes, formats, sizes, and bounded summaries into the report, but it must never assign a replacement candidate or artifact identity.

### 8.3 Change Set and revision ownership

The existing Change Set service remains authoritative for pending review, acceptance, commit, reject, and rollback. The live report mirrors those transitions and records their evidence references. Evaluation status must not be used to bypass `can_review`, `can_commit`, base revision checks, or validation gates.

## 9. CADTest Execution in the Live Session

CADTests execute when their required evidence becomes available. They do not wait until the end to rediscover evidence.

Examples:

- artifact readability runs after the artifact is created and identified;
- bbox, valid solid, solid count, and watertight checks run after authoritative geometry validation;
- printable checks run after the production printability result exists;
- artifact hash integrity runs when the artifact manifest is sealed;
- commit and Change Set tests run after the corresponding service result exists.

Each test result retains:

- test ID and version;
- status and severity;
- evidence IDs;
- source artifact and hash;
- expected and observed values;
- stable failure code;
- operation or validation sequence;
- duration where measured.

Required tests with missing evidence remain `unknown` or `blocked`. A successful workflow result does not promote an absent test result to `pass`.

## 10. Report Finalization

The report writer creates a complete report atomically:

```text
session events and test results
    -> validate report contract
    -> write temporary UTF-8 JSON
    -> flush and replace final path
```

Finalization must occur for success, failure, cancellation, timeout, and evaluation error whenever enough identity exists to name the report. An incomplete report must declare its incomplete state and preserve the last event.

The report verdict is derived from live test results and lifecycle state:

```text
error       evaluator contract or serialization error
failed      required live test failed
blocked     required evidence or runtime dependency blocked execution
not_verified required property lacks authoritative evidence
verified    required live tests passed and lifecycle completed
```

The verdict is diagnostic and does not replace the production task or Change Set status.

## 11. Failure Isolation

The observer is best effort from the business path but strict in its own report:

- observer exceptions are caught at the integration boundary;
- business activity results are returned unchanged;
- no observer exception changes a Temporal retry decision;
- no observer exception changes a candidate or revision status;
- report failures are logged with `evaluation_run_id` and the owning task/workflow ID;
- if report persistence fails, the in-memory session retains an evaluator error until finalization is attempted again;
- the ordinary frontend receives no new evaluation failure state.

This isolation must be tested with injected writer and observer failures.

## 12. Phased Implementation

### Phase 1: Session lifecycle and launcher

Deliver:

- application-owned launcher with `--evaluation-mode`;
- mode injection with `disabled` default;
- `EvaluationSession` contract and local atomic writer;
- task/workflow start and terminal lifecycle events;
- no business behavior changes;
- deterministic tests for disabled side effects and observe report creation.

Acceptance:

- normal startup remains quiet when evaluation mode is disabled; observe-mode startup automatically records live reports for ordinary real tasks;
- observe startup creates no report for requests that do not reach a supported task boundary;
- observe writer failure does not fail the business request;
- reports contain no secrets or generated artifact bytes.

### Phase 2: Real agent activity evidence

Deliver:

- requirements, obligations, plan, operation, artifact, and geometry event adapters;
- event-to-CADTest execution at evidence availability;
- live report timeline with real identity propagation;
- deterministic fixtures using actual activity result contracts.

Acceptance:

- one real task produces a report with requirement, plan, operation, artifact, and geometry evidence;
- every required result is traceable to real evidence or explicitly marked unknown/blocked;
- no duplicate modeling or validation execution is introduced.

### Phase 3: Full candidate and version lifecycle

Deliver:

- visual, DFM, BOM, candidate seal, Change Set, accept, commit, reject, rollback, and restore transitions;
- reports for first generation, part modification, history restore, and post-restore modification;
- report linkage across base revision, candidate revision, and committed revision.

Acceptance:

- a complete generate -> modify -> review -> commit -> restore -> modify -> commit sequence has one coherent report timeline per task/session;
- candidate and commit statuses match the authoritative Change Set and revision services;
- report failures do not alter those statuses.

### Phase 4: Benchmark unification

Deliver:

- benchmark runner starts live observe sessions;
- benchmark and live report IDs are linked;
- existing compare semantics remain unchanged;
- offline `cad_eval` remains available only for compatibility and fixture analysis.

Acceptance:

- a P01 benchmark run produces a live report from the actual workflow;
- the report contains operation and validation evidence without parsing final logs;
- existing benchmark regression tests still pass.

### Phase 5: Intermediate repair and cost analysis

Reserved for later:

- repair gain;
- candidate ranking;
- complexity stratification;
- resource cost reporting;
- CI and nightly evaluation gates.

## 13. Test Strategy

### Contract tests

- session and event serialization is stable and UTF-8 clean;
- event sequence is monotonic;
- missing identities remain null or incomplete rather than fabricated;
- reports reject secret-shaped fields and invalid statuses;
- final verdict follows explicit status precedence.

### Unit tests

- mode resolution and launcher argument handling;
- event adapters for each real activity result shape;
- evidence-to-CADTest execution;
- artifact and revision identity propagation;
- terminal event generation;
- atomic report writing;
- observer failure isolation.

### Integration tests

- one deterministic workflow fixture emits a complete live report;
- geometry and artifact evidence are recorded at their real boundaries;
- Change Set review and commit events are linked to the same session;
- history restore and subsequent modification preserve separate task and revision identities;
- benchmark invocation uses the same adapters as a real user task.

### Regression tests

- ordinary generation, modification, restore, and commit behavior is unchanged when mode is disabled;
- existing benchmark and compare commands remain compatible;
- frontend has no ordinary evaluation panel or evaluation failure state;
- no `.env`, generated reports, artifacts, or provider secrets enter Git tracking.

## 14. Security and Data Handling

Reports may contain prompts, operation summaries, internal IDs, artifact hashes, model names, runtime identity, and failure details. They must remain developer/CI artifacts. The writer must use an allowlist for provenance and payload fields, not serialize arbitrary settings or environment mappings.

The report may include a source prompt only when the existing task contract already stores it. It must not copy provider credentials, database connection strings, object-store secrets, Temporal credentials, or full environment values.

## 15. Open Implementation Constraints

1. The first implementation should use local files and not add a database migration.
2. The observer should run in activity/service boundaries, not as an unregistered Temporal activity that can produce a new worker-poller dependency.
3. The evaluator must not call the model or FreeCAD a second time merely to produce a report.
4. The existing offline evaluator remains until live reports have passed the complete integration matrix.
5. No report field may imply that an unexecuted check passed.

## 16. Review Acceptance Criteria

The design is ready for implementation when the following are accepted:

1. The process-scoped `disabled|observe` launcher behavior is confirmed.
2. Live reports are developer-only local artifacts under the ignored benchmark report directory.
3. The real production workflow is the only source of modeling and validation execution.
4. Evaluation failures cannot change ordinary business state.
5. Phase 1 and Phase 2 are implemented before any candidate-ranking or repair-search work.
6. The existing offline v0 evaluator remains a compatibility tool, not the target architecture.
