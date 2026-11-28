# CAD-Agent Code Review: 2026-11-26 bbe0ae4 codex-rereview

## Conclusion

- Status: `PASS_WITH_LIMITATIONS`
- Reviewer agent: Codex
- Review ID: `codex-rereview`
- Reviewed at: 2026-11-26 (Asia/Shanghai)
- Review range: previous review `4eca9ae` findings through current `HEAD` (`bbe0ae4`), with the full branch range from merge-base `7508b94d9f4e1cf0a306508d66ab27729db2a18e` considered for regression context
- Working tree state: branch `feature/live-cadtest-evaluation`; pre-existing untracked QA reports and planning documents remain; no staged changes
- Business code modified by reviewer: `No`

The three findings from the prior review are closed in the current implementation and covered by focused negative controls. No new material regression was found in the reviewed CADTest/live-evaluation paths. The conclusion is limited because real FreeCAD/Podman acceptance and the full service-backed regression matrix were not available in this Windows environment.

## Closure of Previous Findings

### P1 delivery evidence conflict: closed

`backend/app/evaluation/cad_test_evidence.py:90-110,192-205` now compares `delivery_verified` and `native_delivery.passed` when both are present. A disagreement produces `status="error"`, `verdict=None`, `reason="cad_test_delivery_evidence_conflict"`, and delivery status `error`. Direct reproduction now returns `error None cad_test_delivery_evidence_conflict error` rather than a pass.

Regression control: `backend/tests/test_cad_test_evidence.py::test_normalize_cad_test_result_rejects_conflicting_delivery_evidence`.

### P1 unverified dataset identity: closed

`backend/app/evaluation/cad_test_runner.py:62-69` now calls `benchmark.cad_test_engine.dataset.load_dataset(..., verify_files=True)` instead of reading the manifest field directly. Invalid manifest, controls evidence, or case-file hashes return no dataset hash. `backend/app/evaluation/integration.py:697-723` treats a configured dataset with no verified hash as `cad_test_dataset_integrity_unavailable` and blocks scoring before the scorer is invoked.

Regression controls:

- `backend/tests/test_cad_test_runner.py::test_resolved_dataset_hash_requires_verified_dataset_files`
- `backend/tests/test_cad_test_integration.py::test_benchmark_result_blocks_when_configured_dataset_fails_integrity`

The control confirms that tampered controls evidence and an incomplete dataset are blocked. The verified loader also checks canonical manifest identity, `status`, controls hash, case metadata hashes, and declared file hashes.

### P2 final attachment evidence loss: closed

`backend/app/evaluation/cad_test_observer.py:301-318` now records a structured `error` observation with `cad_test_attachment_failed` and the exception type when final attachment fails. `backend/app/evaluation/integration.py:828-829,906-907` re-raises recording failures so the observer can preserve the failure boundary while the seal activity still isolates evaluator errors from the product workflow.

Regression controls:

- `backend/tests/test_cad_test_observer.py::test_sealed_benchmark_candidate_records_final_attachment_failure`
- `backend/tests/test_cad_test_integration.py::test_observed_cad_test_bundle_propagates_recording_failure`

## Compatibility and Evidence Integrity Review

- Case identity remains bound at the admitted benchmark source and mismatched cases are blocked before scoring.
- Dataset identity is now verified from the same loader used by batch evaluation.
- Artifact hashes are independently computed by the runner and compared against scorer-reported hashes.
- Non-zero scorer/native-verifier exit codes remain failures even if result JSON reports success.
- 2D routes do not require 3D native delivery; 3D routes still require native delivery evidence.
- Ambiguous artifact selection and missing artifact metadata remain blocked.
- Native final-object selection retains explicit history/assembly controls.
- Evaluation recording errors remain isolated from business workflow completion; the failure is now represented in the evaluation report when the final attachment path fails.

No frontend, REST, WebSocket, persistence/RLS, revision-commit, or permission contract regression was found in this change set.

## Scanner Review

The risk scanner reported 24 leads across 54 changed/worktree paths. The production exception leads in observer/integration/activity code were manually reviewed as intentional evaluator-isolation or failure-conversion branches. Historical QA-report mojibake and placeholder/mock wording in planning/review documents are not current product-code defects. No new encoding corruption or fake-success path was confirmed.

## Test Evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Static | `git diff --check` | PASS | LOGIC | No diff whitespace errors. |
| Skill integrity | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | PASS | CONTRACT_ONLY | 24 domains, 433 referenced paths. |
| Architecture | `python scripts/architecture/check_boundaries.py` | PASS | CONTRACT_ONLY | Legacy exceptions remain explicit. |
| Risk scan | `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base 7508b94d... --head HEAD --worktree` | PASS_WITH_LEADS | LOGIC | Leads manually reviewed and classified above. |
| Focused CADTest/evaluation matrix | `python -X utf8 -m pytest tests/test_cad_test_evidence.py tests/test_cad_test_integration.py tests/test_cad_test_observer.py tests/test_cad_test_runner.py tests/test_cad_test_self_contained.py tests/test_native_delivery_selection.py tests/test_live_evaluation_benchmark.py tests/test_eval_metrics.py tests/test_cad_test_report_html.py -q` | PASS: 111 passed, 1 warning | LOGIC/SERVICE-CONTRACT | Existing Pydantic protected-namespace warning only. |
| Full backend safe matrix | `python -X utf8 -m pytest -m "not docker and not llm and not fusion_e2e" -q` | Previously attempted: 3047 passed, 136 failed, 431 skipped, 1 deselected, 2 errors | BLOCKED/ENVIRONMENT | Failures were dominated by local PostgreSQL/asyncpg lifecycle, Windows event-loop cleanup, shell, and external runtime assumptions; not attributed to this patch without isolated evidence. |
| Runtime/product | FreeCAD/Podman live CADTest and native-delivery controls | BLOCKED | REAL_ACCEPTANCE | No fresh real runtime acceptance in this review environment. |

## Remaining Limitations

- No fresh full live CADTest batch was executed, so this review establishes logic/contract closure rather than model-generation quality.
- Real FreeCAD/Podman, PostgreSQL, Temporal, and MinIO acceptance remains an environment prerequisite.
- The existing Pydantic `model_kind` protected-namespace warning is unrelated to this patch and remains non-blocking for the focused matrix.

## Knowledge Update

- Review report: `docs/qa/code-review/2026-11-26-bbe0ae4-codex-rereview.md`
- Review memory: none
- Stable map/invariant update: none

No business code, application tests, migrations, deployment code, generated product artifacts, user files, or Git history were modified by this review.
