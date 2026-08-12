# Durable Agent Fusion Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the proven intelligent MCAD Agent flow into the durable production workflow without creating a second write path.

**Architecture:** Temporal coordinates versioned Agent planning, modeling, repair, and validation steps. PostgreSQL persists state/evidence, `ExecutionBackend` owns every physical CAD run, object storage owns immutable artifacts, and existing APIs/frontends consume replayable projections.

**Tech Stack:** FastAPI, Pydantic, Temporal Python SDK, SQLAlchemy/PostgreSQL, MinIO/S3, CadQuery/build123d/OCP, React/TypeScript, pytest, Vitest.

---

### Task 1: Add versioned Agent planning contracts

**Files:**
- Create: `backend/app/agent/durable_plan.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/workflows/temporal.py`
- Create: `backend/app/workflows/agent_v2.py`
- Modify: `backend/app/workers/workflow_worker.py`
- Modify: `backend/app/models/schemas.py`
- Test: `backend/tests/test_durable_agent_plan.py`

- [ ] Write contract tests for simple, complex, assembly, modification, validation policy, affected objects, and confirmation policy.
- [ ] Run the focused test and confirm it fails before implementation.
- [ ] Add strict, versioned `AgentPlan`, `AgentPlanStep`, repair policy, and validation policy models with no runtime paths or credentials.
- [ ] Add deterministic serialization into the Temporal request payload.
- [ ] Register `McadAgentWorkflowV2` on a dedicated configurable V2 Task Queue; never change V1 history semantics. Add disabled-by-default V2 submission routing and a real V2 worker readiness signal.
- [ ] Test V1 history replay/in-flight completion, old workers never receiving V2 tasks, V2 readiness/routing, rolling deployment, and fail-closed routing when V2 is unavailable.
- [ ] Run the focused tests and commit `feat(agent): define durable agent plans`.

### Task 2: Persist planning and decomposition without generating source

**Files:**
- Create: `backend/app/agent/durable_planner.py`
- Modify: `backend/app/workflows/activities.py`
- Modify: `backend/app/workflows/agent_v2.py`
- Test: `backend/tests/test_durable_agent_planner.py`
- Test: `backend/tests/integration/test_temporal_mcadd_workflow.py`

- [ ] Test simple-plan fast path, complex/assembly decomposition, provider failure, replay, and restart-safe plans.
- [ ] Adapt existing Planner, PlanDecomposer, and AssemblyPlanner without allowing them to generate source, execute CAD, or own product state.
- [ ] Persist separate requirements/plan/decompose StepRuns and events; reuse stored successful results on Activity replay.
- [ ] Keep simple known shapes on one planned modeling step and bound planning LLM calls.
- [ ] Run focused unit/integration tests and commit `feat(agent): persist durable planning steps`.

### Task 3: Add pre-execution confirmation

**Files:**
- Modify: `backend/app/workflows/agent_v2.py`
- Modify: `backend/app/workflows/activities.py`
- Modify: `backend/app/api/tasks.py`
- Modify: `frontend/src/adapters/durableTaskAdapter.ts`
- Test: `backend/tests/integration/test_temporal_mcadd_workflow.py`
- Test: `frontend/tests/websocket-replay.test.ts`

- [ ] Test ambiguous requirements and high-impact modifications pausing after plan/decomposition and before candidate allocation, source generation, or execution.
- [ ] Persist the planned affected objects and confirmation reason.
- [ ] Resume or reject through the existing confirmation signal without creating a duplicate workflow.
- [ ] Verify reject, disconnect, and worker restart create no candidate/source/attempt before confirmation.
- [ ] Commit `feat(agent): confirm durable plans before execution`.

### Task 4: Add candidate-build, staging-manifest, and validation-evidence lifecycle

**Files:**
- Create: `backend/alembic/versions/0007_agent_candidate_builds.py`
- Modify: `backend/app/domain/revisions.py`
- Modify: `backend/app/domain/artifacts.py`
- Modify: `backend/app/repositories/revisions.py`
- Modify: `backend/app/repositories/artifacts.py`
- Modify: `backend/app/services/artifact_commit.py`
- Modify: `backend/app/services/change_sets.py`
- Modify: `backend/app/workflows/agent_v2.py`
- Modify: `backend/app/workflows/activities.py`
- Test: `backend/tests/postgres/test_agent_candidate_builds.py`
- Test: `backend/tests/integration/test_agent_candidate_seal.py`

- [ ] Test building/reviewable/failed/cancelled/abandoned transitions and forbid branch advancement from non-reviewable candidates.
- [ ] Test lease-fenced staging-manifest acceptance, immutable staged validation evidence, replay, supersession, hash mismatch, and cross-candidate rejection.
- [ ] Add immutable accepted staging manifests without creating product Artifact rows.
- [ ] Add persisted seal identity/state and deterministic content-addressed final object-key contracts; do not claim cross-system atomicity.
- [ ] Add one idempotent seal transaction that consumes selected manifests and matching evidence for a building candidate; it is not called until Task 10.
- [ ] Wire confirmation rejection, workflow failure, cancellation, repair exhaustion, and supersession to truthful candidate terminal transitions and replay.
- [ ] Start real PostgreSQL, MinIO, Temporal, and the V2 Worker; run focused integration tests with no skips, including workflow failure/cancel/supersession/replay, duplicate/concurrent seal, object tampering, evidence mismatch, restart, and cleanup.
- [ ] Commit `feat(agent): stage durable candidate outputs`.

### Task 5: Generate and execute simple and complex durable modeling steps

**Files:**
- Create: `backend/app/workflows/modeling.py`
- Modify: `backend/app/workflows/source_preparation.py`
- Modify: `backend/app/workflows/activities.py`
- Modify: `backend/app/workflows/agent_v2.py`
- Test: `backend/tests/integration/test_temporal_mcadd_workflow.py`

- [ ] Test simple source generation and a multi-step part with one ExecutionAttempt per physical run, returning accepted staging-manifest IDs rather than product Artifacts.
- [ ] Run Retriever/CodeGenerator only after confirmation and candidate allocation.
- [ ] Convert AgentPlan modeling steps into ordered StepRuns and assemble accumulated source deterministically.
- [ ] Store source hashes and accepted manifest IDs; stop later steps when a required predecessor fails.
- [ ] Restart the worker between steps and prove no successful step is regenerated.
- [ ] Start real PostgreSQL, MinIO, Temporal, Worker, and Podman; run the focused external test with an explicit assertion that zero selected tests skipped.
- [ ] Run one controlled real Planner + Retriever + CodeGenerator case and assert recorded provider, model, request/response provenance, source hash, and non-empty accepted manifest.
- [ ] Commit `feat(agent): execute complex durable build plans`.

### Task 6: Execute assemblies per part

**Files:**
- Modify: `backend/app/workflows/modeling.py`
- Modify: `backend/app/workflows/agent_v2.py`
- Modify: `backend/app/workflows/activities.py`
- Test: `backend/tests/integration/test_temporal_mcadd_workflow.py`

- [ ] Test bounded parallel part generation, independent part attempts/manifests, partial failure, replay, and final assembly.
- [ ] Persist one StepRun and accepted staging manifest set per part before the assembly-combine step.
- [ ] Preserve successful parts when another part retries or fails.
- [ ] Start the real external stack; verify cancellation stops active part executions and that selected tests have zero skips.
- [ ] Commit `feat(agent): orchestrate durable assemblies`.

### Task 7: Add classified code/kernel repair and strategy fallback

**Files:**
- Create: `backend/app/agent/durable_repair.py`
- Modify: `backend/app/agent/failure_taxonomy.py`
- Modify: `backend/app/workflows/agent_v2.py`
- Modify: `backend/app/workflows/activities.py`
- Test: `backend/tests/test_durable_agent_repair.py`
- Test: `backend/tests/integration/test_temporal_mcadd_workflow.py`

- [ ] Test static/user-code/kernel repair budgets, repeated-error stop, provider/infrastructure/timeout hard stop, and eligible strategy fallback.
- [ ] Persist each repair as a StepRun with prior/new source hashes and classified reason.
- [ ] Ensure each repaired execution creates a new ExecutionAttempt and never overwrites evidence.
- [ ] Prove provider, timeout, lease, database, object-store, worker, and cancellation failures consume zero LLM repair calls.
- [ ] Start the real external stack, run repair cases with zero skips, and commit `feat(agent): add bounded durable repair`.
- [ ] Run one controlled real repair-provider case and assert prior/new source hashes, provider/model provenance, new Attempt, and accepted manifest.

### Task 8: Make geometry validation a required durable gate

**Files:**
- Create: `backend/app/validation/durable_geometry.py`
- Modify: `backend/app/execution/contracts.py`
- Modify: `backend/sandbox/capability_entry.py`
- Modify: `backend/app/workflows/agent_v2.py`
- Modify: `backend/app/workflows/activities.py`
- Modify: `backend/app/services/change_sets.py`
- Test: `backend/tests/test_durable_geometry_validation.py`
- Test: `backend/tests/integration/test_temporal_mcadd_workflow.py`

- [ ] Test valid solid/profile, invalid topology, dimension mismatch, corrupt staged STL/STEP, and bounded geometry repair.
- [ ] Define a geometry-validation capability that consumes accepted staging manifests through `ExecutionBackend`, with its own StepRun and ExecutionAttempt.
- [ ] Persist immutable staged evidence and select only the final repaired manifest; FastAPI/Temporal Worker must not load CAD geometry directly.
- [ ] Implement geometry failure → targeted repair → new source hash → new ExecutionAttempt → new manifest → geometry revalidation.
- [ ] Block seal and engineering success when required geometry validation fails or remains indeterminate.
- [ ] Start the real external stack, run geometry cases with zero skips.
- [ ] Commit `feat(mcad): gate durable results on geometry`.

### Task 9: Add visual and DFM validation

**Files:**
- Create: `backend/app/validation/durable_visual.py`
- Create: `backend/app/validation/durable_dfm.py`
- Create: `backend/app/validation/dfm_policy_snapshot.py`
- Modify: `backend/app/execution/contracts.py`
- Modify: `backend/sandbox/capability_entry.py`
- Modify: `backend/app/workflows/agent_v2.py`
- Modify: `backend/app/workflows/activities.py`
- Modify: `backend/app/services/change_sets.py`
- Test: `backend/tests/test_durable_visual_validation.py`
- Test: `backend/tests/test_durable_dfm_validation.py`
- Test: `backend/tests/integration/test_temporal_mcadd_workflow.py`

- [ ] Test required/advisory × pass/failed/indeterminate for visual and DFM, including unavailable, malformed, and timeout results.
- [ ] Define isolated render and deterministic DFM capabilities, each with its own StepRun/ExecutionAttempt and accepted-manifest inputs.
- [ ] Resolve tenant DFM rules and material/process knowledge into an immutable versioned policy snapshot with canonical hash; pass it as an `ExecutionSpec` input and record it in evidence without database credentials in the sandbox.
- [ ] Persist immutable evidence and runtime provenance; only the vision-provider judgment remains a provider Activity.
- [ ] Implement visual mismatch → `fix_visual_issues` → new source hash → new ExecutionAttempt → new manifest → geometry and visual revalidation within budget.
- [ ] Never convert unavailable or malformed visual output into `passed`.
- [ ] Required `failed`/`indeterminate` blocks seal; advisory outcomes only add Change Set risks.
- [ ] Start the real external stack, run visual/DFM cases with zero skips, and commit `feat(mcad): persist visual and dfm gates`.
- [ ] Run one controlled real vision-provider case and assert provider/model provenance; malformed/unavailable responses must remain `indeterminate`.

### Task 10: Select final manifests and atomically seal the candidate

**Files:**
- Modify: `backend/app/workflows/agent_v2.py`
- Modify: `backend/app/workflows/activities.py`
- Modify: `backend/app/services/artifact_commit.py`
- Modify: `backend/app/services/change_sets.py`
- Test: `backend/tests/integration/test_agent_candidate_seal.py`
- Test: `backend/tests/integration/test_temporal_mcadd_workflow.py`

- [ ] Test selection of final simple, complex, repaired, and assembly manifests with matching required/advisory evidence.
- [ ] Implement a persisted seal saga: deterministic content-addressed copy-if-absent, hash verification, then one PostgreSQL commit of Artifact rows/evidence/reviewable state.
- [ ] Add reconciliation for stale staging, copied-but-uncommitted final objects, and committed objects left after staging cleanup interruption.
- [ ] Test duplicate/concurrent seal, object tampering, cross-candidate/superseded manifest, missing evidence, and crashes after partial copy, after all copies before DB commit, and after DB commit before cleanup.
- [ ] Start the real external stack; prove sealed Artifacts and reviewable Change Set are created once and selected tests have zero skips.
- [ ] Commit `feat(agent): seal validated durable candidates`.

### Task 11: Project durable Agent events into the current UI

**Files:**
- Modify: `backend/app/services/event_relay.py`
- Modify: `backend/app/models/schemas.py`
- Modify: `frontend/src/adapters/durableTaskAdapter.ts`
- Modify: `frontend/src/stores/sessionStore.ts`
- Modify: `frontend/src/components/AgentRunTimeline.tsx`
- Modify: `frontend/src/components/changes/ChangeSetDialog.tsx`
- Test: `frontend/tests/websocket-replay.test.ts`
- Test: `frontend/tests/change-set-flow.test.ts`

- [ ] Test ordered planning/modeling/repair/validation events, deduplication, cursor replay, and terminal projection.
- [ ] Show real current stage, repair evidence, validation results, and risks without simulated progress.
- [ ] Preserve existing Chinese labels, routes, and responsive workspace behavior.
- [ ] Run frontend tests/typecheck/build and commit `feat(agent): expose durable agent progress`.

### Task 12: Remove behavioral duality and run release gates

**Files:**
- Modify: `backend/app/api/generate.py`
- Modify: `backend/app/api/execute.py`
- Modify: `backend/app/api/websocket.py`
- Modify: `backend/app/agent/orchestrator.py`
- Modify: `backend/app/config.py`
- Modify: `docs/qa/mcad-m1-report.md`
- Test: `backend/tests/test_api_durable_cutover.py`
- Test: `backend/tests/integration/test_api_compatibility_matrix.py`

- [ ] Prove every production generate/modify/execute/Agent write creates a WorkflowRun and no legacy writer remains reachable.
- [ ] Route new submissions atomically to V2 only after readiness; retain V1 registration until all V1 histories finish, then remove V1 submission routing without replaying histories under V2.
- [ ] Run unit, PostgreSQL, MinIO, Temporal, WebSocket, API compatibility, frontend, and real Podman MCAD tests after each migration slice.
- [ ] Exercise restart, disconnect, cancellation, duplicate request, stale revision, provider error, invalid geometry, visual indeterminate, and DFM failure.
- [ ] Run the existing real MCAD release gate without weakening thresholds.
- [ ] Update evidence and commit `test(agent): enforce durable agent parity`.
