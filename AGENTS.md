# Repository Agent Instructions

## CAD-Agent review routing

When the user explicitly requests `cad-agent-review`, post-change code review, regression review, or review/test-closure validation:

1. Load and follow `.agents/skills/cad-agent-review/SKILL.md` as the canonical workflow.
2. Use its referenced feature map, feature logic, invariants, test matrix, and maintenance rules.
3. Review and test only; do not modify business code or application tests.
4. During a review invocation, write only under `.agents/skills/cad-agent-review/**` and `docs/qa/code-review/**`.
5. Do not copy canonical Skill knowledge into agent-specific directories.

Agent-specific Skill files are adapters only. The shared maintenance protocol is `.agents/skills/cad-agent-review/references/multi-agent-maintenance.md`.
