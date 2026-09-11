# Cloud Document P0 integration

Source: `/Users/wentao/Downloads/AI_Native_CAD.html` (read 2026-09-07).

The executable scope is P0-A/B/C and P0 collaboration: durable document identity,
operation serialization/logging, authoritative committed state, semantic features,
browser state deltas and mesh preview, presence/comments, and real end-to-end tests.
P1 feature leases/rebase/LOD and P2 CAE/CAM/PLM/hardware are future milestones in
the source proposal. Existing Assembly/BOM and Fusion functionality is retained.

## Decisions

- Reuse branch/revision CAS and Change Set review. A document represents one
  existing project branch. Its committed state changes in the same transaction
  as the branch head; candidates do not silently become the current document.
- Reuse FreeCAD operation generation, checkpoints, integrity gates, Temporal and
  the container runtime. Do not replace these implemented modules.
- Serialize kernel workflows through a PostgreSQL operation queue, using
  Temporal timers while waiting. Validate both revision and state version;
  reject stale operations, never silently rebase them.
- Store operation inputs, actual generated operations and revision links.
  Verified FCStd/state artifacts remain kernel evidence; a semantic projection
  is not a replacement CAD kernel or sufficient to reconstruct an imported model.
- Use the existing STL tessellation as the P0 mesh artifact. Synchronize JSON
  feature deltas and a mesh reference; do not add GLB only to rename a working
  mesh transport. Retain the last committed model until the reviewed commit.
- Keep L0/L1 AI context bounded and preserve kernel names for allowlisted calls.
- Presence and comments use persistent tenant/project-scoped storage and
  document WebSocket events, with existing project permissions.

## Modules and gates

1. Baseline: preserve working files; lint/build/unit tests; isolated DB and
   artifact bucket; real pinned FreeCAD probe.
2. Document schema, projection, state diff, operation queue and commit/rollback
   integration. Test with real PostgreSQL, including concurrency and permissions.
3. Document APIs, Temporal/Agent integration and semantic context. Verify real
   FreeCAD generation, reopen, local parameter edit, failure and stale requests.
4. Browser document store, feature selection/properties, state/event recovery,
   presence/comments. Test adapters and real browser/API/Worker/artifact flows.
5. Repair directly relevant deployment gaps; update documentation; run full
   backend/frontend regression and real end-to-end acceptance.

## Evidence

Initial working files preserved outside the repo in
`/tmp/cad-native-baseline-20260907`; no user changes were reset or stashed.
Test results and remaining verification limitations will be recorded separately.
