# CAD-Agent Review: 2026-09-23 62859fa codex-acceptance-closure

## Conclusion

- Status: `BLOCKED` for formal delivery; reviewed code `PASS_WITH_LIMITATIONS`.
- Reviewer agent: Codex root (self-review; not an independent GitHub reviewer).
- Review ID: codex-acceptance-closure.
- Range: `f6e5a10..62859fa42ba75bffd3186550a25c0ec903b5854e` plus untracked isolated deployment/evidence files.
- Working tree: application code clean; new `deploy/acceptance/` and dated deployment report; pre-existing `source-manifest.json` and local dependency symlink excluded.
- Business code modified by reviewer: No, during this read-only review invocation. The preceding implementation phase made the documented fixes.

## Scope and implementation trace

Inventory domains: 05, 06, 07, 08, 10, 12, 14, 15, 24; related 01/16 permissions and 17/18/19 shared runtime evidence.

| Symbol/contract | Consumers | Judgment |
| --- | --- | --- |
| `ChangeSetDialog.onReviewed` | EngineeringWorkspace refresh/navigation; SharedDocumentWorkspace omits callback | Reject/request-modification refresh data without invoking navigation. Commit/rollback may show committed view. No backend review-state contract change. |
| `useDocumentView.show` | VersionHistoryPanel `onView`, workspace navigation | Same effective document/revision/mode returns without a draft-discard prompt; actual different revision still guarded. Current-head history selection normalizes to committed. |
| VersionHistoryPanel view button | EngineeringWorkspace is the only supplied `onView`; ValidationDialog has no `onView` | Removes double guard from the view button; restore button remains guarded. Cross-version browser negative still passes. |
| `edge_intent` | Deterministic compiler and provider operation-generator path; repair validator | Positive scope separated from protected scope. Original/derived intent conflict fails closed; repair cannot change target/size/scope. Bounded language support, not a claim of arbitrary natural-language completeness. |
| `_dressup_target` | Native fillet and chamfer | Body target binds to its current valid member Tip before feature insertion; explicit Body selectors reject ambiguous remapping. Feature targets unchanged. Actual non-default/multi-Body and final-geometry tests pass. |
| Browser report contract | `docs/architecture/modules.json`, required runtime evidence | Both forced real-response orders, no extra draft prompt, same-revision and real-history guard results are required. |
| Isolated compose | Full-stack release drill only | Separate daemon/project/volumes/auth/DB/object store/Temporal. Same old/new AMD64 application images checked by digest and source hash; explicit emulation resource allowance. |

## Findings

1. **F02 / release blocker:** main protected=false; protection REST endpoint 403 requires private-repository plan upgrade. Required checks, independent review and failing-PR merge denial are not enforceable or proven. Do not merge or call CI alone enforcement. Owner action required.
2. **F06 / remaining delivery boundary:** functional isolated full-stack upgrade/rollback evidence is now present, including in-flight generation fencing and built browser. ARM host emulates the actual AMD64 images; validation containers required a recorded 3 GiB container-level allowance instead of requested 512 MiB. This is not native Tencent resource acceptance. Final main merge SHA and production upgrade remain open.
3. No new material code regression found within tested paths. Broader unsupported chamfer natural language/shape cases remain explicit limitations, not silently routed to all edges.

### Encoding / fake implementation scan

Command: `python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base f6e5a10 --head HEAD --worktree`.

61 changed paths; six leads: four fixture-language occurrences in the real browser regression and two report `fixture` keys. Inspected: HTTP routes use `route.fetch()` and real responses; WebSocket delay forwards actual server collaboration messages to force ordering. Neither fabricates payloads or success. Test-created accounts/documents are real durable fixtures. Production code has no mock/constant success introduced.

### Compatibility boundaries

No migration, persisted workflow request DTO, public HTTP/WebSocket payload, or execution wire schema changed. Candidate/draft/current revision remain separate; rejection does not advance Head. Errors preserve the prior saved geometry. Permission paths unchanged and exercised by real monitor ordinary/admin access plus database UPDATE denial. Shared native target helper exercised for both fillet and chamfer.

## Evidence

Detailed commands, counts, image IDs, retained failure rounds and limitations: [dated deployment report](../deployments/2026-09-23-acceptance-fixes/README.md).

| Layer | Evidence | Result/classification |
| --- | --- | --- |
| Review checks | diff --check; feature-map validator; architecture checker | PASS; map 24 domains / 345 paths; not GitHub enforcement |
| Logic | backend-final2.xml; frontend tests/lint/build | 1600 backend pass / 181 skip / 1 deselect; 154 frontend pass; LOGIC |
| Service | core.xml | 30 pass; real isolated PostgreSQL/S3/Temporal; SERVICE |
| Native runtime | native-* and amd64-* logs | Actual constraints/reference/final chamfer geometry + STEP, FEA/CAM/release; RUNTIME |
| Real model service | live4.xml | EN/ZH 2 pass, actual Provider through saved native candidate and final geometry; REAL_ACCEPTANCE |
| Browser | browser-*.json | Five mandatory suites; N01 two forced real-response orders; PRODUCT |
| Full deployment stack | full-stack-report.json, full-stack-browser.json, live-generation-fence.json | Both directions recover same job at generation 2, payload hash preserved; all stack containers rebuilt; committed artifacts/account survive; PRODUCT with explicit emulation resource limitation |
| Monitor versions | monitor-image-matrix.json, monitor-read-only.json | New/old combinations, 401/403/200, DB write denied; PRODUCT |
| CI | run 35758829094 on exact 62859fa | At review time architecture/frontend/backend passed, real-browser/runtime gate still running; final status must be checked before reporting complete CI. |

No claims of complete Fusion/device, arbitrary CAD features, physical fit, native Tencent resource acceptance, or enforceable main branch protection.

## Knowledge update

This report and corresponding dated memory only; stable maps/invariants unchanged. Existing invariant (refresh is not user navigation; final geometry rather than intermediate feature proves scope) is supported by current tests. Review did not commit/push or edit business code/tests/deployment files.

## Post-review verification update

Run 35758829094 subsequently completed: all five jobs succeeded. Downloaded artifacts and reran required-runtime-evidence validator: 18 required reports verified, including both N01 ordering cases. Formal delivery conclusion remains BLOCKED for F02 and native Tencent/final-merge boundary; this update changes no business code.
