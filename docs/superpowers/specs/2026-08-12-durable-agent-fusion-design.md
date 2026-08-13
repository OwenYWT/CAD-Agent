# Durable Agent Fusion Design

## Status

Approved in conversation on 2026-08-12. Durable orchestration remains the only
production write path. The proven intelligent modeling flow is migrated into
that path; the legacy in-process path is not retained as a second product
implementation.

## Goal

Combine the legacy Agent's planning, complex-part decomposition, assembly
generation, bounded repair, geometry validation, visual validation, and DFM
with the durable control plane's persistence, isolation, revisions, immutable
artifacts, cancellation, replay, tenancy, and auditability.

## Principles

- Temporal owns workflow lifecycle; PostgreSQL owns product state and events.
- Agent reasoning and CAD execution are separate, observable steps.
- Every physical CAD run uses `ExecutionBackend` and a new
  `ExecutionAttempt`; business code never invokes a container directly.
- Retries are bounded by classified failure type. Infrastructure and provider
  failures never trigger CAD-code repair.
- A completed artifact is not an engineering success until the required
  validation gates pass.
- Existing public REST, WebSocket, auth, project, revision, and Change Set
  contracts remain compatible.

## Unified flow

```text
request
  -> requirements / design brief
  -> AgentPlan
  -> optional pre-execution confirmation
  -> candidate revision + Change Set
  -> simple | complex | assembly modeling
  -> isolated execution
  -> staged outputs + integrity verification
  -> geometry validation
  -> bounded repair / strategy fallback when eligible
  -> visual validation
  -> DFM validation
  -> atomically seal accepted artifacts + validation evidence
  -> review and commit
```

## Functional scope

### Planning

The durable workflow persists a versioned `AgentPlan` containing the objective,
design brief, model kind, modeling strategy, ordered modeling steps, expected
outputs, validation plan, predicted affected objects, and confirmation policy.
Planner, decomposition, and code generation are separate `StepRun` records.

Simple known shapes use one modeling step and avoid a second LLM planning call.
Complex/custom shapes may use `PlanDecomposer`. Assemblies use
`AssemblyPlanner` and one durable part step per component.

### Confirmation

Ambiguous requirements pause before code generation. Modifications and other
high-impact plans may pause after planning and before execution. Confirmation
is a durable Temporal signal; browser disconnects and service restarts do not
lose it.

### Modeling and execution

Each modeling step produces source code and runs through `ExecutionBackend`.
Each retry creates a new immutable `ExecutionAttempt`. Successful earlier
steps are replayable and are not regenerated after worker restart.

Before an Attempt lease ends, the control plane verifies its staged output
declarations, size, SHA-256, tenant/project/candidate ownership, and active
lease fence, then persists an immutable accepted staging manifest. This record
does not create a product Artifact or make the candidate reviewable. It is the
only input the later candidate seal may consume, so a multi-step or assembly
seal never depends on an expired worker lease.

Assembly part generation may run concurrently within a configured limit. Each
part has its own source, attempt, artifacts, status, and evidence before the
final assembly step runs.

### Repair and fallback

Static-analysis, user-code, CAD-kernel, geometry, and visual failures may
request a targeted repair. Retry budgets are defined per failure category and
the same failure signature cannot oscillate indefinitely. Provider,
authentication, quota, cancellation, lease, database, object-store, and worker
failures never invoke code repair.

An alternative modeling strategy is attempted only when classification marks
the failure repairable and its budget remains. Every repair records the prior
code hash, new code hash, reason, model/provider, and outcome.

### Validation

Required gates are ordered:

1. artifact integrity: declared output, size, and SHA-256;
2. geometry: loadable solid/profile, topology, expected dimensions, volume,
   watertightness where applicable, and configured build-volume constraints;
3. visual: rendered views compared with the design brief; an unavailable or
   malformed vision response is `indeterminate`, never `passed`;
4. DFM: process/material-aware deterministic and existing DFM analysis.

Geometry failure blocks completion and may trigger bounded repair. Visual and
DFM policy is explicit in `AgentPlan`: any `failed` or `indeterminate` result
blocks a required gate. Advisory failures and indeterminate results do not
block engineering completion but remain visible as risks in the Change Set.

### Result and review

Planning allocates a candidate-revision identity before physical execution, but
that candidate remains `building`: it is not reviewable, publishable, or a
branch head and has no accepted artifact/evidence associations. Its identity,
base revision, and intent manifest are immutable.

Every attempt writes only staging objects. Integrity acceptance is fenced by
that Attempt's active lease and produces the immutable staging manifest defined
above. Geometry, visual, and DFM validation operate on accepted staged outputs
and persist evidence that references their manifest identities. Failed repair
outputs are never selected for sealing.

Once all required gates pass and advisory evidence is complete, one seal
transaction verifies that every selected manifest belongs to the same tenant,
project, workflow, and building candidate; that no manifest was superseded or
previously consumed; and that the stored object hashes still match. It promotes
the selected objects, creates immutable Artifact rows, persists the selected
validation evidence, marks the manifests consumed, and moves the candidate
Change Set to reviewable state. Sealing is idempotent and can happen only once;
any later design change creates a new candidate revision. Unselected staging
objects remain unattached and normal orphan cleanup removes them.

Candidate states are explicit: `building` may become `reviewable`, `failed`,
`cancelled`, or `abandoned`; only `reviewable` enters the existing review and
commit states. Workflow failure or exhausted repair moves it to `failed`, user
cancellation to `cancelled`, and supersession or administrative cleanup to
`abandoned`. These terminal candidates remain queryable as audit evidence,
cannot advance a branch, and their unconsumed staging manifests are eligible
for orphan cleanup. Replaying a terminal workflow returns the stored terminal
candidate instead of creating another one.

The Change Set shows objective, plan, actual steps, repairs, artifact changes,
validation results, risks, and Agent log. Branch advancement still requires
existing review and compare-and-swap rules.

### Events and UI

Persisted task events expose planning, decomposition, source generation,
execution, validation, repair, confirmation, and finalization. The frontend
derives progress exclusively from snapshots/events and can reconnect by
sequence cursor. No simulated progress is introduced.

## Compatibility and cutover

- `/api/generate`, `/api/modify`, `/api/execute`, conversational WebSocket,
  task APIs, revisions, files, and Change Sets retain their external shapes.
- The fused flow is a new Temporal workflow type/version. Existing V1 histories
  continue on V1 workers and are never replayed by the new definition. New
  submissions switch atomically to V2 only after its worker on a dedicated V2
  Task Queue is ready; V1 stays registered until all V1 runs are terminal.
- Production has one Durable write path and never falls back to process-local
  orchestration; there is no runtime cutover switch.
- Legacy Agent classes may be reused behind focused adapters during migration,
  but they cannot own task state, write product artifacts, or execute CAD
  directly.
- The legacy write path is removed after parity and regression gates pass.

## Validation execution boundary

Geometry loading, rendering, and deterministic DFM are physical CAD/file
operations. Each runs as its own capability, StepRun, and ExecutionAttempt
through `ExecutionBackend`, consuming accepted staging manifests as declared
inputs. Only the external vision-model judgment runs as a provider Activity;
it consumes isolated renderer outputs and records provider/model provenance.
Temporal Worker and FastAPI processes do not load untrusted CAD geometry.

Before isolated DFM execution, the control plane resolves tenant rules and
material/process knowledge into an immutable, versioned policy snapshot with a
canonical SHA-256. The snapshot is a declared `ExecutionSpec` input and its
identity/hash is stored in validation evidence. The sandbox receives no
database credentials and never reads product state directly.

## Crash-safe candidate seal

Object storage and PostgreSQL are coordinated as an idempotent saga rather than
claimed to be one distributed transaction. A persisted seal identity selects
immutable manifest/evidence IDs. Final object keys are deterministic and
content-addressed. Copy-if-absent is followed by size/hash verification; after
all final objects exist, one PostgreSQL transaction creates Artifact rows,
marks manifests consumed, stores evidence, and makes the Change Set reviewable.
Retries safely resume any phase. Reconciliation removes unreferenced final
objects and stale staging objects, while DB-committed objects are never deleted.

## Acceptance criteria

- Simple, complex, assembly, modification, repair, geometry, visual, and DFM
  scenarios complete through real Temporal, PostgreSQL, object storage, and
  `ExecutionBackend` paths.
- Restart, disconnect, retry, cancellation, duplicate submission, stale base,
  and corrupted artifact cases produce truthful durable states without
  duplicate revisions or artifacts.
- No production Agent/modeling execution or its artifact/revision mutation
  bypasses WorkflowRun/StepRun/ExecutionAttempt. Authentication, project
  administration, and Change Set review retain their existing transactional
  service paths.
- No failed or indeterminate required validation is reported as engineering
  success.
- Existing API and frontend regression suites pass, followed by the real MCAD
  release gate.
- Stage acceptance includes controlled real provider calls for planning/code
  generation, one repair, and one visual judgment. Unit-test fakes do not count
  as release evidence; provider/model and response provenance must be recorded.

## Out of scope

- Kubernetes and private-worker execution backends.
- New ECAD workflows.
- Redesigning the current product UI.
- Unbounded autonomous tool use or destructive connector actions without
  confirmation.
