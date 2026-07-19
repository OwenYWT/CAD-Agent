# ForgeCAD-Style Printable Example RAG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add metadata-aware printable CadQuery examples and retrieval boosts to improve generation stability.

**Architecture:** Keep the existing retriever interface but extend `find_similar` with optional criteria. Both TF-IDF and Chroma retrievers index enriched metadata and return metadata fields. CodeGen formats this metadata into the prompt.

**Tech Stack:** Python 3.11, scikit-learn TF-IDF, ChromaDB, pytest, JSON example files.

---

### Task 1: Retriever Metadata Tests

**Files:**
- Test: `backend/tests/test_example_retriever_metadata.py`

- [x] Write a test proving `ExampleRetriever.find_similar(..., part_type="bracket", features=["mounting holes"])` boosts a metadata-matching example.
- [x] Write a test proving returned examples include `features_used`, `manufacturing_notes`, `failure_modes`, `print_profile`, and `modeling_hints`.

### Task 2: TF-IDF Retriever Enhancement

**Files:**
- Modify: `backend/app/examples/retriever.py`

- [x] Include metadata in indexed text.
- [x] Add optional `part_type`, `features`, and `modeling_hint` arguments.
- [x] Apply deterministic metadata boosts after cosine similarity.
- [x] Return metadata fields to CodeGen.

### Task 3: Vector Retriever Enhancement

**Files:**
- Modify: `backend/app/examples/vector_retriever.py`

- [x] Include metadata in Chroma documents and metadata payload.
- [x] Add optional criteria arguments.
- [x] Apply deterministic metadata boosts to Chroma results.
- [x] Return the same shape as TF-IDF retriever.

### Task 4: Printable Example Library

**Files:**
- Create JSON examples under `backend/examples/printable_*.json`

- [x] Add 8 focused printable examples: box, bracket, PCB enclosure, pipe adapter, knob, hinge, snap-fit clip, vent panel.
- [x] Include source metadata fields for each example.

### Task 5: Orchestrator And Prompt Integration

**Files:**
- Modify: `backend/app/agent/orchestrator.py`
- Modify: `backend/app/agent/code_gen.py`

- [x] Pass `part_type`, `features`, and `modeling_hint` to retrieval during initial generation.
- [x] Format metadata notes into the example prompt.

### Task 6: Report And Verification

**Files:**
- Modify: `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`

**Commands:**
- `python -m pytest backend/tests/test_example_retriever_metadata.py backend/tests/test_parameters.py -q`
- `python -m pytest backend/tests/test_repair_history.py -q`
