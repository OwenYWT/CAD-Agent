# CAD-Agent Review: 2026-11-24 99d8d52 codex-cadtest-report-045130

## Conclusion

- Status: `FAIL`
- Reviewer agent: Codex
- Review ID: `codex-cadtest-report-045130`
- Reviewed at: `2026-11-24T21:40:58+08:00`
- Review range: report run `20261124T045130Z_98722749`; implementation at `99d8d527a5e1fd492a6b775367afea2f1c523a3f`
- Repository revision: `99d8d527a5e1fd492a6b775367afea2f1c523a3f`
- Working tree state: branch ahead 1; two unrelated untracked personal-composite-tools design documents
- Business code modified by reviewer: `No`

The report contains useful raw evidence, but its displayed success baseline is not trustworthy yet. A live artifact can be counted as passed when CADTest evidence is absent, the difficulty summary drops two dataset difficulty classes, and every Case-table detail link is malformed.

## Change scope

- Change class: report/evaluation evidence review
- User-described goal: identify missing report presentation, bug-induced failures, weakest capability, and next optimization target
- Reviewed artifacts: `backend/benchmark/reports/cad_test/20261124T045130Z_98722749/report.json` and `report.html`
- Implementation paths: `backend/benchmark/live.py`, `backend/benchmark/metrics.py`, `backend/benchmark/cad_test_report.py`, `backend/app/agent/durable_planner.py`, `backend/app/evaluation/cad_test_observer.py`, `backend/app/workflows/activities.py`
- Functional domains: developer evaluation, durable generation, engineering acceptance, artifact validation

## Implementation trace

| Contract | Implementation path | Consumer | Impact judgment |
| --- | --- | --- | --- |
| Batch run `passed` | `backend/benchmark/live.py:312` | aggregate metrics and HTML report | Missing CADTest evidence can become a false pass |
| Difficulty grouping | `backend/benchmark/metrics.py:584` | JSON summary | Dataset uses `basic/combined/complex`; only `complex` is retained |
| Failure category | `backend/benchmark/cad_test_report.py:304` | HTML failure chips | Most requirement/operation failures fall into the generic delivery bucket |
| Case detail link | `backend/benchmark/cad_test_report.py:400` | HTML Case table | Generated anchor is truncated for every Case |
| 2D intent detection | `backend/app/agent/durable_planner.py:33` | requirement normalization | The word `profile` alone overrides explicit solid/STEP/STL intent |
| CADTest observation | `backend/app/evaluation/cad_test_observer.py:170` and `backend/app/workflows/activities.py:115` | live report | Missing configuration and exceptions can leave no explicit CADTest result |

## Findings

### High: Batch success is fail-open when CADTest is absent

- `benchmark_row_from_live_report()` sets `passed` from finished Live status and artifact gate only. CADTest is required only when a non-empty CADTest block happens to be present.
- Displayed result: 28/300 passed runs, 14/100 successful Cases, 5/100 all-success Cases.
- Strict result requiring `cad_test.status == passed`: 20/300 runs, 12/100 successful Cases, 2/100 all-success Cases.
- Eight displayed pass runs have no CADTest status. Two Cases (`ctb-00000007`, `ctb-00997580`) are false additions to Case coverage.
- One contradictory row is `executed=False`, `WorkflowPending`, `passed=True` (`ctb-00008315`, repeat 1).

### High: Solid prompts containing “profile” can be routed as 2D

- `_has_explicit_2d_output_intent()` treats any occurrence of `profile` as explicit 2D intent even when the request says native solid, extrusion, STEP and STL.
- Six dataset Cases contain `profile`; none has a strict CADTest success.
- `ctb-00037276` requested a solid link-shaped extrusion and STEP/STL, but its Live evidence contains only a DXF profile.
- The current tests cover explicit DXF input but not a 3D extrusion whose text describes a cross-section as a profile.

### High: Requirements classification is the dominant execution bottleneck

- 192/258 error runs fail first in `agent_v2.requirements`.
- 112 runs across 48 Cases fail because a solid request is classified as `profile_2d`.
- This semantic check runs in `_normalize_generation_requirements()` after `Planner.plan_new()` has completed its correction loop, so this specific failure does not receive a model correction attempt.
- Another 37 runs across 27 Cases fail exact `source_quote` binding after planner retries.

### Medium: Difficulty summary silently drops 70 Cases

- Dataset distribution is 30 `basic`, 40 `combined`, 30 `complex`.
- `aggregate_summary()` only enumerates `simple`, `moderate`, `complex`, so JSON contains only the 30 complex Cases under `by_difficulty`.
- Derived strict run results are basic 17/90, combined 3/120, complex 0/90. The report currently hides this most important capability gradient.

### Medium: HTML failure classification is materially misleading

- HTML reports 179 runs as “交付或验收未通过” and only 82 as planning failures.
- Structured evidence shows 192 requirement-stage failures and 33 operation-plan failures.
- The generic category therefore hides the primary root cause and points optimization toward the wrong stage.

### Medium: CADTest observation coverage is incomplete and unexplained

- 37 runs produced readable non-empty STEP/STL; 31 also rendered visually.
- Only 21 runs contain a CADTest result, so scorer coverage among visually complete solid runs is 21/31 (67.7%).
- The observer has silent return paths and its workflow wrapper suppresses exceptions; the report cannot distinguish missing configuration, artifact download failure, scorer failure, or observer exception.

### Medium: All 100 Case-table detail links are malformed

- Generated HTML contains 100 instances of `<td><a href=</tr>` and zero valid `href="#case-..."` links.
- Existing HTML tests do not assert complete detail anchors.

### Medium: Timeout/infrastructure failures are mixed into model accuracy

- Fourteen runs report `WorkflowPending`; median wall time is 218.8 seconds.
- Two runs lasted approximately 43.7 and 52.1 minutes despite a nominal 180-second timeout.
- These should be a separate infrastructure/deadline class and should not be used as direct evidence of modeling accuracy.

## Capability diagnosis

- The worst stage is requirements interpretation, not CADTest geometry comparison.
- Stage cost: requirements 4.03 total hours, mean 49.32 seconds, p95 108.18 seconds; modeling 1.11 total hours, mean 39.48 seconds.
- Once independent CADTest actually runs, 20/21 scored runs pass (95.2%). The single real CADTest failure is `ctb-00670231`, where the hole offset is 0.075 instead of the required 0.150.
- Strict success by difficulty: basic 18.9% per run, combined 2.5%, complex 0%.
- After requirements, the next largest blocker is operation generation: 33 failures, including 27 repeated capability-lookup loops across 21 Cases.

## Report presentation recommendations

1. Add a stage funnel: submitted, requirements passed, model produced, STEP/STL valid, visual rendered, CADTest measured, CADTest passed.
2. Display strict CADTest run pass, Case coverage, and all-repeats stability separately; never let missing CADTest count as pass.
3. Add difficulty cards/table for `basic`, `combined`, `complex` with fixed denominators.
4. Replace broad failure chips with normalized error categories and show run count plus affected Case count.
5. Separate product failures, scorer/observer failures, timeouts, and upstream-not-reached states.
6. Add scorer coverage, stage latency p50/p95, model token totals, and top failing requirement IDs.
7. Add filters for difficulty, failure stage, error type, and “has CADTest result”.

## Recommended optimization order

1. Correct report truthfulness first: fail closed when benchmark CADTest is missing, fix difficulty groups, fix links, and expose observer failures.
2. Improve solid-versus-2D requirement classification and move semantic validation into a correction/retry boundary. Do not weaken the validator or convert arbitrary profiles into boxes.
3. Improve engineering-acceptance generation for exact source binding and schema-valid axes/units/provenance.
4. Fix operation-generator capability lookup loops and invalid operation-plan recovery.
5. Then optimize complex geometry fidelity, using strict CADTest pass rate by difficulty as the main accuracy metric.

## Test evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Static | `git diff --check; git diff --cached --check` | PASS | LOGIC | No whitespace errors |
| Skill map | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | PASS | LOGIC | 24 domains, 433 paths |
| Risk scan | `scan_review_risks.py --base origin/feature/live-cadtest-evaluation --head HEAD --worktree` | PASS_WITH_LEADS | LOGIC | Two leads are in unrelated untracked design docs, not reviewed product code |
| Evaluation | CADTest report/self-contained/live/HTML tests | PASS, 61 tests | LOGIC | Does not cover the confirmed link, difficulty-label, or fail-open defects |
| Planner | `python -m pytest tests/test_durable_agent_planner.py -q` | PASS, 15 tests | LOGIC | No 3D prompt containing the word `profile` case |
| Runtime | Report run `20261124T045130Z_98722749` | FAIL | REAL_ACCEPTANCE | 300 real durable attempts; report semantics contain false positives |

## Gaps and blocked prerequisites

- Worker logs for the ten visually complete runs without CADTest evidence were not retained in this report, so the exact observer/scorer failure cause cannot be assigned per run.
- No new runtime batch was executed during review.
- Historical report evidence cannot prove that later fixes work; a fresh repeated batch is required after remediation.

## Knowledge update

- Review report: `docs/qa/code-review/2026-11-24-99d8d52-codex-cadtest-report-045130.md`
- Review memory: not added; findings are report-specific until fixes are reviewed
- Stable map/invariant update: none
- Reason and evidence: current report and current implementation are sufficient for findings, but no fix has been applied or revalidated
