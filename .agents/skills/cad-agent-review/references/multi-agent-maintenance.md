# Multi-Agent Maintenance Protocol

## One canonical source

`.agents/skills/cad-agent-review/` is the only maintained implementation of this Skill. Agent-specific locations may contain a small adapter pointing here, but must not copy review rules, feature maps, invariants, test matrices, scripts, or review memory.

This prevents Codex, Claude, and future agents from reviewing the same change with different knowledge.

## Supported entry patterns

- Codex: discover or explicitly invoke `cad-agent-review` from the repository Skill directory.
- Claude Code: invoke `.claude/skills/cad-agent-review/SKILL.md`; the adapter loads the canonical Skill.
- Other agents: follow root `AGENTS.md`, then load `.agents/skills/cad-agent-review/SKILL.md` and its references.
- Agents without repository Skill support: explicitly read the canonical `SKILL.md` before reviewing.

Adapters are navigation only. Translate platform tool names locally without changing review semantics, write boundaries, evidence classes, or conclusion rules.

## Shared ownership through Git

All developers maintain the same canonical files through normal branches and code review.

- Keep changes small and explain the observed review failure or new verified project fact that requires them.
- Update `feature-test-map.json` when implementation or test ownership moves.
- Update `feature-logic.md` or `invariants.md` only for durable rules supported by current code and evidence.
- Update `test-matrix.md` when commands, prerequisites, or evidence levels change.
- Validate stable knowledge changes with `scripts/validate_feature_map.py` and the Skill validator.
- Do not weaken an invariant merely to resolve a merge conflict; require human review of intended behavior.

## Concurrent reviews

Reports and review-memory files are append-only evidence. Use:

```text
YYYY-MM-DD-<head>-<review-id>.md
```

`review-id` should identify the agent or task, for example `codex-sidebar`, `claude-auth`, or `api-contract-2`. If a path exists, create a different ID. Never overwrite another agent's report or rewrite old evidence to match newer code.

When two reviews establish the same stable fact, keep both evidence records and consolidate the stable map or invariant in a later reviewed change.

## Allowed writes during review

The review invocation may write only:

- `.agents/skills/cad-agent-review/**`
- `docs/qa/code-review/**`

It must not modify business code or application tests. Agent adapters and repository routing files are maintained only through an explicit Skill-infrastructure task, not as a side effect of an ordinary review.

## Change checklist

1. Confirm canonical knowledge was not copied into an adapter.
2. Confirm business code and application tests are untouched.
3. Validate `feature-test-map.json` and Skill metadata.
4. Compile or run changed helper scripts.
5. Check Markdown links and `git diff --check`.
6. Record why the Skill changed in the commit or pull-request description.
