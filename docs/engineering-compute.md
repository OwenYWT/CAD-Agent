# Native engineering computations

The cloud document inspector provides finite-element analysis and 2.5D external contour milling. Both operate on the exact committed FCStd revision selected at submission. They do not modify the CAD head or create a design change set.

## Execution and evidence

`POST /api/documents/{document}/engineering` accepts an explicit typed task, expected revision/state version and idempotency key. The existing project permission check, document CAS, immutable task row, workflow creation and Temporal dispatch outbox share a transaction. The worker verifies the source artifact's size and SHA-256 before mounting it read-only in the pinned native runtime. The normal execution-attempt lease, heartbeats, cancellation, staging upload, hash verification and artifact commit are reused.

`GET /api/documents/{document}/engineering` lists the latest 50 tasks. `GET /api/documents/{document}/engineering/{workflow}` returns a successful report and document-authorized artifact references. Pending, cancelled or failed work has no successful result response. Each analysis has distinct filenames within the source revision, allowing repeated studies without overwriting evidence. Viewer roles may inspect reports and visual fields; computation and file export require the existing project permissions. Membership is checked again when computing and committing output.

Migration `0022_engineering_tasks` stores the immutable source document, revision, state version, FCStd artifact/hash, submitting principal and request identity. Tenant RLS and the evidence mutation trigger protect these records. Artifact rows retain the actual runtime image digest and execution provenance.

## Finite-element analysis

The packaged FreeCAD runtime includes Gmsh 4.15.0 and CalculiX 2.23. The actual native solid is exported to STEP, meshed into quadratic C3D10 tetrahedra and solved with CalculiX. Inputs are a named homogeneous isotropic material with Young's modulus in MPa and Poisson ratio, mesh size in mm, a fixed boundary plane, a loaded boundary plane, and a nonzero three-component force in N.

This implementation supports one valid solid and small-displacement linear elasticity. Boundary selection uses actual planar mesh faces on a chosen native bounding-box extreme. Curved boundaries without such a plane are rejected. The fixed plane restrains all translations. A uniform load is distributed using consistent quadratic triangular face weights. The worker reads real displacement, nodal stress and support reaction output; it rejects missing/non-finite results and a force balance error greater than 0.1%.

The browser verifies the result field hash and renders stress or displacement, with an optional deformation display multiplier. The multiplier leaves the reported numerical values unchanged. The downloadable evidence archive includes the native STEP, Gmsh input/mesh, CalculiX input, FRD/DAT output, solver logs and JSON report/field. Stress values are extrapolated nodal values for the chosen mesh. This does not implement plasticity, thermal analysis, contact, mesh-convergence certification or an unprovided material's allowable stress.

## External contour milling

The CAM task requires a named flat-end cutter with diameter and effective cutting length, axial stepdown, cutting/plunge feed, spindle speed, safe height, declared stock side margin, radial allowance, chord tolerance and explicit G54 origin. The supported postprocessor is `grbl_1_1`, using mm, absolute coordinates and linear interpolation. Tool installation and G54 setup belong to the actual machine configuration.

FreeCAD verifies that the selected solid is a vertical extrusion of one horizontal top face. Its native outer wire is offset by the cutter radius, allowance and a bounded chord margin. Every rounded segment in the emitted NC coordinates is checked against the native outline. The path enters from outside the declared stock, completes each depth layer, exits along the verified lead and retracts before lateral rapid travel. The worker rejects a short cutter, stepped/three-dimensional surface, invalid offset, target gouge or excessive path budget.

The browser shows the actual target mesh, cutting/rapid segments and cutter position; a slider and playback follow the emitted path. Downloads include the GRBL NC file and the report, path and native STEP evidence archive. The reported feed time excludes rapid travel. This operation handles the exterior contour only: holes and pockets are counted explicitly as not machined. Fixture clearance, machine travel, spindle/tool suitability and physical setup require an actual target; the software does not claim they have been verified.

## Agent context

CAD operation acceptance freezes up to four successful report references for that exact source revision, choosing the latest per task type/component. Before planning, the worker rechecks ownership, source identity, report size and SHA-256, then adds bounded measured summaries to the normal Agent model context. Real provider request/context hashes are recorded. Reports remain attached to their original revision after a geometry edit; the Agent is instructed to rerun analysis before making claims about the changed design.

## Acceptance evidence

Runnable tests live in `backend/tests/e2e`:

- `freecad_fea_acceptance.py`: actual Gmsh/CalculiX axial beam versus the analytical solution, doubled-force response and bending response, plus support reaction balance.
- `cloud_engineering_acceptance.py`: real HTTP/outbox/Temporal/kernel/S3 result, duplicate request, stale version and invalid/missing input failures.
- `cloud_engineering_browser.py`: actual UI submission, stress/displacement rendering, unchanged values under display scaling and evidence download.
- `cloud_engineering_controls.py`: real worker stop/restart, persisted cancellation, viewer/export boundaries and revoked editor rejection.
- `cloud_engineering_agent.py` and `cloud_engineering_agent_database.py`: real analysis-to-provider-to-native-modification/review/commit, with persisted context hashes and source-bound historical results.
- `freecad_cam_acceptance.py`: four native profiles, with independent swept cutter/target Boolean intersections for the emitted NC moves, origin/depth/rapid checks and unsupported geometry/tool failures.
- `cloud_cam_browser.py`: actual path computation, playback, NC/evidence download, idempotency and native short-tool rejection.
- `cloud_engineering_database.py`: dispatch, artifact lineage, immutable evidence and tenant RLS.

Use isolated databases, buckets and task queues. Never point the acceptance fixtures at user or production storage. Physical machinery and external PLM systems require their own configured target acceptance; they are not replaced by a successful software-only test.
