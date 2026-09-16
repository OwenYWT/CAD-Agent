# Knowledge Maintenance Rules

The Skill has two kinds of memory:

- Stable knowledge: `project-architecture.md`, `invariants.md`, `feature-logic.md`, `test-matrix.md`, and `feature-test-map.json`.
- Review evidence: append-only dated files under `references/review-memory/` and `docs/qa/code-review/`.

Read `multi-agent-maintenance.md` before changing either category.

## Add review memory when

- A changed path was traced to a feature domain and concrete tests.
- A test command produced a meaningful result or reproducible block reason.
- A boundary, contract, limitation, or impact relationship is supported by current code and evidence.

Each record includes reviewer agent, review ID, time, range, working-tree state, changed paths, affected domain IDs, implementation symbols, tests actually run, evidence classification, findings, and unresolved limits.

## Promote stable knowledge only when

- Current implementation and concrete evidence support the fact.
- The fact applies beyond one change or workstation.
- The change does not contradict an existing invariant without explicit human review.
- The update is small, attributable, and reviewable.

Do not promote assumptions, skipped tests, mock-only behavior, old reports, or unexplained failures. Keep uncertain observations in the dated report as open questions.

## Safe update order

1. Write the dated report without overwriting existing evidence.
2. Add review memory only for reusable, verified observations.
3. Make the smallest stable map or invariant update justified by evidence.
4. Run `scripts/validate_feature_map.py` and the Skill validator.
5. Verify the final diff contains no business-code or application-test changes made by the reviewer.

Review memory is append-only by file. Never rewrite older records to make a later change look green.
