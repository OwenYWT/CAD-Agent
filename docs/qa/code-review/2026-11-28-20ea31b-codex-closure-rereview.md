# CAD-Agent Review: CADTest closure re-review

## Conclusion

- Status: `FAIL`
- Reviewer agent: Codex
- Review ID: `closure-rereview`
- Reviewed at: 2026-11-28 (Asia/Shanghai)
- Review range: current worktree and `origin/main...HEAD`
- Repository revision: `20ea31b fix: register CADTest evaluation boundaries`
- Working tree state: dirty; existing user changes and QA artifacts were retained
- Business code, application tests, deployment files, and Git history modified by reviewer: `No`

The latest artifact proves that the real CADTest and native-delivery path can close for successful candidates: four attempts reached terminal `cad_test_verified` with independent artifact identity, matching case/dataset/workflow/candidate/worker-build identities, and native-delivery evidence. However, the batch is not fully closed: the third case failed in both repeats before candidate sealing, so the batch has `4/6` scored attempts and `2/3` cases covered. The JSON aggregate also reports `pass@1=1.0` and `pass^k=1.0` by excluding the unscored case, which is misleading for a three-case batch and remains a merge-blocking report-contract issue.

## Evidence Reviewed

Primary artifact:

`backend/benchmark/reports/cad_test/20261127T184543Z_bc6ae822/report.json`

Readable report:

`backend/benchmark/reports/cad_test/20261127T184543Z_bc6ae822/report.html`

The report is valid UTF-8 JSON and contains `manifest`, `results`, and HTML-generation inputs for all six attempts.

## Findings

### P1: JSON summary reports 100% pass rates while two of six attempts were never scored

- The report summary contains:

  ```text
  n_cases=3
  pass@1.mean=1.0
  pass@k=1.0
  pass^k=1.0
  candidate_counts.generated=4
  candidate_counts.scored=4
  candidate_counts.terminal_scored=4
  candidate_counts.blocked_or_unavailable=2
  cad_test_status.passed=4, unavailable=2, total=6
  evaluation_status.scored=4, unavailable=2, total=6
  final_gate_status.passed=4, unavailable=2, total=6
  ```

- `backend/benchmark/metrics.py:420-426` removes non-Boolean verdicts before calculating `pass@k` and `pass^k`.
- `backend/benchmark/metrics.py:537-540` removes cases whose `pass@1.mean` is `None` before calculating the batch mean.
- Consequently, the third case with two unavailable attempts contributes nothing to the top-level pass rates. The JSON says `n_cases=3`, but the pass metrics represent only the two scored cases.
- The generated HTML does show the safer status-based values, including `最终通过 4 / 6` and `Case 成功覆盖率 66.7%（2 / 3 个 Case）`. This means the visual report partially masks the JSON contract defect rather than fixing it.

**Impact:** Any CI, dashboard, or script consuming `report.json` can treat this incomplete batch as 100% successful. This violates the required fixed denominator and makes `pass^k=1.0` claim stability even though one case has zero scored repeats.

**Required follow-up:** Define the denominator semantics explicitly in the JSON schema. For full-batch acceptance, unscored/blocked/unavailable runs must remain in the denominator or the metric must be marked partial with explicit `scored` and `total` fields. Add a regression fixture matching this artifact shape: two cases pass twice, one case is unavailable twice; assert that no overall pass metric can report full-batch 1.0 without a partial/scored indicator.

### P1: The batch is only partially closed; `ctb-00001490` remains unscored in both repeats

- `ctb-00000007`: 2/2 attempts scored and passed.
- `ctb-00001411`: 2/2 attempts scored and passed.
- `ctb-00001490`: 0/2 scored; both attempts ended before candidate sealing and CADTest invocation.
- The first failed attempt contains a Live geometry-validation failure involving non-coaxial cylindrical boundaries.
- The second failed attempt contains:

  ```text
  checks.single-solid.source_quote is absent from objective
  ```

- No `cad_test` evidence, candidate revision, or native-delivery result exists for either attempt.

**Impact:** The successful attempts prove the scoring and delivery chain works for two cases, but they do not establish full dataset or full selected-batch acceptance. The third case still needs a generalized planner/modeling fix or an explicitly documented model failure classification before it can be called evaluated.

**Required follow-up:** Treat this case as an unscored failure, not as a CADTest failure or pass. Investigate the shared source-quote binding and geometry-validation behavior with reusable controls. Do not add a case-specific phrase or bypass the acceptance check. Rerun the case until it reaches either a terminal CADTest `passed`/`failed` result or a documented, intentionally excluded case status.

### P2: Scorer implementation hash is computed but omitted from normalized report evidence

- `backend/app/evaluation/cad_test_runner.py:302-304` computes `scorer_script_sha256`.
- `backend/app/evaluation/cad_test_evidence.py:261-265` stores `result.get("scorer_hash")` as `case.scorer_hash` instead.
- The latest successful report has `case.scorer_hash=null` for all four scored attempts even though the runner computed a script hash. It does contain the scorer image and worker build ID, but not the scorer script identity.

**Impact:** A later scorer-script change can produce reports with the same image/worker-build metadata while the report cannot independently identify the exact repository scorer implementation used. This weakens reproducibility and auditability of the successful evidence.

**Required follow-up:** Use one canonical scorer identity field and propagate the computed scorer script digest into the normalized evidence and batch report. Add an assertion that a terminal scored result cannot omit the scorer implementation identity when the repository-owned scorer is used.

## What Is Now Verified

The latest report closes several previously unverified links for four successful attempts:

- `cad_test.status=passed`, `verdict=true`, and `reason=cad_test_verified`.
- `evaluation_status=scored` and `final_gate_status=passed`.
- `case_match=true`, `dataset_match=true`, `workflow_match=true`, and `candidate_revision_match=true`.
- `artifact_match=true`, with independently recorded artifact and scorer artifact SHA-256 values equal.
- `worker_build_id=cadtest-local-20261128-r1`, `expected_worker_build_id=cadtest-local-20261128-r1`, and `worker_build_match=true`.
- Native delivery is terminally passed with one native solid, one STEP solid, zero symmetric-difference volume, nonzero STL facets, and matching STL bounds.
- The previous `cad_test_configuration_unavailable` condition is absent from the four successful attempts.
- The previous old `fcstd` request-schema error is absent from this report.
- The HTML report correctly groups repeated attempts behind a selector and uses the complete `4/6` denominator for the visible gate summary.

These are real runtime observations from the supplied report, not just unit-test evidence.

## Evaluation Evidence Integrity Gate

- **Admission identity:** `PASS_WITH_LIMITATIONS` for the four scored attempts; all recorded identity matches are true. The unscored case has no candidate identity by design because it failed before sealing.
- **Artifact identity:** `PASS_WITH_LIMITATIONS`; four attempts have independent artifact and scorer hashes matching. No wrong-case or swapped-artifact control was run as part of this batch.
- **Process/result agreement:** `PASS_WITH_LIMITATIONS`; terminal native delivery passed and the scorer path produced terminal results. The supplied batch does not itself exercise a negative non-zero-exit control.
- **Final geometry selection:** `PASS_WITH_LIMITATIONS`; real native delivery reported one selected native solid and exact STEP equivalence for the four successful attempts. Boolean-history and assembly negative controls remain test-level evidence, not part of this batch.
- **Negative controls:** Focused logic controls pass; real wrong-case, modified-artifact, scorer-exception, and non-zero-verifier controls are not evidenced by this batch.
- **No case-specific optimization:** No case-specific exception is accepted by this review. The remaining third-case failure must be addressed through shared planner/validation rules.

## Test Evidence

| Layer | Command or evidence | Result | Classification | Notes |
|---|---|---|---|---|
| Latest batch artifact | `backend/benchmark/reports/cad_test/20261127T184543Z_bc6ae822/report.json` | 4/6 scored and passed; 2/6 unavailable | `FAIL` | Two cases close; third case is unscored in both repeats. |
| HTML report | `backend/benchmark/reports/cad_test/20261127T184543Z_bc6ae822/report.html` | Shows `4 / 6` and `2 / 3` case coverage | `PASS_WITH_LIMITATIONS` | Display denominator is correct; JSON aggregate is not. |
| Focused CADTest/live suite | `python -X utf8 -m pytest tests/test_cad_test_external_recheck.py tests/test_cad_test_pass_gates.py tests/test_cad_test_review_closure.py tests/test_cadtest_live_batch_closure.py tests/test_live_evaluation_benchmark.py tests/test_eval_metrics.py -m "not docker" -q` | `114 passed, 1 skipped, 2 deselected, 1 warning` | `PASS` | Logic evidence; does not close the remaining real case. |
| Feature map | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | Passed | `CONTRACT_ONLY` | 24 domains, 433 referenced paths. |
| Architecture | `python scripts/architecture/check_boundaries.py` | Passed | `CONTRACT_ONLY` | Legacy exceptions remain explicit. |
| Formatting | `git diff --check` | No whitespace errors | `CONTRACT_ONLY` | Line-ending warnings only. |
| Risk scan | `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base origin/main --head HEAD --worktree` | 40 leads | `CONTRACT_ONLY` | Production exception/empty-branch leads reviewed; historical QA artifacts add noise. |

## Acceptance Decision

`FAIL` for full batch merge acceptance, with `PASS_WITH_LIMITATIONS` for the four-attempt runtime scoring path. The important progress is real: the scorer, artifact identity gate, native-delivery verifier, worker-build identity, and repeated-attempt UI all work together in actual execution. The branch still cannot be accepted as a complete batch evaluation because one selected case has no CADTest result in either repeat, and the JSON summary can report 100% pass rates while excluding those unscored attempts.

## Files Written

- `docs/qa/code-review/2026-11-28-20ea31b-codex-closure-rereview.md`

No business code, application tests, deployment files, generated product artifacts, or Git history were modified by this review.
