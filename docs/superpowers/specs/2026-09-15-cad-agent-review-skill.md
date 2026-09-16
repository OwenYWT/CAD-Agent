# CAD-Agent Review Skill Specification

Date: 2026-09-15
Updated: 2026-09-16

## Goal

Create a repository-shared, multi-agent Skill that reviews CAD-Agent changes, runs an appropriate regression matrix, maps changed behavior to existing functionality and tests, and records verified knowledge without modifying business logic.

## Distribution model

`.agents/skills/cad-agent-review/` is the single canonical source shared through Git. Codex may discover it directly. Claude uses `.claude/skills/cad-agent-review/SKILL.md` as a thin adapter. Other agents use root `AGENTS.md` or an explicit prompt to load the canonical file.

Agent-specific adapters must not copy the feature map, invariants, test matrix, scripts, or review memory. Platform-specific tool names may differ, but review semantics and safety boundaries remain identical.

## Hard scope

The review may read source, tests, docs, history, and evidence; run existing checks; and write only to `.agents/skills/cad-agent-review/**` and `docs/qa/code-review/**`.

It must never modify application source, application tests, migrations, deployment code, generated product artifacts, user files, or Git history. Skill infrastructure adapters and routing files change only through an explicit maintenance task, not during ordinary review execution.

## Required review behavior

Each invocation establishes the change range, classifies the change, scans changed text for encoding and fake-implementation risks, traces changed contracts across clients, services, workflows, persistence, runtimes, permissions, and artifacts, maps impact to the functional inventory and tests, runs the narrowest useful regression checks, expands to broader layers when required, and produces an evidence-based conclusion.

Conclusions distinguish PASS, PASS_WITH_LIMITATIONS, FAIL, and BLOCKED. Test results distinguish logic, service, runtime, and product evidence; skipped, mocked, contract-only, and historical evidence cannot be reported as real acceptance.

## Self-maintenance and concurrency

Each review may add an immutable report and compact verified-memory file using `YYYY-MM-DD-<head>-<review-id>.md`. Concurrent agents must use unique IDs and never overwrite existing evidence.

Stable feature mappings or invariants change only when current code plus concrete evidence shows a generalizable fact. All agents maintain the same canonical knowledge through Git review; merge conflicts must not be resolved by weakening invariants.

## Project invariants

Candidate, draft, and committed revision identity; durable task authority; stale-base and ABA protection; idempotency; artifact provenance; server-side permission boundaries; real-kernel versus mock evidence; and preservation of the last valid model are mandatory review gates.
