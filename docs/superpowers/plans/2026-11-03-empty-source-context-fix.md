# Empty Source Context Compatibility Fix Implementation Plan

> **For agentic workers:** Execute this plan task-by-task with TDD checkpoints.

**Goal:** Allow an empty `SourceContextV1` returned by the planner to represent the same state as an omitted source context, while preserving strict comparison for real source references.

**Architecture:** Normalize only the empty context at the validation boundary in `requirement_binding.py`. Keep populated contexts unchanged so source provenance remains exact. Add focused regression tests alongside the existing source-context tests.

**Tech Stack:** Python, Pydantic v2, pytest.

---

### Task 1: Add regression coverage

**Files:**
- Modify: `backend/tests/test_design_source_context.py`

- [x] Add a test for a modification objective with one overall-dimension requirement and an explicitly empty `source_context`; assert `validate_requirement_bindings` returns `status == "bound"`.
- [x] Run the focused test and verify it fails with `requirement_binding_mismatch: source context differs from saved user input`.

### Task 2: Normalize empty contexts at the validation boundary

**Files:**
- Modify: `backend/app/validation/requirement_binding.py`

- [x] Add a private helper that converts only a default `cad_geometry` `SourceContextV1` with no design requests, external references, or unverified aspects to `None`.
- [x] Apply the helper to both the extracted expected context and `contract.source_context` before comparison.
- [x] Leave populated contexts and non-default execution scopes strict and unchanged.

### Task 3: Verify the regression and adjacent behavior

**Files:**
- No additional files.

- [x] Run the new focused test and the existing source-context tests.
- [x] Run the full requirement-binding/source-obligation test selection.
- [x] Inspect the diff and confirm no model, runtime, or persistence code changed.
