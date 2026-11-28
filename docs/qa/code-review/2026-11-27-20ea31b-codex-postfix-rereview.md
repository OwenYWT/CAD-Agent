# CAD-Agent Review: 2026-11-27 postfix CADTest closure re-review

## Conclusion

- Status: `FAIL`
- Reviewer agent: Codex
- Review ID: `postfix-rereview`
- Reviewed at: 2026-11-27 (Asia/Shanghai)
- Review range: current worktree against `origin/feature/live-cadtest-evaluation`
- Repository revision: `20ea31b5d0d5faf766be2b178c33c374e7559154`
- Working tree state: dirty; existing user changes and QA artifacts were retained
- Business code or application tests modified by reviewer: `No`

The latest report does not demonstrate a closed CADTest loop. All 10 runs are stopped before candidate generation and before CADTest scoring because the running Worker rejects the `fcstd` format. The local source currently accepts `fcstd`, which indicates a stale Worker/container or a version-mismatched runtime. In addition, the focused review suite still has two contract failures around `passed=None` versus `False`. The result cannot be accepted until the runtime is updated and a new run reaches terminal CADTest and native-delivery states.

## Scope

- Change class: CADTest/live evaluation closure, artifact provenance, native delivery gates, batch reporting, and runtime compatibility.
- Evidence supplied: `backend/benchmark/reports/cad_test/20261127T145748Z_93f61cda/report.json` and its HTML report.
- Affected feature areas: live benchmark admission, Temporal Worker request validation, CADTest observer/runner, candidate sealing, native delivery, and batch result aggregation.

## Findings

### P1: The latest batch is blocked by a stale or mismatched Worker contract

- Evidence: `backend/benchmark/reports/cad_test/20261127T145748Z_93f61cda/report.json` records 5 cases with `repeat=2`, for 10 total runs. It reports `generated=0`, `scored=0`, `cad_test_status.unavailable=10`, `evaluation_status.unavailable=10`, `delivery_status.unavailable=10`, and `final_gate_status.unavailable=10`.
- Every run contains the same validation error:

  ```text
  McadAgentWorkflowV2Request.output_formats.2
  Input should be 'step', 'stl', 'dxf' or 'svg'
  input_value='fcstd'
  ```

- The current source at `backend/app/models/workflow_requests.py:305` declares `fcstd` in the `McadAgentWorkflowV2Request.output_formats` literal, and a direct local construction test succeeds with `('step', 'stl', 'fcstd')`.
- The batch caller at `backend/benchmark/live.py:420-422` intentionally adds `fcstd` for required 3D CADTest runs.
- Impact: the batch uses the repaired admission contract, but the Worker that processed the run uses the old contract. No artifact, candidate seal, independent scorer result, or native-delivery result exists in this report. This is a hard runtime blocker, not a CAD geometry failure and not evidence that the new scorer gates passed.
- Required follow-up: stop and recreate the Worker using the current source/image, verify the actual Worker-loaded `workflow_requests.py` accepts `fcstd`, then run `--limit 1 --repeat 1` before starting a larger batch. The new report must show candidate generation, sealed artifact identity, a terminal CADTest status, and native-delivery evidence.

### P1: The public `passed` contract is still inconsistent and the focused review suite fails

- Locations: `backend/benchmark/live.py:350-369`, `backend/tests/test_cad_test_external_recheck.py:126`, and `backend/tests/test_cad_test_external_recheck.py:171`.
- Reproduction command:

  ```text
  cd backend
  python -X utf8 -m pytest tests/test_cad_test_external_recheck.py tests/test_cad_test_pass_gates.py tests/test_cad_test_review_closure.py tests/test_cadtest_live_batch_closure.py -m "not docker" -q
  ```

- Result: `2 failed, 57 passed, 1 skipped, 2 deselected`.
- Both failures expect `row["passed"] is False` for required evaluations that are not terminally scored. The current implementation returns `None` whenever `evaluation_status != "scored"`.
- Impact: JSON consumers and the external review contract disagree about whether an unverified required evaluation is false or indeterminate. HTML strict evaluation treats it as non-pass, but downstream code must currently understand the separate `None` state. This remains a repository-level test failure and is not closed by the latest runtime report.
- Required follow-up: choose one explicit contract and update all producers, consumers, and tests together. If `None` remains valid, the external checks must assert the status fields and strict gate rather than treating `passed` as a boolean. If `passed` is required to be boolean, use `False` for unverified required evaluations while preserving `model_verdict=None` and `evaluation_status=unavailable`.

### P2: Failure classification hides the immediate runtime cause

- In the latest report a representative run has `live_report_status=failed` and the concrete `fcstd` validation error, but also carries `live_report_error="live report is not finished"` and `evaluation_reason="cad_test_observation_timeout"`.
- Impact: the report correctly does not claim a pass, but its headline evaluation reason points to an observation timeout instead of the request-schema rejection that stopped the workflow. This makes triage slower and can mislead operators into increasing the CADTest wait time when the Worker must actually be updated.
- Required follow-up: preserve and prioritize the terminal workflow/request error when the Live report is failed; only use `cad_test_observation_timeout` when a still-running report genuinely reaches the observation deadline without a terminal CADTest result.

### P2: Output-format validation remains duplicated across request models

- `backend/app/models/workflow_requests.py` accepts `fcstd` for the durable v2 request, while `backend/app/models/schemas.py:252-260` keeps the REST `_VALID_FORMATS` set at `step/stl/dxf/svg`.
- This did not cause the supplied error, which names `McadAgentWorkflowV2Request`, and the REST API may intentionally expose a different public format contract. However, the duplicated format sets are a contract-drift risk for any path that needs to request native FCStd output.
- Required follow-up: document the boundary or centralize the format policy, and add a test proving which public and internal paths may request `fcstd`. Do not silently broaden the REST API without confirming its artifact and permission semantics.

## Evaluation Evidence Integrity Gate

- Admission identity: source changes and logic tests cover benchmark case/dataset/route binding, but the supplied runtime run never reached scoring.
- Independent artifact identity: the reviewed implementation has local hash/size and sealed-candidate checks; this report contains no artifact on which to exercise them.
- Process/result agreement: logic coverage exists for scorer and native-verifier exit-code disagreement; no terminal runtime result is present here.
- Final geometry selection: repository final-object selection is covered by logic controls; no FreeCAD Boolean/history or assembly runtime control ran in this batch.
- Negative controls: unit/logic controls exist, but wrong-case, swapped-artifact, modified-artifact, scorer mismatch, and native-delivery controls have not been proven by a post-fix real runtime report.
- No case-specific optimization: the reviewed fixes are generalized identity, route, and result-contract logic. The current batch is blocked before those invariants execute.

## Test Evidence

| Layer | Command or evidence | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Batch artifact | `backend/benchmark/reports/cad_test/20261127T145748Z_93f61cda/report.json` | 10/10 unavailable before generation | `FAIL` | Same `fcstd` validation error; zero generated/scored candidates. |
| Focused logic | `python -X utf8 -m pytest tests/test_cad_test_external_recheck.py tests/test_cad_test_pass_gates.py tests/test_cad_test_review_closure.py tests/test_cadtest_live_batch_closure.py -m "not docker" -q` | 2 failed, 57 passed, 1 skipped, 2 deselected | `FAIL` | `passed=None` versus `False` contract failures. |
| Request model | Direct `McadAgentWorkflowV2Request` construction with `('step','stl','fcstd')` | Passed locally | `CONTRACT_ONLY` | Confirms checked-out source differs from the Worker that produced the report. |
| Feature map | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | Passed | `CONTRACT_ONLY` | 24 domains, 433 referenced paths. |
| Architecture | `python scripts/architecture/check_boundaries.py` | Passed | `CONTRACT_ONLY` | Legacy exceptions remain explicit. |
| Formatting | `git diff --check` | Passed with line-ending warnings | `CONTRACT_ONLY` | No whitespace errors. |
| Real CADTest/FreeCAD | Post-fix terminal scorer and native-delivery evidence | Not available | `BLOCKED` | The supplied run stopped before artifact creation; local Podman is also unavailable in this environment. |

## Acceptance Decision

`FAIL`. The report is useful evidence that the running Worker is stale or version-mismatched, but it is not a CADTest result and cannot validate the repaired provenance, scorer, native-delivery, or final-gate path. After the Worker is recreated from the current code, rerun one 3D case once, confirm the report contains terminal CADTest and delivery records, resolve the two focused test failures, and only then rerun the larger batch.

## Files Written

- `docs/qa/code-review/2026-11-27-20ea31b-codex-postfix-rereview.md`

No business code, application tests, deployment files, or Git history were modified by this review.
