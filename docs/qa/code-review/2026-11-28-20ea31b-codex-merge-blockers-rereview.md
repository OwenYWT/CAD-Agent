# CAD-Agent Review: merge blockers re-review

## Conclusion

- Status: `FAIL`
- Reviewer agent: Codex
- Review ID: `merge-blockers-rereview`
- Reviewed at: 2026-11-28 (Asia/Shanghai)
- Review range: current worktree against `origin/main...HEAD`, with targeted audit of `2026-11-28-20ea31b-codex-merge-blockers.md`
- Repository revision: `20ea31b5d0d5faf766be2b178c33c374e7559154`
- Working tree state: dirty; all pre-existing changes and evidence were retained
- Business code, application tests, deployment files, and Git history modified by reviewer: `No`

The merge blockers remain open. The latest available live batch is still `20261127T162621Z_ea9d063f`, with 2 generated candidates, 0 scored, and 0 terminal scores. Worker startup/configuration, runtime identity, row-level provenance, and telemetry durability gaps remain. The selected unit suite passes, but cannot substitute for real scorer/FreeCAD acceptance.

## Scope

- Re-reviewed all five findings against the current worktree.
- Checked `backend/benchmark/reports/cad_test/20261127T162621Z_ea9d063f/`; its JSON parses successfully.
- No newer batch directory exists in `backend/benchmark/reports/cad_test/`.
- Relevant areas: CADTest/live evaluation, Temporal Worker configuration and compatibility, report provenance, and telemetry persistence.

## Findings

### P1: No terminal CADTest score exists in the latest batch

- Latest aggregate: `generated=2`, `scored=0`, `terminal_scored=0`; CADTest status: `passed=0`, `failed=0`, `blocked=2`, `unavailable=3`.
- The two sealed candidates have no terminal scorer result. Live geometry evidence does not establish independent CADTest or native-delivery acceptance.
- **Impact:** No real acceptance evidence for scorer/artifact identity, process/result agreement, or native FCStd delivery.
- **Required follow-up:** Run at least one case to terminal `passed` or `failed` and run wrong-case, swapped/modified artifact, scorer error/non-zero exit, Boolean/history, assembly, and multi-artifact controls in real container/FreeCAD.
- **Classification:** `FAIL` for merge acceptance; evidence gap, not proof of a specific scorer defect.

### P1: Worker accepts benchmark work without validated CADTest configuration

- `backend/app/workers/workflow_worker.py:105-118` starts workers without `validate_cad_test_worker_config()`.
- Strict preflight runs from CLI (`backend/benchmark/cad_test_batch.py:171-174`), not Worker startup. Missing config is accepted by `backend/app/evaluation/cad_test_observer.py:87-109` and recorded only after candidate sealing at lines 301-305.
- Default `docker-compose.yml` has no `CAD_TEST_DATASET_ROOT` wiring or dataset mount.
- **Impact:** A partially configured Worker can generate a candidate before discovering required CADTest cannot run.
- **Required follow-up:** Fail evaluator Worker startup on invalid shared config, or route evaluation to an explicitly provisioned Worker; align CLI and Worker identity checks.

### P1: Runtime compatibility identity is not checked

- `backend/app/evaluation/cad_test_observer.py:101-151` checks runtime prerequisites but not Worker revision, image digest, activity version, or active task-queue consumer identity.
- `backend/app/workers/workflow_worker.py:94-101` registers CADTest on the V2 queue without publishing or validating runtime identity.
- **Impact:** A local preflight may pass while a stale Worker consumes tasks; reports cannot prove consistent activity implementation. This is a prevention gap, not proof the latest batch mixed workers.
- **Required follow-up:** Bind immutable Worker/activity identity to admission/preflight, reject incompatible consumers, and add a negative control.

### P2: Batch row omits the sealed candidate revision identity

- In the latest batch JSON, the two sealed cases have nested `cad_test.identity.candidate_revision_id` values, while row-level `candidate_revision_id` is `null` for all five cases.
- `benchmark_row_from_live_report()` (`backend/benchmark/live.py:311-356`) copies nested CADTest evidence but does not project the candidate revision identity into the row; batch serialization does not restore it.
- **Impact:** Consumers of the documented row cannot directly correlate a case result with its sealed candidate and must depend on an undocumented nested path.
- **Required follow-up:** Establish one documented canonical location (preferably also a row-level projection) and test JSON/HTML report consistency.

### P2: Telemetry write failures can be swallowed without durable failure state

- `McadWorkflowActivities._run_observed()` catches and suppresses errors from activity evidence recording and workflow terminal recording (`backend/app/workflows/activities.py:117-129`); it logs and continues if CADTest observation dispatch fails (`:130-136`).
- `attach_cad_test_observation()` logs persistence/normalization failures and still returns the run ID (`backend/app/evaluation/integration.py:1005-1063`). The observer also catches failure-recording errors (`backend/app/evaluation/cad_test_observer.py:443-461`).
- These paths do not currently establish a CADTest pass when evidence is absent: the batch final gate remains false. However, they can leave no durable, queryable indication of lost telemetry while the business workflow reports success.
- **Impact:** Operational state and evidence completeness can diverge, complicating audit and recovery even when the batch eventually rejects the missing score.
- **Required follow-up:** Persist an explicit observation-failure terminal state (or fail required evaluation work), and test recorder failure through batch report status and process exit code.

## Test Evidence

| Layer | Command or evidence | Result | Classification | Notes |
|---|---|---|---|---|
| Latest live batch | `backend/benchmark/reports/cad_test/20261127T162621Z_ea9d063f/report.json` | 2 generated, 0 scored, 0 terminal-scored | `FAIL` | No later batch artifact is present. |
| Focused tests | `python -X utf8 -m pytest tests/test_cad_test_review_closure.py tests/test_cadtest_live_batch_closure.py tests/test_cad_test_observer.py tests/test_live_evaluation_benchmark.py -m "not docker" -q` | `63 passed, 2 deselected` | `PASS` | Logic/unit evidence only; one existing Pydantic warning. |
| Feature map | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | Passed; 24 domains, 433 paths | `CONTRACT_ONLY` |  |
| Architecture boundaries | `python scripts/architecture/check_boundaries.py` | Passed | `CONTRACT_ONLY` | Legacy exceptions remain explicit. |
| Whitespace | `git diff --check` | No whitespace errors | `CONTRACT_ONLY` | Git emitted line-ending normalization warnings for dirty files. |
| Risk scan | `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base origin/main --head HEAD --worktree` | 39 leads across 728 paths | `CONTRACT_ONLY` | Historical QA artifacts add noise; production exception leads were checked against the telemetry finding. |
| Real scorer/native verifier | Podman + FreeCAD terminal evaluation | Not evidenced in current batch | `BLOCKED` | No real terminal scorer result or required negative-control run. |

## Acceptance Decision

`FAIL`. All five findings remain materially open. The focused tests and static checks pass, but the absence of a terminal real CADTest result alone is sufficient to withhold merge acceptance; configuration and provenance/telemetry contracts also need closure. No case-specific workaround should replace generalized runtime and identity controls.

## Files Written

- `docs/qa/code-review/2026-11-28-20ea31b-codex-merge-blockers-rereview.md`

No business code, application tests, deployment files, generated product artifacts, or Git history were modified by this review.
