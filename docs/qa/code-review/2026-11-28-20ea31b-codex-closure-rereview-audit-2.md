# CAD-Agent Review: closure rereview audit 2

## Conclusion

- Status: `FAIL`
- Reviewer agent: Codex
- Review ID: `closure-rereview-audit-2`
- Reviewed at: 2026-11-28 (Asia/Shanghai)
- Review range: current worktree and `origin/main...HEAD`
- Repository revision: `20ea31b`
- Working tree state: dirty; existing user changes and QA artifacts retained
- Business code or application tests modified by reviewer: `No`

The three material findings in `2026-11-28-20ea31b-codex-closure-rereview.md` remain open. The supplied live batch still has only four scored attempts out of six, while the JSON summary reports all three pass metrics as `1.0`; the third case remains unavailable in both repeats. The scorer script digest is still not propagated into normalized CADTest evidence. The recent Worker routing change passes focused logic tests, but no newer real batch report proves runtime recovery or full-batch closure.

## Change Scope

- Change class: CADTest batch metrics, evaluation evidence normalization, and Temporal worker routing recheck
- User-described goal: verify whether findings in the closure rereview are resolved
- Affected paths: `backend/benchmark/metrics.py`, `backend/benchmark/cad_test_report.py`, `backend/app/evaluation/cad_test_runner.py`, `backend/app/evaluation/cad_test_evidence.py`, `backend/app/evaluation/integration.py`, `backend/app/workers/workflow_worker.py`, and related tests/reports
- Functional inventory domains: `12` (inspection, validation, and evidence), `24` (reliability, deployment, and operations; especially `24.12` quality checks)

## Findings

### P1: Overall JSON pass metrics still exclude unscored cases

Confirmed in current `backend/benchmark/metrics.py`: `aggregate_case` filters verdicts to Boolean values before computing `pass@k` and `pass^k`, and `aggregate_summary` excludes cases whose metric is `None` before computing the overall mean. A current-tree synthetic control with two cases passing twice and one case unavailable twice still returns `n_cases=3`, `pass@1.mean=1.0`, `pass@k=1.0`, and `pass^k=1.0`, while status counters correctly report four scored and two unavailable attempts. The real report `backend/benchmark/reports/cad_test/20261127T184543Z_bc6ae822/report.json` has the same contradictory values.

Impact: JSON consumers can accept a partial batch as fully successful unless they independently inspect status counters. This remains a report-contract defect, not merely an HTML presentation issue; HTML's `4/6` and `66.7%` coverage do not correct the JSON summary.

### P1: The latest supplied batch is not fully evaluated

The latest timestamped batch report available in the worktree is `20261127T184543Z_bc6ae822`. It records `ctb-00000007` and `ctb-00001411` as scored and passed in both repeats, but `ctb-00001490` as unavailable in both repeats. The two unavailable attempts stopped before candidate sealing/CADTest scoring: one at geometry validation and one at planner validation (`checks.single-solid.source_quote is absent from objective`). The run therefore covers only `2/3` cases and `4/6` attempts.

Impact: this is an unresolved real-acceptance blocker for the reported batch, not evidence that CADTest scored the third case as failed. No later timestamped batch report is present to establish closure after the Worker routing fix.

### P2: Scorer implementation digest remains absent from normalized evidence

`backend/app/evaluation/cad_test_runner.py` emits `scorer_script_sha256`, while `backend/app/evaluation/cad_test_evidence.py` normalizes only `result.scorer_hash` into `case.scorer_hash`. The attachment path in `backend/app/evaluation/integration.py` does not map those keys. The four scored attempts in the supplied report consequently have `cad_test.case.scorer_hash=null`, despite the runner computing the digest.

Impact: the report identifies the scorer image but cannot identify the exact repository scorer implementation used for those results, weakening reproducibility and auditability.

## Compatibility and Related Impact

- The current Worker builder preserves `TEMPORAL_WORKER_BUILD_ID` while no longer opting into Temporal worker versioning. Its focused regression test passes, but the reviewed batch predates that runtime change; no live Temporal history or subsequent completed batch was used to claim recovery.
- No frontend/backend or persistence contract changes were made by this review. Candidate/artifact matching in the four scored attempts remains supported by the report's matching identity fields.
- The report's garbled Chinese snippets were a PowerShell display-decoding artifact: the UTF-8 source and HTML contain correct Chinese text. No encoding defect was confirmed.

## Test Evidence

| Layer | Command or evidence | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Real batch artifact | `backend/benchmark/reports/cad_test/20261127T184543Z_bc6ae822/report.json` and `report.html` | 4/6 scored; 2/6 unavailable; JSON reports 1.0 pass metrics | `FAIL` | Historical real runtime evidence; latest timestamped batch report available locally |
| Metric reproduction | `python -X utf8 -c "from benchmark.metrics import aggregate_case,aggregate_summary; passed={'executed':True,'passed':True,'model_verdict':True,'evaluation_scope':'cad_test','evaluation_status':'scored','delivery_status':'passed','cad_test_status':'passed','wall_time_ms':1,'attempts':1}; unavailable={'executed':True,'passed':None,'model_verdict':None,'evaluation_scope':'cad_test','evaluation_status':'unavailable','delivery_status':'unavailable','cad_test_status':'unavailable','wall_time_ms':1,'attempts':1}; cases=[{'id':f'case-{i}','runs':[passed.copy(),passed.copy()],'agg':aggregate_case([passed.copy(),passed.copy()])} for i in (1,2)]; runs=[unavailable.copy(),unavailable.copy()]; cases.append({'id':'case-3','runs':runs,'agg':aggregate_case(runs)}); print(aggregate_summary(cases)['overall'])"` | Three cases, four passes, two unavailable still produce all three overall pass metrics as `1.0` | `LOGIC` | Direct current-code reproduction |
| Focused regression suite | `python -X utf8 -m pytest tests/test_eval_metrics.py tests/test_cad_test_evidence.py tests/test_cadtest_live_batch_closure.py -q` | `52 passed, 1 warning` | `LOGIC` | Warning is the existing Pydantic `model_kind` protected-namespace warning |
| Feature map | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | Passed; 24 domains, 433 referenced paths | `CONTRACT_ONLY` |  |
| Architecture boundaries | `python scripts/architecture/check_boundaries.py` | Passed | `CONTRACT_ONLY` | Legacy exceptions remain explicit |
| Risk scanner | `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base origin/main --head HEAD --worktree` | 41 leads across 733 paths | `CONTRACT_ONLY` | Leads are dominated by historical QA artifacts; relevant production paths were manually traced |
| Whitespace | `git diff --check` | No whitespace errors | `CONTRACT_ONLY` | Git reported existing LF-to-CRLF working-copy notices |
| New real batch after Worker fix | Not available | Not verified | `BLOCKED` | Requires restarting the updated Worker and completing a new full batch |

## Acceptance Decision

`FAIL` for full batch acceptance. Findings P1 (partial JSON metrics) and P2 (missing scorer implementation identity) remain reproducible in current code. The real batch report independently remains incomplete because one selected case has no scored result in either repeat. Unit tests and the Worker routing change do not substitute for a new completed live batch.

## Files Written

- `docs/qa/code-review/2026-11-28-20ea31b-codex-closure-rereview-audit-2.md`

No business code, application tests, deployment files, generated product artifacts, or Git history were modified by this review.
