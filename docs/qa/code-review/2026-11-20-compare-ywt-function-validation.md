# compare_YWT Function Validation

- Reviewer: Codex
- Review ID: `compare-ywt-function-validation`
- Date: 2026-11-20
- Scope: validate the functionality introduced by `compare_YWT` after the local rebase
- Range: `main...compare_YWT`, with the current rebased `HEAD` used for execution checks
- Working tree: dirty; existing local modifications were not changed or treated as part of the branch review
- Business code modified during review: none

## Conclusion

The compare_YWT implementation is present and most of its deterministic logic is working. Requirement semantics, source binding, native-reference contracts, assembly/material verification, FreeCAD contracts, and the frontend engineering intake tests pass. The local API is also currently healthy: `/health` returns `ok`, `/ready` returns `ready`, and the readiness response reports one workflow poller and one activity poller for both workers.

The review is `FAIL_WITH_FINDING` for one user-facing contract defect and `PASS_WITH_LIMITATIONS` for the remaining unverified runtime areas:

1. Equivalent natural-language cube requests are not admitted consistently. The direct wording `生成一个边长10cm的正方体` binds successfully, but `帮我生成一个立方体，它的边长是10cm` and `Create one cube with edge length 100 mm` produce `required_clause_unbound` and will be rejected before modeling.
2. Real FreeCAD/Podman execution was not run from this review shell because the host `podman` client cannot read the configured connection identity file. The application `/ready` endpoint is ready, but that is not a substitute for a real generated artifact and native geometry check.

## What Was Validated

### Requirement and source semantics

- Numeric units and primitive shape binding: PASS.
- `cube_shape` and `cube_edge` contract: PASS.
- Source context and source-predicate coverage: PASS.
- Fail-closed admission for unsupported requirements: PASS.
- Coordinate, hole, assembly, quantity, and verification-intent contracts: PASS.

Relevant paths include `backend/app/validation/requirement_binding.py`, `backend/app/validation/source_obligations.py`, `backend/app/validation/quantities.py`, `backend/app/validation/rectangular_assembly_requirements.py`, and `backend/app/validation/verification_intent.py`.

### Native model and engineering evidence

- Native reference contracts and topology identity: PASS at logic/contract level.
- Native oracle controls and frozen evidence handling: PASS at logic/contract level.
- FreeCAD inspection, operation, capability, and state contracts: PASS at logic/contract level.
- Material measurement and rectangular assembly contract logic: PASS at logic/contract level.

### Frontend contract surface

- Engineering measurement adapter tests: PASS.
- Requirement intake tests: PASS.
- Full frontend test collection: PASS, `180 passed`.

## Findings

### High: equivalent cube wording is rejected inconsistently

**Affected call chain**

`backend/app/validation/requirement_binding.py:159` -> `extract_source_requirements` -> `require_bound_sources` at `backend/app/validation/requirement_binding.py:993` -> durable planning/admission.

**Reproduction**

Run from `backend`:

```powershell
python -X utf8 -c "from app.validation.requirement_binding import extract_source_requirements; import json; texts=['生成一个边长10cm的正方体','帮我生成一个立方体，它的边长是10cm','Create a cube with edge length 100 mm','Create one cube with edge length 100 mm']; print(json.dumps({t:{'requirements':[(q['kind'],q['comparison']['value']) for q in extract_source_requirements(t)['requirements']],'unsupported':[u['reason'] for u in extract_source_requirements(t)['unsupported']]} for t in texts},ensure_ascii=False,indent=2))"
```

Observed behavior:

- `生成一个边长10cm的正方体`: no unsupported conditions; `cube_shape` and `cube_edge` are bound.
- `帮我生成一个立方体，它的边长是10cm`: `required_clause_unbound`.
- `Create a cube with edge length 100 mm`: no unsupported conditions; `cube_shape` and `cube_edge` are bound.
- `Create one cube with edge length 100 mm`: `required_clause_unbound`.

The parser already recognizes the shape and edge dimension in the failing inputs, but the remaining source obligation is not covered. `require_bound_sources` then raises `engineering_verification_unavailable` before model execution. This conflicts with the intended natural-language intake behavior and makes tool-path consistency depend on whether the user says `a` versus `one`, or uses a trailing explanatory clause in Chinese.

**Suggested follow-up**

Add focused regression cases for these equivalent phrasings and update the source-obligation binding so the delivery-count phrase and the shape/dimension clause are both covered without weakening fail-closed behavior. Do not solve this by deleting the unsupported diagnostic or by accepting an unbound requirement.

### Medium: legacy source-context test contract is not synchronized

`python -X utf8 -m pytest tests/test_design_source_context.py -q` reports `5 failed, 61 passed`.

The failures include old expectations for three `overall_dimension` checks where the current contract intentionally uses `cube_shape + cube_edge`, and one old expected exception ordering where the current logic correctly emits `engineering_verification_unavailable` earlier. These are test-contract mismatches, but the suite needs to be updated or explicitly retired before claiming a fully green repository.

## Test Evidence

| Layer | Command/result | Classification |
|---|---|---|
| Feature map | `validate_feature_map.py`: 24 domains, 433 paths | PASS / LOGIC |
| Backend compare_YWT focused tests | 611 passed, one Pydantic warning | PASS / LOGIC |
| Frontend full tests | 180 passed | PASS / LOGIC |
| Frontend lint/build | Both passed; existing Browserslist/chunk warnings only | PASS / BUILD |
| API health | `GET http://127.0.0.1:8000/health`: `{"status":"ok"}` | PASS / SERVICE |
| API readiness | `GET http://127.0.0.1:8000/ready`: `status=ready`; PostgreSQL, object store, Temporal, and both Worker poller probes ready | PASS / SERVICE |
| Legacy source-context suite | 5 failed, 61 passed | FAIL / CONTRACT DRIFT |
| Host Podman client | `podman ps` blocked by access denied on configured connection identity file | BLOCKED / RUNTIME |
| Real FreeCAD artifact execution | Not run from this shell | NOT RUN / RUNTIME |

The scanner produced many leads in the developer live-evaluation code and tests. They were not automatically treated as fake implementations; the relevant branches record evidence or deliberately handle optional observation failures. No confirmed fake-success path was established by this review.

## User Verification Procedure

### 1. Verify service readiness

```powershell
curl.exe http://127.0.0.1:8000/health
curl.exe http://127.0.0.1:8000/ready
```

The second response must be `status=ready` and must show active workflow and activity pollers for the normal Worker and Agent V2 Worker.

### 2. Verify requirement binding through the normal UI

Use the same workspace and run these four requests as separate tasks:

```text
生成一个边长10cm的正方体
帮我生成一个立方体，它的边长是10cm
Create a cube with edge length 100 mm
Create one cube with edge length 100 mm
```

For the first and third cases, the request should reach planning and produce a candidate. The second and fourth currently reproduce the finding above and should be recorded as failures, not retried until they pass.

### 3. Verify compare_YWT native/reference behavior

After a successful native model exists, perform one selected-feature edit and one history restore, then inspect the resulting task and revision evidence. Verify that:

- the selected feature/reference belongs to the submitted base revision;
- the resulting candidate is distinct from the committed head until acceptance/commit;
- the native artifact and state evidence identify the same revision;
- reopening or reloading does not silently change the selected native reference;
- a failed geometry or stale-base check does not advance the branch head.

### 4. Verify the real artifact and reports

Run the normal generate -> modify -> restore -> modify-again flow. Confirm the final candidate/revision state in the UI and inspect `backend/benchmark/reports/live/` for the live report. A unit test or readiness response does not replace checking the actual FCStd/STEP artifact and native geometry evidence.

## Limitations and Working-Tree Boundary

- The current worktree contains pre-existing uncommitted planner, validation, FreeCAD, workflow, and test changes. This review did not alter or stage them.
- `backend/benchmark/reports/live/` contains prior live reports, but historical reports are not current proof of the new runtime path.
- The host `podman` CLI failure is an environment/access issue in this review shell; the backend readiness endpoint currently reports its own runtime dependency as ready. The two observations must not be conflated.
- No application code, application test, migration, generated artifact, Git history, or user file was modified by this review.
