# CAD-Agent Review: 2026-11-27 20ea31b final-cadtest-rereview

## Conclusion

- Status: `FAIL`
- Reviewer agent: Codex
- Review ID: `final-cadtest-rereview`
- Reviewed at: 2026-11-27 (Asia/Shanghai)
- Review range: current worktree against `origin/feature/live-cadtest-evaluation`
- Repository revision: `20ea31b5d0d5faf766be2b178c33c374e7559154`
- Working tree state: dirty; pre-existing and user-provided changes retained
- Business code modified by reviewer: `No`

The core provenance and delivery-gate logic is materially improved, but this revision is not ready for acceptance. Two current CADTest review tests fail, and no post-fix Podman/FreeCAD run has produced terminal CADTest evidence. Therefore the result cannot be represented as a closed runtime loop.

## Change Scope

- Change class: CADTest/live evaluation closure, provenance gates, native delivery verification, report status handling, and benchmark aggregation.
- User-described goal: re-review the repaired implementation and confirm that the CADTest batch flow closes without regressions or fake passes.
- Affected domains: live evaluation, CADTest observer/runner, artifact provenance, native delivery, benchmark aggregation/reporting, workflow dispatch, and backend tests.

## Findings

### P1: Current review test contract is still failing

- Locations: `backend/tests/test_cad_test_external_recheck.py:126` and `backend/tests/test_cad_test_external_recheck.py:171`.
- Call chain: `benchmark.live.benchmark_row_from_live_report()` -> `_apply_live_evaluation_outcomes()` -> `row["passed"]`.
- Evidence: the focused suite reported `2 failed, 49 passed, 1 skipped, 2 deselected`. Both failures expect `row["passed"] is False` when CADTest is not terminally scored, while the current implementation returns `None` at `backend/benchmark/live.py:368` for `evaluation_status != "scored"`.
- Impact: the JSON row contract and the external review contract disagree for unavailable/incomplete CADTest evidence. HTML strict evaluation returns false, while the JSON headline field is indeterminate. This currently fails the repository test suite and leaves downstream consumers to distinguish `None` from `False` correctly.
- Follow-up: resolve the contract deliberately. Either make the public `passed` field explicitly false for an unverified required evaluation while retaining separate `model_verdict/evaluation_status` fields, or update the external review tests and all consumers to require `None` for indeterminate evidence. Do not leave both interpretations active.

### P1: Runtime acceptance remains unproven

- Evidence: no new report was generated after the latest logic changes. The existing `backend/benchmark/reports/cad_test/20261127T091838Z_ad60becd` report predates this review and is historical evidence only; it records `cad_test_status=unavailable` for all ten runs and does not prove the repaired path.
- The two real-container tests in `backend/tests/test_cad_test_review_closure.py` were not usable in this environment: when executed, both returned `cad_test_scorer_error` because the local test environment did not provide the required scorer image/runtime. The focused non-container run therefore classified them as deselected, not passed.
- Required follow-up: run at least one repaired 3D case with the configured Podman scorer and FreeCAD runtime. The resulting report must contain terminal `cad_test.status` (`passed`, `failed`, `blocked`, or `error`), independent scorer authority, matching case/dataset/candidate/artifact identities, and populated native/STEP/STL delivery evidence.

## Implementation Trace

| Changed contract | Implementation | Review judgment |
| --- | --- | --- |
| Benchmark source identity | `backend/app/services/durable_submission.py`, `backend/app/evaluation/session.py`, `backend/benchmark/live.py` | Dataset hash and modeling route are admitted with the benchmark source; static/logic evidence supports the binding. |
| Sealed artifact identity | `backend/app/evaluation/integration.py:765`, `backend/app/evaluation/store.py` | Scoring requires the stored candidate seal, recomputes local SHA-256/size, and blocks missing or swapped artifacts before invoking the scorer. |
| Scorer identity and process result | `backend/app/evaluation/cad_test_runner.py:276`, `backend/benchmark/cad_test_engine/score_step.py`, `backend/benchmark/cad_test_engine/score_dxf.py` | Independent artifact hash and non-zero process exit are treated as non-pass; logic tests cover hash mismatch and scorer failure controls. |
| Native delivery | `backend/app/evaluation/cad_test_runner.py:308`, `backend/benchmark/cad_test_engine/check_native_delivery.py` | JSON result and verifier exit code are combined; final-object selection is delegated to the repository selection helper. Real FreeCAD execution is not verified in this environment. |
| Normal observe compatibility | `backend/app/workers/workflow_worker.py`, `backend/benchmark/cad_test_batch.py:171` | Ordinary Worker startup does not force CADTest preflight; strict configuration validation is performed by the batch CLI. No compatibility regression was found statically. |
| Batch/report status | `backend/benchmark/live.py`, `backend/benchmark/metrics.py`, `backend/benchmark/cad_test_gate.py` | Separate model verdict, evaluation status, delivery status, and final gate are present, but the `passed=None` versus `False` contract remains unresolved. |

## Test Evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Logic | `python -m pytest tests/test_cadtest_live_batch_closure.py tests/test_live_evaluation_benchmark.py tests/test_eval_metrics.py tests/test_cad_test_observer.py tests/test_cad_test_integration.py tests/test_cad_test_runner.py tests/test_cad_test_pass_gates.py tests/test_cad_test_review_closure.py -m "not docker" -q` | `122 passed, 2 deselected` | `LOGIC` | The focused core/gate suite is green; container tests were excluded because the local runtime prerequisite is absent. |
| Logic | `python -m pytest tests/test_cad_test_external_recheck.py tests/test_cad_test_pass_gates.py tests/test_cad_test_review_closure.py -m "not docker" -q` | `2 failed, 49 passed, 1 skipped, 2 deselected` | `FAIL` | Both failures are the `passed=None` versus expected `False` contract described above. |
| Architecture | `python scripts/architecture/check_boundaries.py` | Passed | `CONTRACT_ONLY` | Output: `Architecture boundaries passed (legacy exceptions remain explicit).` |
| Feature map | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | Passed | `CONTRACT_ONLY` | `24 domains, 433 referenced paths`. |
| Formatting | `git diff --check` | Passed with CRLF/LF warnings | `CONTRACT_ONLY` | No whitespace errors. |
| Risk scan | `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base origin/feature/live-cadtest-evaluation --head HEAD --worktree` | `16 findings` | `CONTRACT_ONLY` | Production leads were manually reviewed; `TemporaryFile(...): pass` and `UnsupportedDrawing: pass` are intentional. Observer isolation catches are intentional but remain a diagnostic risk. Historical QA artifacts account for most remaining noise. |
| Backend regression | `python -X utf8 -m pytest -m "not docker and not llm and not fusion_e2e" -q` | `3112 passed, 138 failed, 432 skipped, 2 errors` | `BLOCKED` | Full local run is not a valid closure signal here: many failures are unrelated service/platform/database failures, and the environment lacks the required PostgreSQL/runtime setup. The two CADTest external-recheck failures are included in the focused finding above. |
| Runtime | Repaired live batch with Podman/FreeCAD and terminal report | Not run | `BLOCKED` | User has not executed a post-fix batch run; no current terminal CADTest report exists. |

## Evaluation Evidence Integrity Gate

- Admission identity: covered by current source metadata and benchmark submission tests; no cross-case admission bypass was found in the reviewed path.
- Independent artifact identity: covered by seal verification, local hash/size recomputation, scorer hash comparison, and swapped-artifact tests.
- Process/result agreement: covered by runner logic and native-delivery tests; a non-zero verifier result cannot become a pass in the reviewed code.
- Final geometry selection: repository-owned final-object selection is used, and logic controls exist; actual FreeCAD Boolean/history/assembly controls were not run in this environment.
- Negative controls: current tests cover unsealed input, swapped bundle artifacts, missing scorer hash, wrong scorer hash, incomplete delivery, and non-terminal evidence. Runtime wrong-case/wrong-dataset and native-history controls remain unexecuted after the latest changes.
- No case-specific optimization: the reviewed changes are shared identity/route/result-contract logic, not a single-case branch. Runtime evidence is still required to establish that the generalized path works.

## Gaps and Blocked Prerequisites

- Resolve the two failing external review tests and make the `passed`/indeterminate contract consistent across JSON, HTML, and metrics consumers.
- Provide a configured CADTest scorer image and working Podman/FreeCAD runtime for the two container tests.
- Run a new post-fix 3D batch case. A historical `unavailable` report cannot close the current review.
- Re-run the focused suite, then the CI-equivalent backend/architecture checks in an environment with the required services and UTF-8 locale.

## Knowledge Update

- Review report: this file.
- Review memory: not added; no new stable invariant is proven until the failing contract and runtime gaps are closed.
- Stable map/invariant update: none.
- Business code or application tests modified by reviewer: `No`.
