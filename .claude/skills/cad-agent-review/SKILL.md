---
name: cad-agent-review
description: Review CAD-Agent changes for regressions and test closure without modifying business code.
---

# CAD-Agent Review Adapter

This is a thin Claude Code adapter. Before reviewing, load and follow:

```text
../../../.agents/skills/cad-agent-review/SKILL.md
```

Treat that repository-shared file and its referenced resources as the only source of truth. Translate tool names if necessary, but preserve its review workflow, evidence classifications, conclusion rules, and write boundary.

Do not copy or independently maintain feature maps, invariants, test matrices, scripts, or review memory under `.claude/`.
