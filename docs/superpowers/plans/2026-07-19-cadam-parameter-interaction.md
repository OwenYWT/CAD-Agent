# CADAM-Style Parameter Interaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add CADAM-style numeric parameter extraction and no-LLM parameter re-execution to CAD-Agent.

**Architecture:** A backend parser owns parameter metadata and safe code replacement. Generation/execution responses include parsed parameters. The frontend renders grouped controls from that schema and posts updated code to the existing execute path.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, pytest, React, TypeScript, Vite.

---

### Task 1: Backend Parameter Parser

**Files:**
- Create: `backend/app/parameters.py`
- Test: `backend/tests/test_parameters.py`

- [ ] Write failing tests for parsing comments, ranges, units, and replacement.
- [ ] Implement `extract_parameters(code)` and `apply_parameter_values(code, values)`.
- [ ] Run `python -m pytest backend/tests/test_parameters.py -v`.

### Task 2: API Schema Integration

**Files:**
- Modify: `backend/app/models/schemas.py`
- Modify: `backend/app/agent/orchestrator.py`
- Modify: `backend/app/api/execute.py`

- [ ] Add `CADParameter` response model.
- [ ] Include `parameters` in generation/modify/execute responses.
- [ ] Add request fields for parameter value updates if needed.

### Task 3: Prompt Contract

**Files:**
- Modify: `backend/app/agent/prompts.py`

- [ ] Require codegen to declare editable numeric parameters at the top.
- [ ] Document group, label, and range comment conventions.

### Task 4: Frontend Types And Panel

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/components/ParameterPanel.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] Add parameter metadata types.
- [ ] Render grouped sliders using backend `min/max/step/unit/comment`.
- [ ] On debounce, call existing code execution path with updated code.

### Task 5: Verification

**Commands:**
- `python -m pytest backend/tests/test_parameters.py -v`
- `python -m pytest backend/tests -v`
- `npm.cmd run build`
