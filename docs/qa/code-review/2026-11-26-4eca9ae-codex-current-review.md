# CAD-Agent Code Review: 2026-11-26 4eca9ae codex-current-review

## Conclusion

- Status: `FAIL`
- Reviewer agent: Codex
- Review ID: `codex-current-review`
- Reviewed at: 2026-11-26 (Asia/Shanghai)
- Review range: merge-base `7508b94d9f4e1cf0a306508d66ab27729db2a18e` through `HEAD` (`4eca9ae6bca7fb6e636b07a0adcf7a1418b4e33a`), plus current worktree leads
- Working tree state: branch `feature/live-cadtest-evaluation`, ahead 1 and behind 1 relative to `origin/feature/live-cadtest-evaluation`; four pre-existing untracked documentation/report files; no staged changes
- Business code modified by reviewer: `No`

The branch's focused CADTest/evaluation checks pass, and the earlier 2D gate and observer metadata-loss findings are covered by current code/tests. Two current evidence-integrity defects remain: contradictory delivery evidence can be normalized as a pass, and the live observer trusts an unverified dataset hash. A third lower-severity observability gap still drops a final recording exception. These are material for a developer-only evaluator because they can make a reported pass or dataset identity look more authoritative than the underlying evidence.

## Change scope

- Change class: integrated live CADTest evaluation, independent scoring/native-delivery verification, batch reporting, and evaluation identity boundaries
- User-described goal: review the current project for obvious defects using `cad-agent-review`
- Changed paths: `backend/app/evaluation/**`, `backend/benchmark/**`, related evaluation tests/docs, and review-skill metadata in the selected range
- Functional inventory domains: 05 Agent modeling collaboration; 07 CAD generation/native modeling; 12 inspection/validation/evidence; 19 files/export/artifacts; 21 capability/runtime catalog; 24 reliability/deployment/operations

## Findings

### P1: contradictory delivery channels can be reported as a verified pass

- Path/symbol: `backend/app/evaluation/cad_test_evidence.py:179-205`, `normalize_cad_test_result`
- Evidence: the function first reads `result["delivery_verified"]` at line 184 and only falls back to `native_delivery["passed"]` when it is `None` (line 185-186). It never checks that both booleans agree. A direct reproduction with a passing score, `delivery_verified=True`, and `native_delivery={"passed": False}` returns `status="passed"`, `verdict=True`, and `reason="cad_test_verified"`; the normalized delivery status is also `passed`.
- Impact: a stale, forged, or incorrectly merged result can claim native delivery passed while the native verifier says it failed. The final benchmark gate consumes the normalized status, so this can become a false evaluation pass. The runner currently produces matching values in its normal bundle path, but the normalizer is the trust boundary and accepts mappings from observation/attachment paths; it must fail closed on contradictory evidence.
- Suggested follow-up: require `delivery_verified` and `native_delivery.passed` to be equal when both are present; treat disagreement, non-boolean values, or a `passed` delivery status without a boolean result as `error`/`unknown`. Add a negative test that asserts no pass for this contradiction.

### P1: live dataset identity is accepted from an unverified manifest field

- Path/symbol: `backend/app/evaluation/cad_test_runner.py:62-76`, `CadTestRunnerConfig.resolved_dataset_hash`; consumed by `backend/app/evaluation/cad_test_observer.py` and `backend/app/evaluation/integration.py:_cad_test_identity_block`.
- Evidence: `resolved_dataset_hash()` reads `data/dataset-v1.json` and returns its `dataset_hash` field directly. It does not canonicalize the manifest, compare the computed hash, validate `status`, verify `controls-validation.json`, or verify per-case file hashes. The repository's authoritative `benchmark.cad_test_engine.dataset.load_dataset()` performs all of those checks, but the live observer does not call it. The observer then compares this unverified value with the admitted source metadata and proceeds to score the mounted case files.
- Impact: if the dataset manifest or case/check files are changed while the old `dataset_hash` field is retained, live scoring can run against altered requirements while still reporting the admitted dataset identity as matching. This undermines the cross-dataset/case negative control and makes benchmark results non-reproducible or falsely attributable to the recorded dataset.
- Suggested follow-up: resolve the dataset through the same verified loader used by batch evaluation, or independently recompute and validate the manifest/control/case hashes before returning the identity. Any mismatch or incomplete validation must produce a blocked evaluation result, never proceed to scoring.

### P2: final CADTest attachment failures remain only log warnings

- Path/symbol: `backend/app/evaluation/cad_test_observer.py:285-299`, `observe_sealed_candidate`; related isolation in `backend/app/workflows/activities.py:118-129`.
- Evidence: exceptions from `attach_cad_test_artifacts` are caught and only logged as `CADTest sealed-candidate observation failed`; no structured blocked/error observation is appended. The activity wrapper separately catches observer and terminal-recording failures to preserve the business result, which is appropriate for isolation but leaves the evaluator report without a terminal reason.
- Impact: the batch gate fails closed when CADTest evidence is absent, so this does not by itself create a false pass. It does make scorer failure, report-write failure, and observer invocation failure indistinguishable from missing configuration or a missing worker callback, weakening diagnosis and coverage metrics.
- Suggested follow-up: on the final attach exception, append a bounded `evaluation_error`/`blocked` observation containing workflow, candidate revision, case, and reason. Keep the business workflow isolated, but make the evaluation report explain the loss.

## Compatibility and impact trace

| Changed symbol or contract | Consumers | Impact judgment |
| --- | --- | --- |
| `normalize_cad_test_result` evidence verdict | `merge_cad_test_result`, `FileEvaluationStore`, `benchmark.live._live_cad_test_gate`, batch report aggregation | P1 result-contract integrity defect when delivery channels disagree |
| `CadTestRunnerConfig.resolved_dataset_hash` | `attach_cad_test_artifacts`, observer identity block, live benchmark scoring | P1 provenance defect; source identity can match altered dataset contents |
| `observe_sealed_candidate` attachment path | seal activity, live report timeline, CADTest coverage metrics | P2 observability gap; business workflow remains isolated |
| 2D route gate and artifact selection | `benchmark_row_from_live_report`, observer, native delivery checker | Current focused tests confirm prior 2D false-negative and ambiguous-artifact issues are fixed |

No frontend, REST, WebSocket, PostgreSQL/RLS, revision-commit, or user permission contract was changed by this range. The evaluation boundary is developer-facing, but it still carries claims used for benchmark comparisons and must preserve identity/provenance invariants.

## Scanner and false-positive review

- Risk scanner: 20 leads across 52 changed paths.
- Confirmed in business code: the three findings above. Broad exception handlers in observer/integration/activity code are intentional isolation boundaries only where they preserve the product workflow; the final attach handler is reported because it loses structured evaluator evidence.
- Dismissed leads: placeholder/mock wording in review/planning documents; historical QA-report text; intentional scorer exception branches that convert failures to non-pass results.
- No business-code encoding corruption was promoted from the scanner in the selected range. Existing unrelated mojibake in older documentation/reports is dated evidence, not a newly introduced runtime finding.

## Test evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Static | `git diff --check` | PASS | LOGIC | No whitespace errors in selected range; Git also reports existing Windows ignore-file permission warnings. |
| Skill integrity | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | PASS | CONTRACT_ONLY | 24 domains, 433 referenced paths. |
| Architecture | `python scripts/architecture/check_boundaries.py` | PASS | CONTRACT_ONLY | Legacy exceptions remain explicit. |
| Risk scan | `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base 7508b94d... --head HEAD --worktree` | PASS_WITH_LEADS | LOGIC | Leads manually reviewed above. |
| Focused CADTest/evaluation matrix | `python -X utf8 -m pytest tests/test_cad_test_evidence.py tests/test_cad_test_integration.py tests/test_cad_test_observer.py tests/test_cad_test_runner.py tests/test_cad_test_self_contained.py tests/test_native_delivery_selection.py tests/test_live_evaluation_benchmark.py tests/test_eval_metrics.py tests/test_cad_test_report_html.py -q` | PASS: 105 passed, 1 warning | LOGIC/SERVICE-CONTRACT | No real FreeCAD/Podman execution. Existing Pydantic protected-namespace warning. |
| Full backend safe matrix | `python -X utf8 -m pytest -m "not docker and not llm and not fusion_e2e" -q` | FAIL: 3047 passed, 136 failed, 431 skipped, 1 deselected, 2 errors | BLOCKED/ENVIRONMENT | Failures dominated by unavailable PostgreSQL/asyncpg lifecycle, Windows event-loop cleanup, shell/FreeCAD/container-dependent tests, and pre-existing environment assumptions; not attributed wholesale to this range. |
| Runtime/product | FreeCAD/Podman live CADTest and native-delivery controls | BLOCKED | REAL_ACCEPTANCE | No fresh real runtime acceptance was performed in this Windows review. |

## Gaps and blocked prerequisites

- No fresh full live CADTest batch was run; the review verifies code paths and focused controls, not model-generation quality.
- PostgreSQL/Temporal/MinIO and the complete FreeCAD/Podman environment were not used for product acceptance.
- The two P1 findings need negative controls in the application test suite before the branch can be considered closed.

## Knowledge update

- Review report: `docs/qa/code-review/2026-11-26-4eca9ae-codex-current-review.md`
- Review memory: none; no stable invariant was changed during a review-only invocation.
- Stable map/invariant update: none.

No business code, application tests, migrations, deployment code, generated product artifacts, user files, or Git history were modified by this review.
