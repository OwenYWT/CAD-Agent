# CAD-Agent Review: 2026-11-24 99d8d52 codex-cadtest-report-corrections

## Conclusion

- Status: `PASS_WITH_LIMITATIONS`
- Reviewer agent: Codex
- Review ID: `codex-cadtest-report-corrections`
- Reviewed at: 2026-11-24 22:09:37 +08:00
- Review range: `HEAD` plus current working tree
- Repository revision: `99d8d52`
- Working tree state: six modified benchmark/test files; three unrelated untracked documents; no staged changes
- Business code modified by reviewer: `No`

## Change scope

- Change class: CADTest batch evaluation correctness and developer report presentation
- User-described goal: correct deterministic report defects before optimizing generation behavior
- Changed paths: `backend/benchmark/live.py`, `backend/benchmark/metrics.py`, `backend/benchmark/cad_test_report.py`, and their focused tests
- Functional inventory domains: developer-only benchmark/evaluation tooling; no frontend, API, persistence, workflow admission, or revision contract changed

## Implementation trace

| Changed symbol or contract | Implementation path | Consumers | Impact judgment |
| --- | --- | --- | --- |
| Strict batch pass gate | `benchmark.live.benchmark_row_from_live_report` | CADTest live batch rows and aggregate metrics | Missing CADTest evidence now fails closed without changing delivery evidence |
| Dynamic difficulty aggregation | `benchmark.metrics.aggregate_summary` | JSON summary and report consumers | Preserves existing labels and includes dataset-defined labels such as `basic` and `combined` |
| Historical strict status derivation | `benchmark.cad_test_report._strict_run_passed` | Summary, case table, failure summary, attempt details | Ignores stale `run.passed` and derives display state from recorded evidence |
| Failure categorization | `benchmark.cad_test_report._failure_category` | HTML failure summary | Uses the first failed tool call before error-text fallback |
| Difficulty and coverage display | `benchmark.cad_test_report.render_html` | Developer-only static HTML report | Adds stable success, CADTest observation coverage, and strict difficulty breakdown |

## Findings

### Encoding and fake implementation review

- Scanner result: two leads, both in unrelated untracked `personal-composite-tools` planning documents.
- Confirmed findings: none in the six changed benchmark or test files.
- False-positive leads dismissed with context: planning-language mentions of fixtures and placeholder scanning are not product implementation and were not modified.
- UTF-8 verification: regenerated target HTML contains zero Unicode replacement characters.
- Fake-success verification: a finished delivery-ready run without CADTest evidence is explicitly tested as `passed=False`.

### Compatibility review

- Frontend/backend contract: no endpoint, UI, or WebSocket contract changed.
- WebSocket/event contract: no producer or reducer changed.
- Persistence/migration/RLS: not affected.
- Workflow/retry/cancellation: generation behavior is unchanged; only benchmark row classification consumes existing evidence.
- Revision/candidate/draft/artifact identity: not affected.
- Permissions and external boundaries: report remains developer-only and self-contained; no external CADTest source dependency added.

## Test evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Logic | `python -m pytest tests/test_live_evaluation_benchmark.py tests/test_eval_metrics.py tests/test_cad_test_report_html.py -q` | 61 passed | PASS | Includes strict pass, dynamic difficulty, link, failure-category, stable-success, and coverage regressions |
| Regression | `python -m pytest tests/test_eval_cases.py tests/test_eval_metrics.py tests/test_live_evaluation_benchmark.py tests/test_cad_test_report_html.py tests/test_cad_test_self_contained.py -q` | 96 passed | PASS | One pre-existing Pydantic protected-namespace warning |
| Static | `python -m compileall -q benchmark app/evaluation` | passed | PASS | No syntax/import compilation failure |
| Repository | `git diff --check` | passed | PASS | Only line-ending notices from the Windows checkout |
| Skill integrity | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | passed | PASS | 24 domains and 433 referenced paths valid |
| Product artifact | Regenerated `20261124T045130Z_98722749/report.html` | passed | CONTRACT_ONLY | 20/300 strict runs, 12/100 Case coverage, 2/100 stable, 21/31 CADTest coverage; 100 valid detail links and zero malformed links |

## Gaps and blocked prerequisites

- A new 300-run real CADTest batch was not executed. The review verifies corrected classification against the existing authoritative JSON plus automated tests, not fresh model/runtime acceptance.
- Generation routing and planner retry behavior were intentionally excluded from this phase.

## Knowledge update

- Review report: `docs/qa/code-review/2026-11-24-99d8d52-codex-cadtest-report-corrections.md`
- Review memory: none
- Stable map/invariant update: none
- Reason and evidence: behavior is task-specific and fully covered by production tests; no canonical feature-map change is required.
