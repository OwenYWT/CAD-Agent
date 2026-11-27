# CAD-Agent Review: 2026-11-27 20ea31b cadtest-closure-rereview

## Conclusion

- Status: `FAIL`
- Reviewer agent: Codex
- Review ID: `cadtest-closure-rereview`
- Reviewed at: `2026-11-27`
- Review range: current working tree at `20ea31b5d0d5faf766be2b178c33c374e7559154`
- Working tree state: dirty; existing user changes and prior review artifacts were not modified or reverted
- Business code modified by reviewer: `No`
The previous integration gaps are partially addressed, but the change is not closed and is not ready for user acceptance. The code now requests `FCStd` for 3D benchmark runs, separates the CADTest wait timeout from generation timeout, preserves unscored runs as non-verdicts, and preflights the batch configuration. However, the focused regression suite has one failure, and no post-fix real batch report has reached an independent CADTest/native-delivery terminal result. The current state must not be reported as a verified end-to-end closure.

## Findings

### P1 - Existing CADTest evidence contract regression remains unfixed

- Paths: `backend/app/evaluation/cad_test_evidence.py:216-230`, `backend/tests/test_cad_test_integration.py:97-105`
- Failure: the focused suite reports `86 passed, 1 failed`; `test_observed_cad_test_is_attached_to_the_live_report` expected `cad_test_evidence_incomplete` but received `cad_test_delivery_evidence_incomplete`.
- Impact: the implementation changed the public reason string for an incomplete scorer result without updating the existing integration contract. This is a real backend regression, not a stale historical test. CI will reject the change and downstream report consumers may depend on the existing reason value.
- Evidence: `python -m pytest tests/test_cadtest_live_batch_closure.py tests/test_live_evaluation_benchmark.py tests/test_eval_metrics.py tests/test_cad_test_observer.py tests/test_cad_test_integration.py tests/test_cad_test_runner.py -q` from `backend`.
- Required fix: decide and document whether delivery evidence absence is a new stable reason. Either preserve `cad_test_evidence_incomplete` for compatibility or intentionally version/update every consumer and test. Do not leave the implementation and test contract divergent.
- Required regression control: assert both incomplete scorer evidence and missing native delivery evidence independently, including the expected normalized status/reason and report schema.

### P1 - Normal `observe` worker startup now requires CADTest infrastructure

- Paths: `backend/app/workers/workflow_worker.py:107-110`, `backend/app/evaluation/cad_test_observer.py:101-151`, documented normal workflow `backend/benchmark/README.md:83-105`.
- Problem: every Worker started with `CAD_EVALUATION_MODE=observe` calls `validate_cad_test_worker_config()` and requires the dataset, scorer image, Podman, FreeCAD native runtime, and writable CADTest report root before the normal worker starts.
- Impact: normal frontend observe mode is no longer independently usable without CADTest infrastructure. This conflicts with the documented normal observe workflow, which is intended to record Live process evaluation for ordinary frontend operations without benchmark case IDs. It also violates the accepted product boundary that ordinary frontend use may have no CADTest truth and should still work.
- Required fix: keep strict CADTest preflight for the batch command or an explicitly benchmark-enabled Worker configuration. Normal observe mode must continue to start and record Live process evidence when CADTest configuration is absent; benchmark cases must be rejected or marked configuration-blocked before scoring, not by preventing all normal Worker startup.
- Required regression control: start the Worker in observe mode with no `CAD_TEST_DATASET_ROOT` and verify ordinary frontend workflow execution still works; start the batch path with the same missing configuration and verify it fails before submitting cases.

### P1 - No real post-fix CADTest/native-delivery acceptance evidence exists

- Evidence available: the latest real report currently present is `backend/benchmark/reports/cad_test/20261127T091838Z_ad60becd/report.json`, generated before the current fixes. It contains ten runs with no terminal CADTest verdict and cannot validate the current code.
- Missing evidence: no newer report was present under `backend/benchmark/reports/cad_test`; no run has been observed with all of `cad_test.status`, `cad_test.verdict`, `cad_test.authority=independent_cad_scorer`, dataset identity, independent artifact identity, and `delivery.status` populated from the corrected path.
- Impact: unit tests prove routing and aggregation logic only. They do not prove that the Worker dispatches the observation, downloads all three artifacts, runs the independent scorer, runs FreeCAD native delivery, and writes a terminal report before the batch evaluation timeout.
- Required acceptance run: run one simple 3D case with `STEP + FCStd + STL`, a configured dataset hash, the scorer image, and `--evaluation-timeout-seconds 420` or a measured sufficient value. The resulting report must show a terminal CADTest status and populated native delivery facts for the same workflow/candidate/artifact identity.

## Verified Improvements

- `backend/benchmark/live.py:418-436` now requests `fcstd` for required 3D CADTest cases and selects the FreeCAD backend.
- `backend/benchmark/live.py:458-476` now waits using a separate CADTest evaluation timeout and does not turn an unscored row into `passed`.
- `backend/benchmark/live.py:341-370` and `backend/benchmark/metrics.py:361-425` preserve pending/unavailable evaluation as missing verdict evidence; unscored rows no longer become model failures or artificial zero pass rates.
- `backend/benchmark/cad_test_batch.py:156-175,203-212` validates the dataset/configuration and passes the separate evaluation timeout into the Live context.
- `backend/app/workers/workflow_worker.py:107-110` and `backend/app/evaluation/cad_test_observer.py:101-151` add a strict configuration/runtime preflight, but the preflight scope is too broad for ordinary observe mode as described above.

## Test Evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Focused backend | `python -m pytest tests/test_cadtest_live_batch_closure.py tests/test_live_evaluation_benchmark.py tests/test_eval_metrics.py tests/test_cad_test_observer.py tests/test_cad_test_integration.py tests/test_cad_test_runner.py -q` | `FAIL` | `CONTRACT_ONLY` | `86 passed, 1 failed`; integration reason contract mismatch |
| Architecture map | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | `PASS` | `CONTRACT_ONLY` | Feature map valid: 24 domains, 433 referenced paths |
| Formatting | `git diff --check` | `PASS` | `CONTRACT_ONLY` | No whitespace errors; line-ending warnings are present in the dirty working tree |
| Risk scan | `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base origin/feature/live-cadtest-evaluation --head HEAD --worktree` | `LEADS` | `CONTRACT_ONLY` | Leads were reviewed; no scanner lead was used as acceptance proof |
| Real product batch | `backend/benchmark/reports/cad_test/20261127T091838Z_ad60becd/report.json` | `BLOCKED` | `REAL ACCEPTANCE` | Historical pre-fix run; no post-fix terminal scorer/native-delivery result exists |

## Acceptance Criteria For Next Re-review

1. The focused suite passes, including the incomplete-evidence reason contract.
2. Normal observe mode starts and runs without CADTest dataset/scorer infrastructure when used by the frontend.
3. The batch command performs strict CADTest preflight before submitting any benchmark case.
4. One post-fix 3D batch run reaches a terminal CADTest result and writes the full evidence chain:

```text
live_report_status=finished
cad_test.status=passed or failed
cad_test.authority=independent_cad_scorer
cad_test.verdict=true or false
identity.case_match=true
identity.dataset_match=true
identity.artifact_match=true
delivery.status=passed or failed
native_solids, step_solids, stl_facets, symmetric_difference_volume_mm3 populated
```

5. Negative controls still reject missing `FCStd`, wrong case, wrong dataset, swapped artifact, modified artifact, scorer hash mismatch, scorer exception, non-zero verifier exit, missing evidence, and observer timeout.

## Knowledge Update

- Review report: this file
- Review memory: not added; the findings are specific to the current unclosed change
- Stable map/invariant update: none
- Business code modified by reviewer: `No`
