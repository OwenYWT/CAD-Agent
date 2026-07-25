# M2 Scalable and Private Execution Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` only after M1 passes. Kubernetes Jobs and private workers are execution carriers, never product task records.

**Goal:** Add production-scale Kubernetes execution and outbound-pull enterprise workers while preserving the same contracts, fencing, artifact commit, security, and product APIs proven in M1.

**Architecture:** A trusted supervisor claims leases and exchanges only short-lived task credentials. Untrusted CAD code runs in an isolated runtime with no control-plane credentials. `KubernetesExecutionBackend` and `PrivateWorkerBackend` implement the same versioned execution contract as Podman.

**Tech stack:** Kubernetes Jobs, Python worker/supervisor, OCI digests, cosign, SBOM/provenance, optional gVisor, PostgreSQL/Temporal/S3, pytest, kind.

---

### Task 1: Add backend conformance and capability routing

**Files:**
- Add: `backend/app/execution/router.py`
- Add: `backend/app/execution/conformance.py`
- Add: `backend/tests/execution/test_backend_conformance.py`
- Modify: `backend/app/config.py`

- [ ] Define one conformance suite for submit/status/cancel/timeout/duplicate/stale lease/result/artifact behavior.
- [ ] Route by tenant policy, capability, platform, region, and availability without exposing backend choice to business code.
- [ ] Run the suite against real Podman before implementing other backends.
- [ ] Commit: `feat: enforce execution backend conformance`.

### Task 2: Implement Kubernetes Job execution

**Files:**
- Add: `backend/app/execution/kubernetes_backend.py`
- Add: `backend/app/execution/kubernetes_models.py`
- Add: `deploy/kubernetes/worker-rbac.yaml`
- Add: `deploy/kubernetes/runtime-class.yaml`
- Modify: `backend/app/workflows/activities.py`
- Modify: `backend/app/execution/router.py`
- Modify: `backend/app/workers/workflow_worker.py`
- Add: `backend/tests/execution/test_kubernetes_backend.py`
- Add: `backend/tests/integration/test_kind_execution.py`
- Add: `backend/tests/integration/test_kubernetes_route_e2e.py`

- [ ] Create Jobs idempotently from attempt ID and reject same ID/different hash.
- [ ] Use immutable image digest, active deadline, TTL cleanup, resource requests/limits, non-root/read-only/no capabilities/no service-account token/no network by default.
- [ ] Treat duplicate Pods as duplicate physical attempts whose result acceptance is controlled only by fencing.
- [ ] Inject Kubernetes into application composition and dispatch it through the existing Temporal execution activity and `ExecutionRouter`.
- [ ] Run real kind tests for success, failure, cancel, timeout, node/Pod loss, duplicate start, and late result.
- [ ] Submit through the public task API and verify Temporal → Kubernetes Job → artifact commit → revision/event replay.
- [ ] Commit: `feat: execute fenced MCAD attempts as Kubernetes Jobs`.

### Task 3: Build and package the trusted supervisor/untrusted runtime split

**Files:**
- Add: `worker/supervisor/__init__.py`
- Add: `worker/supervisor/main.py`
- Add: `worker/supervisor/runtime.py`
- Add: `worker/pyproject.toml`
- Add: `worker/requirements.lock`
- Add: `worker/Dockerfile`
- Add: `deploy/private-worker/compose.yml`
- Add: `deploy/private-worker/install.sh`
- Add: `deploy/private-worker/uninstall.sh`
- Add: `worker/tests/test_credential_boundary.py`

- [ ] Prove the CAD runtime cannot read lease credentials, database/object-store permanent keys, Kubernetes API tokens, or supervisor filesystem.
- [ ] Reuse the single M0/M1 MCAD Runtime image digest; do not create or publish a second CAD dependency image. Give it only staged inputs and a write-only local output directory.
- [ ] Let the supervisor perform authenticated claim/heartbeat/control/upload/complete calls.
- [ ] Run attack probes against process environment, mounts, network, procfs, sockets, and metadata endpoints.
- [ ] Build/install the Supervisor package and image from lockfiles, start it through the deployment bundle, and verify upgrades, credential rotation, and clean uninstall.
- [ ] Commit: `feat: separate trusted worker control from CAD code`.

### Task 4: Implement outbound-pull private workers

**Files:**
- Add: `backend/alembic/versions/0006_private_workers.py`
- Add: `backend/app/api/private_workers.py`
- Add: `backend/app/domain/workers.py`
- Add: `backend/app/repositories/workers.py`
- Add: `backend/app/execution/private_worker_backend.py`
- Add: `worker/supervisor/client.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/workflows/activities.py`
- Modify: `backend/app/execution/router.py`
- Modify: `backend/app/workers/workflow_worker.py`
- Add: `backend/tests/integration/test_private_worker_protocol.py`
- Add: `backend/tests/integration/test_private_worker_route_e2e.py`

- [ ] Implement enrollment/rotation/revocation, capability declaration, long poll, lease, heartbeat, control/cancel, disconnect recovery, and idempotent result submission.
- [ ] Require outbound worker connections only; the SaaS control plane never initiates into customer networks.
- [ ] Enforce tenant/backend policy and prevent one worker from claiming another tenant’s work.
- [ ] Add tenant-safe schema/RLS, register the API router, inject the backend into application composition, and dispatch it through the existing Temporal execution activity.
- [ ] Test real worker process disconnect/restart, expired credentials, stale leases, duplicate result, cancel race, and offline queueing.
- [ ] Submit through the public task API and verify Temporal → ExecutionRouter → Private Worker → artifact commit → event replay.
- [ ] Commit: `feat: add enterprise outbound-pull MCAD workers`.

### Task 5: Secure and promote runtime images

**Files:**
- Add: `.github/workflows/runtime-image.yml`
- Add: `scripts/runtime/verify_image.py`
- Add: `scripts/runtime/promote_image.py`
- Add: `deploy/runtime-allowlist.json`
- Modify: `backend/app/config.py`
- Modify: `backend/app/execution/router.py`
- Modify: `backend/app/services/run_state.py`
- Modify: `backend/app/workflows/activities.py`
- Add: `backend/tests/test_runtime_supply_chain.py`
- Add: `backend/tests/integration/test_runtime_allowlist_e2e.py`
- Add: `deploy/test-infra/registry.sh`
- Add: `deploy/test-infra/kind.sh`
- Add: `deploy/test-infra/gvisor.sh`
- Add: `deploy/test-infra/private-worker.sh`

- [ ] Build multi-architecture images, generate SBOM/provenance, scan, sign by digest, and verify signatures before allowlisting.
- [ ] Record CadQuery/build123d/OCP/Python/platform/digest/input/code versions in every attempt.
- [ ] Make production Attempt creation and dispatch reject unsigned/unallowlisted/tag-only runtime references before a Podman container, Kubernetes Job, or private-worker lease is created.
- [ ] Run signature/tamper/revocation/promotion/rollback tests against a real registry.
- [ ] Submit invalid and valid digests through the public task API and prove only the verified digest can reach Temporal and an execution backend.
- [ ] Provision and tear down the real local registry, kind cluster, gVisor-qualified node/runtime, and independent private-worker environment with checked-in scripts; store command/version evidence.
- [ ] Commit: `build: enforce signed MCAD runtime promotion`.

### Task 6: Validate gVisor and restricted fallback

**Files:**
- Add: `backend/alembic/versions/0007_sandbox_provenance.py`
- Add: `backend/benchmark/runtime_compatibility.py`
- Add: `docs/qa/gvisor-compatibility.json`
- Modify: `deploy/kubernetes/runtime-class.yaml`
- Add: `deploy/kubernetes/restricted-nodepool-policy.yaml`
- Modify: `backend/app/execution/contracts.py`
- Modify: `backend/app/execution/kubernetes_backend.py`
- Modify: `backend/app/execution/kubernetes_models.py`
- Modify: `backend/app/execution/router.py`
- Modify: `backend/app/repositories/runs.py`
- Modify: `backend/app/workflows/activities.py`
- Add: `backend/tests/integration/test_kubernetes_sandbox_tier.py`

- [ ] Run the complete semantic suite on baseline restricted runc and gVisor.
- [ ] Require identical artifact/geometry correctness, no hard regression, P95 overhead ≤25%, and memory overhead ≤20%.
- [ ] If and only if gVisor misses the gate, select the documented dedicated-node restricted fallback and persist `sandbox_tier=restricted-runc`.
- [ ] Apply the selected `runtimeClassName`, dedicated-node selector/toleration, and network policy to every real Job and persist the actual tier in ExecutionResult/ExecutionAttempt provenance.
- [ ] Run real public API jobs on the qualified tier and verify scheduling, result provenance, and no per-attempt silent fallback.
- [ ] Never silently fall back per attempt.
- [ ] Commit: `test: qualify the production MCAD sandbox tier`.

### Task 7: Add scale, fairness, and backpressure

**Files:**
- Add: `backend/app/services/scheduling.py`
- Modify: `backend/app/api/tasks.py`
- Modify: `backend/app/api/generate.py`
- Modify: `backend/app/api/execute.py`
- Modify: `backend/app/api/batch.py`
- Modify: `backend/app/api/websocket.py`
- Modify: `backend/app/api/error_messages.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/repositories/runs.py`
- Modify: `backend/app/workflows/activities.py`
- Modify: `backend/app/execution/router.py`
- Modify: `backend/app/execution/private_worker_backend.py`
- Modify: `backend/app/execution/kubernetes_backend.py`
- Add: `backend/tests/load/test_execution_capacity.py`
- Add: `backend/tests/load/test_tenant_fairness.py`
- Add: `scripts/load/run_m2_gate.py`

- [ ] Add per-tenant concurrency, queue quotas, global capacity, fair scheduling, admission control, and one normalized capacity exception mapped to truthful HTTP `429`/`Retry-After` and equivalent structured WebSocket retry metadata at every retained public entry; enforce it again at dispatch/lease claim.
- [ ] Run 50 simultaneous jobs: ≥95% leave queued within 60s, all reach truthful terminal state, no duplicate Step commit.
- [ ] Run 500-request burst: no unexplained 5xx, API p95 <500ms under bounded concurrency, overload is explicit.
- [ ] Drive the 50-job and 500-request gates through authenticated task, generate, execute, batch, async, and WebSocket entries across multiple real tenants and both execution backends; require no capacity-related 500/generic WS error and verify one tenant cannot starve another or bypass billing/audit.
- [ ] Commit: `feat: enforce execution capacity and tenant fairness`.

### Task 8: Run canary, disaster, and full regression gates

**Files:**
- Add: `docs/qa/mcad-m2-report.md`
- Add: `scripts/canary/mcad_runtime_canary.py`
- Modify: `.github/workflows/ci.yml`

- [ ] Run at least 100 real tasks through a 5% candidate-runtime canary.
- [ ] Require zero security/artifact-integrity/duplicate-commit errors, failure-rate delta ≤1 percentage point, and p95 latency delta ≤25%.
- [ ] Test database/object-store/Temporal/Kubernetes/control-plane restart, node loss, worker revocation, credential rotation, orphan cleanup, backup restore, and audit reconstruction.
- [ ] Run all M0 and M1 gates plus desktop/tablet/mobile UI regression against both Kubernetes and private-worker paths.
- [ ] Record exact raw evidence, fixes, reruns, and residual risks; do not mark M2 complete if external infrastructure was not actually exercised.
- [ ] Commit: `test: enforce the M2 commercial execution gate`.
