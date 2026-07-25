# Commercial MCAD Execution Architecture Design

## Status

Approved by the user on 2026-07-25. This specification turns the approved
M0 → M1 → M2 architecture into implementation requirements. It does not claim
that M1 or M2 capabilities already exist.

## Goal

Turn the existing WordsWave/CAD-Agent application into a commercially viable
MCAD product whose generation, modification, validation, versioning, export,
progress, and recovery paths are real, persistent, isolated, auditable, and
replaceable across local, cloud, and enterprise execution environments.

The implementation must preserve the current REST APIs, WebSocket entry point,
authentication, session/panel history, file ownership, Fusion 360 integration,
Onshape integration, capability registry, frontend routes, and existing response
shapes. Compatibility adapters may translate the current public contracts into
the new internal domain model.

## Delivery strategy

The design is implemented as three independently testable milestones:

1. **M0 — complete local MCAD:** introduce stable execution contracts and a
   Podman backend, then make the existing MCAD journey work end to end.
2. **M1 — durable commercial control plane:** add PostgreSQL, S3-compatible
   immutable artifacts, Temporal workflows, revisions, tenant isolation, and
   replayable events.
3. **M2 — scaled and private execution:** add Kubernetes Jobs, private workers,
   production sandbox hardening, and evidence-based scaling.

Every milestone has a real-runtime acceptance gate. Fakes and mocks remain valid
for isolated unit tests but never count as milestone completion evidence.

M0 keeps the existing SQLite `model_snapshots` records as the compatibility
version mechanism used by the current UI. It does not introduce the commercial
Revision schema. M1 migrates those snapshots into immutable `ProjectRevision`
rows and changes the compatibility adapter to read and write the new model.
This keeps M0 independently usable without building two competing Revision
systems.

## Target architecture

```text
React / Three.js
        │ REST + replayable WebSocket
        ▼
FastAPI control plane
tenant / project / branch / revision / task / audit / metering
        │
        ├── PostgreSQL: product state and durable events
        ├── S3-compatible storage: immutable CAD artifacts
        └── Temporal: durable workflow coordination
                    │
                    ▼
            ExecutionBackend
        ┌───────────┼─────────────┐
        ▼           ▼             ▼
 PodmanBackend  KubernetesBackend  PrivateWorkerBackend
        │           │             │
        └───────────┴─────────────┘
                    │
          trusted Worker Supervisor
      lease / transfer / validation / callback
                    │
           untrusted MCAD Runtime
       CadQuery / build123d / OCP / DFM
```

Podman, Kubernetes Jobs, and private workers are execution carriers rather than
business task models. Business services describe execution intent and never
call Docker, Podman, Kubernetes, or a shell directly.

## Existing implementation to reuse

- `backend/app/sandbox/executor.py` already contains hardened Docker and Podman
  execution primitives.
- `backend/app/capabilities/artifacts.py` already confines paths and calculates
  streaming SHA-256 hashes.
- `backend/app/fusion360/policy.py` already contains canonical hashing patterns.
- `backend/app/fusion360/runtime_store.py` already demonstrates idempotency,
  leases, attempt counters, stale-result rejection, cancellation observation,
  and result persistence.
- `backend/app/fusion360/agent_store.py` and related APIs already validate
  uploaded artifact size and SHA-256.
- `backend/app/storage/history.py` is the migration source for sessions, panels,
  messages, and model snapshots.
- The frontend already centralizes most backend access in services, adapters,
  stores, and `useWebSocket`.

These semantics should be extracted or adapted into shared execution-domain
components. Fusion-specific public contracts and behavior must remain stable.

## Execution contracts

### ExecutionSpec

`ExecutionSpec` is immutable, persisted, versioned, and hashable. It contains no
secret, presigned URL, callback credential, or host path.

Required fields:

- `schema_version`
- `execution_attempt_id`
- `tenant_id`
- `capability` and `capability_version`
- input artifact identities, sizes, and SHA-256 values
- expected output kinds and size limits
- MCAD Runtime image digest and platform
- CPU, memory, disk, process, network, and wall-clock limits
- trace and source identifiers

The canonical `spec_hash` is computed from deterministic JSON. Business code
selects a fixed capability such as `mcad.generate`, `mcad.modify`,
`mcad.validate`, or `mcad.export`; it cannot inject arbitrary container flags or
shell commands.

Canonicalization uses RFC 8785 JSON Canonicalization Scheme after Pydantic has
materialized every declared default and validated every string as Unicode NFC.
Optional fields are present as JSON `null`; unset fields cannot silently
disappear. Spec fields use integers, booleans, strings, arrays, and objects.
Non-finite numbers are rejected, and values requiring decimal precision are
encoded as normalized decimal strings rather than binary floats. The
`spec_hash` is lowercase SHA-256 over the canonical UTF-8 bytes. The same
versioned conformance vectors must pass in Python and every private worker
implementation before protocol negotiation succeeds.

### ExecutionLeaseEnvelope

`ExecutionLeaseEnvelope` is short-lived and excluded from `spec_hash`.

It contains:

- `lease_token`
- monotonic `lease_generation`
- input download grants
- output upload grants
- callback authorization
- expiry and heartbeat deadlines

The envelope may be reissued without changing the Attempt identity.

### ExecutionResult

`ExecutionResult` is versioned and normalized. It records:

- attempt identity and terminal state
- output upload identities
- structured validations and metrics
- normalized error category and retryability evidence
- actual image digest, CPU architecture, Python/CadQuery/build123d/OCP versions
- start/end times and resource usage

The control plane, not the worker, decides whether a failure is retryable.

## Task and state model

### WorkflowRun

One complete user operation. It owns ordered `StepRun` records and is the unit
users query, cancel, audit, and resume.

```text
PENDING ─────────────────────────────────────────→ CANCELLED
PENDING → RUNNING → WAITING_CONFIRMATION → RUNNING
             │              └────────────→ CANCELLED
             ├──────────────→ CANCELLING → CANCELLED
             ├──────────────→ SUCCEEDED
             ├──────────────→ FAILED
             └──────────────→ INTERRUPTED → RECONCILING
                                                 ├→ SUCCEEDED
                                                 ├→ FAILED
                                                 └→ CANCELLED
```

Cancellation of `SUCCEEDED`, `FAILED`, or `CANCELLED` is an idempotent no-op
that returns the stored terminal state.

`INTERRUPTED` and `RECONCILING` are M0 compatibility states, not a promise of
durable workflow resumption. On startup M0 atomically marks non-terminal
process-owned runs `INTERRUPTED`, then moves each to `RECONCILING`. Reconciliation
may resolve to `SUCCEEDED` only when an already accepted terminal result and its
committed artifacts can be verified. An existing cancellation resolves to
`CANCELLED`; every other interrupted run resolves to `FAILED` with
`process_restarted`. M1 Temporal recovery normally keeps runs in their logical
state and does not use these compatibility transitions.

### StepRun

A logical planning, modeling, validation, modification, or export step.

```text
PENDING ────────────→ CANCELLED
PENDING → READY ────→ CANCELLED
READY → RUNNING → SUCCEEDED
          ├─────→ FAILED
          ├─────→ CANCELLING → CANCELLED
          └─────→ WAITING_CONFIRMATION → READY
                         └──────────────→ CANCELLED
```

### ExecutionAttempt

One physical execution try for a Step. Retrying always creates a new Attempt;
previous rows are never reset or overwritten.

```text
QUEUED ───────────────→ CANCELLED / FAILED / TIMED_OUT
QUEUED → LEASED ──────→ FAILED / TIMED_OUT / LEASE_LOST
             ├────────→ CANCELLING → CANCELLED
             └→ RUNNING ───────────→ FAILED / TIMED_OUT / OOM / LEASE_LOST
                    ├───────────────→ CANCELLING → CANCELLED
                    └→ UPLOADING ───→ FAILED / TIMED_OUT / LEASE_LOST
                              ├─────→ ARTIFACT_REJECTED
                              ├─────→ CANCELLING → CANCELLED
                              └─────→ SUCCEEDED
```

Cancellation invalidates the active lease before requesting backend
termination. Uploads from cancelled Attempts remain uncommitted and are cleaned
as orphans. Any late completion or upload-finalization request is rejected by
the lease/fencing check.

Queue expiry produces `TIMED_OUT`; dispatch or runtime-launch failure produces
`FAILED`; heartbeat expiry produces `LEASE_LOST`; runtime deadline and memory
enforcement produce `TIMED_OUT` and `OOM`; upload deadline, object-store
unavailability, and verification failure produce `TIMED_OUT`, `FAILED`, and
`ARTIFACT_REJECTED`, respectively. The stored normalized error category records
the precise cause while the state remains one of the legal terminal values
above. A retry always creates a new `ExecutionAttempt`.

## Lease, fencing, and idempotency

- Claiming an Attempt uses an atomic compare-and-swap operation.
- Each claim receives a random lease token and monotonic lease generation.
- Heartbeat, control polling, upload authorization, and completion require the
  active token and generation.
- A stale completion returns `409 stale_lease` and cannot link artifacts or
  advance a revision.
- Replaying an already accepted completion returns the stored result.
- Submitting the same idempotency key with a different canonical payload returns
  a conflict.
- Kubernetes may start duplicate work; correctness must not depend on
  exactly-once execution.

## Artifact commit protocol

Artifacts use a two-phase protocol:

1. Worker reports kind, name, size, and SHA-256.
2. Control plane creates an `ArtifactUpload` tied to the active Attempt.
3. Worker receives a short-lived upload grant for one immutable object key.
4. Object storage validates the upload checksum where supported.
5. Control plane verifies existence, size, type, and SHA-256.
6. One PostgreSQL transaction validates the lease, accepts the result, creates
   immutable `Artifact` metadata, and links the exact Revision.
7. Uncommitted objects are quarantined and deleted by lifecycle cleanup.

Workers cannot choose canonical paths, overwrite committed objects, delete
objects, or directly advance a Revision. Product artifacts never use mutable
names such as `current.step`.

## Revision and Change Set semantics

- `ProjectRevision` is immutable.
- `ProjectBranch` owns a mutable `head_revision_id`.
- Generate and modify operations create candidate Revisions.
- A modification request includes `branch_id` and
  `expected_base_revision_id`.
- Accepting a Change Set advances the Branch Head with an atomic compare and
  swap. A mismatch returns `409 revision_conflict`.
- Validation and export results bind to an exact Revision, never an implicit
  latest version.
- Rejecting a Change Set retains its audit record without advancing the Head.
- Rollback creates a new Revert Revision instead of deleting history.
- Agent changes require explicit confirmation before Branch Head advancement.

## Events and WebSocket

PostgreSQL stores append-only `TaskEvent` rows with a monotonic sequence per
Workflow. State updates and event/outbox writes occur in one transaction.

The WebSocket gateway streams only persisted events. Clients:

1. fetch a REST snapshot;
2. subscribe from the snapshot sequence;
3. deduplicate by event ID;
4. reconnect using the last accepted sequence.

PostgreSQL `LISTEN/NOTIFY` may wake a relay but cannot be the event store.
Temporal History is not exposed as the product event API.

## Workflow boundary

M0 retains process-local orchestration behind a `WorkflowOrchestrator` boundary.
Browser refresh or WebSocket disconnect must not cancel the running operation,
but a FastAPI process restart may mark an in-flight M0 task as
`INTERRUPTED/RECONCILING`. M0 does not pretend to provide durable resumption.

M1 introduces Temporal after execution contracts, PostgreSQL, artifacts, and
revisions exist. Temporal owns retries, timers, cancellation propagation,
human-confirmation waits, and crash recovery. PostgreSQL remains the product
source of truth for users, permissions, projects, billing, revisions, audit, and
product-visible events. No interim custom workflow engine is permitted.

## Tenant isolation

- Migration/owner, runtime application, background worker, and audited operator
  roles are separate.
- Application and Temporal workers are non-owners without `BYPASSRLS`.
- Each business transaction runs `SET LOCAL app.tenant_id = ...`.
- Tenant tables use `FORCE ROW LEVEL SECURITY`.
- Missing tenant context denies access.
- Tenant-scoped foreign keys include `tenant_id` to prevent cross-tenant links.
- Cross-tenant operator actions use separate audited endpoints and credentials.
- Object grants are issued only after PostgreSQL authorization.

Legacy principals migrate as follows:

- A registered login user receives a personal Tenant and owner Membership on
  first M1 migration or login. Existing `user_id` values map through that
  Membership rather than becoming Tenant IDs directly.
- A configured legacy API key maps to a dedicated service principal and Tenant
  through a database record keyed by a SHA-256 credential fingerprint. Raw API
  keys are never persisted.
- `AUTH_REQUIRED=false` with no configured API keys is supported only outside
  production and maps to one explicit `local-dev` Tenant.
- Legacy rows with a `user_id` inherit that user's personal Tenant. Ownerless
  rows map to `local-dev` only in auth-disabled development; otherwise they are
  placed in an admin-only quarantine Tenant and remain hidden from normal
  history and file APIs until claimed.
- Migration reconciles session ownership, panel ownership, file ownership,
  Fusion/Onshape records, and request principals before RLS becomes enforcing.

## Worker and sandbox trust boundary

The trusted Worker Supervisor handles network communication, Lease state,
downloads, uploads, and result submission. Generated CAD code runs in a
separate least-privilege Runtime without credentials.

Local Podman requirements:

- `--network none`
- non-root user
- read-only root filesystem
- bounded writable temporary/output volumes
- all Linux capabilities dropped
- `no-new-privileges`
- CPU, memory, PIDs, file size, disk, and timeout limits
- no container socket or host paths

Kubernetes requirements:

- `automountServiceAccountToken: false`
- Restricted Pod Security posture
- seccomp and non-root enforcement
- no host namespace, hostPath, privileged mode, or Kubernetes API credential
- deny-all egress except a narrowly scoped Artifact Gateway where required
- trusted transfer sidecar/supervisor with attempt-scoped credentials
- gVisor only after CadQuery/OCP compatibility and performance tests

gVisor becomes the default only when the complete deterministic Runtime suite
passes without a semantic geometry, export, rendering, filesystem,
cancellation, or resource-limit regression, and its P95 runtime overhead is at
most 25% with peak memory overhead at most 20% against the same
node/image/workload. If it fails, M2 uses a dedicated hardened node pool with
the Restricted Pod Security profile, seccomp, deny-all egress, and the same
credential boundary; deployments report `sandbox_tier=restricted-runc` instead
of claiming gVisor isolation.

Private workers enroll with an organization-approved identity, declare
capabilities and Runtime digests, pull outbound, heartbeat, renew leases, obey
cancellation, and submit fenced idempotent results. Revoked or incompatible
workers receive no work.

## Runtime image supply chain

The MCAD Runtime is a versioned product artifact:

- CI builds an immutable OCI image.
- Dependency versions are locked.
- CI emits SBOM, provenance, signature, platform, and digest.
- A control-plane allowlist controls executable digests.
- The same tested digest is promoted to production without rebuilding.
- Runtime smoke tests perform real CadQuery/build123d/OCP operations, STEP/STL
  export and readback, geometry validation, rendering, and constrained
  execution.
- Execution records store the actual platform, digest, dependency versions,
  input hash, and generated-code hash.

Initial cloud production support targets `linux/amd64`. Local Apple Silicon
development must use a tested native image or an explicitly measured AMD64
emulation path.

## Error, retry, and cancellation semantics

Stable error categories include:

- invalid input
- policy denied
- capability unavailable
- infrastructure unavailable
- runtime failure
- timeout
- out of memory
- cancellation
- lease lost
- artifact invalid
- revision conflict
- internal error

Infrastructure, policy, authorization, stale lease, and revision conflict errors
never trigger LLM code-repair retries. Geometry/code failures may use bounded
repair policies. Cancellation is idempotent and late results cannot change
terminal state.

## Compatibility and migration

- Existing public REST routes and `/ws/{session_id}` remain available.
- Compatibility adapters translate existing session/panel requests into the new
  domain model.
- Business code stops calling container technology directly after M0.
- Only one write path is authoritative at a time.
- M1 uses expand/backfill/verify/cutover/contract migrations.
- The current SQLite and file store are migration sources, not long-term dual
  write targets.
- After cutover, rollback must preserve new writes; schema changes use
  backward-compatible expand/contract releases and forward fixes.

## Performance constraints

- EPHEMERAL_JOB is the default.
- Artifact hashing and transfer are streaming and size-limited.
- Event queries are indexed and cursor-paginated.
- Slow WebSocket clients have bounded buffers and reconnect from persisted
  sequence rather than accumulating unbounded memory.
- Per-tenant concurrency, queue depth, CPU time, artifact bytes, and LLM usage
  are measured.
- WARM_SESSION is deferred until cold start contributes more than 30% of P95
  modify latency or adds roughly three seconds persistently.
- PostgreSQL sharding and multi-region writes are deferred until measured load
  requires them.

## Milestone acceptance

### M0

The complete local MCAD flow must work against a real Podman Runtime:

```text
requirement → plan → model → STEP/STL → view → parameter modify
→ recompute → geometry/DFM → Change Set/version → export → history restore
```

The gate includes real file readback and geometry checks, frontend/backend
integration, failure cases, and full regression. MCAD must never display a
success state without backend artifacts. ECAD remains visible and truthfully
states that it is not connected.

M0 uses the existing versioned 50-case `backend/benchmark/eval_cases.py` suite
with three runs per case. Before changing execution semantics, the same Runtime,
model, temperature, RAG setting, and case-set hash produce the baseline. The M0
candidate must make `benchmark.compare` exit zero, contain no hard regression,
have no net broken cases, and not reduce aggregate pass@1 by more than one
baseline standard deviation. Every successful 3D case must include readable
STEP/STL, geometry evidence, and four rendered views; a false success fails the
gate regardless of aggregate score.

### M1

The gate uses real PostgreSQL, S3-compatible storage, and Temporal. It kills and
restarts FastAPI, Temporal workers, and MCAD workers; tests lease races,
duplicate callbacks, artifact corruption, revision conflicts, WebSocket replay,
tenant isolation, and migration reconciliation.

### M2

The gate uses real Kubernetes Jobs and a separately hosted private worker. It
tests duplicate Pods, node failure, cancellation, worker revocation, disconnect
recovery, sandbox escape controls, gVisor compatibility, autoscaling,
backpressure, and canary rollback.

The M2 capacity gate uses a checked-in workload manifest:

- 50 simultaneous EPHEMERAL_JOB submissions on the declared staging capacity:
  at least 95% leave `QUEUED` within 60 seconds, every request reaches a truthful
  terminal state, and no Step commits more than one accepted result.
- A burst of 500 workflow-create requests: no unexplained 5xx response, API P95
  below 500 ms, bounded queue/memory growth, and explicit `429` responses after
  the configured tenant quota.
- A 5% canary over at least 100 representative MCAD tasks: zero cross-tenant,
  authorization, artifact-integrity, or duplicate-Revision errors; terminal
  failure rate may not exceed the stable pool by more than one percentage point,
  and P95 execution latency may not regress by more than 25%.

The workload manifest, node types, quotas, image digest, raw measurements, and
pass/fail calculation are stored with release evidence. Merely running the load
test is not acceptance.

## Required regression gates

- Complete backend Pytest suite.
- Complete frontend lint, Node tests, TypeScript check, and Vite build.
- Existing REST, WebSocket, authentication, history, file ownership, DFM,
  capability, Fusion 360, and Onshape contract suites.
- Browser tests at desktop, tablet, and mobile widths.
- Real LLM/sandbox benchmark for representative MCAD prompts.
- No console errors, false success states, static placeholder results, or mock
  acceptance evidence.

## Explicitly out of scope

- Full ECAD implementation.
- WARM_SESSION before measurement justifies it.
- PostgreSQL sharding and multi-region active-active writes.
- Real-time concurrent editing inside one mutable geometry model.
- Moving Fusion, Onshape, step.parts, or future ECAD into the MCAD Runtime.
- A custom durable workflow engine.
- Kubernetes as a prerequisite for M0.
