# CAD-Agent Review: 2026-11-06 beb2da3 codex-task7-live-gate

## Conclusion

- Status: `PASS_WITH_LIMITATIONS`
- Reviewer agent: Codex
- Review ID: codex-task7-live-gate
- Reviewed at: 2026-11-06 Asia/Shanghai
- Review range: `origin/main...HEAD` plus the current working tree
- Repository revision: `beb2da3`
- Working tree state: existing mixed tracked and untracked Task 7, history-restore, and design-document changes; no generated report or `.env` file was added to the reviewed paths
- Business code modified by reviewer: `No` (the review phase only recorded evidence)

## Change scope

- Change class: live benchmark evidence mapping and release-gate compatibility
- User-described goal: continue Task 7 and keep live execution reports authoritative without fake evidence or regressions
- Changed paths reviewed: `backend/benchmark/live.py`, `backend/benchmark/compare.py`, `backend/tests/test_live_evaluation_benchmark.py`
- Functional inventory domains: 05, 06, 07, 12, 14, 15, 23, 24

## Implementation trace

| Changed symbol or contract | Implementation path | Consumers | Impact judgment |
| --- | --- | --- | --- |
| Live artifact gate | `benchmark.live._artifact_gate_from_live_report` | `benchmark_row_from_live_report`, benchmark report rows | Uses only geometry evidence artifacts and the real visual-render event; does not rerun FreeCAD or invent view counts |
| Live benchmark row | `benchmark.live.benchmark_row_from_live_report` | `benchmark.eval.run_eval` and benchmark aggregation | Finished status and complete live evidence are required before a successful response can pass |
| Artifact comparison | `benchmark.compare._candidate_artifact_failures` | release-gate comparison | Legacy rows still require `rendered_views >= 4`; live rows require `visual_rendered == true` |

## Findings

### Encoding and fake implementation review

- Scanner result: no encoding-category findings were emitted for the reviewed Task 7 implementation or tests.
- Confirmed findings: none.
- The live gate contains no static success path, fabricated artifact hash, fabricated revision ID, or fabricated `rendered_views` value.
- The scanner reported broad `except Exception` leads in the existing best-effort evaluation observer and activity wrappers. Manual inspection classified these as intentional evaluator-isolation handlers that log or record the error and preserve the business result; they are not empty success branches.
- Test and documentation mentions of fixtures, mocks, or placeholders were classified as non-production context.

### Compatibility review

- Frontend/backend contract: live evaluation fields are benchmark-row metadata only; no ordinary frontend response contract was changed.
- WebSocket/event contract: no WebSocket event names or payload contracts were changed by this fix.
- Persistence/migration/RLS: no database schema or persistence ownership changes.
- Workflow/retry/cancellation: live admission and confirmation behavior remains covered by the existing Task 7 tests; the gate consumes the finalized report after the real workflow completes.
- Revision/candidate/draft/artifact identity: the gate reads the report's runtime geometry evidence and does not infer identity from planner fields.
- Permissions and external boundaries: no new external provider or permission path.

## Test evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Logic | live/CAD evaluation, case, and metric tests | `159 passed, 1 warning` | LOGIC | Includes new live artifact-gate and compare regressions |
| Service | durable/API/Worker focused tests | `36 passed, 1 warning` | SERVICE | `test_api_durable_cutover.py`, retry, activity wiring, Agent v2 worker |
| Runtime | Standard JSON load plus live-row conversion against an existing real report | PASS | RUNTIME evidence inspection | Real report mapped to STEP, STL, non-empty geometry, and rendered visual evidence; row passed |
| Repository | `python -X utf8 -m compileall -q app benchmark tests` | PASS | LOGIC | No syntax/compile errors |
| Repository | `git diff --check` | PASS | LOGIC | No whitespace errors |
| Repository | feature map validation | PASS: 24 domains, 424 referenced paths | CONTRACT_ONLY | Review map remains valid |
| Full backend | `pytest -m "not docker and not llm and not fusion_e2e" -x -vv` | `117 passed; first failure in fusion360/test_install_scripts.py` | BLOCKED | Existing Windows/bash incompatibility: `bash -n scripts/fusion360/install.sh` returned 1; unrelated to Task 7 |

## Gaps and blocked prerequisites

- A fresh live `P01` benchmark run was not claimed. The approved plan requires a dedicated benchmark project, branch, and current base revision; those identifiers were not available, and the user's business branch must not be mutated for this check.
- PostgreSQL, Temporal, Podman, and FreeCAD product acceptance remain environment-dependent and were not treated as passing based on unit tests.
- The repository-wide suite remains blocked by the unrelated Windows Fusion script test failure above.

## Knowledge update

- Review report: this file
- Review memory: not updated; no reusable invariant beyond the existing Task 7 design was established
- Stable map/invariant update: none
- Reason and evidence: the live gate now preserves the distinction between real report evidence and legacy offline `rendered_views` evidence, with focused regression coverage.
