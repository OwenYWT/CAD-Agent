# Review Audit: `2026-11-26-4eca9ae-codex-current-review.md`

- Reviewer: Codex
- Review ID: `2026-11-26-4eca9ae-codex-review-audit`
- Reviewed at: 2026-11-26 (Asia/Shanghai)
- Audited report: `docs/qa/code-review/2026-11-26-4eca9ae-codex-current-review.md`
- Code under audit: `HEAD=4eca9ae`, branch `feature/live-cadtest-evaluation`
- Mode: report audit and read-only code verification

## Conclusion

**The audited report is reasonable. Its `FAIL` conclusion is supported by the current implementation.** The two P1 findings are real evidence-integrity defects, and the P2 finding is a real observability gap. No material overstatement was found in the report's conclusion or severity ordering.

The focused test count and environment limitations in the audited report are also consistent with an independent rerun. No business code or application tests were modified during this audit.

## Finding Audit

### P1: contradictory delivery evidence

**Confirmed.** `normalize_cad_test_result` first uses `result["delivery_verified"]` and only falls back to `native_delivery["passed"]` when the first value is absent. It does not reject contradictory booleans.

Independent reproduction:

```text
result.status = pass
result.delivery_verified = true
result.native_delivery.passed = false
score.passed = true
score.geometry_valid = true
requirements_passed = requirements_total = 1
```

Observed normalized result:

```text
status = passed
verdict = true
reason = cad_test_verified
delivery.status = passed
```

This confirms the report's statement that conflicting delivery channels can become a false evaluation pass. The relevant implementation is `backend/app/evaluation/cad_test_evidence.py` in `normalize_cad_test_result`; the stored normalized result is consumed by the live/batch gate. Severity `P1` is appropriate because this violates the evidence integrity invariant and can directly affect benchmark pass rates.

### P1: unverified live dataset identity

**Confirmed by code inspection; runtime tamper control not executed.** `CadTestRunnerConfig.resolved_dataset_hash` reads `data/dataset-v1.json` and returns the manifest's `dataset_hash` field. It does not call `benchmark.cad_test_engine.dataset.load_dataset`, recompute the canonical manifest hash, validate `status`, verify `controls-validation.json`, or verify case file hashes.

The existing verified loader does perform those checks. The live observer uses the weaker runner method for `_cad_test_identity_block`, then resolves and mounts case files independently. Therefore, if the manifest or case files are changed while the declared hash remains unchanged, the live path can accept the old identity and score altered inputs.

The current local dataset was checked with both paths and presently produces the same hash:

```text
runner_hash = 77c6c137722f3b2fe57059f587aaba7c0d4f37c7f8e06d3180e8553646c089c9
verified_hash = 77c6c137722f3b2fe57059f587c7f8e06d3180e8553646c089c9
same = True
```

That only establishes the current dataset is consistent; it does not disprove the provenance gap. The report correctly labels this as a P1 because the defect affects reproducibility and cross-dataset attribution. A temporary tamper control should be added before implementation closure.

### P2: final CADTest attachment failures are only logged

**Confirmed.** `observe_sealed_candidate` catches exceptions from `attach_cad_test_artifacts` and logs a warning. The integration function also catches its own store/recording exceptions and logs them. The seal activity intentionally isolates evaluation failure from the business result, but there is no structured terminal `blocked`/`evaluation_error` observation for this final attach failure.

This does not create a false pass because missing CADTest evidence fails the benchmark gate. It does make scorer failure, persistence failure, and missing observer evidence look alike in reports. Severity `P2` is appropriate.

## Report Accuracy

- The report's `FAIL` conclusion is correct.
- The finding order, P1/P2 severity, impact descriptions, and suggested follow-ups are technically consistent with the implementation.
- The report correctly distinguishes unit/mock evidence from real FreeCAD/Podman acceptance and does not claim `REAL_ACCEPTANCE`.
- The report correctly identifies the earlier 2D gate and observer metadata-loss issues as covered by the amended code/tests.
- The report's `105 passed, 1 warning` focused result was independently reproduced with the listed test set.
- The full-backend limitations are plausible and are not treated as CADTest-specific regressions without isolated evidence.
- The historical working-tree description is dated evidence and is not contradicted by the current post-amend state.

## Independent Validation

| Check | Result | Classification |
| --- | --- | --- |
| Contradictory delivery reproduction | Normalizer returned `passed=True` | LOGIC / confirmed defect |
| Current dataset runner vs verified loader | Hashes equal for current files | LOGIC / current-state check only |
| Focused CADTest/evaluation matrix | `105 passed, 1 warning` | LOGIC/SERVICE-CONTRACT |
| Risk scan | Leads present; no new contrary finding | LOGIC |
| Feature map validation | Passed: 24 domains, 433 referenced paths | CONTRACT_ONLY |
| Architecture boundaries | Passed | CONTRACT_ONLY |
| Real FreeCAD/Podman acceptance | Not run | BLOCKED / REAL_ACCEPTANCE unavailable |

## Recommended Closure Criteria

The audited report should remain open until all of the following are implemented and covered by negative controls:

1. Contradictory `delivery_verified` and `native_delivery.passed` values normalize to non-pass.
2. Live dataset resolution uses the verified dataset loader or an equivalent complete integrity check.
3. A final CADTest attachment failure leaves structured report evidence while preserving product workflow isolation.
4. Negative controls cover contradictory delivery, tampered manifest, tampered case files, and final attachment failure.
5. Real FreeCAD/Podman controls are run before claiming runtime acceptance.

No business code, application tests, migrations, deployment code, generated artifacts, user files, or Git history were modified during this audit.
