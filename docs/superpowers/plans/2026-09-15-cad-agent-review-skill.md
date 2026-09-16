# CAD-Agent Review Skill Implementation Plan

> For agentic workers: the Skill is read-only over business code. Ordinary reviews write only Skill knowledge and review-report paths.

**Goal:** Add a shared multi-agent Skill for evidence-based code review and regression testing without changing business logic.

**Architecture:** `.agents/skills/cad-agent-review/` is the canonical source. `AGENTS.md` and `.claude/skills/cad-agent-review/SKILL.md` are thin routing layers. A checked-in feature/test map and invariant catalog provide context. Standard-library helpers scan changed text and validate the map. Collision-resistant dated reports and per-review memory support concurrent reviewers.

**Tech Stack:** Markdown, JSON, Python standard library, Git, pytest, Node test runner, npm, and existing project test commands.

## File plan

- `.agents/skills/cad-agent-review/SKILL.md`: agent-neutral entrypoint, boundaries, workflow, conclusion policy.
- `.agents/skills/cad-agent-review/agents/openai.yaml`: Codex/OpenAI UI metadata.
- `.agents/skills/cad-agent-review/references/project-architecture.md`: current system flow and module boundaries.
- `.agents/skills/cad-agent-review/references/invariants.md`: mandatory correctness invariants.
- `.agents/skills/cad-agent-review/references/test-matrix.md`: safe, integration, runtime, and product test tiers.
- `.agents/skills/cad-agent-review/references/feature-test-map.json`: inventory domains mapped to implementation and tests.
- `.agents/skills/cad-agent-review/references/knowledge-maintenance.md`: evidence-driven self-upgrade rules.
- `.agents/skills/cad-agent-review/references/multi-agent-maintenance.md`: single-source, ownership, concurrency, and conflict policy.
- `.agents/skills/cad-agent-review/scripts/scan_review_risks.py`: read-only diff scanner.
- `.agents/skills/cad-agent-review/scripts/validate_feature_map.py`: path and schema validator.
- `.claude/skills/cad-agent-review/SKILL.md`: thin Claude adapter.
- `AGENTS.md`: repository-wide routing for compatible agents.
- `docs/qa/code-review/USAGE.md`: usage for Codex, Claude, and other agents.
- `docs/qa/code-review/**`: report template and dated evidence.
- `docs/README.md`: discoverability entry.

## Validation

1. Validate Skill frontmatter and structure with the Skill creator validator.
2. Validate the JSON map and all referenced paths.
3. Compile and smoke-test helper scripts.
4. Verify adapters resolve to the canonical Skill and contain no copied knowledge.
5. Check Markdown links and `git diff --check`.
6. Verify the final diff contains no business-code or application-test changes.
