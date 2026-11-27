# CAD-Agent Review: merge blockers for CADTest/live evaluation

## Conclusion

- Status: `FAIL`
- Reviewer agent: Codex
- Review ID: `merge-blockers`
- Reviewed at: 2026-11-28 (Asia/Shanghai)
- Review range: current worktree and `origin/main...HEAD`
- Repository revision: `20ea31b fix: register CADTest evaluation boundaries`
- Working tree state: dirty; existing user changes and QA artifacts were retained
- Business code, application tests, deployment files, and Git history modified by reviewer: `No`

The branch is not ready to merge. The latest batch report contains no terminal CADTest score: `generated=2`, `scored=0`, `terminal_scored=0`. Two generated candidates are blocked by unavailable CADTest configuration, and the remaining three cases fail before the scorer is reached. The focused logic suite is green, but the required real scorer/FreeCAD acceptance chain is still unproven.

## Scope

This review covers the CADTest admission and scoring path, live-process evidence, native delivery verification, worker/runtime configuration, batch aggregation, and the latest supplied artifact:

`backend/benchmark/reports/cad_test/20261127T162621Z_ea9d063f/report.json`

The current worktree also contains unrelated or pre-existing dirty files. They were not reverted or rewritten.

## Findings

### P1: The latest batch still has zero independently scored cases

- Evidence from the latest report:

  ```text
  candidate_counts: generated=2, scored=0, terminal_scored=0
  cad_test_status: passed=0, failed=0, blocked=2, unavailable=3
  evaluation_status: scored=0, blocked=2, unavailable=3
  final_gate_status: passed=0, blocked=2, unavailable=3
  ```

- `ctb-00000007` and `ctb-00001411` reached a sealed candidate and Live geometry evidence, but both ended with `cad_test_configuration_unavailable` before the scorer/native-delivery verifier ran.
- `ctb-00001490` failed planner routing with `solid-output request as profile_2d`.
- `ctb-00001615` failed planner acceptance source binding because `rect-cut-bounds.source_quote` was not accepted as belonging to the objective.
- `ctb-00001817` failed because the required `fcstd` output was not produced.
- Therefore this run cannot validate the high-risk contracts that motivated this change: independent artifact identity, scorer exit-code agreement, native FCStd geometry, final-object selection, or a terminal final gate.

**Impact:** The branch currently has process metrics, but not a CADTest result that can be used as merge evidence. A Live geometry pass is not a CADTest pass.

**Required follow-up:** Make the worker/scorer configuration available, run at least one case through a terminal `passed` or `failed` CADTest result, and execute the wrong-case, swapped-artifact, modified-artifact, scorer-error, non-zero-exit, and native-history/assembly controls in the real container path.

### P1: Worker configuration is allowed to be absent until after candidate generation

- `validate_cad_test_worker_config()` performs the required dataset, scorer image, script, report-root, Podman, and FreeCAD checks in `backend/app/evaluation/cad_test_observer.py:101`.
- The batch CLI calls that preflight at `backend/benchmark/cad_test_batch.py:173`, but `backend/app/workers/workflow_worker.py:105` starts the worker without calling it.
- When the environment is absent, `config_from_environment()` returns `None` at `backend/app/evaluation/cad_test_observer.py:87`, and `observe_sealed_candidate()` records `cad_test_configuration_unavailable` at `backend/app/evaluation/cad_test_observer.py:372` instead of rejecting the evaluation run before generation.
- The default `docker-compose.yml` worker environment at `docker-compose.yml:145-178` passes `SANDBOX_IMAGE` and service settings but does not pass `CAD_TEST_DATASET_ROOT` or mount a CADTest dataset. This makes the newly added scoring path unavailable in that worker configuration unless an external environment setup supplies it.

**Impact:** A deployment can appear healthy and generate candidates while silently producing no CADTest score. The batch process then spends the full modeling time before reporting a configuration block. This is an incomplete runtime contract, not merely a missing optional metric.

**Required follow-up:** Define one explicit policy and enforce it consistently: either provision and validate CADTest configuration for evaluation workers before accepting benchmark work, or make the benchmark command use a dedicated evaluator worker with an immutable dataset/image contract. In either case, the worker and CLI must validate the same configuration before a run can claim CADTest evaluation.

### P1: Runtime version identity is not part of the worker preflight

- The preflight in `backend/app/evaluation/cad_test_observer.py:101-151` verifies the dataset hash, scorer image presence, scorer scripts, report-root writability, Podman availability, and FreeCAD startup.
- It does not verify the worker application revision, worker image digest, activity implementation version, or that all consumers of `settings.temporal_agent_v2_task_queue` run the same revision.
- `backend/app/workers/workflow_worker.py:94-101` registers the current activities and CADTest observation workflow on the shared queue, but no code-level compatibility identity is exposed or checked.
- The latest report no longer contains the old `fcstd` schema error, so this is not a claim that the supplied run currently shows old/new mixing. It remains a regression-prevention gap after the earlier mixed-worker failure.

**Impact:** A stale worker can consume a task from the same queue while the local preflight still passes. The resulting report can combine activity results from incompatible code revisions without a trustworthy runtime identity.

**Required follow-up:** Bind the worker/activity implementation identity and immutable runtime image digest to the evaluation admission or preflight, and verify the active queue consumers before accepting a benchmark run. Add a negative control that fails when an incompatible worker identity is present.

### P2: Batch rows drop the candidate revision identity at the report boundary

- In the latest report, the two sealed cases contain a nested identity such as:

  ```text
  cad_test.identity.candidate_revision_id=<uuid>
  cad_test.identity.candidate_revision_match=true
  cad_test.identity.workflow_match=true
  ```

- The same batch run rows expose `candidate_revision_id=None` at the row level for all five cases.
- `benchmark_row_from_live_report()` in `backend/benchmark/live.py:311-356` copies the nested CADTest object and process snapshot but does not project the candidate revision identity into the batch row. `benchmark/cad_test_batch.py:247-263` then serializes those rows without restoring that identity.

**Impact:** Consumers that read the batch row rather than the nested evidence cannot correlate a run with the sealed candidate. This is a provenance/report contract mismatch and makes audit tooling depend on an undocumented nested path.

**Required follow-up:** Choose one canonical identity location and populate it consistently. Preserve the nested evidence for the integrity decision, but expose the workflow and candidate identities in the documented batch-row schema and test JSON/HTML equivalence.

### P2: Evaluation telemetry failures are intentionally swallowed and can hide incomplete evidence

- `backend/app/workflows/activities.py:96-121` catches exceptions from activity evidence recording and continues with the business result.
- `backend/app/evaluation/cad_test_observer.py:445-461` catches attachment failures and even catches failures while recording the attachment failure.
- `backend/app/evaluation/integration.py:1061-1063` logs observation recording failures and returns the workflow run ID.

This does not currently create a fake CADTest pass: the final gate remains false when evidence is absent. However, it makes a worker appear successful while process/CADTest evidence is missing, and the current report already demonstrates the resulting `blocked`/`unavailable` states.

**Impact:** Operational failures can be discovered only by inspecting the report after a full run. A CI or developer workflow that checks only workflow success can mistakenly treat an unevaluated candidate as completed.

**Required follow-up:** Keep business workflows isolated from optional observation if that is intentional, but emit a durable, queryable observation failure with a mandatory terminal status and make benchmark commands fail explicitly when required evidence was not recorded. Add a test for recorder failure followed by the batch exit code and report status.

## Findings not classified as confirmed code defects

- The planner 3D/2D misclassification and `source_quote` rejection are real failed cases in the supplied report. The evidence is insufficient to prove that either is caused by a case-specific implementation bug rather than model output or overly strict generalized validation. They remain acceptance blockers because they prevent scoring, but the fix must be generalized and must include reusable cross-case controls.
- The missing `fcstd` case is a real required-output failure. The repository now requests `fcstd` for 3D CADTest runs in `backend/benchmark/live.py:445-460` and recognizes the format in the output contract. The supplied report does not establish whether the remaining failure is generation logic, worker runtime behavior, or model output; it should not be fixed with a case-specific fallback.
- The previous old/new Worker schema error is absent from the supplied `20261127T162621Z_ea9d063f` report. That item is not reported as a current failure, but the missing runtime identity check above means it is not prevented by code.

## Evaluation Evidence Integrity Gate

- **Admission identity:** Passed in focused unit controls for sealed candidate and candidate-revision matching; real terminal admission was not exercised in the supplied batch because scoring never started.
- **Artifact identity:** Focused swapped-artifact and missing-seal controls passed; no real scorer invocation occurred in the supplied batch.
- **Process/result agreement:** Focused non-zero verifier and malformed/missing evidence controls passed; no real native verifier result was produced in the supplied batch.
- **Final geometry selection:** Repository tests cover hidden Boolean/history and independent assembly selection, but no real FreeCAD native-delivery control ran in this batch.
- **Negative controls:** Logic-level controls passed; real Podman/FreeCAD controls remain unexecuted for this revision.
- **No case-specific optimization:** No case-specific exception was accepted in this review. The two planner failures require shared route/source-binding investigation.

## Test Evidence

| Layer | Command or evidence | Result | Classification | Notes |
|---|---|---|---|---|
| Latest batch artifact | `backend/benchmark/reports/cad_test/20261127T162621Z_ea9d063f/report.json` | 5 cases; 0 scored; 0 passed | `FAIL` | Two blocked by configuration; three fail before scorer. |
| Focused CADTest/live suite | `python -X utf8 -m pytest tests/test_cad_test_external_recheck.py tests/test_cad_test_pass_gates.py tests/test_cad_test_review_closure.py tests/test_cadtest_live_batch_closure.py tests/test_live_evaluation_benchmark.py tests/test_eval_metrics.py -m "not docker" -q` | `110 passed, 1 skipped, 2 deselected, 1 warning` | `PASS` | Logic evidence only; Docker/FreeCAD acceptance excluded. |
| Feature map | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | Passed | `CONTRACT_ONLY` | 24 domains, 433 referenced paths. |
| Architecture | `python scripts/architecture/check_boundaries.py` | Passed | `CONTRACT_ONLY` | Legacy exceptions remain explicit. |
| Formatting | `git diff --check` | No whitespace errors | `CONTRACT_ONLY` | Line-ending warnings only. |
| Risk scan | `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base origin/main --head HEAD --worktree` | 37 leads | `CONTRACT_ONLY` | Production exception/empty-branch leads reviewed; historical QA files add noise. |
| Real scorer/native verifier | Podman + FreeCAD terminal path | Not reached | `BLOCKED` | Latest run had no terminal CADTest evidence. |

## Acceptance Decision

`FAIL`. The branch has useful logic-level protections and the focused suite is green, but merge acceptance requires more than those mocks and unit controls. Before merge, the runtime must have a validated, shared CADTest configuration, must produce at least one terminal independent score, and must expose a consistent candidate/workflow identity. The runtime-version check and report identity projection should also be closed so the previous Worker-mixing and provenance failures cannot recur silently.

## Files Written

- `docs/qa/code-review/2026-11-28-20ea31b-codex-merge-blockers.md`

No business code, application tests, deployment files, generated product artifacts, or Git history were modified by this review.
