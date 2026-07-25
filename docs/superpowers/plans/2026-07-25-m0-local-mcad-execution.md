# M0 Real Local MCAD Execution Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Stop on a failed gate, find the root cause, fix it, and rerun the complete task gate before continuing.

**Goal:** Put every existing MCAD generate, modify, inspect, export, history, and WebSocket path behind one real Podman-backed execution contract without changing public APIs or presenting false success.

**Architecture:** Introduce versioned wire contracts plus an internal materialized outcome. `PodmanExecutionBackend` owns container-specific work; a compatibility executor preserves the current `SandboxResult` interface while the orchestrator and DFM layers stop importing Docker/Podman code. M0 intentionally keeps SQLite snapshots and process-local workflow state, but browser disconnects do not cancel work and process restart is reported honestly.

**Tech stack:** Python 3.11+, FastAPI, Pydantic v2, RFC 8785 JCS, Podman, CadQuery/build123d/OCP, SQLite, pytest, React/TypeScript/Vite.

**Compatibility matrix:** Preserve response/status/error semantics for `/api/generate`, `/api/modify`, `/api/execute`, `/api/batch/generate`, `/api/generate/async`, `/api/tasks/{id}`, `/api/files/**`, `/api/history/**`, `/api/analyze/**`, `/ws/{session_id}`, and all current auth/rate-limit checks. ECAD remains visible as “暂未接通” and is never returned as completed.

---

### Task 1: Capture the real pre-change baseline

**Files:**
- Modify: `backend/benchmark/eval.py`
- Modify: `backend/benchmark/metrics.py`
- Modify: `backend/tests/test_eval_cases.py`
- Modify: `backend/tests/test_eval_metrics.py`
- Add: `docs/qa/mcad-m0-baseline.json`

- [ ] Extend the existing 50-case `benchmark.eval` tests to require deterministic case IDs, STEP/STL readability, non-empty geometry, four views, and three-run result aggregation.
- [ ] Run `cd backend && python -m pytest tests/test_eval_cases.py tests/test_eval_metrics.py -q` and confirm the new artifact gate fails before implementation.
- [ ] Extend the existing evaluator and metrics; do not create a parallel benchmark or hard-code passing results.
- [ ] Start the Podman machine if required, build the current sandbox image, and record the resolved OCI image digest and host platform.
- [ ] Run the 50-case suite three times against the unmodified execution path and save raw result evidence in `docs/qa/mcad-m0-baseline.json`.
- [ ] Verify every reported 3D success has readable STEP/STL, non-empty geometry, and four rendered views; a missing artifact is a failure.
- [ ] Run `cd backend && python -m pytest tests/test_eval_cases.py tests/test_eval_metrics.py -q`.
- [ ] Commit: `test: establish real MCAD commercial baseline`.

### Task 2: Define canonical versioned execution contracts

**Files:**
- Modify: `backend/requirements.txt`
- Add: `backend/app/execution/__init__.py`
- Add: `backend/app/execution/contracts.py`
- Add: `backend/app/execution/canonical.py`
- Add: `backend/tests/test_execution_contracts.py`
- Add: `backend/tests/fixtures/execution_canonical_vectors.json`

- [ ] Add tests for immutable `ExecutionSpec`, `ExecutionLeaseEnvelope`, `ExecutionResult`, enums, schema versions, and forbidden lease/secrets in persisted specs.
- [ ] Add cross-language canonical vectors covering key order, Unicode NFC, explicit nulls, materialized defaults, decimal strings, and finite numeric primitives.
- [ ] Run `cd backend && python -m pytest tests/test_execution_contracts.py -q` and confirm failures.
- [ ] Add the pinned `rfc8785` dependency and implement SHA-256 over RFC 8785 canonical UTF-8 bytes.
- [ ] Implement contracts without local paths or transport credentials in wire objects.
- [ ] Run the contract tests and the existing Fusion contract tests.
- [ ] Commit: `feat: define versioned execution contracts`.

### Task 3: Add the backend boundary and Podman implementation

**Files:**
- Add: `backend/app/execution/backend.py`
- Add: `backend/app/execution/podman_backend.py`
- Add: `backend/app/execution/compat_executor.py`
- Modify: `backend/app/sandbox/executor.py`
- Modify: `backend/app/config.py`
- Add: `backend/tests/test_execution_backend.py`
- Modify: `backend/tests/test_executor.py`

- [ ] Write tests for backend submission, normalized results/errors, local materialized paths, timeout, unavailable runtime, output verification, and no secret persistence.
- [ ] Run the focused tests and confirm they fail before implementation.
- [ ] Move Podman/container selection behind `ExecutionBackend`; keep low-level hardened sandbox mechanics in `app.sandbox`.
- [ ] Implement `PodmanExecutionBackend` and a compatibility executor returning the current `SandboxResult`.
- [ ] Resolve and record the actual image digest, CPU architecture, runtime versions, input hash, and code hash on every result.
- [ ] Preserve network-none, read-only root filesystem, non-root UID, capability drop, PID/file/memory/CPU limits, and timeout.
- [ ] Run focused tests plus a real Podman success, syntax failure, timeout, and forbidden-network execution.
- [ ] Commit: `feat: route local CAD work through Podman execution backend`.

### Task 4: Produce and verify the unified reproducible MCAD runtime

**Files:**
- Modify: `backend/sandbox/Dockerfile`
- Modify: `backend/sandbox/executor_entry.py`
- Add: `backend/sandbox/runtime-lock.json`
- Add: `backend/sandbox/runtime_probe.py`
- Add: `backend/tests/test_runtime_manifest.py`
- Modify: `backend/.env.example`
- Modify: `docker-compose.yml`

- [ ] Add failing manifest tests requiring pinned Python/CadQuery/build123d/OCP/ezdxf plus every Node/Playwright/Three.js or native dependency used by migrated CAD, DXF, implicit-CAD, inspect, export, and snapshot actions.
- [ ] Pin the runtime dependencies and add all missing local-compute Capability dependencies without weakening the sandbox.
- [ ] Make deployment configuration accept an immutable digest and reject `latest` outside local development.
- [ ] Build the image with Podman and run the complete runtime probe for standard generate, build123d, implicit-CAD, STEP/STL/DXF/SVG/PNG export, inspect, and snapshot; import every produced artifact and record the digest.
- [ ] Run malicious file/network/process probes and require the sandbox policy to block them.
- [ ] Commit: `build: make the unified MCAD runtime reproducible`.

### Task 5: Remove business-layer container coupling

**Files:**
- Modify: `backend/app/agent/orchestrator.py`
- Modify: `backend/app/agent/multi_step.py`
- Modify: `backend/app/dfm/step_analysis.py`
- Modify: `backend/app/capabilities/runtime.py`
- Modify: `backend/app/capabilities/registry.py`
- Modify: `backend/app/api/capability_actions.py`
- Modify: `backend/app/main.py`
- Add: `backend/tests/test_execution_wiring.py`
- Modify: `backend/tests/test_capability_runtime.py`
- Modify: `backend/tests/test_capability_actions_api.py`

- [ ] Add an import-boundary test that fails when agent, API, DFM, validation, or MCAD Capability business modules import Docker, Podman, subprocess, or `CadQueryExecutor`.
- [ ] Add dependency-injection tests proving one backend instance serves REST, WebSocket, multi-step, DFM, CAD/DXF/implicit-CAD generation, inspect, export, and snapshot actions.
- [ ] Replace direct executor construction with the compatibility boundary from application composition.
- [ ] Route local-compute Capability actions through `ExecutionBackend`. Keep network/device Connectors such as Onshape, Fusion, Bambu, and future ECAD outside the MCAD runtime.
- [ ] Run `rg -n "CadQueryExecutor|docker|podman|subprocess" backend/app/{agent,api,dfm,validation,capabilities}` and allow only reviewed connector/process-supervisor exceptions, never CAD computation.
- [ ] Run focused tests and real REST generate/modify/execute/capability action requests.
- [ ] Commit: `refactor: isolate MCAD execution from business orchestration`.

### Task 6: Normalize failures and prevent false repair retries

**Files:**
- Modify: `backend/app/agent/failure_taxonomy.py`
- Modify: `backend/app/agent/orchestrator.py`
- Modify: `backend/app/agent/recovery_actions.py`
- Modify: `backend/app/api/error_messages.py`
- Modify: `backend/tests/test_failure_taxonomy.py`
- Modify: `backend/tests/test_retry_intent_ratelimit.py`

- [ ] Add failing tests for `SandboxUnavailable`, launch failure, timeout, OOM, invalid code, CAD kernel failure, artifact rejection, cancellation, and retryability.
- [ ] Prove infrastructure failures make zero LLM repair calls and exactly one physical attempt.
- [ ] Implement normalized categories and user-safe Chinese messages while preserving current response shapes.
- [ ] Run focused tests and force each real runtime failure that is practical locally.
- [ ] Commit: `fix: make MCAD execution failures truthful and bounded`.

### Task 7: Make M0 task/disconnect behavior honest

**Files:**
- Add: `backend/app/workflows/__init__.py`
- Add: `backend/app/workflows/local.py`
- Add: `backend/app/storage/local_runs.py`
- Modify: `backend/app/api/batch.py`
- Modify: `backend/app/api/websocket.py`
- Modify: `backend/app/main.py`
- Add: `backend/tests/test_local_workflow_lifecycle.py`
- Modify: `backend/tests/test_deploy_websocket.py`

- [ ] Add tests that client refresh/disconnect does not cancel execution, progress send failures are isolated, final result is persisted before delivery, and reconnect reads current state.
- [ ] Add restart reconciliation tests for SQLite-persisted `RUNNING → INTERRUPTED → RECONCILING → FAILED/process_restarted` and for a verifiable already-committed success.
- [ ] Persist the minimum WorkflowRun identity, owner, state, timestamps, terminal result reference, and cancellation flag in the existing SQLite store. Process-local refers only to execution/orchestration; task state must survive restart.
- [ ] Implement only the specified process-local orchestrator boundary; do not invent durable retries or attempt to resume uncommitted computation.
- [ ] Replace the comment and behavior that imply the in-memory async endpoint is production durable.
- [ ] Run real WebSocket disconnect/reconnect and FastAPI restart probes.
- [ ] Commit: `fix: preserve local MCAD work across client disconnects`.

### Task 8: Verify the complete existing MCAD product flow

**Files:**
- Modify only if a real defect is found: `frontend/src/services/engineeringService.ts`
- Modify only if a real defect is found: `frontend/src/hooks/useWebSocket.ts`
- Modify only if a real defect is found: `frontend/src/hooks/useCADModel.ts`
- Modify only if a real defect is found: `frontend/src/components/viewer/MechanicalWorkspace.tsx`
- Modify only if a real defect is found: `frontend/src/components/parameters/ParameterDrawer.tsx`
- Modify only if a real defect is found: `frontend/src/components/validation/ValidationDialog.tsx`
- Modify only if a real defect is found: `frontend/src/components/export/ExportDialog.tsx`
- Add: `frontend/src/components/changes/ChangeSetDialog.tsx`
- Add: `frontend/src/adapters/changeSetAdapter.ts`
- Modify: `frontend/src/types/engineering.ts`
- Modify: `frontend/src/services/engineeringService.ts`
- Modify: `frontend/src/components/workspace/EngineeringWorkspace.tsx`
- Add: `frontend/tests/mcad-real-flow.test.ts`
- Add: `frontend/tests/change-set-adapter.test.ts`

- [ ] Add contract tests proving no frontend success state can be produced without backend result and downloadable artifacts.
- [ ] Build the M0 compatibility Change Set only from real snapshot ancestry, task result, artifact metadata, validations, and parameter/code/file differences. Missing evidence remains unknown; never synthesize successful validation or geometric change.
- [ ] Wire “查看变更” to a real review dialog supporting restore/rollback through the existing history API. Label accept/submit actions unavailable unless a real existing endpoint supports them; M1 replaces this adapter with durable Change Sets.
- [ ] Start backend, frontend, and Podman runtime; use real authentication.
- [ ] In a browser perform: create → view model → property modify → progress → inspect → export STEP/STL/DXF/PNG → download → history restore → reconnect.
- [ ] Verify missing LLM credentials, missing runtime, invalid parameter, execution timeout, artifact failure, unauthorized file, and stale history errors.
- [ ] Test 1440 desktop, 1280 laptop, 1024 tablet, 768 tablet, and 390 mobile; fix only observed regressions.
- [ ] Verify ECAD is visible and truthfully says “暂未接通”.
- [ ] Commit any observed UI fixes separately by defect.

### Task 9: Run the M0 release gate

**Files:**
- Add: `docs/qa/mcad-m0-candidate.json`
- Add: `docs/qa/mcad-m0-report.md`
- Modify: `backend/benchmark/compare.py`
- Modify: `backend/tests/test_eval_metrics.py`
- Modify: `.github/workflows/ci.yml`

- [ ] Run the full backend hermetic, Fusion contract, frontend lint/test/typecheck/build suites.
- [ ] Run the 50-case candidate suite three times with the same configuration as baseline.
- [ ] Add failing compare tests for metadata mismatch, Runtime digest/model/temperature/RAG/case-set mismatch, hard regression, pass@1 below one baseline standard deviation, unreadable/missing STEP/STL, non-empty geometry failure, and fewer than four views.
- [ ] Make `benchmark.compare` return non-zero for every failed gate above; `benchmark.eval --baseline` must propagate that exit status.
- [ ] Require: no hard regression, pass@1 not below one baseline standard deviation, zero false success, and all successful artifacts readable with valid geometry/four views.
- [ ] Run real API, WebSocket, auth, history, file ownership, and browser regression suites.
- [ ] Add a separate runtime CI job that builds/probes the real sandbox; do not weaken the hermetic job.
- [ ] Record commands, versions, evidence paths, failures, fixes, reruns, and residual risks in the report.
- [ ] Commit: `test: enforce the M0 real MCAD release gate`.
