# CADTest Follow-up Review

- Reviewer: Codex
- Review ID: `2026-11-26-db47cca-codex-cadtest-followup`
- Review time: 2026-11-26 (Asia/Shanghai)
- Range: `origin/main...HEAD` (`db47cca`, branch `feature/live-cadtest-evaluation`)
- Working tree: no application-code changes; local changes are limited to review-skill adapters and existing documentation files.
- Review mode: read-only business-code review. No business code or application tests were modified.

## Conclusion

**FAIL**. The identity and native-delivery controls added by this branch are present and the CADTest-focused tests pass, but one route-level gate defect prevents every required 2D live benchmark from being reported as passed. A second issue silently drops observer failure evidence, and the generated HTML report contains mojibake status text. Real FreeCAD/Podman acceptance was not available in this Windows review environment.

## Findings

### P1: 2D live runs cannot pass the final CADTest gate

- Files/symbols: `backend/benchmark/live.py:_artifact_gate_from_live_report` around lines 248-255 and `benchmark_row_from_live_report` around lines 300-323.
- Reproduction:
  - A 2D report with `status=finished`, a parseable DXF, `cad_test.status=passed`, and `cad_test.delivery.status=passed` was passed to `benchmark_row_from_live_report`.
  - Observed result: `artifact_gate={'dxf_readable': True, 'passed': True}`, `cad_test_gate_status='unavailable'`, and `row['passed'] == False`.
- Cause: the 2D branch returns before `_live_cad_test_gate(report)` is merged into `artifact_gate`. The later final-row expression still requires `cad_test_gate_status == 'passed'` and `native_delivery_verified is True` for every required case. Native 3D delivery is not applicable to the 2D route, so the route can never satisfy the condition.
- Impact: all 2D cases with the default `cad_test_required=true` are false negatives even when their DXF and independent CADTest score pass. This affects route-level accuracy and makes the 2D benchmark unusable for pass-rate comparisons.
- Follow-up: make the final gate route-aware. A 2D pass must require the independent 2D CADTest result and DXF delivery evidence, while it must not require the 3D native-delivery flag. Add a regression test with a passing 2D CADTest and a negative test with a failed 2D CADTest.

### P2: observer download/integrity failures lose failure evidence

- Files/symbols: `backend/app/evaluation/cad_test_observer.py` around lines 190-232 and `backend/app/workflows/activities.py` around lines 115-123.
- Cause: benchmark artifact download errors, missing metadata, and hash/size mismatches log a warning and return without calling `attach_cad_test_observation` or recording a blocked/error CADTest result. The seal activity also catches observer exceptions and uses `pass`.
- Impact: this does not create a false pass because the batch gate fails closed when CADTest evidence is absent. However, the report cannot distinguish observer failure, artifact integrity rejection, scorer unavailability, and a missing observer invocation. This undermines the developer-facing goal of diagnosing the complete execution chain.
- Follow-up: preserve the best-effort isolation from the product workflow, but append a structured `blocked` or `evaluation_error` CADTest observation with the case, workflow, candidate revision, failed artifact kind, and reason. Add controls for download failure, metadata failure, hash mismatch, and scorer failure and assert that each appears in the report.

### P2: generated HTML report status text is mojibake

- File/symbols: `backend/benchmark/cad_test_report.py:_STATUS_LABELS` and the formatting helpers around lines 12-90 and 180-360.
- Evidence: source literals include mojibake such as `閫氳繃`, `澶辫触`, `鏈褰`, and malformed separator text. These values are used directly for status labels, summary cards, ratio details, and failure categories, so the defect is user-visible in generated `report.html` files.
- Impact: developers cannot reliably read the report's status and failure summaries. This is presentation/data-quality damage, not a pass/fail bypass.
- Follow-up: restore UTF-8 Chinese labels (or use stable English labels) and add an HTML-content test that rejects known mojibake markers while checking the expected status labels.

## Controls reviewed

- Cross-case identity controls are present in `backend/app/evaluation/integration.py` and the focused integration tests reject a scorer result for a case different from the admitted source case.
- The scorer runner independently hashes the artifact and rejects scorer hash mismatches; a non-zero scorer exit code forces `evaluation_error`/failure even if the score JSON says passed.
- Native delivery result handling checks the result file and converts a non-zero verifier exit into failure. The native selection helper excludes retained history bodies and keeps independent assembly roots according to the current unit controls.
- No new cross-case false-pass or non-zero-exit false-pass was reproduced in this review. Real FreeCAD/Podman controls were not available, so this is not a `REAL_ACCEPTANCE` result.

## Validation

### Passed

- `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py`
  - `Feature map valid: 24 domains, 433 referenced paths.`
- `python scripts/architecture/check_boundaries.py`
  - `Architecture boundaries passed.`
- `git diff --check`
  - Passed; only line-ending normalization warnings for the review-skill files.
- Focused CADTest/live matrix:
  - `python -X utf8 -m pytest tests/test_live_evaluation_benchmark.py tests/test_cad_test_integration.py tests/test_cad_test_observer.py tests/test_cad_test_runner.py tests/test_cad_test_self_contained.py tests/test_native_delivery_selection.py tests/test_cad_test_report_html.py -q`
  - `65 passed, 1 warning`.
- Manual 2D gate reproduction described under P1: reproduced deterministically.

### Limited or blocked

- `python -X utf8 -m pytest tests -q`:
  - `3044 passed, 136 failed, 432 skipped, 2 errors`.
  - The failures are dominated by unavailable PostgreSQL/asyncpg state, Windows event-loop cleanup, Podman/FreeCAD-dependent tests, and shell-script tests. They are not attributed to the CADTest findings without isolated evidence. The CADTest-focused matrix above remains green.
- `bash scripts/ci/run.sh quick architecture` and `bash scripts/ci/run.sh quick backend` could not execute in this Windows environment because the shell process was rejected with `E_ACCESSDENIED`. The underlying architecture checker was run directly and passed.
- No real FreeCAD/Podman execution was available; therefore no claim of runtime acceptance is made.

## Scope and non-findings

- The review included committed changes from `origin/main...HEAD`, current tracked/untracked review-scope files, the feature map, architecture boundary rules, benchmark/live/CADTest integration, scorer result contracts, native delivery selection, and focused tests.
- Scanner findings for intentional exception isolation, test/QA wording, and historical QA documents were manually reviewed and were not promoted to production defects.
- No business code, application test, generated product artifact, commit, branch, or history was modified by this review.

