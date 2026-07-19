# ForgeCAD-Style Printable Example RAG Design

## Goal
Upgrade CAD-Agent's existing example retrieval into a 3D-printing-oriented CadQuery template library with metadata-aware retrieval, borrowing ForgeCAD's broad example-kit strategy.

## Source Inspiration
- Project: `forgecad-public-kit`
- Borrowed idea: CAD agents become more reliable when they work from a curated library of runnable, inspectable CAD-as-code examples rather than generating every modeling pattern from scratch.
- Local references:
  - `../forgecad-public-kit/README.md` repository contents and examples workflow
  - `../forgecad-public-kit/examples/api/` API pattern examples
  - `../forgecad-public-kit/examples/mechanical/` mechanical examples
  - `../forgecad-public-kit/examples/products/` product examples

## CAD-Agent Scope: Medium Version B
- Add curated printable CadQuery examples with explicit metadata.
- Extend retrieval documents with metadata such as part type, features, modeling hints, print profile, manufacturing notes, and failure modes.
- Add optional retrieval criteria from `CADPlan` so part type/features/modeling hint can boost relevant examples.
- Include metadata in CodeGen prompt formatting so the LLM sees what pattern to reuse and what failure modes to avoid.

## Out Of Scope
- Full template-code generation that bypasses the LLM.
- New vector database migration format.
- Automatic verification of every example through the sandbox in this pass.

## Acceptance Criteria
- Retriever returns metadata-rich examples.
- Metadata boosts relevant printable examples when query text is ambiguous.
- Orchestrator passes planner part type/features/modeling hint into retrieval for new generation.
- CodeGen prompt includes manufacturing notes and failure modes when examples provide them.
- Comprehensive report records the source, adaptation, and verification.
