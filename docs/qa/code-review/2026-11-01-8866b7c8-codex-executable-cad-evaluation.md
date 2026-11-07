# CAD-Agent Code Review: Executable CAD Evaluation v0

## Conclusion

**PASS_WITH_LIMITATIONS**

The reviewed work adds an isolated developer-only executable CAD evaluation layer. Focused tests pass, the feature map validates, production application and frontend code do not import the evaluator, and no fake pass path was found in the changed business code. Full repository closure is limited by the local environment and unrelated existing test failures, so this review does not claim full product or runtime acceptance.

No business code was modified by this review. This report is the only review artifact written during the review step.

## Review Metadata

- Reviewer: Codex
- Review ID: `codex-executable-cad-evaluation`
- Review time: 2026-11-01 22:25 +08:00
- Base: `HEAD` at `8866b7c8d4552f71190d9c24de99ad80171f465c`
- Scope: current working tree, including staged, unstaged, and untracked paths
- Branch: `main`, ahead of `origin/main` by one local commit
- Affected feature domains: `12.01-12.11` inspection/validation/evidence; developer benchmark tooling under domain 24 operational scope
- Directly affected paths: `backend/benchmark/**`, `backend/tests/test_cad_evaluation_*.py`, `backend/benchmark/README.md`
- Explicitly not affected: `backend/app/**`, `frontend/**`, document/revision/history APIs, Temporal workflows, Podman configuration, and user-facing UI

## Findings

### No material findings

No regression, fake implementation, encoding defect, or production contract drift was confirmed in the changed implementation.

The scanner reported `backend/benchmark/cad_tests/executor.py:50` as `empty-production-branch` because the executor catches a handler exception. Manual inspection confirms that the branch creates a concrete `TestResult` with `status=error`, `failure_code=test_handler_error`, and the exception type in details. It is intentional case-level error isolation required by the design, not a success or empty branch.

The scanner also reported fixture/mock language in the new unit tests and the two newly added design documents. Those are test fixtures and documentation statements, not product-path mock data. Existing unrelated untracked composite-tool documents were included in the worktree scan and were not modified.

## Compatibility Review

- `backend/benchmark/metrics.py` adds an `evidence` object while preserving existing metric keys and the existing `passed` calculation.
- `backend/benchmark/eval.py` adds streamed SHA-256 provenance to existing artifact evidence without changing artifact readability or gate decisions.
- `backend/benchmark/cad_eval.py` does not import `app.main`, `Orchestrator`, provider clients, sandbox executors, or persistence modules.
- `CAD_EVALUATION_MODE` is resolved inside the benchmark package and defaults to `disabled`; application startup does not read it.
- `backend/benchmark/cad_tests/definitions.py` uses only evidence supplied by the context. Missing evidence returns `unknown` or `blocked`; it is not converted into a pass.
- `metrics._valid_solid` accepts only an explicitly named valid-solid field or geometry check and does not treat an aggregate inspect verdict as valid-solid proof.
- Artifact hash tests require a referenced existing file and compare the actual file digest; a missing source is `unknown` and a mismatch is `fail`.
- No frontend, WebSocket, revision, candidate, commit, restore, permission, or user task state contract changed.

## Verification Matrix

| Layer | Command | Result | Classification |
| --- | --- | --- | --- |
| Focused new tests | `python -m pytest tests/test_cad_evaluation_contracts.py tests/test_cad_evaluation_modes.py tests/test_cad_evaluation_evidence.py tests/test_cad_evaluation_executor.py tests/test_cad_evaluation_report.py -m unit -q` | `29 passed` after the final artifact-hash regression test | LOGIC |
| Existing benchmark tests | `python -m pytest tests/test_eval_cases.py tests/test_eval_metrics.py -m unit -q` | `56 passed` | LOGIC |
| Combined benchmark/evaluator tests | `python -m pytest ... -m unit -q` | `85 passed` | LOGIC |
| Python compilation | `python -m compileall -q ...` for changed Python files | PASS | LOGIC |
| Feature map | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | `Feature map valid: 24 domains, 424 referenced paths.` | CONTRACT_ONLY |
| Production import isolation | `rg -n "benchmark\\.cad_tests|benchmark\\.cad_eval|CAD_EVALUATION_MODE" backend/app frontend` | No matches | CONTRACT_ONLY |
| Encoding scan | UTF-8 scan over changed source/tests | Zero replacement characters and zero checked mojibake markers | CONTRACT_ONLY |
| Diff whitespace | `git diff --check` | PASS; only existing LF/CRLF warnings | CONTRACT_ONLY |
| Risk scan | `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base HEAD --head HEAD --worktree` | 14 heuristic findings; manually reviewed, no material confirmed finding | CONTRACT_ONLY |
| Ruff | `python -m ruff check ...` | BLOCKED: `ruff` module is not installed in this Python environment | BLOCKED |
| Default full backend suite | `python -m pytest -m "not docker and not llm" -q` | BLOCKED during collection by Windows default GBK decoding of `tests/fixtures/contracts/baseline-62084d5.json` | BLOCKED |
| UTF-8 full backend suite | `python -X utf8 -m pytest -m "not docker and not llm and not fusion_e2e" -q` | `2112 passed, 310 skipped, 137 failed, 1 error`; failures are concentrated in unavailable Podman/PostgreSQL/service state, script encoding/runtime integration, and existing application fixtures | BLOCKED / LIMITATION |
| Live CAD/runtime acceptance | No live evaluator runtime was required or invoked | Not run by design; v0 is offline over existing JSON | SKIPPED |

## Evidence and Test Coverage

The new tests cover:

- versioned contract serialization and all test status values;
- safe default mode and invalid mode rejection;
- measured bbox extraction and missing-evidence semantics;
- no solid-count inference from planner fields;
- real-file SHA-256 behavior;
- required versus diagnostic tests;
- dimension mismatch, missing dimensions, empty/missing artifacts, invalid test kinds, missing solid counts, and artifact-source verification;
- UTF-8 report serialization and secret exclusion;
- disabled CLI no-side-effect behavior;
- observe CLI reading an existing benchmark JSON and writing a developer-only report.

The existing benchmark suite still verifies its prior case definitions and metric semantics. In particular, planner-only part type and feature count diagnostics remain non-gating.

## Limitations

- The review did not establish real Podman, FreeCAD, Temporal, PostgreSQL, MinIO, provider, or browser acceptance for the evaluator. v0 is intentionally offline.
- `ruff` is unavailable in the current Python environment, so lint closure is blocked.
- The full backend suite is not a clean signal in this environment. It has a pre-existing default-encoding collection issue and, under UTF-8 mode, many service/runtime-dependent failures. These failures were not changed or repaired by this task.
- Requirement binding, intermediate operation evidence, candidate ranking, repair gain, complexity matrices, and ordinary production workflow integration remain out of scope for v0.

## Files Written During Review

- `docs/qa/code-review/2026-11-01-8866b7c8-codex-executable-cad-evaluation.md`

No application code, application tests, migrations, deployment files, generated artifacts, `.env` files, or Git history were modified during review.
