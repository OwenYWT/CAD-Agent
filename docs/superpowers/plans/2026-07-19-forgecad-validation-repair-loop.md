# ForgeCAD-Style Validation And Repair Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add visible repair-loop metadata to CAD-Agent generation and execution results.

**Architecture:** Reuse the existing orchestrator retry loop. Add a typed repair-history model to API schemas, append repair events at existing fix points, and render the history in the frontend analysis/parameter side panel.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, pytest, React, TypeScript, Vite.

---

### Task 1: Response Model

**Files:**
- Modify: `backend/app/models/schemas.py`
- Test: `backend/tests/test_repair_history.py`

- [ ] Add `RepairStep` Pydantic model.
- [ ] Add `repair_history` to `GenerationResult` and `GenerateResponse`.
- [ ] Test serialization for successful and failed responses.

### Task 2: Orchestrator Recording

**Files:**
- Modify: `backend/app/agent/orchestrator.py`
- Test: `backend/tests/test_repair_history.py`

- [ ] Initialize `repair_history` inside `_execute_with_retry`.
- [ ] Append entries before each `fix_error` / `fix_visual_issues` retry.
- [ ] Return collected history on success and failure.

### Task 3: WebSocket Consistency

**Files:**
- Modify: `backend/app/api/websocket.py`

- [ ] Include `repair_history` in the manual `execute_code` WebSocket response.

### Task 4: Frontend Display

**Files:**
- Modify: `frontend/src/types/index.ts`
- Create: `frontend/src/components/RepairHistory.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] Add `RepairStep` type.
- [ ] Render a compact repair timeline when entries exist.
- [ ] Keep UI hidden for empty history.

### Task 5: Report Update And Verification

**Files:**
- Modify: `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`

**Commands:**
- `python -m pytest backend/tests/test_repair_history.py backend/tests/test_parameters.py -q`
- `cd frontend && npm.cmd run build`
