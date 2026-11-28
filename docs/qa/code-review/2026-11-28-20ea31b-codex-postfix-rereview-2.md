# CAD-Agent Review: 2026-11-27 runtime postfix re-review

## Conclusion

- Status: `FAIL`
- Reviewer agent: Codex
- Review ID: `postfix-rereview-2`
- Reviewed at: 2026-11-28 (Asia/Shanghai)
- Review range: current worktree against `origin/feature/live-cadtest-evaluation`
- Repository revision: `20ea31b5d0d5faf766be2b178c33c374e7559154`
- Working tree state: dirty; existing user changes and QA artifacts were retained
- Business code or application tests modified by reviewer: `No`

The latest report does fix two reporting issues from the previous review: all blocked runs now expose `passed=false`, and the evaluation reason is `live_report_failed` instead of the misleading observation-timeout reason. The runtime loop is still not closed, however. All 10 runs stop at the Worker request-validation stage because the running Worker rejects `fcstd`; no candidate, artifact seal, CADTest scorer result, or native-delivery result exists.

## Scope

- Evidence supplied: `backend/benchmark/reports/cad_test/20261127T155443Z_40b2488b/report.json` and `report.html`.
- Change class: CADTest/live evaluation closure, runtime format compatibility, artifact provenance, native delivery, and report status handling.
- Affected feature areas: benchmark admission, Temporal Worker request validation, CADTest observer/runner, candidate sealing, native delivery, and result aggregation.

## Findings

### P1: The real Worker still runs an incompatible `output_formats` contract

- The latest batch manifest contains 5 cases with `repeat=2`, producing 10 runs.
- Aggregate evidence from `backend/benchmark/reports/cad_test/20261127T155443Z_40b2488b/report.json`:

  ```text
  generated=0
  scored=0
  terminal_scored=0
  cad_test_status.unavailable=10
  evaluation_status.unavailable=10
  delivery_status.unavailable=10
  final_gate_status.unavailable=10
  ```

- Every run has `live_report_status=failed`, `evaluation_reason=live_report_failed`, `passed=false`, and the same error:

  ```text
  McadAgentWorkflowV2Request.output_formats.2
  Input should be 'step', 'stl', 'dxf' or 'svg'
  input_value='fcstd'
  ```

- The checked-out source at `backend/app/models/workflow_requests.py:305` includes `fcstd` in the v2 request literal. The batch caller at `backend/benchmark/live.py:420-422` intentionally adds `fcstd` for required 3D CADTest runs. Therefore the report was produced by a Worker/container that has not loaded the current request model, or by a different deployed revision.
- Impact: this is still a hard pre-scoring blocker. The report proves that the system refuses the benchmark's native-output request, but it does not test candidate sealing, independent artifact hashing, scorer identity checks, native delivery, final-object selection, or the final gate.
- Required follow-up: recreate/restart the actual Worker from the current code/image, verify inside that Worker that `McadAgentWorkflowV2Request` accepts `('step', 'stl', 'fcstd')`, then run one 3D case with `--limit 1 --repeat 1`. Do not use this report as runtime acceptance evidence.

### P1: Runtime acceptance evidence is still absent

- The latest report contains no generated candidate and no artifact records. Its HTML correctly shows every attempt as `阻断`, with an empty `CADTest 与交付结果` section.
- The focused logic suite is now green, but that is not a substitute for a real Podman/FreeCAD run. The evaluation integrity gate requires a terminal scorer result and native-delivery evidence before accepting the repaired path.
- Required post-fix evidence must include, for at least one run: authoritative case/dataset/route identity, candidate/revision identity, sealed artifact identity, independently computed artifact hash/size, terminal CADTest status, scorer result/exit agreement, native delivery status, and final gate status. Negative controls for swapped or modified artifacts must remain green.

## Resolved Items Verified

- `passed` contract: the latest report has `false` for all 10 unscored required runs. The previous focused failures are no longer present.
- Failure classification: the latest report uses `evaluation_reason=live_report_failed` and preserves the concrete schema validation error. It no longer labels this immediate rejection as `cad_test_observation_timeout`.

## Test Evidence

| Layer | Command or evidence | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Batch artifact | `backend/benchmark/reports/cad_test/20261127T155443Z_40b2488b/report.json` | 10/10 blocked before generation | `FAIL` | Same Worker `fcstd` validation error; zero candidates and zero scores. |
| Focused logic | `python -X utf8 -m pytest tests/test_cad_test_external_recheck.py tests/test_cad_test_pass_gates.py tests/test_cad_test_review_closure.py tests/test_cadtest_live_batch_closure.py tests/test_live_evaluation_benchmark.py tests/test_eval_metrics.py -m "not docker" -q` | `110 passed, 1 skipped, 2 deselected` | `PASS` | Report status and gate contract tests now pass; Docker tests were excluded. |
| Feature map | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | Passed | `CONTRACT_ONLY` | 24 domains, 433 referenced paths. |
| Architecture | `python scripts/architecture/check_boundaries.py` | Passed | `CONTRACT_ONLY` | Legacy exceptions remain explicit. |
| Formatting | `git diff --check` | Passed with line-ending warnings | `CONTRACT_ONLY` | No whitespace errors. |
| Risk scan | `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base origin/feature/live-cadtest-evaluation --head HEAD --worktree` | 18 findings | `CONTRACT_ONLY` | Production `pass`/exception leads were manually reviewed as intentional evaluator isolation or error conversion; most remaining leads are historical QA artifacts. |
| Real CADTest/FreeCAD | Post-fix terminal scorer and native-delivery evidence | Not available | `BLOCKED` | The supplied run stops before artifact creation. |

## Evaluation Evidence Integrity Gate

- Admission identity: logic coverage exists, but the runtime request never reaches the admission-to-candidate path with the current format contract.
- Independent artifact identity: implementation tests exist; no artifact is present in this report to exercise the checks.
- Process/result agreement: implementation tests exist; no scorer or verifier process ran in this report.
- Final geometry selection: logic controls exist; no FreeCAD Boolean/history/assembly runtime control ran here.
- Negative controls: focused logic controls pass; real post-fix container controls remain unexecuted.
- No case-specific optimization: the reviewed logic changes are generalized, but the real runtime has not reached them.

## Acceptance Decision

`FAIL`. The reporting fixes are validated, and the focused non-container regression suite passes. The actual CADTest evaluation loop is still blocked by the running Worker version mismatch. Recreate the Worker, run one minimal 3D case, and provide a report with terminal CADTest and delivery evidence before accepting the larger batch.

## Files Written

- `docs/qa/code-review/2026-11-28-20ea31b-codex-postfix-rereview-2.md`

No business code, application tests, deployment files, generated product artifacts, or Git history were modified by this review.
