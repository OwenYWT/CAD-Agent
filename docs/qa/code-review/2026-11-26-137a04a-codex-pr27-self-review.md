# CAD-Agent Review: 2026-11-26 137a04a CADTest Controls

## Conclusion

- Status: `FAIL`
- Reviewer: Codex
- Review ID: `codex-pr27-self-review`
- Reviewed revision: `137a04a`
- Review range: `origin/main...HEAD`
- Business code modified during review: `No`

The colleague review is materially correct. The current CADTest integration is not
safe to merge as an evaluation gate. It can associate a score with the wrong
benchmark case, can reject valid native-history models, and does not route 2D and
assembly cases according to the dataset. The native verifier also has an exit-code
contract gap, although the in-process CAD-Agent wrapper currently converts a
`passed=false` JSON result into a failed delivery result.

The colleague's external control artifacts were not present in this workspace, so
their exact numeric outputs were not independently replayed here. The code paths
below independently confirm the underlying defects and explain the reported
observations.

## Findings

### P1: CADTest case identity is not bound to the submitted task

**Confirmed.** `attach_cad_test_result()` and `attach_cad_test_artifacts()` accept
an arbitrary `case_id` and pass it directly to the scorer. The normalized identity
checks only workflow ID, candidate revision ID, and an artifact hash. It does not
prove that the case ID is the case that originated the workflow, nor that the
artifact is the expected artifact for that case.

Relevant paths:

- `backend/app/evaluation/integration.py:683`
- `backend/app/evaluation/integration.py:745`
- `backend/app/evaluation/cad_test_evidence.py:95`
- `backend/app/evaluation/cad_test_evidence.py:111`

The `artifact_match=true` result is only a self-consistency check: the expected
hash is populated from the artifact being scored in
`backend/app/evaluation/integration.py:708` and `backend/app/evaluation/integration.py:770`.
It is not a dataset truth hash or a server-proven artifact identity. Therefore a
model from case A can be submitted under case B and still receive
`artifact_match=true`; if it satisfies B's checks, the report can say `passed`.

Impact: a benchmark score can be a false positive and must not be used as a
regression gate until case identity is bound at the trusted submission boundary.

### P1: 2D and assembly cases are not routed by their dataset definition

**Confirmed.** The live batch selector overwrites every selected case with
`"path": "extrude_cut"` in `backend/benchmark/cad_test_batch.py:94`.
The score command requires a STEP input at `backend/benchmark/cad_test_batch.py:46`,
while the test definitions explicitly distinguish 2D cases and require DXF at
`backend/benchmark/cad_tests/definitions.py:305-310`.

The live observer also recognizes only `step`, `fcstd`, and `stl` artifacts at
`backend/app/evaluation/cad_test_observer.py:23` and `backend/app/evaluation/cad_test_observer.py:72`.
It cannot attach a DXF-only candidate. Consequently, the reported X01/X02 DXF
regression is consistent with the implementation: the PR path is not evaluating
the same route as the main path. Assembly-specific format and component handling
is likewise not represented by the hard-coded batch path.

Impact: whole dataset categories can be skipped, misclassified, or scored using
the wrong artifact contract. Aggregate accuracy is therefore not trustworthy.

### P1: Native delivery verification counts historical bodies instead of the final
native result

**Confirmed.** `check_native_delivery.py` selects every non-empty
`PartDesign::Body` and then selected top-level `Part::Feature` objects:

- `backend/benchmark/cad_test_engine/check_native_delivery.py:14-17`
- `backend/benchmark/cad_test_engine/check_native_delivery.py:19`

It then compounds all selected shapes and compares the resulting solid count with
the STEP solid count at line 28. There is no final-tip/visibility/ownership
resolution, no exclusion of source bodies retained for history, and no distinction
between construction history and delivered result.

This explains the reported false failure for a Boolean model whose reopened
document retains the two input bodies plus a final result. The checker can count
historical inputs as delivered solids even when the exported STEP is geometrically
equivalent.

Impact: valid native editable models are penalized, and the native-delivery score
cannot currently be used as a reliable acceptance signal for history-preserving
FreeCAD documents.

### P2/P1 depending on invocation: native verifier failure does not affect process
exit status

**Confirmed contract gap; impact qualification required.** The native verifier
writes `native-delivery.json` and prints the boolean result, but never calls
`sys.exit(1)` when `passed` is false:

- `backend/benchmark/cad_test_engine/check_native_delivery.py:37-38`

Thus a direct invocation can return exit code 0 for a failed geometric comparison.
The wrapper in `backend/app/evaluation/cad_test_runner.py:370-373` does inspect the
JSON and maps `passed=false` to `status="failed"`, so the current wrapper does not
blindly trust only the process exit code. The colleague finding is nevertheless
valid for any pipeline or operator that invokes the repository-owned verifier as a
command and gates only on its exit status.

Impact: direct scorer integrations can allow failed delivery checks to pass. The
verifier must have an explicit nonzero exit contract, and the wrapper should retain
the JSON validation as defense in depth.

## Test Evidence

- `python -X utf8 scripts/architecture/check_boundaries.py`: `PASS`
- `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py`: `PASS`
- CADTest/evaluation regression set: `54 passed`
- The existing tests do not cover wrong-case artifact substitution, DXF live
  routing, assembly routing, retained historical Body filtering, or a native
  verifier false result with process return code assertion.
- A repository-wide `git diff --check` against `origin/main...HEAD` reports an
  existing blank line at EOF in
  `docs/qa/code-review/2026-11-23-HEAD-cadtest-report-review.md`; this is not a
  business-code defect and is unrelated to the four findings.

## Recommended Follow-up

1. Bind the benchmark case to the trusted workflow admission/source record and
   reject any attach request whose case, dataset hash, and source identity do not
   match. Never derive the expected artifact identity from the artifact being
   scored.
2. Carry the dataset route and required artifact formats through batch selection,
   workflow submission, observer attachment, and report aggregation. Add explicit
   DXF and assembly controls.
3. Define native-result selection for FreeCAD history documents using final result
   ownership/tip semantics, then compare only the delivered result. Add controls for
   Boolean history, multiple bodies, and assemblies.
4. Make the native verifier exit nonzero for `passed=false`, malformed output, and
   verifier errors; keep the JSON result as the authoritative diagnostic payload.
5. Add regression tests that fail before each fix and rerun the external control
   artifacts before reopening the PR.

## Review Boundary

No application code, tests, migrations, deployment files, or Git history were
modified during this review. Only this review record was added under the permitted
`docs/qa/code-review/` path.
