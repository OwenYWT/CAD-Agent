# CAD-Agent Review: 2026-11-27 restarted Worker runtime re-review

## Conclusion

- Status: `FAIL`
- Reviewer agent: Codex
- Review ID: `runtime-rereview-3`
- Reviewed at: 2026-11-28 (Asia/Shanghai)
- Review range: current worktree against `origin/feature/live-cadtest-evaluation`
- Repository revision: `20ea31b5d0d5faf766be2b178c33c374e7559154`
- Working tree state: dirty; existing user changes and QA artifacts were retained
- Business code or application tests modified by reviewer: `No`

Restarting a Worker changed the execution trace, but did not close the evaluation loop. The new report contains real FreeCAD geometry evidence for one run, yet all five runs still end with `cad_test_status=unavailable`, `scored=0`, and no sealed candidate accepted by the CADTest gate. Four runs still execute an old `fcstd`-rejecting validation path, which strongly indicates that old and new Workers are mixed on the Temporal task queue or that different activity handlers are running different revisions. The fifth independent failure is a planner acceptance-contract rejection.

## Scope

- Evidence supplied: `backend/benchmark/reports/cad_test/20261127T160733Z_692ca187/report.json` and `report.html`.
- Change class: CADTest/live evaluation closure, Worker runtime compatibility, planner acceptance validation, artifact provenance, native delivery, and result aggregation.
- Affected feature areas: Temporal activity execution, candidate lifecycle, Live geometry evidence, CADTest observer/runner, native delivery, and batch reporting.

## Findings

### P1: Old and new Worker revisions are still mixed in the live task execution path

- The latest report has five single-attempt runs. Four cases still fail with the old request-model error:

  ```text
  McadAgentWorkflowV2Request.output_formats.2
  Input should be 'step', 'stl', 'dxf' or 'svg'
  input_value='fcstd'
  ```

- For `ctb-00000007`, the Live timeline shows two failed `agent_v2.plan` attempts with that old validator, then a successful third plan attempt, followed later by the same validation error in `agent_v2.judge_visual`, followed by `terminate_candidate` with `status=failed`.
- This is no longer explained by a single uniformly old Worker. The same workflow progressed through `execute_freecad` and `validate_geometry`, while old-schema validation errors appeared in other activity attempts. The evidence is consistent with multiple Worker revisions consuming the same Temporal task queue, or with separate activity services/images not updated together.
- Current checked-out source at `backend/app/models/workflow_requests.py:305` includes `fcstd`, and the focused contract test accepts the 3D batch format. The runtime report therefore does not represent one consistent current-code deployment.
- Impact: a generated STEP/STL geometry observation can exist, but candidate termination fails and no candidate revision is sealed. CADTest must not score this run. Restarting only one Worker is insufficient if another old Worker remains registered on the queue.
- Required follow-up: quiesce the task queue, stop all old Worker processes/containers, recreate every Worker/activity image from the same revision, and verify the active Worker set before rerunning. A one-case run must show no old `fcstd` schema error in any activity before the batch is expanded.

### P1: No run reaches terminal CADTest or native-delivery evaluation

- Aggregate values in `backend/benchmark/reports/cad_test/20261127T160733Z_692ca187/report.json` are:

  ```text
  generated=0
  scored=0
  terminal_scored=0
  cad_test_status.unavailable=5
  evaluation_status.unavailable=5
  delivery_status.unavailable=5
  final_gate_status.unavailable=5
  ```

- The HTML report correctly shows `最终通过 0 / 5` and every attempt as `阻断`.
- `ctb-00000007` has Live geometry evidence (`geometry_status=passed`, STEP/STL evidence, and `artifact_gate.passed=true`), but its Live report has `candidate=None`, `cad_test={}`, and `lifecycle_status=failed`. This is process-level geometry evidence only; it is not a sealed candidate or a CADTest pass.
- Impact: the report still cannot validate independent artifact identity, scorer hash binding, scorer exit-code agreement, native FCStd verification, final-object selection, or final gate behavior in a successful/failed terminal scoring path.
- Required follow-up: produce a new minimal run with candidate revision/seal identity, terminal CADTest result, independent artifact hash/size, native delivery result, and final gate status. A Live geometry pass before candidate termination is insufficient for acceptance.

### P1: `ctb-00001615` is blocked by planner acceptance-source binding

- The objective explicitly contains the requested rectangular cut:

  ```text
  Remove a rectangular block of X length 0.6/2.55, Y width 0.6 and Z height 1.5/5.125 from the upper positive-X side.
  ```

- The run fails before candidate generation with:

  ```text
  planner model did not return a valid CAD plan:
  checks.rect_cut_bounds.source_quote is absent from objective
  ```

- This is separate from the Worker `fcstd` issue. The schema is rejecting the model-generated acceptance evidence because its `source_quote` does not bind to the objective text. The strict rejection is preferable to silently accepting an unbound check, but the case remains unscored and the report cannot close.
- Required follow-up: inspect the planner output/source-span normalization for rectangular-cut bounds. The generalized fix must preserve exact source binding for equivalent wording without adding a case-specific exception or weakening the provenance invariant.

## Resolved Items Verified

- The previous `passed=None` versus `False` report contract is fixed in this artifact: blocked runs expose `passed=false`.
- The previous generic timeout classification is fixed: runs expose `evaluation_reason=live_report_failed` and retain the concrete underlying error.
- The focused non-container regression suite remains green.

## Test Evidence

| Layer | Command or evidence | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Batch artifact | `backend/benchmark/reports/cad_test/20261127T160733Z_692ca187/report.json` | 5/5 unavailable before CADTest scoring | `FAIL` | Four old-schema failures; one planner acceptance failure. |
| Live process evidence | `ctb-00000007` raw Live report | Geometry evidence passed, lifecycle failed | `CONTRACT_ONLY` | STEP/STL evidence exists, but no candidate seal or CADTest result. |
| Focused logic | `python -X utf8 -m pytest tests/test_cad_test_external_recheck.py tests/test_cad_test_pass_gates.py tests/test_cad_test_review_closure.py tests/test_cadtest_live_batch_closure.py tests/test_live_evaluation_benchmark.py tests/test_eval_metrics.py -m "not docker" -q` | `110 passed, 1 skipped, 2 deselected` | `PASS` | Container tests excluded; this does not prove real runtime closure. |
| Feature map | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | Passed | `CONTRACT_ONLY` | 24 domains, 433 referenced paths. |
| Architecture | `python scripts/architecture/check_boundaries.py` | Passed | `CONTRACT_ONLY` | Legacy exceptions remain explicit. |
| Formatting | `git diff --check` | Passed with line-ending warnings | `CONTRACT_ONLY` | No whitespace errors. |
| Risk scan | `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base origin/feature/live-cadtest-evaluation --head HEAD --worktree` | 18 findings | `CONTRACT_ONLY` | Production exception/empty-branch leads were manually reviewed; remaining findings are primarily historical QA artifacts. |
| Real CADTest/FreeCAD | Terminal scorer and native-delivery result | Not available | `BLOCKED` | FreeCAD geometry observation ran, but CADTest was never invoked. |

## Evaluation Evidence Integrity Gate

- Admission identity: the runtime never reached a sealed CADTest admission for any case.
- Independent artifact identity: Live validation evidence exists for one failed lifecycle, but no candidate seal was accepted and no scorer hash comparison ran.
- Process/result agreement: no CADTest/native verifier process ran in this batch.
- Final geometry selection: no terminal native-delivery check ran; the Live geometry observation must not be treated as its substitute.
- Negative controls: focused logic controls pass, but real swapped/modified artifact and native-history/assembly controls remain unexecuted after the runtime fix.
- No case-specific optimization: the observed planner failure requires a generalized source-binding investigation, not a case-specific exemption.

## Acceptance Decision

`FAIL`. The report proves partial progress after restarting a Worker, and the reporting-contract fixes are valid. It does not prove a closed CADTest loop because old and new runtime behavior are still mixed, no case reached CADTest scoring, and one case has an independent planner acceptance failure. Stop all old queue consumers, recreate the complete Worker set from one revision, fix the generalized planner source-binding path, and rerun one case until a terminal CADTest/delivery record exists.

## Files Written

- `docs/qa/code-review/2026-11-28-20ea31b-codex-runtime-rereview-3.md`

No business code, application tests, deployment files, generated product artifacts, or Git history were modified by this review.
