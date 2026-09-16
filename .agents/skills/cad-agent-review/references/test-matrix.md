# CAD-Agent Test Matrix

The reviewer selects tests from the changed paths and the affected feature domains. Do not run external-provider or device tests without the required environment and explicit authorization.

## Always-safe review checks

```text
git diff --check
python .agents/skills/cad-agent-review/scripts/validate_feature_map.py
python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base <base> --head HEAD --worktree
```

## Frontend

Use when frontend source, API clients, adapters, stores, or workspace components change:

```text
cd frontend
node --test --experimental-strip-types tests/*.test.ts
npm run lint
npm run build
```

Relevant focused files include `task-state.test.ts`, `websocket-replay.test.ts`, `cloud-document.test.ts`, `change-set-flow.test.ts`, `change-set-commit-gating.test.ts`, `native-view.test.ts`, `history-restore-commit-regression.test.ts`, `parameter-validation.test.ts`, `sketch-preview.test.ts`, `engineering-field.test.ts`, and `cam-toolpath.test.ts`.

## Backend safe regression

Use for most backend code changes when local dependencies are present:

```text
cd backend
python -m pytest -m "not docker and not llm and not fusion_e2e" -q
```

Run focused tests first. Important groups include auth, requirement/brief, task/retry/recovery, durable planner/validation, FreeCAD contracts/state/operations, document diff, engineering contracts, capability runtime, tools, Onshape contracts, and deployment contracts.

## Persistence and service integration

Use when changing repositories, migrations, RLS, workflow dispatch, durable tasks, artifacts, history, collaboration, releases, or object storage:

```text
cd backend
python -m pytest tests/postgres -q
python -m pytest tests/integration -q
```

These require configured PostgreSQL, MinIO, Temporal, or other service fixtures. If unavailable, mark the result BLOCKED rather than treating unit tests as closure.

## Runtime and product acceptance

Use only when the corresponding runtime and isolated environment are available:

- FreeCAD/CadQuery: `backend/tests/e2e/freecad_*.py`, `backend/tests/e2e/native_*.py`.
- Durable browser and deployment: `backend/tests/e2e/cloud_*.py`, `backend/tests/e2e/task_state_*.py`, `backend/tests/e2e/continuation_recovery_browser.py`.
- FEA/CAM: `backend/tests/e2e/freecad_fea_acceptance.py`, `backend/tests/e2e/freecad_cam_acceptance.py`, `backend/tests/e2e/cloud_engineering_*.py`, `backend/tests/e2e/cloud_cam_browser.py`.
- Release and Bridge: `backend/tests/e2e/freecad_release_acceptance.py`, `backend/tests/e2e/cloud_release_*.py`, `backend/tests/e2e/cloud_bridge_*.py`.
- Fusion: `backend/tests/fusion360/` for contracts and adapters; `fusion_e2e` requires an explicitly configured live Fusion fixture.

Read `backend/tests/e2e/README.md`, `docs/development.md`, `docs/fusion360-installation.md`, and the relevant dated QA report before declaring REAL_ACCEPTANCE.

## Evidence classification

For every command record command, result, environment, and classification. A green unit test is LOGIC only. A green API integration test is SERVICE. A real FreeCAD or solver run is RUNTIME. A real browser/deployment/provider/device/filesystem scenario is PRODUCT. Historical evidence is dated context, not a current test result.