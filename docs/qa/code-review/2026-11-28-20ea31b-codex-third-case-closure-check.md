# CAD-Agent Review: 2026-11-28 20ea31b third-case-closure-check

## Conclusion

- Status: `BLOCKED`
- Reviewer agent: Codex
- Review ID: `third-case-closure-check`
- Reviewed at: 2026-11-28 Asia/Shanghai
- Review range: current `HEAD` plus working-tree implementation for live CADTest aggregation and batch exit behavior; historical run evidence `20261127T184543Z_bc6ae822`
- Repository revision: `20ea31b`
- Working tree state: pre-existing dirty worktree; unrelated changes preserved
- Business code modified by reviewer: `No`

The concern is valid as an acceptance/closure blocker for the cited batch, but it does not justify converting unscored attempts into CADTest failures or changing pass-rate denominators to include them. The cited batch has 4 scored passes and 2 unavailable attempts (4/6 score coverage); it is not a fully closed evaluation. Current live-batch code returns exit code 1 unless every selected row has `passed is True`, so this batch cannot be accepted by relying on the CLI exit code. Current summary code also exposes scored-vs-total case/run coverage. The cited historical JSON predates that coverage field and still shows a 100% rate over only the scored subset.

### Finding: selected runs remain unscored

- Severity: acceptance blocker for this run; no separate metric-logic defect confirmed in current code.
- Evidence: `backend/benchmark/reports/cad_test/20261127T184543Z_bc6ae822/report.json` records 6 selected attempts, 4 `scored`/`passed`, and 2 `unavailable`; the two `ctb-00001490` attempts have live statuses `failed` and no CADTest verdict. One failed during geometry validation before a sealed candidate was scored; the other failed planner validation because the generated `source_quote` was absent from the objective.
- Impact: the 100% pass rate in this historical report describes the scored subset only. It is not evidence that all six attempts completed evaluation, nor a representative complete-batch result.
- Current behavior: `backend/benchmark/metrics.py` keeps unavailable verdicts out of pass-rate denominators, retains status counts, and emits `pass_metric_coverage`. `backend/benchmark/cad_test_batch.py` returns nonzero when any selected row is not passed. These are the correct semantics: `unavailable` is not a CADTest `failed` result.
- Follow-up: rerun the affected case(s) after addressing the generation/validation failures and obtain a real terminal scorer verdict for each intended sample. Report acceptance should require no `pending`/`unavailable` selected attempts, while allowing any number of legitimately scored model failures.

## Change scope

- Change class: focused evaluation-closure review
- User-described goal: determine whether the third-case finding still requires a code change
- Changed paths: none in business code; this review report only
- Functional inventory domains: evaluation evidence and reliability/operations (domains 12 and 24)

## Implementation trace

| Changed symbol or contract | Implementation path | Consumers | Impact judgment |
| --- | --- | --- | --- |
| Pass metric aggregation and coverage | `backend/benchmark/metrics.py` (`aggregate_case`, `aggregate_summary`) | batch JSON/HTML summaries | Unscored rows remain visible and are excluded from scored pass rates; coverage states their denominator impact. |
| Live batch success contract | `backend/benchmark/cad_test_batch.py` (`_live`) | CLI/pipeline exit status | Any selected row not explicitly passed yields exit code 1. |

## Findings

### Encoding and fake implementation review

- Scanner result: not run; this review was scoped to the recorded CADTest batch and its completion contract.
- Confirmed findings: the historical batch is incomplete at 4/6 scored attempts.
- False-positive leads dismissed with context: requiring every model attempt to pass would conflate model accuracy with evaluation closure. A legitimate scored `failed` verdict must remain a valid terminal evaluation result.

### Compatibility review

- Frontend/backend contract: not applicable to this batch-level finding.
- WebSocket/event contract: not re-reviewed; historical evidence shows workflow terminal failures, not a transport pass.
- Persistence/migration/RLS: not applicable.
- Workflow/retry/cancellation: this batch runner does not automatically retry generation failures; unavailable rows are retained and fail the batch exit gate.
- Revision/candidate/draft/artifact identity: unavailable rows have no completed CADTest verdict; they cannot be treated as sealed/scored artifacts.
- Permissions and external boundaries: not applicable.

## Test evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Logic | `python -X utf8 -m pytest tests/test_eval_metrics.py tests/test_cad_test_evidence.py tests/test_cadtest_live_batch_closure.py tests/test_live_evaluation_benchmark.py tests/test_cad_test_report_html.py -q` (from `backend`) | PASS: 88 passed | LOGIC | Includes partial-coverage aggregation and unavailable-sample behavior. One existing Pydantic protected-namespace warning. |
| Repository contract | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | PASS | CONTRACT_ONLY | 24 domains and 433 referenced paths valid. |
| Working-tree hygiene | `git diff --check` | PASS | CONTRACT_ONLY | Only line-ending warnings for existing modified files. |
| Runtime/product | Historical report inspection | LIMITATION | Historical evidence | Confirms 4/6 scored; does not establish a fresh runtime rerun or close the two unavailable attempts. |

## Gaps and blocked prerequisites

- The 2026-11-27 report was generated before `pass_metric_coverage` was added; it must not be reused as a complete-coverage result.
- No fresh live rerun was performed in this review. The two cited failures need new runs that reach an authoritative CADTest `passed` or `failed` verdict before this batch can be called fully closed.
- A 3-case/6-attempt sample is not a 100-case baseline, regardless of its score coverage.

## Knowledge update

- Review report: `docs/qa/code-review/2026-11-28-20ea31b-codex-third-case-closure-check.md`
- Review memory: not added; this is a case-specific confirmation.
- Stable map/invariant update: none.
- Reason and evidence: current implementation semantics and the cited run report were sufficient to classify the finding without changing business behavior.
