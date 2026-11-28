# CAD-Agent Review: 2026-11-27 20ea31b cadtest-live-run-ad60becd

## Conclusion

- Status: `FAIL`
- Reviewer agent: Codex
- Review ID: `cadtest-live-run-ad60becd`
- Reviewed at: `2026-11-27`
- Review range: current working tree at `20ea31b5d0d5faf766be2b178c33c374e7559154` plus runtime report `20261127T091838Z_ad60becd`
- Repository revision: `20ea31b` (`fix: register CADTest evaluation boundaries`)
- Working tree state: dirty; pre-existing user changes and review artifacts were not modified or reverted
- Business code modified by reviewer: `No`

The supplied batch report does not demonstrate ten CADTest model failures. It demonstrates ten completed Live generations with zero completed CADTest verdicts. The 3D benchmark path currently cannot reach a trustworthy CADTest pass because the Live submission requests only `STEP` and `STL`, while the native-delivery bundle requires `STEP`, `FCStd`, and `STL`. The asynchronous observation also expires under the batch wait deadline before a terminal CADTest result is available. This is a product/integration failure that must be fixed before using `pass@k` as model accuracy.

## Change Scope

- Change class: benchmark Live execution, independent CADTest observation, artifact delivery, and report aggregation
- User-described goal: determine why `ctb-00000007` produced zero successes across ten repeats and whether the cause is a bug
- Evidence report: `backend/benchmark/reports/cad_test/20261127T091838Z_ad60becd/report.json`
- Human report: `backend/benchmark/reports/cad_test/20261127T091838Z_ad60becd/report.html`
- Functional inventory domains: benchmark execution, evaluation evidence, Temporal workflow dispatch, artifact provenance, native delivery, and batch reporting

## Findings

### P1 - 3D Live submission omits the native `FCStd` artifact required by CADTest

- Paths: `backend/benchmark/live.py:392`, `backend/app/evaluation/integration.py:826`, `backend/app/evaluation/cad_test_runner.py:601`
- Call chain: `run_live_case()` submits `output_formats=["step", "stl"]` for every non-2D case -> seal dispatches CADTest observation -> `_score_sealed_artifacts()` requires `{"step", "fcstd", "stl"}` for the 3D bundle -> `run_cad_test_bundle()` rejects the bundle when `fcstd` is absent.
- Impact: the 3D benchmark path cannot perform the required native-delivery comparison, even when Live geometry and DFM validation pass. A generated model can therefore never become a complete CADTest acceptance result through this path.
- Runtime evidence: all ten runs in `20261127T091838Z_ad60becd/report.json` have `artifact_gate.passed=true` or a completed Live report, but `cad_test.delivery.status` is never `passed`; the aggregate has `cad_test_status.unavailable=10` and `pass@k=0`.
- Required fix: define one shared 3D delivery contract and use it at admission, generation, artifact sealing, object persistence, observer selection, and native delivery. For 3D cases the contract must produce and preserve `STEP`, `STL`, and `FCStd`; for 2D cases it must remain explicit and separate.
- Required regression control: a valid 3D candidate with all three artifacts must reach the scorer and native delivery checker; the same candidate with `FCStd` removed must be `blocked` with `required_delivery_artifacts_missing`, never `unknown` or `passed`.

### P1 - Batch completion deadline is shorter than the asynchronous CADTest observation path

- Paths: `backend/benchmark/live.py:339-368`, `backend/benchmark/live.py:419-436`, `backend/app/evaluation/cad_test_observer.py:29-66`, `backend/app/workflows/cad_test_observation.py:10-16`
- Call chain: candidate sealing starts a separate `CadTestObservationWorkflow` -> the batch runner waits only `context.timeout_seconds` (defaulting to `settings.generate_deadline_s`, currently 180 seconds) -> `_wait_for_report()` retains a finished Live report without a terminal `cad_test.status` -> the row is marked `CADTest observation did not reach a terminal result before the deadline`.
- Impact: a successful Live generation is reported as a failed final gate before the independent scorer/native delivery result is written. The report conflates “model failed” with “evaluation pending or unavailable,” making `pass@1`, `pass@k`, and final failure counts unusable for accuracy analysis.
- Runtime evidence: 8/10 runs contain `live_report_status=finished` plus `live_report_error=CADTest observation did not reach a terminal result before the deadline`; their CADTest status is `unknown` or absent. The Live process metrics are nevertheless healthy: geometry pass rate `1.0`, DFM pass rate `1.0`, failed call count `0`.
- Required fix: use a distinct pending state while the asynchronous observer is still running, or wait on the observation workflow/result with a separate bounded evaluation timeout. Do not convert a non-terminal observation into a model `failed` result. On timeout, report `cad_test.status=blocked` or `unavailable` with an explicit timeout reason and exclude it from verdict-based accuracy denominators.
- Required regression control: delay the observer beyond the generation deadline and verify the batch report records `pending`/`blocked` without incrementing model failures; then allow the observer to finish and verify the same run is updated to one terminal CADTest result.

### P1 - Worker CADTest configuration is not guaranteed at runtime

- Paths: `backend/app/evaluation/cad_test_observer.py:86-103`, `backend/app/evaluation/cad_test_observer.py:317-325`
- Impact: when the Worker does not have `CAD_TEST_DATASET_ROOT` (or equivalent configuration), the observer records `cad_test_configuration_unavailable` after the model has already generated and sealed artifacts. This prevents independent scoring and makes the batch outcome dependent on an implicit Worker environment.
- Runtime evidence: runs 5 and 8 have `cad_test.status=blocked`, `reason=cad_test_configuration_unavailable`, and `authority=independent_cad_scorer`; no CADTest verdict or native-delivery result exists. The remaining runs are not evidence that the scorer passed or failed.
- Required fix: validate the complete CADTest configuration when the Worker starts, including dataset root, report root, scorer/runtime image, and native-delivery capability. Fail startup or mark the benchmark run as configuration-blocked before submitting cases. Ensure the CLI and Worker resolve the same immutable configuration and dataset hash.
- Required regression control: start the Worker without each required setting and verify a deterministic startup/configuration failure; start it with the same settings as the CLI and verify the observer reaches the scorer.

### P2 - Report aggregation labels evaluation unavailability as final failure

- Paths: `backend/benchmark/live.py:424-436`, `backend/benchmark/cad_test_batch.py` aggregation path, report `summary.overall`
- Impact: the report shows `final_gate_status.failed=10`, `pass@1=0`, and `pass@k=0` while `cad_test_status.failed=0` and `cad_test_status.unavailable=10`. This presentation can be read as ten model failures even though the independent scorer supplied no verdict.
- Runtime evidence: all ten rows have `passed=false`, but none has `cad_test.verdict=true` or `cad_test.verdict=false`; eight are non-terminal observation cases and two are configuration-blocked.
- Required fix: separate `model_verdict`, `cad_test_verdict`, `evaluation_status`, and `delivery_status` in aggregation. A missing/blocked/timeout evaluation must not count as a failed model verdict. Keep the denominator explicit: generated candidates, scored candidates, terminal scored candidates, and blocked/unavailable candidates.
- Required regression control: a batch containing only scorer-unavailable rows must report zero scored failures and a nonzero unavailable/blocked count, with `pass@k` marked not computable rather than zero unless the report explicitly defines the denominator.

## Implementation Trace

| Contract | Implementation path | Consumer | Impact judgment |
| --- | --- | --- | --- |
| Benchmark output formats | `backend/benchmark/live.py:384-397` | Durable submission and artifact seal | 3D `FCStd` is omitted before evaluation begins |
| CADTest observer dispatch | `backend/app/evaluation/cad_test_observer.py:29-66` | Seal activity and Temporal observation workflow | Observation is asynchronous and configuration-dependent |
| 3D bundle requirements | `backend/app/evaluation/cad_test_runner.py:599-629` | CADTest scorer and native delivery checker | Requires all `step`, `fcstd`, `stl` artifacts |
| Batch report wait | `backend/benchmark/live.py:339-368,419-436` | Batch row and aggregate metrics | Non-terminal observation becomes a final failed gate |
| Evidence normalization | `backend/app/evaluation/cad_test_evidence.py` | Report/store quality matrix | Correctly refuses to invent a verdict, but upstream never supplies terminal evidence |

## Test Evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Runtime | Read `backend/benchmark/reports/cad_test/20261127T091838Z_ad60becd/report.json` | `FAIL` | `REAL ACCEPTANCE` | One case, ten repeats; zero terminal CADTest passes; eight observation-timeout rows and two configuration-blocked rows |
| Runtime | Aggregate process metrics in the report | `PASS` | `REAL ACCEPTANCE` | Live process metrics measured for all ten; geometry pass rate `1.0`, DFM pass rate `1.0`, failed calls `0` |
| Evaluation integrity | Check case, workflow, and artifact identity fields | `PASS_WITH_LIMITATIONS` | `REAL ACCEPTANCE` | No cross-case claim observed; however no terminal scorer/native evidence was available to prove the full chain |
| Contract trace | Compare Live output formats with 3D bundle requirements | `FAIL` | `CONTRACT_ONLY` | Live requests `step/stl`; bundle requires `step/fcstd/stl` |
| Configuration | Inspect observer configuration path | `BLOCKED` | `CONTRACT_ONLY` | The report proves configuration was unavailable in two runs; Worker startup environment was not independently captured |

## Gaps and Blocked Prerequisites

- No run in this report reached `cad_test.status=passed` or `cad_test.status=failed` with an independent scorer verdict.
- No run reached `cad_test.delivery.status=passed`; native `FCStd`/STEP/STL comparison values are absent.
- The report does not contain a scorer process exit code, scorer result hash, dataset hash in the normalized CADTest case block, or native-delivery measurements for a terminal result.
- Real scorer/container acceptance remains unverified. Focused unit tests or Live geometry evidence cannot substitute for a valid 3D native-delivery run.
- The review does not modify or infer the Worker environment used for the batch; configuration must be captured in a subsequent acceptance run.

## Acceptance Criteria For Re-review

Before closing this review, run one simple 3D case with the corrected shared delivery contract and verify the same run contains:

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

Also run negative controls for missing `FCStd`, wrong case, wrong dataset, swapped artifact, modified artifact, scorer hash mismatch, non-zero scorer exit, missing scorer result, and observer timeout. A valid pass must not be claimable when any identity or delivery evidence is missing.

## Knowledge Update

- Review report: this file
- Review memory: not added; the findings are recorded here for the requested handoff
- Stable map/invariant update: none
- Reason and evidence: this review is based on the supplied real batch report and current source trace; another review is required after implementation and a real scorer/native-delivery run
