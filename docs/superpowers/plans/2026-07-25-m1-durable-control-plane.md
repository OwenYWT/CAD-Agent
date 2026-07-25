# M1 Durable Control Plane Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` after M0 passes. Apply expand/backfill/verify/cutover/contract; never keep two active write paths.

**Goal:** Replace process-local task, snapshot, and file metadata with a tenant-safe PostgreSQL/Temporal/S3 control plane whose workflows, events, revisions, and artifacts survive browser and service restarts.

**Architecture:** FastAPI remains the public control plane. PostgreSQL is the product source of truth, Temporal owns durable workflow mechanics, and S3-compatible storage owns immutable bytes. Existing REST/WebSocket/frontend contracts remain compatible through adapters during a one-way cutover.

**Tech stack:** FastAPI, SQLAlchemy 2 async, asyncpg, Alembic, PostgreSQL, Temporal Python SDK, MinIO/S3, Pydantic, pytest.

---

### Task 1: Add real infrastructure and readiness

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/app/config.py`
- Modify: `docker-compose.yml`
- Add: `backend/app/db.py`
- Add: `backend/app/object_store.py`
- Add: `backend/app/temporal_client.py`
- Add: `backend/tests/test_infrastructure_readiness.py`

- [ ] Add failing configuration/readiness tests.
- [ ] Add PostgreSQL, MinIO, and Temporal services with health checks and non-placeholder local secrets supplied by environment.
- [ ] Implement fail-closed production configuration and detailed `/ready` dependency status.
- [ ] Start all services and prove real SQL, object put/get/delete, and Temporal workflow round trips.
- [ ] Commit: `build: add durable control-plane dependencies`.

### Task 2: Create projects, tenants, identity, policy, audit, and usage schema

**Files:**
- Add: `backend/alembic.ini`
- Add: `backend/alembic/env.py`
- Add: `backend/alembic/versions/0001_tenants_and_identity.py`
- Add: `backend/app/domain/identity.py`
- Add: `backend/app/domain/projects.py`
- Add: `backend/app/domain/audit.py`
- Add: `backend/app/domain/usage.py`
- Add: `backend/app/repositories/identity.py`
- Add: `backend/app/repositories/projects.py`
- Add: `backend/app/repositories/audit.py`
- Add: `backend/app/repositories/usage.py`
- Modify: `backend/app/api/auth.py`
- Modify: `backend/app/api/login.py`
- Add: `backend/tests/postgres/test_tenant_rls.py`

- [ ] Test user-personal tenant, API-key fingerprint service principal, local-dev anonymous tenant, and quarantine mappings.
- [ ] Test Project, membership/role policy, permission decisions, append-only audit records, usage meters, and billing dimensions without embedding payment-provider logic.
- [ ] Test runtime/worker roles cannot bypass RLS and cross-tenant IDs fail under pooled concurrent transactions.
- [ ] Add composite tenant foreign keys, `FORCE ROW LEVEL SECURITY`, non-owner roles, and transaction-level `SET LOCAL`.
- [ ] Migrate real local identities and reconcile ownership before enabling enforcement.
- [ ] Commit: `feat: establish tenant-safe control-plane ownership`.

### Task 3: Create workflow, step, attempt, event, and outbox schema

**Files:**
- Add: `backend/alembic/versions/0002_workflows_and_events.py`
- Add: `backend/app/domain/runs.py`
- Add: `backend/app/repositories/runs.py`
- Add: `backend/app/services/run_state.py`
- Add: `backend/tests/postgres/test_run_state_machine.py`
- Add: `backend/tests/postgres/test_event_outbox.py`

- [ ] Encode every approved legal transition and reject all others.
- [ ] Implement immutable attempts, unique idempotency payload hashes, lease token/generation fencing, heartbeats, cancellation, timeout, and replayed completion.
- [ ] Write state mutation, task event, and outbox row in one transaction with monotonic workflow sequence.
- [ ] Prove duplicate/stale completions cannot change state or attach artifacts.
- [ ] Commit: `feat: persist fenced workflow execution state`.

### Task 4: Create project revision, branch, and candidate Change Set schema

**Files:**
- Add: `backend/alembic/versions/0003_revisions_and_changes.py`
- Add: `backend/app/domain/revisions.py`
- Add: `backend/app/repositories/revisions.py`
- Add: `backend/tests/postgres/test_revision_cas.py`

- [ ] Test immutable revisions, candidate Change Sets, expected-base identity, and branch-head compare-and-swap primitives without accepting a candidate yet.
- [ ] Create an initial branch/revision and reject concurrent stale-base candidate creation without overwriting either result.
- [ ] Commit: `feat: add immutable project revision schema`.

### Task 5: Implement immutable two-phase artifact storage

**Files:**
- Add: `backend/alembic/versions/0004_artifacts.py`
- Add: `backend/app/domain/artifacts.py`
- Add: `backend/app/repositories/artifacts.py`
- Add: `backend/app/services/artifact_commit.py`
- Add: `backend/tests/postgres/test_artifact_commit.py`
- Add: `backend/tests/integration/test_minio_artifacts.py`

- [ ] Test staging upload authorization, declared size/hash verification, attempt ownership, exact Revision foreign key, commit transaction, replay, rejection, and orphan cleanup.
- [ ] Store immutable tenant/project/revision/attempt object keys; never use `current.step`.
- [ ] Use short-lived scoped signed URLs and keep permanent credentials out of workers.
- [ ] In one database transaction verify the active fence and staged objects, insert immutable Artifact rows bound to the candidate Revision, accept the Attempt, and append the event/outbox record.
- [ ] Prove partial upload, hash mismatch, object-store outage, stale lease, cancellation, and duplicate completion cannot produce success.
- [ ] Commit: `feat: commit immutable CAD artifacts atomically`.

### Task 6: Implement Change Set review and branch advancement

**Files:**
- Add: `backend/app/services/change_sets.py`
- Add: `backend/tests/postgres/test_change_sets.py`

- [ ] Test accept/reject/request-change/rollback/commit after real committed artifacts exist.
- [ ] Require successful validation evidence and committed artifacts before branch-head CAS.
- [ ] Prove stale base, rejected artifact, failed validation, duplicate accept, and concurrent accept cannot advance the branch incorrectly.
- [ ] Commit: `feat: review and commit verified CAD changes`.

### Task 7: Migrate every legacy product store

**Files:**
- Add: `backend/alembic/versions/0005_legacy_product_stores.py`
- Add: `backend/app/migrations/legacy_import.py`
- Add: `backend/tests/integration/test_legacy_import.py`
- Add: `backend/tests/postgres/test_legacy_store_rls.py`
- Add: `scripts/migrate_legacy_control_plane.py`
- Modify: `backend/app/storage/auth.py`
- Modify: `backend/app/storage/history.py`
- Modify: `backend/app/storage/file_ownership.py`
- Modify: `backend/app/dfm/rule_store.py`
- Modify: `backend/app/dfm/knowledge_graph.py`
- Modify: `backend/app/api/feedback.py`
- Modify: `backend/app/fusion360/runtime_store.py`
- Modify: `backend/app/fusion360/agent_store.py`
- Modify: `backend/app/fusion360/token_store.py`
- Modify: `backend/app/fusion360/artifacts.py`
- Modify: `backend/app/api/onshape.py`
- Modify: `backend/app/capabilities/artifacts.py`
- Add: `backend/app/repositories/dfm.py`
- Add: `backend/app/repositories/knowledge.py`
- Add: `backend/app/repositories/feedback.py`
- Add: `backend/app/repositories/connectors.py`
- Add: `backend/app/repositories/capability_artifacts.py`

- [ ] Build a per-store inventory for sessions/panels/messages/snapshots/files, DFM rules, knowledge graph, feedback, Fusion runtime/agent/token/audit/artifacts, Onshape links, and Capability artifacts.
- [ ] Add PostgreSQL schemas, tenant ownership, repositories, route/service cutover mapping, and RLS tests for each store. Secret token bytes remain encrypted and never enter logs or audit payloads.
- [ ] Test repeatable import, collision handling, ownerless quarantine, checksums, counts, referential integrity, and rollback before cutover.
- [ ] Convert snapshots to `ProjectRevision` ancestry only at M1; do not retain a second version model.
- [ ] Run the importer twice on a copy of real local data and compare deterministic reports.
- [ ] Upgrade schema, run backfill, enable RLS, and cut authentication plus each legacy route/service to a PostgreSQL compatibility repository; prove no SQLite writer remains after cutover.
- [ ] Commit: `feat: migrate legacy product data into the control plane`.

### Task 8: Add durable Temporal workflows and activities

**Files:**
- Add: `backend/app/workflows/temporal.py`
- Add: `backend/app/workflows/definitions.py`
- Add: `backend/app/workflows/activities.py`
- Add: `backend/app/workers/workflow_worker.py`
- Modify: `backend/app/execution/backend.py`
- Add: `backend/tests/integration/test_temporal_mcadd_workflow.py`

- [ ] Test plan → model → validate → confirmation → modify/export, retry policy, timers, cancellation, heartbeats, and crash recovery.
- [ ] Keep LLM calls and ExecutionBackend submissions in activities; keep workflow code deterministic.
- [ ] Make Temporal IDs derive from persisted WorkflowRun IDs and make activity retries idempotent.
- [ ] Kill/restart FastAPI and Temporal worker during real execution and prove recovery without duplicate commits.
- [ ] Commit: `feat: run MCAD tasks as durable Temporal workflows`.

### Task 9: Build durable event subscription and product APIs before cutover

**Files:**
- Add: `backend/app/api/tasks.py`
- Add: `backend/app/api/changes.py`
- Add: `backend/app/api/revisions.py`
- Add: `backend/app/services/event_relay.py`
- Modify: `backend/app/models/schemas.py`
- Modify: `backend/app/api/generate.py`
- Modify: `backend/app/api/execute.py`
- Modify: `backend/app/api/batch.py`
- Modify: `backend/app/api/websocket.py`
- Modify: `backend/app/main.py`
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/types/engineering.ts`
- Modify: `frontend/src/services/engineeringService.ts`
- Modify: `frontend/src/hooks/useWebSocket.ts`
- Modify: `frontend/src/stores/sessionStore.ts`
- Modify: `frontend/src/components/changes/ChangeSetDialog.tsx`
- Add: `backend/tests/integration/test_change_set_api.py`
- Add: `backend/tests/integration/test_websocket_replay.py`
- Add: `frontend/tests/change-set-flow.test.ts`
- Add: `frontend/tests/websocket-replay.test.ts`

- [ ] Add `project_id`, `branch_id`, `expected_base_revision_id`, and idempotency key to synchronous, batch, async, and WebSocket request schemas and route parsers behind a disabled-by-default durable-cutover flag; do not change execution behavior yet. Derive safe defaults only from authenticated project context.
- [ ] Persist project/branch/base/current revision identity in frontend session state and send it on every modifying request.
- [ ] Implement task snapshot/event cursor APIs; Change Set accept/reject/request-change/rollback/commit APIs; and Temporal confirmation signal endpoints.
- [ ] Test snapshot sequence, ordered replay, deduplication, reconnect cursor, slow client, authorization, confirmation, stale base, and retention boundary.
- [ ] Build the WebSocket persisted-event subscription bridge while the legacy route is still active, but do not enable dual writes.
- [ ] Wire the real Change Set UI and remove the M0 snapshot adapter only at cutover.
- [ ] Test an old client, a current client, and two concurrent stale/current clients through REST, batch, async, and WebSocket.
- [ ] Commit: `feat: expose durable tasks revisions and changes`.

### Task 10: Atomically cut REST and WebSocket to one durable path

**Files:**
- Modify: `backend/app/api/generate.py`
- Modify: `backend/app/api/execute.py`
- Modify: `backend/app/api/batch.py`
- Modify: `backend/app/api/analyze.py`
- Modify: `backend/app/api/history.py`
- Modify: `backend/app/api/files.py`
- Modify: `backend/app/api/websocket.py`
- Modify: `backend/app/main.py`
- Add: `backend/tests/integration/test_api_compatibility_matrix.py`

- [ ] Snapshot existing route schemas/status/error/auth behavior.
- [ ] In one feature-flagged deployment boundary, route every REST and WebSocket mutation to persisted WorkflowRun/Temporal and every stream to persisted events.
- [ ] Remove `_tasks`, WebSocket orchestration/history/snapshot writes, and old file/snapshot writers only after verified backfill and compatibility tests.
- [ ] Keep compatibility reads/adapters only where needed and add explicit deprecation evidence.
- [ ] Prove one and only one active write path with import/static and database integration tests.
- [ ] Use outbox relay plus optional PostgreSQL notification as a wake-up, never as storage.
- [ ] Disconnect/reconnect and restart gateway during a real task; prove complete ordered playback.
- [ ] Commit: `refactor: cut all public writes to durable workflows`.

### Task 11: Complete M1 cutover and regression gate

**Files:**
- Add: `docs/qa/mcad-m1-report.md`
- Modify: `.github/workflows/ci.yml`
- Modify: deployment/readiness documentation identified during release sync

- [ ] Run migrations/backfill/verification on a production-shaped data copy.
- [ ] Exercise authenticated generate/modify/check/export/version/change/history/file flows through FastAPI, Temporal, PostgreSQL, MinIO, Podman, and WebSocket.
- [ ] Test PostgreSQL/MinIO/Temporal/FastAPI/worker/network interruption, cancellation races, duplicate callbacks, stale leases, concurrent base revisions, and storage corruption.
- [ ] Run the full M0 release gate and all M1 tests; compare API compatibility snapshots.
- [ ] Delete old writers only after verified cutover; rerun from a clean database and from migrated data.
- [ ] Commit: `test: enforce the M1 durable control-plane gate`.
