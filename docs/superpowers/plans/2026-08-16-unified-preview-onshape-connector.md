# Unified Preview and Durable Onshape Connector Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver one real mechanical workspace for native Durable Agent V2 projects, user-owned Onshape projects, and quota-limited guest projects, with no direct external write fallback or frontend-only parameter mutation.

**Architecture:** Keep `McadAgentWorkflowV2`, `ProjectRevision`, `ExecutionBackend`, PostgreSQL, S3-compatible artifact storage, Temporal, and task-event replay as the native production path. Add a separate durable `OnshapeMutationWorkflow` over the same run/attempt/event primitives, evolve the existing connector tables for OAuth ownership and external revisions, and project both sources through one discriminated frontend adapter. Every Onshape target write is previewed on a workflow-owned workspace, validated from an exact microversion artifact, confirmed, replayed against an unchanged target, and read back before it is called applied.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy async/PostgreSQL/RLS, Alembic, Temporal Python SDK, httpx, encrypted OAuth credentials, MinIO/S3, `ExecutionBackend`, CadQuery/OCP validation, React 19, TypeScript 6, Vite, Three.js, Node test runner, pytest, Docker/Podman.

---

## Non-negotiable boundaries

- Native generation and modification remain on `McadAgentWorkflowV2`; do not add a second native writer.
- Onshape mutations use a dedicated Temporal workflow; FastAPI routes, Agent tools, and UI components only submit typed intents.
- Shared `ONSHAPE_ACCESS_KEY` / `ONSHAPE_SECRET_KEY` credentials are development or migration-only and never a production user fallback.
- A preview is derived evidence. Native `ProjectRevision` or an Onshape microversion remains authoritative.
- The first Onshape editing release only updates existing quantity, boolean, and enum feature parameters.
- Do not create synthetic native revisions for external CAD state.
- Do not claim a lost provider response succeeded or failed until operation-specific reconciliation proves it.
- Unit fakes do not satisfy the live Onshape release gate.
- Keep the unrelated untracked `isearch-ai-pr-platform/` tree untouched.

## Release gates

1. **Gate A — Native parity:** Native registered projects still generate, preview, change typed parameters, validate, review, commit, export, cancel, restart, and replay through Durable V2.
2. **Gate B — Onshape read-only:** OAuth connect, account-scoped document selection, exact-microversion preview, and typed read-only parameter projection are live; mutations remain disabled.
3. **Gate C — Onshape editing beta:** Preview workspace, typed parameter update, exact artifact validation, confirmation, target replay, read-back, and reconciliation all pass against a dedicated real account.
4. **Gate D — Guest trial:** A production guest can run real Durable V2 under quota, then claim the same tenant/project identities during signup.

### Task 1: Freeze current native and connector behavior with characterization tests

**Files:**
- Create: `backend/tests/test_onshape_durable_cutover.py`
- Create: `backend/scripts/run_release_gate.py`
- Create: `backend/tests/test_release_gate_runner.py`
- Create: `frontend/tests/cad-source-contract.test.ts`
- Modify: `backend/tests/test_onshape_integration.py`
- Modify: `backend/tests/test_tools_onshape_plugin.py`
- Modify: `frontend/tests/mcad-real-flow.test.ts`

- [ ] Add a passing characterization inventory that names the four current direct-write entries: `POST /api/onshape/documents`, `POST /api/onshape/publish`, `onshape_create_document`, and `onshape_publish_step`. Task 10 will invert this assertion before implementing cutover.
- [ ] Add passing tests proving `OnshapeService.create_document()` and `publish_step()` are currently reachable from request handlers, so the later cutover removes an evidenced path rather than relying on code search.
- [ ] Add a passing frontend characterization proving `ParameterDrawer` currently depends on `patchCodeParameters`. Task 12 will invert this assertion before removing the client-side source rewrite.
- [ ] Extend native flow assertions to cover `workflow_run_id`, `project_id`, `branch_id`, `expected_base_revision_id`, durable event replay, and real artifact URLs.
- [ ] Add a reusable named release-gate runner that captures JUnit XML, requires a positive selected-test count, and exits nonzero on any failure, error, or skip. Unit-test the runner against pass, fail, empty selection, and skipped fixture suites without invoking external dependencies.
- [ ] Run `cd backend && python -m pytest tests/test_release_gate_runner.py tests/test_onshape_durable_cutover.py tests/test_onshape_integration.py tests/test_tools_onshape_plugin.py -q` and require a clean baseline pass.
- [ ] Run `cd frontend && node --test --experimental-strip-types tests/cad-source-contract.test.ts tests/mcad-real-flow.test.ts` and require a clean baseline pass.
- [ ] Commit `test(onshape): freeze connector cutover boundaries`.

### Task 2: Evolve durable run and connector persistence for external CAD state

**Files:**
- Create: `backend/alembic/versions/0011_durable_onshape_connector.py`
- Modify: `backend/app/domain/runs.py`
- Modify: `backend/app/services/run_state.py`
- Create: `backend/app/domain/external_changes.py`
- Create: `backend/app/repositories/connectors.py`
- Create: `backend/app/repositories/external_changes.py`
- Create: `backend/tests/postgres/test_onshape_connector_state.py`
- Modify: `backend/tests/postgres/test_run_state_machine.py`
- Modify: `backend/tests/postgres/test_legacy_store_rls.py`
- Modify: `backend/scripts/run_release_gate.py`

- [ ] Write PostgreSQL tests first for tenant isolation, account ownership/delegation, resource-link access, immutable external revisions/intents/evidence, legal state transitions, and forbidden native-revision references on external Change Sets.
- [ ] Extend `connector_tokens` with stable `account_id`, provider subject, verified issuer/API base, scopes, lifecycle state, credential generation, refresh lease generation/deadline, and encrypted refresh-journal fields. Preserve the existing `(tenant_id, principal_id, connector)` key for one user account per provider in release one, add a unique `(tenant_id, account_id)` target for foreign keys, and allow encrypted token material to be cleared while retaining disconnected account metadata.
- [ ] Add `connector_account_grants` keyed by tenant/account/principal; project membership alone must not authorize credentials.
- [ ] Extend `connector_links` with `account_id`, `resource_type`, last observed microversion, access mode, capability snapshot, active preview workspace, last synchronization time, and a unique `(tenant_id, id)` key. Preserve existing publish-link rows and response fields.
- [ ] Add tenant-scoped `external_revisions`, `external_change_sets`, `external_change_intents`, and `external_validation_evidence`. Store canonical before/after snapshot hashes, intent generation, expected/resulting microversions, preview workspace/microversion, typed parameter bindings, artifact/evidence links, risk, reconciliation status, and preview-workspace cleanup state/deadline/attempt count/retention reason.
- [ ] Add `execution_kind`, provider operation, provider request ID, and response classification to `execution_attempts`. Extend attempt state with `unknown_commit`, step state with `reconciliation_required`, and workflow state with `awaiting_reconciliation`. Permit only `running -> unknown_commit`, `running -> reconciliation_required`, `running -> awaiting_reconciliation`, and fenced reconciliation transitions back to `running`, `waiting_confirmation`, `succeeded`, or `failed`; unknown outcomes are never cancellable.
- [ ] Enable and force RLS on every new table, grant minimum rights to existing runtime/worker/migrator roles, and add append-only or immutable triggers for external revisions, intents, and validation evidence.
- [ ] Backfill existing connector-token and connector-link rows without changing their visible publishing behavior. The downgrade must refuse if new external workflow records exist instead of silently deleting evidence.
- [ ] Add a named `onshape-postgres` release suite selecting `tests/postgres/test_onshape_connector_state.py`, `tests/postgres/test_run_state_machine.py`, and `tests/postgres/test_legacy_store_rls.py`, requiring `CAD_AGENT_TEST_DATABASE_URL`. Run `cd backend && python scripts/run_release_gate.py onshape-postgres`; any skip exits nonzero.
- [ ] Run a clean `cd backend && DATABASE_URL=<empty-test-db-url> alembic upgrade head`, inspect all new constraints/policies, then run downgrade/upgrade on a schema containing only legacy connector rows.
- [ ] Commit `feat(onshape): persist external cad workflow state`.

### Task 3: Extract a provider-neutral encrypted OAuth account store without breaking Fusion

**Files:**
- Create: `backend/app/connectors/__init__.py`
- Create: `backend/app/connectors/errors.py`
- Create: `backend/app/connectors/crypto.py`
- Create: `backend/app/connectors/postgres_oauth_store.py`
- Modify: `backend/app/fusion360/postgres_token_store.py`
- Modify: `backend/app/fusion360/cloud.py`
- Create: `backend/tests/test_connector_oauth_store.py`
- Modify: `backend/tests/fusion360/test_cloud_security.py`
- Modify: `backend/tests/integration/test_postgres_fusion_connectors.py`
- Modify: `backend/scripts/run_release_gate.py`

- [ ] Test encrypted token storage, one-time expiring state, principal binding, issuer/host pinning, account grants, disconnect generation fencing, and absence of plaintext credentials in SQL rows, exceptions, logs, events, and audit payloads.
- [ ] Move Fernet JSON encoding and generic PostgreSQL state/token operations behind provider-neutral contracts. Keep `PostgresEncryptedTokenStore` as a compatibility wrapper so Fusion imports and API behavior do not change.
- [ ] Implement a per-account refresh lease and generation CAS. Persist the encrypted refresh journal before calling the provider; an expired uncertain journal transitions to `reconnect_required` and is never blindly retried.
- [ ] Require callers to supply the expected credential generation and recheck it immediately before dispatch. Disconnect increments the generation before best-effort provider revocation.
- [ ] Accept outbound API hosts only from deployment-owned provider configuration and verified account metadata. Resource URLs may contribute IDs but never a network host.
- [ ] Run `cd backend && python -m pytest tests/test_connector_oauth_store.py tests/fusion360/test_cloud_security.py -q`.
- [ ] Add a named `fusion-postgres` release suite selecting `tests/integration/test_postgres_fusion_connectors.py`, requiring `CAD_AGENT_TEST_DATABASE_URL` and object-store settings. Run `cd backend && python scripts/run_release_gate.py fusion-postgres`; any skip exits nonzero.
- [ ] Commit `refactor(connectors): share encrypted oauth storage`.

### Task 4: Add user-owned Onshape OAuth and account lifecycle

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/.env.example`
- Create: `backend/app/integrations/onshape/contracts.py`
- Create: `backend/app/integrations/onshape/oauth.py`
- Modify: `backend/app/integrations/onshape/client.py`
- Modify: `backend/app/integrations/onshape/__init__.py`
- Modify: `backend/app/api/onshape.py`
- Modify: `backend/app/models/schemas.py`
- Create: `backend/tests/test_onshape_oauth.py`
- Modify: `backend/tests/test_deploy_auth_security.py`

- [ ] Add tests for connect URL, signed single-use state, callback principal binding, safe return path, code exchange, verified user lookup, minimum scopes, refresh rotation, disconnect/revoke, reconnect, invalid grant, enterprise issuer allowlist, and redaction.
- [ ] Add deployment settings for OAuth client ID/secret/redirect URI, encryption key, allowed API origins, Onshape editing flag, and dedicated Temporal task queue. Production-like startup must fail closed when editing is enabled but any required setting is absent or unsafe.
- [ ] Extend `OnshapeClient` to accept an injected bearer-token provider and injected `httpx` transport. Keep HMAC signing only behind an explicit development/service-account client factory.
- [ ] Route bearer requests through one credential broker: on the first provider `401`, acquire the per-account refresh lease, refresh with generation CAS, persist the rotated encrypted bundle, and replay the original request exactly once with the returned generation. Concurrent callers adopt the committed generation; `invalid_grant` or an expired uncertain refresh journal moves the account to `reconnect_required` without another refresh.
- [ ] Test concurrent `401` responses, one-winner refresh rotation, a follower adopting the new generation, refresh timeout before provider receipt, response loss after provider rotation, `invalid_grant`, disconnect during refresh, and the one-replay limit.
- [ ] Implement `GET /api/onshape/account`, `POST /api/onshape/oauth/start`, `GET /api/onshape/oauth/callback`, and `DELETE /api/onshape/account`. Do not send tokens to the browser.
- [ ] Make `/api/onshape/config` report capabilities (`oauth_available`, `connected`, `read_available`, `editing_available`, `reconnect_required`) rather than treating shared HMAC keys as a user connection.
- [ ] Return upstream auth failures as connector errors without invalidating the WordsWave login session.
- [ ] Run `cd backend && python -m pytest tests/test_onshape_oauth.py tests/test_deploy_auth_security.py tests/test_onshape_integration.py -q`.
- [ ] Commit `feat(onshape): connect user oauth accounts`.

### Task 5: Implement bounded Onshape reads, resource linking, typed bindings, and exact previews

**Files:**
- Modify: `backend/app/integrations/onshape/client.py`
- Modify: `backend/app/integrations/onshape/service.py`
- Create: `backend/app/integrations/onshape/parameters.py`
- Create: `backend/app/integrations/onshape/resources.py`
- Modify: `backend/app/api/onshape.py`
- Modify: `backend/app/models/schemas.py`
- Create: `backend/tests/test_onshape_parameters.py`
- Create: `backend/tests/test_onshape_resource_links.py`
- Create: `backend/tests/integration/test_postgres_onshape_resources.py`
- Modify: `backend/scripts/run_release_gate.py`

- [ ] Test document/workspace/element listing, current microversion reads, Part Studio feature reads, exact-microversion export, permission failures, unsupported feature types, stale cache invalidation, pagination, and provider `401/403/404/429/5xx` classification.
- [ ] Parse pasted Onshape URLs into document/workspace/element IDs only, then verify access with the connected account's registered API origin.
- [ ] Persist a project-scoped resource link only after checking both project permission and explicit connector-account access.
- [ ] Map supported existing feature parameters to versioned discriminated bindings: quantity uses canonical decimal strings and units; boolean uses canonical booleans; enum uses provider values and the observed allowed set. Mark unsupported or required-but-unsafe parameters read-only.
- [ ] Canonicalize binding JSON and source feature snapshots before hashing. Repeat generation, type, unit, range, enum, required, and snapshot validation server-side.
- [ ] Export an STL display artifact and a STEP validation artifact from the same exact observed microversion. Verify both provider lineage responses resolve to that microversion, stream them into separate immutable object keys, verify size/SHA-256, persist `ExternalRevision` plus connector artifact metadata, and return presigned artifact URLs. A lineage mismatch or moving-head-only export is a hard failure.
- [ ] Add authenticated endpoints to list authorized resources, create/read a resource link, refresh a stale link, fetch typed bindings, and fetch the current exact preview. Reads must be bounded and audited but need not create a mutation workflow.
- [ ] Run `cd backend && python -m pytest tests/test_onshape_parameters.py tests/test_onshape_resource_links.py tests/test_onshape_integration.py -q`.
- [ ] Extend `onshape-postgres` with `tests/integration/test_postgres_onshape_resources.py` and object-store prerequisites. Run `cd backend && python scripts/run_release_gate.py onshape-postgres`; any skip exits nonzero.
- [ ] Commit `feat(onshape): project exact external previews`.

### Task 6: Add fenced external HTTP attempts and operation-specific reconciliation

**Files:**
- Create: `backend/app/services/external_attempts.py`
- Create: `backend/app/integrations/onshape/reconciliation.py`
- Modify: `backend/app/services/run_state.py`
- Modify: `backend/app/repositories/runs.py`
- Modify: `backend/app/services/event_relay.py`
- Modify: `backend/app/models/schemas.py`
- Create: `backend/tests/test_external_attempts.py`
- Create: `backend/tests/test_onshape_reconciliation.py`
- Modify: `backend/tests/postgres/test_run_state_machine.py`

- [ ] Test one `StepRun` per logical provider operation and one `ExecutionAttempt(execution_kind=external_http)` per physical request, including retry numbering, lease fencing, duplicate completion, cancellation, retry exhaustion, and credential generation mismatch.
- [ ] Build idempotency keys from workflow, logical step, connector account, resource link, intent generation, expected microversion, before hash, and intended after hash.
- [ ] Persist endpoint operation and target identity, request hash, provider request ID when available, response classification, and timing; never persist headers or credential material.
- [ ] Implement `APPLIED`, `NOT_APPLIED`, and `AMBIGUOUS` reconciliation results. Map ambiguous writes to `unknown_commit` / `reconciliation_required` / `awaiting_reconciliation` and prevent subsequent writes.
- [ ] Add reconcilers for deterministic pre-change version creation, preview workspace creation, feature update, translation/import, and cleanup deletion exactly as defined in the approved spec.
- [ ] Ensure a `429` honors bounded `Retry-After`; timeout or `5xx` retries only after a reconciler proves `NOT_APPLIED`. In mutation workflows, the credential broker may not hide multiple network sends in one Attempt: each original request, controlled replay, and OAuth refresh request gets its own external HTTP Attempt under the appropriate operation StepRun.
- [ ] Project reconciliation state through the existing snapshot/event API and WebSocket without a connector-only event stream.
- [ ] Run `cd backend && python -m pytest tests/test_external_attempts.py tests/test_onshape_reconciliation.py -q`.
- [ ] Run `cd backend && python scripts/run_release_gate.py onshape-postgres`; any skip exits nonzero.
- [ ] Commit `feat(workflows): reconcile external http attempts`.

### Task 7: Build the durable Onshape preview workflow

**Files:**
- Create: `backend/app/workflows/onshape.py`
- Create: `backend/app/workflows/onshape_activities.py`
- Create: `backend/app/workers/onshape_worker.py`
- Modify: `backend/app/workflows/temporal.py`
- Modify: `backend/app/workers/workflow_worker.py`
- Modify: `backend/app/temporal_client.py`
- Create: `backend/app/services/onshape_submission.py`
- Modify: `backend/app/api/tasks.py`
- Modify: `backend/scripts/run_release_gate.py`
- Create: `backend/app/api/external_changes.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_onshape_workflow_contracts.py`
- Create: `backend/tests/integration/test_temporal_onshape_workflow.py`

- [ ] Define strict Temporal request/signal/result contracts containing only IDs, hashes, typed intent values, timeout policy, and expected credential/microversion generations.
- [ ] Submit by persisting `WorkflowRun(kind=onshape.mutation.v1)`, the external Change Set, and immutable intent generation before starting Temporal. Re-entry with the same key repairs the DB/Temporal start window; a mismatched payload conflicts.
- [ ] Register `OnshapeMutationWorkflow` on a dedicated task queue and add a readiness probe. Never register it on the V1 or V2 MCAD queue.
- [ ] Implement the first confirmation gate before any external mutation. Rejection/cancellation before preview creates no version or workspace.
- [ ] Create a deterministic pre-change version from the exact target microversion, then a Change-Set-owned preview workspace from that version. Adopt a uniquely reconciled existing resource on replay.
- [ ] Apply only validated typed quantity/boolean/enum changes to the preview workspace with its last recorded `sourceMicroversion` and `rejectMicroversionSkew=true`.
- [ ] Immediately before every provider dispatch, recheck project permission, connector-account ownership/delegation, required scope, active resource link, account lifecycle, and current credential generation in one authorization boundary. Confirmation granted earlier is not authorization to write later.
- [ ] Read the preview feature back, verify canonical after values, persist the resulting preview microversion/external revision, and stop in `preview_ready` awaiting the second user decision.
- [ ] Implement separate actions for preview confirmation, apply confirmation, reject, revise, cancel, and reconnect. Signals must carry expected workflow phase and intent generation to reject stale UI actions.
- [ ] On revise, create a new immutable intent generation and new preview workspace; mark the prior generation `superseded` without deleting evidence.
- [ ] Test revoking project membership, account delegation, scope, resource access, or the account itself after either confirmation and prove zero subsequent provider writes.
- [ ] Run `cd backend && python -m pytest tests/test_onshape_workflow_contracts.py tests/test_temporal_workflow_contracts.py -q`.
- [ ] Add the named `onshape-durable` suite to `run_release_gate.py`, selecting `tests/integration/test_temporal_onshape_workflow.py` and requiring `CAD_AGENT_TEST_DATABASE_URL`, `CAD_AGENT_TEST_TEMPORAL=1`, `TEMPORAL_TARGET`, object-store credentials, and a running Onshape worker.
- [ ] Run `cd backend && python scripts/run_release_gate.py onshape-durable`; any selected skip must exit nonzero.
- [ ] Commit `feat(onshape): build durable preview workspaces`.

### Task 8: Validate exact Onshape preview artifacts through the isolated MCAD runtime

**Files:**
- Create: `backend/app/validation/external_preview.py`
- Modify: `backend/app/workflows/onshape.py`
- Modify: `backend/app/workflows/onshape_activities.py`
- Modify: `backend/app/execution/contracts.py`
- Modify: `backend/sandbox/capability_entry.py`
- Modify: `backend/scripts/run_release_gate.py`
- Create: `backend/tests/test_external_preview_validation.py`
- Modify: `backend/tests/integration/test_temporal_onshape_workflow.py`

- [ ] Export STEP and display artifacts from the exact persisted preview microversion, verify response lineage, stream to immutable object keys, and persist size/SHA-256 before validation.
- [ ] Add an external-preview validation adapter that feeds those immutable artifacts into existing geometry loading, render/visual, and deterministic DFM capabilities through `ExecutionBackend`; FastAPI and Temporal must not load CAD geometry directly.
- [ ] Persist a distinct StepRun/ExecutionAttempt and immutable evidence row for geometry, visual, and DFM, including runtime image digest, tool versions, input artifact hash, policy hash, outcome, and report hash.
- [ ] Test required/advisory × passed/failed/indeterminate. Required failure or indeterminate blocks apply; advisory outcomes add risk only.
- [ ] Recheck that selected artifacts/evidence belong to the active intent generation and exact preview microversion before enabling apply.
- [ ] Test corrupt STEP, lineage mismatch, renderer failure, vision-provider malformed/unavailable output, DFM policy mismatch, timeout, restart, and superseded intent.
- [ ] Run `cd backend && python -m pytest tests/test_external_preview_validation.py tests/test_durable_geometry_validation.py tests/test_durable_visual_validation.py tests/test_durable_dfm_validation.py -q`.
- [ ] Extend the named `onshape-durable` release suite with `tests/test_external_preview_validation.py` and the exact-preview integration cases; require sandbox command/image in addition to Task 7 dependencies.
- [ ] Run `cd backend && python scripts/run_release_gate.py onshape-durable`; any geometry, visual, DFM, or integration skip must exit nonzero.
- [ ] Commit `feat(onshape): validate exact preview evidence`.

### Task 9: Apply accepted changes to the target and verify or reconcile the result

**Files:**
- Modify: `backend/app/workflows/onshape.py`
- Modify: `backend/app/workflows/onshape_activities.py`
- Modify: `backend/app/integrations/onshape/reconciliation.py`
- Modify: `backend/app/repositories/external_changes.py`
- Modify: `backend/app/api/external_changes.py`
- Create: `backend/app/workflows/onshape_cleanup.py`
- Create: `backend/app/services/onshape_cleanup.py`
- Modify: `backend/app/workers/onshape_worker.py`
- Modify: `backend/app/config.py`
- Modify: `backend/.env.example`
- Modify: `backend/scripts/run_release_gate.py`
- Modify: `backend/tests/integration/test_temporal_onshape_workflow.py`
- Modify: `backend/tests/test_onshape_reconciliation.py`

- [ ] Before target mutation, re-read the target microversion and feature snapshot. Any mismatch marks the intent `stale` and performs no write.
- [ ] Repeat the Task 7 dispatch authorization guard immediately before target mutation and cleanup. Test permission/delegation/account revocation after preview approval and require zero target writes.
- [ ] Replay the exact validated typed feature update against the target using expected `sourceMicroversion` and `rejectMicroversionSkew=true`; do not merge the preview workspace in release one.
- [ ] Read the target back and require the intended canonical values plus a verified resulting microversion before transitioning to `applied`.
- [ ] Test human edits before preview, between preview and apply, and during apply. Never select a silent conflict strategy.
- [ ] Simulate a response loss after the real target write. Prove the operation-specific reconciler adopts only a conclusive applied result; an unexplained changed microversion stays `reconciliation_required` and cannot be cancelled, revised, or retried.
- [ ] Add explicit administrator/user resolution only for cases where evidence can classify the result; manual abandonment must not assert that no write occurred.
- [ ] Persist target result, resulting microversion, source/after hashes, audit records, and event projection exactly once.
- [ ] Persist preview-workspace `cleanup_state`, `cleanup_after`, attempt count, and retention reason on each intent generation. Applied/rejected generations become eligible after the configured retention interval; unknown, stale, failed, or cancelled generations remain retained until reconciliation proves cleanup safe.
- [ ] Add a separately durable cleanup workflow plus an idempotently installed Temporal Schedule. It deletes only a still-unchanged workflow-owned workspace, treats `404` as deleted, reconciles lost responses, and records `deleted`, `retained`, or `reconciliation_required`; it never runs as an in-process timer.
- [ ] Test cleanup scheduling, revise-created workspace accumulation, account disconnect, changed workspace, duplicate sweep, worker restart, deletion response loss, and safe retention of ambiguous resources.
- [ ] Run `cd backend && python -m pytest tests/test_onshape_reconciliation.py tests/test_onshape_workflow_contracts.py -q`.
- [ ] Extend `onshape-durable` with target-apply, unknown-commit, cleanup, API restart, worker restart, duplicate apply, and stale-signal integration cases, then run `cd backend && python scripts/run_release_gate.py onshape-durable`; any skip exits nonzero.
- [ ] Commit `feat(onshape): verify durable target application`.

### Task 10: Cut every public Onshape write entry point over to durable submission

**Files:**
- Modify: `backend/app/api/onshape.py`
- Modify: `backend/app/tools/plugins/business/tool_onshape.py`
- Modify: `backend/app/api/agent_tools.py`
- Modify: `backend/app/integrations/onshape/service.py`
- Modify: `backend/app/api/capabilities.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/models/schemas.py`
- Modify: `backend/tests/test_onshape_durable_cutover.py`
- Modify: `backend/tests/test_agent_tools_api.py`
- Modify: `backend/tests/test_tools_onshape_plugin.py`

- [ ] First invert the Task 1 inventory and route/tool tests to require durable submission or truthful disablement; run them and confirm they fail for the four characterized direct-write entries.
- [ ] Disable `POST /api/onshape/documents` and `POST /api/onshape/publish` in release one with a stable `409 capability_not_supported` response directing users to link an existing Part Studio. Disable `onshape_create_document` and `onshape_publish_step` in tool discovery/execution. Do not implement an import workflow in this release.
- [ ] Remove the two legacy create/publish tools and add exactly one model-visible mutation tool: `onshape_submit_parameter_change`. Its versioned arguments are `schema_version=onshape.parameter-change.v1`, `project_id`, `resource_link_id`, `expected_microversion`, `source_snapshot_sha256`, non-empty unique typed `changes[{binding_id, kind, value}]`, and `idempotency_key`; account/principal identity always comes from authenticated context.
- [ ] Mark the replacement tool `safety_level=write` and `requires_confirmation=true`. After ToolExecutor confirmation, the handler rechecks project permission, account ownership/delegation, scope, resource link, snapshot, and credential generation, then calls only `submit_onshape_change()`. Persist that confirmation with the intent so Temporal may begin preview construction; it never authorizes target apply.
- [ ] Return `status=accepted` with `workflow_run_id`, `external_change_set_id`, `intent_generation`, `workflow_status`, and task snapshot/WebSocket paths. Idempotent replay returns the same identities; a changed payload with the same key conflicts.
- [ ] Test schema rejection, missing ToolExecutor confirmation, permission/delegation failure, stale microversion/snapshot, duplicate replay/conflict, result shape, and an injected `OnshapeClient`/`OnshapeService` spy proving the tool performs zero provider requests. Keep bounded read tools account-scoped and remove `allow_shared_onshape` as a production user capability.
- [ ] Make `OnshapeService` mutating methods callable only from `OnshapeWorkflowActivities`; enforce this with a non-forgeable internal dependency boundary rather than a request boolean.
- [ ] Keep translation refresh read-only. It may update a local projection transactionally but cannot create or retry a translation.
- [ ] Capability discovery advertises editing only when OAuth, account state, resource permission, feature support, server flag, and Onshape worker readiness all pass.
- [ ] Production-like startup rejects any configuration that advertises editing while direct HMAC fallback is enabled.
- [ ] Run `cd backend && python -m pytest tests/test_onshape_durable_cutover.py tests/test_agent_tools_api.py tests/test_tools_onshape_plugin.py tests/test_onshape_integration.py -q`; all intentional Task 1 failures must now pass.
- [ ] Commit `refactor(onshape): remove public direct writes`.

### Task 11: Move native typed parameter changes into Durable Agent V2

**Files:**
- Modify: `backend/app/models/schemas.py`
- Modify: `backend/app/parameters.py`
- Modify: `backend/app/services/durable_submission.py`
- Modify: `backend/app/workflows/temporal.py`
- Modify: `backend/app/workflows/agent_v2.py`
- Modify: `backend/app/workflows/activities.py`
- Modify: `backend/app/api/generate.py`
- Modify: `backend/app/api/websocket.py`
- Create: `backend/app/api/native_parameters.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_native_parameter_submission.py`
- Create: `backend/tests/test_native_parameter_api.py`
- Modify: `backend/tests/integration/test_temporal_mcadd_workflow.py`
- Modify: `backend/scripts/run_release_gate.py`

- [ ] Define `POST /api/projects/{project_id}/native-parameter-changes`. Request `schema_version=native.parameter-change.v1` carries `branch_id`, `expected_base_revision_id`, `source_sha256`, non-empty unique `changes[{binding_id, kind=quantity, magnitude, canonical_unit}]`, `output_formats`, and `idempotency_key`; it never accepts source code.
- [ ] Return HTTP 202 with `workflow_run_id`, project/branch/base identity, task status/URL, and optional `candidate_build_id` / `change_set_id` only when already persisted on an idempotent replay. Later identities arrive through the existing task snapshot/events, never placeholders.
- [ ] Validate names, types, canonical decimal strings, finite converted values, ranges, units, source hash, and current branch head server-side. Reject unknown, duplicate, stale, wrong-type/unit, or unchanged changes before starting compute. Change `apply_parameter_values()` or its strict wrapper so unknown keys cannot be silently ignored.
- [ ] Use the existing server-side `apply_parameter_values()` to create the deterministic candidate source inside a V2 Activity. Do not call the LLM for a pure typed parameter patch and do not expose patched source as a browser-authoritative write.
- [ ] Keep the request on `McadAgentWorkflowV2`: persist the source, run real MCAD execution, geometry/visual/DFM gates, seal, create the native Change Set, await review, and advance the branch only through the existing CAS commit path.
- [ ] Preserve natural-language V2 modification for changes that cannot be represented as typed parameter updates.
- [ ] Do not add a WebSocket write message for this contract and do not route it through `execute_code`; the HTTP submission plus existing durable task WebSocket is the only typed-parameter path.
- [ ] Test duplicate submission, stale base, stale source hash, out-of-range value, no-op, execution failure rollback, validation failure, cancellation, restart, review rejection, and successful commit.
- [ ] Run `cd backend && python -m pytest tests/test_native_parameter_submission.py tests/test_native_parameter_api.py tests/test_parameters.py tests/test_api_durable_cutover.py -q`.
- [ ] Add a named `native-parameter-durable` suite selecting the typed-parameter cases in `tests/integration/test_temporal_mcadd_workflow.py`, requiring PostgreSQL, MinIO, Temporal, V2 Worker, and sandbox settings. Run `cd backend && python scripts/run_release_gate.py native-parameter-durable`; any skip exits nonzero.
- [ ] Commit `feat(mcad): submit typed parameters through v2`.

### Task 12: Add one discriminated frontend CAD source model and service boundary

**Files:**
- Modify: `frontend/src/types/engineering.ts`
- Create: `frontend/src/adapters/cadSourceAdapter.ts`
- Modify: `frontend/src/adapters/projectAdapter.ts`
- Modify: `frontend/src/services/engineeringService.ts`
- Modify: `frontend/src/utils/parameterValidation.ts`
- Modify: `frontend/src/hooks/useWebSocket.ts`
- Modify: `frontend/src/stores/sessionStore.ts`
- Modify: `frontend/tests/cad-source-contract.test.ts`
- Create: `frontend/tests/parameter-binding.test.ts`
- Modify: `frontend/tests/parameter-validation.test.ts`

- [ ] First invert the Task 1 frontend characterization to forbid imports/calls of `patchCodeParameters` from UI code; run it and confirm failure before implementation.
- [ ] Define `CadSource = native | onshape | guest`, discriminated quantity/boolean/enum `ParameterBinding`, immutable preview reference, source revision identity, capability flags, and `EngineeringChangeSetView = native | onshape`.
- [ ] Adapt existing `GenerationResult` into native/guest source projections without changing backend response fields or native history restoration.
- [ ] Add Onshape API adapters in `engineeringService.ts`; React components must not contain `fetch`, provider payload assembly, or raw feature JSON.
- [ ] Replace numeric-only validation with discriminated canonical value validation and preserve Chinese display units/labels separately from request values.
- [ ] Remove `patchCodeParameters` from `projectAdapter.ts`. Native parameter submit calls the Task 11 API; Onshape parameter submit creates an external intent. Neither path sends browser-patched source code.
- [ ] Extend task snapshot/event handling so native and Onshape workflow kinds reconnect through the existing durable stream.
- [ ] Run `cd frontend && node --test --experimental-strip-types tests/cad-source-contract.test.ts tests/parameter-binding.test.ts tests/parameter-validation.test.ts tests/websocket-replay.test.ts`.
- [ ] Run `cd frontend && npm run lint && npx tsc --noEmit -p tsconfig.app.json && npx vite build`.
- [ ] Commit `refactor(frontend): unify cad source contracts`.

### Task 13: Integrate Onshape connection, preview, parameters, and external review into the existing workspace

**Files:**
- Create: `frontend/src/components/connectors/OnshapeConnectionDialog.tsx`
- Create: `frontend/src/components/connectors/OnshapeResourceDialog.tsx`
- Modify: `frontend/src/components/workspace/EngineeringWorkspace.tsx`
- Modify: `frontend/src/components/viewer/MechanicalWorkspace.tsx`
- Modify: `frontend/src/components/parameters/ParameterDrawer.tsx`
- Modify: `frontend/src/components/changes/ChangeSetDialog.tsx`
- Modify: `frontend/src/components/project/WorkspaceHeader.tsx`
- Modify: `frontend/src/components/export/ExportDialog.tsx`
- Modify: `frontend/src/hooks/useCADModel.ts`
- Create: `frontend/tests/onshape-workspace.test.ts`
- Create: `frontend/tests/presigned-model-fetch.test.ts`
- Modify: `frontend/tests/change-set-flow.test.ts`
- Modify: `frontend/tests/engineering-labels.test.ts`

- [ ] Add a source selector/status entry that leaves native as the default and treats “未连接 Onshape” as a normal state, not a blocking error.
- [ ] Implement OAuth connection and resource selection dialogs with loading, empty, error, retry, success, expired/reconnect, unauthorized, missing, and unsupported states.
- [ ] Render the exact Onshape preview artifact in the existing Three.js workspace. Show source badge and native revision or Onshape microversion; do not add a second CAD page.
- [ ] Load same-origin model URLs with `authFetch`, but load external presigned STL URLs with plain `fetch(..., {credentials: "omit"})` and no WordsWave `Authorization` header. Add a regression test that captures request headers and credentials mode for both cases.
- [ ] Make `ParameterDrawer` render quantity, boolean, and enum bindings. Stage changes locally, show before/after values, and use `预览变更` as the primary action.
- [ ] For native/guest, submit the typed V2 parameter request. For Onshape, create an intent and follow persisted preview progress; keep the drawer draft when submission fails.
- [ ] Extend `ChangeSetDialog` with discriminated handlers. Native keeps accept/commit/rollback behavior; Onshape shows preview generation/evidence, revise/reject/apply/reconciliation actions and never calls a native revision endpoint.
- [ ] Keep export truthful: native exports immutable native artifacts; Onshape exports the selected exact external revision artifact. Hide/disable legacy create-document and STEP-import controls with the Task 10 release-one limitation; do not imply a durable import workflow exists.
- [ ] Disable editing controls for unsupported features, stale bindings, insufficient account scope, missing worker readiness, required validation failure, or reconciliation state, with a concise Chinese explanation.
- [ ] Run `cd frontend && node --test --experimental-strip-types tests/onshape-workspace.test.ts tests/presigned-model-fetch.test.ts tests/change-set-flow.test.ts tests/engineering-labels.test.ts tests/frontendText.test.ts`.
- [ ] Run frontend lint, explicit app typecheck, and build as in Task 12.
- [ ] Start the app and use the browser QA tool at 1440×900, 1280×800, 1024×768, 768×1024, and 390×844. Verify dialog focus/escape, drawers, viewer resize, long Chinese labels, touch targets, and no console errors.
- [ ] Commit `feat(frontend): edit onshape in unified workspace`.

### Task 14: Add real quota-limited guest projects and claim-on-signup

**Files:**
- Create: `backend/alembic/versions/0012_guest_project_lifecycle.py`
- Modify: `backend/app/domain/identity.py`
- Modify: `backend/app/repositories/identity.py`
- Create: `backend/app/repositories/guests.py`
- Create: `backend/app/services/guest_cleanup.py`
- Create: `backend/app/workflows/guest_maintenance.py`
- Create: `backend/app/services/guest_claim.py`
- Create: `backend/app/api/guest.py`
- Modify: `backend/app/api/auth.py`
- Modify: `backend/app/api/websocket.py`
- Modify: `backend/app/api/login.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/storage/auth.py`
- Modify: `backend/app/storage/postgres_auth.py`
- Modify: `backend/app/services/durable_submission.py`
- Modify: `backend/app/services/artifact_commit.py`
- Modify: `backend/app/workflows/activities.py`
- Modify: `backend/app/workers/workflow_worker.py`
- Modify: `backend/app/workflows/temporal.py`
- Modify: `backend/app/services/run_state.py`
- Modify: `backend/scripts/run_release_gate.py`
- Modify: `frontend/src/auth.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/LoginPage.tsx`
- Modify: `frontend/src/components/project/ProjectStart.tsx`
- Create: `backend/tests/integration/test_guest_project_claim.py`
- Create: `frontend/tests/guest-flow.test.ts`

- [ ] Add a `guest` principal kind and per-guest provisional personal tenant. Persist only the SHA-256 hash of a server-issued 256-bit opaque credential, lifecycle CAS state, expiry, quota snapshot, counters, cleanup fencing, and per-workflow usage reservations with reserved/actual/settled quantities.
- [ ] Deliver the credential only in a `Secure`, `HttpOnly`, `SameSite` cookie with production domain/path policy. Make `authFetch` include credentials without exposing the secret to JavaScript.
- [ ] Add guest start/status/revoke endpoints. Every guest API and WebSocket operation must resolve and bind the guest principal from the cookie; no global anonymous tenant is allowed in production.
- [ ] Add durable guest usage reservations. Admission locks the guest row and atomically reserves the workflow's maximum model-call, CAD-runtime, storage, project, and concurrency envelope before `WorkflowRun` creation; concurrent submissions cannot overbook the quota.
- [ ] Implement reservation plus idempotent `WorkflowRun` creation in one PostgreSQL transaction at the Temporal submission boundary. A crash before commit creates neither row; a crash after commit but before Temporal start is repaired by the existing idempotent start path and reuses the same reservation.
- [ ] Record actual model calls at provider-Activity completion, CAD runtime from fenced ExecutionAttempt timing, and storage from immutable artifact commit, each with an idempotency key. On every terminal outcome, idempotently settle the reservation and release unused capacity; retries, failures, cancellation, and replay never double charge or erase consumed work.
- [ ] Route `succeeded`, `failed`, `cancelled`, and `timed_out` WorkflowRun transitions through one settlement hook in `run_state.py` (or an equivalent single state-machine boundary) in the same transaction as the terminal status/event. Reconciliation can safely replay settlement but no individual Activity owns terminal release.
- [ ] Disable Onshape/Fusion connectors, shared credentials, account delegation, and disallowed exports for guest principals.
- [ ] Run guest generation through the same Durable V2, sandbox, validation, Change Set, preview, and event stream as a registered native user.
- [ ] Extend code and invite signup plus PostgreSQL auth persistence to accept an unexpired guest claim. CAS `guest_active -> claiming -> claimed`, add the authenticated principal to the existing tenant, store that tenant/principal on `auth_users`, return it from user lookups, revoke the guest principal/credential, and create the authenticated session in that tenant without creating a second personal tenant. `reconcile_authenticated_user()` must honor the persisted tenant rather than deriving another personal tenant from user ID.
- [ ] Fail closed if ordinary signup provisioning already began, any guest workflow is active, cleanup is `expiring`, the credential is revoked/expired, or another principal/claim already won.
- [ ] Prove tenant, project, revision, artifact, workflow, event, and audit IDs are unchanged after claim. Existing accounts cannot import a guest project in release one.
- [ ] Register `GuestMaintenanceWorkflow` on the existing durable worker and install its Temporal Schedule idempotently during deployment bootstrap. It CASes `guest_active -> expiring`, defers active workflows, settles abandoned reservations from persisted evidence, and invokes existing artifact retention; no FastAPI in-process timer owns guest lifecycle.
- [ ] Test concurrent quota admission, same-key and conflicting replay, crash before reservation commit, crash after DB commit before Temporal start, provider failure, execution retry, success, cancellation, timeout, duplicate terminal transition, API/worker restart, terminal settlement, expiry cleanup CAS, active-workflow deferral, duplicate claim, cleanup race, revoked cookie, credential rotation, cross-principal rejection, and RLS isolation with real PostgreSQL/MinIO/Temporal.
- [ ] Add a named `guest-durable` suite selecting `tests/integration/test_guest_project_claim.py` and requiring PostgreSQL, MinIO, Temporal, MCAD V2 worker, guest maintenance schedule, and sandbox settings. Run `cd backend && python scripts/run_release_gate.py guest-durable`; any skip exits nonzero.
- [ ] Run `cd frontend && node --test --experimental-strip-types tests/guest-flow.test.ts tests/mcad-real-flow.test.ts` plus lint/typecheck/build.
- [ ] Commit `feat(guest): claim real durable trial projects`.

### Task 15: Add real Onshape fault injection and live release evidence

**Files:**
- Create: `backend/tests/onshape/live_config.py`
- Create: `backend/tests/onshape/fault_proxy.py`
- Create: `backend/tests/integration/test_onshape_live.py`
- Modify: `backend/scripts/run_release_gate.py`
- Modify: `backend/pytest.ini`
- Modify: `.github/workflows/ci.yml`
- Create: `docs/qa/onshape-connector-report.md`

- [ ] Add an `onshape_live` pytest marker gated by explicit dedicated test-account variables. Skipping locally is allowed; enabling editing in a release environment requires a non-skipped pass and stored evidence.
- [ ] Extend the Task 1 release-gate runner with a fixed `onshape-live` suite and its prerequisite names. Preserve positive selection and zero-failure/error/skip enforcement without printing secret values.
- [ ] Build a test-only HTTPS forwarding proxy that can reject before forwarding, delay, return controlled `429`/`5xx`, forward a real write then drop its response, and preserve request/response redaction. Never import this proxy from application code.
- [ ] Against disposable real documents, test OAuth connect/refresh/revoke/reconnect, resource selection, quantity/boolean/enum reads and writes, exact preview, accept, reject, revise, target replay, and final visibility in Onshape.
- [ ] Test real `401/403/404/429/5xx`, human target skew, preview workspace skew, response loss after version/workspace/update/translation/delete, and operation-specific reconciliation.
- [ ] Restart FastAPI and the Onshape Temporal worker during each durable phase; disconnect the browser; repeat submissions; verify one logical result and replayable events.
- [ ] Assert no credential or raw provider payload secrets appear in PostgreSQL event/audit projections, object metadata, WebSocket frames, application logs, or test artifacts.
- [ ] Store only redacted IDs/hashes/timestamps and pass/fail evidence in `docs/qa/onshape-connector-report.md`; never commit account secrets, bearer tokens, private document URLs, or raw captures.
- [ ] Add an opt-in CI/manual workflow job that fails when `ONSHAPE_EDITING_ENABLED=true` but live tests were skipped.
- [ ] Run `cd backend && python scripts/run_release_gate.py onshape-live` in the secured environment with `ONSHAPE_LIVE=1`, OAuth client settings, encrypted dedicated test-account credentials, disposable document/workspace/element IDs, fault-proxy TLS settings, PostgreSQL, MinIO, Temporal, Onshape worker, and sandbox image configured. The runner must exit nonzero for any skip.
- [ ] Commit `test(onshape): verify live durable editing`.

### Task 16: Run full regression, deployment, rollback, and capability cutover gates

**Files:**
- Modify: `docker-compose.yml`
- Create: `deploy/object-store/cors.local.xml`
- Create: `backend/scripts/check_object_store_cors.py`
- Create: `backend/tests/test_object_store_cors_check.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `DEPLOY.md`
- Modify: `docs/development.md`
- Modify: `docs/qa/onshape-connector-report.md`
- Modify: `README.md` only if public setup/capability behavior changed

- [ ] Add the Onshape task queue, OAuth secrets, encryption secret, issuer allowlist, capability flag, guest cookie/quota settings, and readiness checks to deployment configuration. Secret values remain environment/deployment owned.
- [ ] Configure the local MinIO bucket with deployment-owned CORS allowing only configured frontend origins and methods `GET`/`HEAD` (plus required preflight metadata); never use credentialed wildcard origins. Document the equivalent S3-compatible production policy and make origin changes an operator-owned deployment step.
- [ ] Add a CORS checker that obtains a real presigned STL URL, sends browser-equivalent `Origin` requests, and fails unless the response permits the exact configured origin and methods without exposing WordsWave credentials. Unit-test rejected origin, wildcard-with-credentials, missing headers, and successful policies.
- [ ] Start a clean stack with PostgreSQL, MinIO, Temporal, API, both MCAD workers, Onshape worker, frontend, and real sandbox image. Run migrations before accepting traffic.
- [ ] Verify `/health` and `/ready` distinguish API health from V1, V2, and Onshape worker readiness; Onshape editing capability must disappear when its worker or OAuth dependency is unavailable while native remains available.
- [ ] Run the hermetic backend gate: `cd backend && python -m pytest -m "not docker and not llm and not fusion_e2e and not onshape_live" -q`.
- [ ] Run Fusion regression: `cd backend && PYTHONPATH=.. python -m pytest tests/fusion360 -m "not fusion_e2e" -q`.
- [ ] Run frontend regression: `cd frontend && npm run lint && node --test --experimental-strip-types tests/*.test.ts && npx tsc --noEmit -p tsconfig.app.json && npx vite build`.
- [ ] Run `cd backend && python scripts/run_release_gate.py durable-cad`. The named suite must explicitly select `tests/integration/test_temporal_mcadd_workflow.py`, `tests/integration/test_websocket_replay.py`, `tests/integration/test_change_set_api.py`, `tests/integration/test_postgres_project_files.py`, `tests/integration/test_api_compatibility_matrix.py`, `tests/integration/test_temporal_onshape_workflow.py`, `tests/integration/test_postgres_onshape_resources.py`, and `tests/integration/test_guest_project_claim.py`; require `CAD_AGENT_TEST_DATABASE_URL`, `CAD_AGENT_TEST_OBJECT_STORE=1`, `CAD_AGENT_TEST_TEMPORAL=1`, `TEMPORAL_TARGET`, object-store credentials, sandbox command/image, and both workers. Any skip exits nonzero.
- [ ] Exercise authenticated native generation/modification/parameter change/check/review/commit/export/history, Onshape read/preview/edit/reconcile, and guest generate/claim through actual HTTP, WebSocket, PostgreSQL, object storage, Temporal, worker, provider, and browser calls.
- [ ] Exercise invalid input, stale revision/microversion, duplicate key, permission denial, token expiry, quota exhaustion, object hash mismatch, provider timeout, cancellation, browser refresh, API restart, worker restart, and unavailable dependency.
- [ ] Run responsive browser QA at the five Task 13 viewports and confirm no console errors, broken routes, blocked native flows, overflow, or fake-success states.
- [ ] From the real browser at both local Vite and built frontend origins, load the presigned Onshape STL successfully, inspect the object-store request to prove no `Authorization` header/cookie was sent, and verify an unconfigured origin is rejected by CORS.
- [ ] Perform rollout in order: schema and compatible code; Gate A; OAuth/read flag for Gate B; editing flag for selected accounts after live evidence for Gate C; guest flag after quota/claim evidence for Gate D.
- [ ] Verify rollback by disabling capabilities, not by re-enabling direct writes. Old Temporal workflow definitions/workers remain registered until their histories are terminal.
- [ ] Update the QA report with exact commands, selected/pass/skip counts, dependency versions, runtime image digest, redacted Onshape test fixture identity, failures fixed, and residual external risks.
- [ ] Commit `test(release): gate unified cad workspace`.

## Completion criteria

- Native registered and guest projects both produce real Durable V2 artifacts and validation evidence.
- The browser never rewrites source code to claim a parameter modification.
- A connected user can open a supported Onshape Part Studio, preview its exact microversion, stage typed changes, validate a workflow-owned preview, confirm, and observe the verified result in Onshape.
- No target Onshape write occurs before confirmation and required validation.
- Every public Onshape write creates a durable workflow; no request-process or Agent-tool direct write is reachable in production.
- Retry, restart, disconnect, duplicate submission, and response loss cannot silently duplicate or misclassify provider writes.
- Connector credentials remain encrypted, tenant/account scoped, generation fenced, redacted, and unavailable to the browser, LLM, sandbox, Temporal history, and ordinary logs.
- Guest claim preserves all durable IDs and does not merge tenants after the fact.
- All hermetic, real dependency, live provider, frontend build, responsive browser, and original regression gates pass with documented evidence before the corresponding feature flag is enabled.
