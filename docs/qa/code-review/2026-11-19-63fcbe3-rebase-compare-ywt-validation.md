# Rebase Validation: compare_YWT

- Reviewer: Codex
- Review ID: `rebase-compare-ywt-validation`
- Date: 2026-11-19
- Scope: verify that the local `cms_dev` rebase retained the substantive `compare_YWT` changes
- Range: `compare_YWT` (`2e68cce`) through `HEAD` (`63fcbe3`)
- Working tree: dirty; the uncommitted paths were not treated as rebase commits
- Business code modified during this review: none

## Conclusion

`compare_YWT` is fully present in the rebased commit history and its main implementation areas remain available in the current tree. The available hermetic regression evidence is positive, so the user can continue with the previous normal workflow for a real acceptance run.

This is `PASS_WITH_LIMITATIONS`, not a claim that every runtime path is proven. PostgreSQL, Temporal, Podman, the FreeCAD runtime image, provider credentials, and the complete browser workflow were not exercised in this review. The local worktree also contains uncommitted application changes, so a clean committed-tree acceptance run is still required before pushing.

## Git Evidence

- `git merge-base --is-ancestor compare_YWT HEAD`: PASS.
- `compare_YWT` is the direct parent-base of the five rebased `cms_dev` commits.
- `git rev-list --count compare_YWT..HEAD`: `5`.
- `git rev-list --count main..compare_YWT`: `22`.
- `git diff --diff-filter=D --name-only compare_YWT HEAD`: empty; no file introduced by `compare_YWT` was deleted by the rebased commits.
- `git diff --check compare_YWT HEAD`: PASS.
- Current branch status remains `ahead 27, behind 5` relative to `origin/cms_dev`, which is expected after local history rewriting. No push was performed.

## What compare_YWT Adds

### Requirement and source semantics

The branch strengthens the intake and acceptance contract. It binds numeric meaning and primitive shape, preserves engineering qualifiers and source context, prevents model-created provenance from becoming authoritative, and closes coverage gaps before CAD execution. The relevant implementation is centered in `backend/app/validation/requirement_binding.py`, `backend/app/validation/source_obligations.py`, `backend/app/validation/verification_intent.py`, and the planner/acceptance contracts.

The cube/primitive contract is represented by `cube_shape` and `cube_edge` in the new acceptance semantics. This is the reason old tests that expect three independent `overall_dimension` checks are not valid assertions for the rebased contract.

### Native FreeCAD references and geometry evidence

The branch adds native reference contracts and state projection, retained model readability, native geometry inspection, and a native oracle path. The main areas are `backend/app/contracts/native_references.py`, `backend/app/freecad/native_references.py`, `backend/benchmark/native_oracle.py`, `backend/benchmark/native_oracle_worker.py`, `backend/sandbox/material_measurements.py`, and the related FreeCAD/sandbox handlers.

### Engineering and assembly verification

It adds quantity normalization, rectangular assembly requirements, material/measurement evidence, source-predicate coverage, and stricter geometry verification. These are covered by the new contract and integration tests under `backend/tests/test_*`, `backend/tests/e2e/`, and `backend/tests/integration/`.

### Runtime, CI, and frontend contracts

It updates the FreeCAD capability metadata, sandbox image inputs, native contract scripts, CI case inventory, frontend requirement adapters/cards, and related tests. The compare branch changes are not documentation-only; they span application code, runtime/deployment checks, tests, and frontend contracts.

## Regression Evidence

| Check | Result | Notes |
|---|---|---|
| Requirement/source/native focused tests | PASS: `486 passed` | Run with `python -X utf8 -m pytest ...` from `backend` |
| Planner/acceptance/FreeCAD focused tests | PASS: `95 passed` | One existing Pydantic protected-namespace warning |
| Frontend lint | PASS | `npm.cmd run lint` |
| Frontend production build | PASS | `npm.cmd run build`; only Browserslist-age and chunk-size warnings |
| Feature map | PASS | `24` domains, `433` referenced paths |
| Git whitespace check | PASS | `git diff --check` |
| Real service workflow | NOT RUN | Requires local PostgreSQL, Temporal, Worker, Podman, FreeCAD image, and model configuration |

## Known Test Compatibility Gap

`python -X utf8 -m pytest tests/test_design_source_context.py -q` currently reports `5 failed, 61 passed`. The failures are concentrated in old expectations:

- old tests expect a cube to produce three `overall_dimension` checks;
- the current contract intentionally uses `cube_shape` plus `cube_edge`;
- one old test expects source-context validation before the newer fail-closed `engineering_verification_unavailable` diagnostic.

This was not hidden or worked around during review. It should be handled as a separate test-contract synchronization task, not by weakening the current source-binding behavior.

Running the same collection without `-X utf8` on this Windows host fails during collection because a UTF-8 fixture is read using the system `gbk` default. This is a test invocation/encoding prerequisite, not evidence that the fixture contains mojibake.

## Runtime Acceptance Procedure

For ordinary product behavior, the previous frontend command remains valid. For developer live reports, start both API and Worker with the same observe mode:

```powershell
cd D:\project\projectcad\CAD-Agent\backend
python -X utf8 -m app.workers.run --evaluation-mode observe
```

In another terminal:

```powershell
cd D:\project\projectcad\CAD-Agent\backend
python -X utf8 -m app.run --evaluation-mode observe --reload --port 8000
```

In a third terminal:

```powershell
cd D:\project\projectcad\CAD-Agent\frontend
npm.cmd run dev
```

Before using the UI, verify `http://127.0.0.1:8000/ready` reports ready. Then run the existing business flow: generate, modify, restore history, and modify again. Confirm that each task is accepted, the Worker has an active poller, the candidate/revision lifecycle completes, and a report is written under `backend/benchmark/reports/live/`.

## Working-Tree Boundary

The following tracked files and untracked test/document files were already present as local modifications when this review ran. They are not evidence of the rebase itself and were left untouched:

- modified backend planner, FreeCAD, validation, workflow, and test files;
- `backend/tests/test_modification_shape_transition.py`;
- existing QA/design documents under `docs/qa/code-review/` and `docs/superpowers/`.

No business code, application test, Git history, or user file was changed by this review.
