# Native engineering reliability implementation

Base: 09c3ccb (includes the post-evaluation draft and edge-scope fixes).
Evaluation reference: af0a090, fixed Kimi 2.7 Code ten-case run of 2026-09-25/26.

Core implementation and final deterministic regression are complete;
the full live-model acceptance is blocked by provider quota. See
[the detailed report](native-reliability-report.md) for verified scope and remaining failures.

- [x] Explicit coordinate semantics and retained-plan compatibility
- [x] Signed coordinate constraints and deterministic geometric compilation
- [x] Structured execution evidence and bounded repair routing
- [x] Native hole entrance semantics and opposite-entrance preservation
- [x] Capability contract, sweep/loft/revolution/pattern execution and editing
- [x] Tangency/self-intersection minimal reproduction
- [x] Final-solid and STEP checks for the covered native contracts
- [ ] Complete engineering-basis/derived-dimension verification for arbitrary prompts
- [ ] Full fixed live-provider evaluation; stopped on real quota failure

No production deployment or branch publication is part of this implementation turn.

## Verified so far (not final acceptance)

- Explicit Body frames: three planes, signed offsets, translated/rotated Body, final STEP and through-cut thickness edits. Legacy placement and all eight retained evaluation source plans preserve their canonical bytes.
- Native editable profiles, hole entrance forms, lofts, sweeps with straight ends, revolutions and patterns tested on the real FreeCAD 1.1.3 kernel. Pattern Body Tip/export mismatch was caught and fixed.
- The retained no-effect cut now succeeds after changing only its reversed flag. Intent guard rejects dimensional or unrelated-operation changes.
- Exact tangent post/wall geometry fails independently in native features and direct OCCT booleans; it is rejected with the actual feature identity, without nudging dimensions.
- Host/runner/prompt capability parity is checked; an actual old sandbox image is rejected before execution. Rejected provider responses are stored privately with digest metadata.
- A browser parameter task exposed a second global defect: validation phases ignored configured sandbox memory and used 512 MiB. All execution composition sites now resolve deployment memory while preserving existing operation allocation floors and unchanged wire defaults.
- Hermetic backend regression: 1639 passed; frontend: 155 passed, lint/typecheck/build passed. Skipped external suites are not counted as validated.
- Native, browser, durable and database evidence is recorded separately in the detailed report. The fixed Kimi run generated and saved P01/P03/P08, then stopped during P17 repair on provider quota; the remaining six cases did not run.
- P17 also proved a missing geometry tag and an independent-versus-derived constraint conflict. Schema tags and planner signatures are now generated consistently; arbitrary raw-sketch redundancy and trusted engineering-basis verification remain open.
