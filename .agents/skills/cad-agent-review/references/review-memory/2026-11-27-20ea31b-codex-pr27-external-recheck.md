# 2026-11-27 · CADTest PR #27 external report recheck

- Reviewer: Codex, `codex-pr27-external-recheck`.
- Reviewed range: `7508b94d9f4e1cf0a306508d66ab27729db2a18e...20ea31b5d0d5faf766be2b178c33c374e7559154` plus the existing dirty worktree on `feature/live-cadtest-evaluation`. No application paths were edited during review.
- Domains: 05, 06, 07, 11, 12, 14, 19, 24. Paths: `backend/app/evaluation/`, `backend/app/workflows/activities.py`, `backend/benchmark/` and corresponding tests.
- Review report: `docs/qa/code-review/2026-11-27-20ea31b-codex-pr27-external-recheck.md`.

## Reusable verified checks

- Exercise the repository's real CLI/launcher with matching FCStd plus a same-STEP/STL control with incorrect FCStd. FreeCAD 1.1.4 measured 0 and about 274604 mm³ symmetric difference respectively; CLI returned 0 and 1. A partial bundle must return nonzero even when the STEP alone is valid.
- Keep candidate seal SHA and scorer-returned SHA separate. The controlled sealed-file swap was rejected before scoring, but a passing production STEP result still omitted the scorer-side artifact hash.
- Use both current persisted 2D evidence and older or sparse inputs when checking JSON/HTML equivalence. The real current Podman-tested DXF results agree; a fixture with `passed` and no delivery field diverges because HTML requires the native-delivery flag regardless of route.
- To check the seal/observer sequencing independently of Temporal, pause the observer with an event after the business callback. The Activity is still pending and terminal recording has not occurred. This proves waiting, not a production timeout or retry.

Validation: 197 focused non-container tests, two real Podman DXF tests, real FreeCAD native bundle controls, external dataset integrity, feature map and architecture checks. Affected service/product layers were not rerun. No stable rule or map changes.
