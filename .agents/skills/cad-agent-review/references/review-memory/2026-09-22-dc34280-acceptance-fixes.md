# Acceptance fixes: verified review memory

- Agent: Codex; date: 2026-09-22; review ID: acceptance-fixes.
- Range: dc342808390134e54db48fb63393dbc588b6004a plus working-tree fixes; PR #9.
- Domains: 01, 05–08, 10, 12, 14–16, 19, 24.
- Conclusion: code and local required matrix passed; release BLOCKED by F02/F06. Review-only phases did not modify business code. An alias normalization gap found during review was repaired in an implementation phase and then retested.
- Report: `docs/qa/code-review/2026-09-22-dc34280-acceptance-fixes.md`.

Verified reusable observations:

1. Upstream Hole state does not prove the final solid kept its hole dimensions. Test final Body Tip and reimported STEP with an independent geometry oracle. The old compiler/runtime are identical between 62084d5 and dc34280; the scope defect predates decoupling.
2. Parse TS alias/extension forms before applying forbidden-import rules. Baseline accepts `@/services/engineeringService.ts`; current negative tests reject it.
3. A passing empty import ledger was incomplete coverage. Current guard explicitly records 609 legacy dependency exceptions; this is not zero coupling.
4. Old release replay fixtures here are captured from unchanged released code, with no patch override, not historical customer exports. Twelve frozen histories and six dynamic histories passed current replay.
5. New/current source process protocol drills prove database compatibility and generation fencing, not mixed deployment-image acceptance. Cloud final SHA/digests/rollout remain required.
6. Branch protection API returns 403 due repository plan. CODEOWNERS plus a CI aggregate alone does not enforce merge gating.

Evidence: backend 1561 passed (180 conditional skips), frontend 154 passed, 17 architecture tests, 80 transaction tests, 30 real core integrations, 4 live Provider integrations, 12 long-running model lifecycle tests, schema migration, real native constraints/reference/chamfer/engineering, five browser combinations, old/new monitor role tests. Scope limitations and commands are in the report; optional external connector/device tests are not real acceptance.

Stable map update: added edge intent/native geometry, independent workflow DTO, mandatory evidence validator, replay/drill/monitor permissions test paths. No invariants weakened.
