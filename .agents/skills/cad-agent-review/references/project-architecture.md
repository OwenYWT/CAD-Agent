# CAD-Agent Project Architecture

This is a review reference, not a replacement for current source code. Re-check changed paths before relying on it.

## Product path

```text
React/Vite workspace
  -> auth, session/panel store, document view, task state adapters
  -> REST and WebSocket clients
FastAPI control plane
  -> auth, history, documents, tasks, changes, revisions, engineering, releases
PostgreSQL
  -> principals, projects, branches, revisions, workflow runs, events, artifacts, permissions
Temporal and workers
  -> durable Agent/CAD/scene/engineering/release workflows
ExecutionBackend
  -> isolated FreeCAD, CadQuery, Gmsh, CalculiX, capability runtimes
S3-compatible object store
  -> immutable CAD, scene, evidence, engineering, release, and Bridge artifacts
```

## Core code areas

- Frontend shell and orchestration: `frontend/src/App.tsx`, `frontend/src/components/workspace/EngineeringWorkspace.tsx`, `frontend/src/components/workspace/WorkspaceShell.tsx`.
- Frontend durable state: `frontend/src/stores/sessionStore.ts`, `frontend/src/adapters/taskState.ts`, `frontend/src/adapters/durableTaskAdapter.ts`, `frontend/src/adapters/documentView.ts`.
- Frontend server clients: `frontend/src/hooks/useWebSocket.ts`, `frontend/src/hooks/useCloudDocument.ts`, `frontend/src/services/engineeringService.ts`.
- Authentication: `backend/app/api/login.py`, `backend/app/storage/auth.py`, `backend/app/storage/postgres_auth.py`, `frontend/src/auth.ts`.
- Durable task API: `backend/app/api/tasks.py`, `backend/app/services/durable_submission.py`, `backend/app/services/task_retry.py`, `backend/app/services/workflow_dispatch.py`.
- Agent workflow: `backend/app/workflows/agent_v2.py`, `backend/app/agent/durable_planner.py`, `backend/app/agent/durable_repair.py`, `backend/app/services/task_evidence.py`.
- Cloud document and revision path: `backend/app/api/documents.py`, `backend/app/services/cloud_documents.py`, `backend/app/services/document_branches.py`, `backend/app/repositories/revisions.py`.
- Candidate and commit path: `backend/app/api/changes.py`, `backend/app/services/change_sets.py`, `backend/app/repositories/agent_candidates.py`, `backend/app/services/artifact_commit.py`.
- Native CAD path: `backend/app/freecad/contracts.py`, `backend/app/freecad/state_projector.py`, `backend/app/freecad/operation_generator.py`, `backend/sandbox/freecad_entry.py`.
- Scene and selection: `backend/app/services/scene_jobs.py`, `backend/app/services/document_geometry.py`, `backend/sandbox/freecad_scene.py`, `backend/app/freecad/selection.py`.
- Engineering: `backend/app/services/document_engineering.py`, `backend/app/workflows/engineering_activities.py`, `backend/sandbox/freecad_engineering.py`, `backend/sandbox/freecad_cam.py`.
- Releases and delivery: `backend/app/services/document_releases.py`, `backend/app/services/local_bridge.py`, `backend/app/api/local_bridge.py`, `backend/app/integrations/local_bridge_client.py`.
- Capabilities and tools: `backend/app/capabilities/registry.py`, `backend/app/capabilities/runtime.py`, `backend/app/api/capability_actions.py`, `backend/app/tools`, `backend/app/api/agent_tools.py`.
- External CAD: `backend/app/api/onshape.py`, `backend/app/integrations/onshape`, `backend/app/fusion360`, `fusion_addin`.
- Runtime and readiness: `backend/app/main.py`, `backend/app/config.py`, `backend/app/execution`, `backend/app/temporal_client.py`, `backend/app/object_store.py`.

## Authority flow

Task lifecycle is persisted in workflow/run/event records. The frontend receives snapshots and ordered events and projects them for display. A browser WebSocket is a transport and replay mechanism, not the lifecycle authority.

A durable modeling request carries project/branch identity, expected revision/state version, operation context, idempotency key, objective, and optional selection/engineering basis. The service validates the request before persistence and dispatch.

A successful modeling workflow creates an immutable candidate and evidence. Candidate review actions determine whether it is accepted, rejected, or committed. Only commit advances the document Head.

All downstream views must bind to the viewed revision: parameters, scene, checks, BOM, exports, engineering evidence, and release inputs.