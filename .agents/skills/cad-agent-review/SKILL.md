---
name: cad-agent-review
description: Review CAD-Agent changes for regressions, fake implementations, encoding corruption, contract drift, related-feature impact, and incomplete test closure; run safe checks and record evidence without modifying business code.
metadata:
  short-description: Review CAD-Agent changes without fixing business code
---

# CAD-Agent Review

Use this repository-shared Skill after a change is finished, before merge, or whenever one developer's change may affect another feature.

## Canonical source

`.agents/skills/cad-agent-review/` is the only source of truth. Codex may discover it directly. Claude and other agents may use thin adapters, but adapters must load this file and must not copy its maps, rules, scripts, or memory. Follow `references/multi-agent-maintenance.md` when maintaining the Skill.

## Hard boundary

This Skill is a reviewer, not an implementer.

- Read business code, tests, docs, Git history, and evidence; run existing checks when prerequisites are available.
- Never edit application code, application tests, migrations, deployment code, generated product artifacts, user files, or Git history.
- Never commit, push, merge, reset, stash, clean, delete, or rewrite history.
- During review, write only under `.agents/skills/cad-agent-review/**` and `docs/qa/code-review/**`.
- Report defects with path, symbol, impact, evidence, and suggested follow-up; do not repair them.
- A developer performs fixes and requests a new review.

## Sources of truth

Read in order:

1. Selected Git range and current working tree.
2. Current implementation and tests.
3. `docs/functional-inventory-2026-09-14.md`.
4. `references/feature-test-map.json` and `references/feature-logic.md`.
5. `references/project-architecture.md`, `references/invariants.md`, and `references/test-matrix.md`.
6. Existing QA reports only as dated evidence, never as proof of current behavior.

## Workflow

### 1. Establish scope

- Inspect `git status --short --branch`.
- Prefer a user-provided base/range. Otherwise use the PR base or merge-base with the tracked default branch. Include staged, unstaged, and untracked files for local reviews.
- List changed paths, change class, and affected frontend, API, service, persistence, workflow, runtime, connector, deployment, test, and documentation surfaces.

### 2. Check encoding and fake implementation risks

Run:

```text
python .agents/skills/cad-agent-review/scripts/scan_review_risks.py --base <base> --head HEAD --worktree
```

Manually inspect every lead. Check mojibake, untranslated UI strings, TODO/FIXME/NotImplementedError, empty production branches, static success, placeholder IDs or URLs, mock data in product paths, UI-only success, incomplete endpoints, and simulated evidence described as real acceptance. Scanner findings are leads, not automatic defects.

### 3. Trace compatibility and related impact

For each changed symbol, endpoint, event, type, schema, status, artifact, or configuration value:

1. Find callers and consumers with `rg`.
2. Compare frontend and backend contracts.
3. Check WebSocket production, replay, reducers, and terminal states.
4. Check migrations, repositories, RLS/tenant checks, and persistence reads.
5. Check workflow idempotency, retry, cancellation, stale-base, and ABA guards.
6. Check revision, candidate, draft, artifact, BOM, scene, export, release, and permission boundaries.
7. Map the impact to `feature-test-map.json`; apply `feature-logic.md` and `invariants.md`.

A related feature is affected even if its file did not change when it consumes a changed contract, event, identity, status, or persisted record.

### 4. Run the regression matrix

Use focused tests first, then expand according to `references/test-matrix.md`. At minimum for code changes attempt:

```text
git diff --check
python .agents/skills/cad-agent-review/scripts/validate_feature_map.py
```

Record each selected layer as `PASS`, `FAIL`, `BLOCKED`, `SKIPPED`, `CONTRACT_ONLY`, or `REAL_ACCEPTANCE`. Never silently skip a required test or treat a mock, unit test, or historical report as real acceptance.

### 5. Decide closure

- `PASS`: no material finding and required available layers passed.
- `PASS_WITH_LIMITATIONS`: no material regression found, but a declared external or product boundary remains unverified.
- `FAIL`: regression, contract drift, fake implementation, encoding defect, or required test failure found.
- `BLOCKED`: required evidence unavailable; do not claim correctness.

### 6. Write evidence and self-maintain

Choose a unique lowercase kebab-case `review-id`, preferably identifying agent or task, such as `codex-sidebar` or `claude-api-contract`. Never overwrite an existing report.

```text
docs/qa/code-review/YYYY-MM-DD-<head>-<review-id>.md
.agents/skills/cad-agent-review/references/review-memory/YYYY-MM-DD-<head>-<review-id>.md
```

Use `docs/qa/code-review/templates/review-report.md`. Include reviewer agent, review ID, time, exact range, working-tree state, commands, affected feature IDs, findings, evidence classes, limitations, and conclusion.

Add review memory only for reusable verified observations. Update stable maps or invariants only when current code plus concrete evidence supports a reusable fact. Follow `references/knowledge-maintenance.md`.

## Repository priorities

When relevant, always inspect candidate/draft/committed identities; durable facts versus UI projections; stale-base, state-version, idempotency and ABA protection; artifact provenance; permissions; real runtime versus mocks; engineering evidence binding; optional external dependencies; and incomplete boundaries in the functional inventory.

## Deliverable

Return the conclusion first, followed by findings ordered by severity, affected call chains, test evidence, gaps, files written, and an explicit statement that no business code was modified.
