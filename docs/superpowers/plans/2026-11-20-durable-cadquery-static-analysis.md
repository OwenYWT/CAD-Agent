# Durable CadQuery Static Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Durable Agent V2 from executing CadQuery source that subtracts an already modified body from its own parent.

**Architecture:** Extend the existing AST analyzer with narrow provenance tracking, then invoke it at the Durable execution boundary before Podman. Reuse the existing structured validation failure and bounded repair loop rather than adding a second repair mechanism.

**Tech Stack:** Python 3.11, `ast`, FastAPI/Temporal activity handlers, pytest.

---

### Task 1: Reproduce the destructive double subtraction [x]

**Files:**
- Test: `backend/tests/test_code_analyzer.py`

- [x] Add a test containing the exact `result -> top_wp -> groove_cut -> result - groove_cut` data flow and assert that a double-subtraction warning is returned.
- [x] Add negative tests for direct assignment of a body-derived `cutBlind()` result and for subtraction of an independent cutter.
- [x] Run `python -m pytest tests/test_code_analyzer.py -q` from `backend` and confirm the bad-pattern test fails because no warning is produced.

### Task 2: Add AST provenance analysis [x]

**Files:**
- Modify: `backend/app/sandbox/code_analyzer.py`
- Test: `backend/tests/test_code_analyzer.py`

- [x] Track simple-name assignment dependencies and whether an assigned expression calls `cutBlind`, `cutThruAll`, or `hole` on a value derived from another body variable.
- [x] Detect both `body = body - derived_result` and `body = body.cut(derived_result)` when the right-hand value is an already modified derivative of `body`.
- [x] Emit an actionable warning that says to assign the subtractive operation result directly.
- [x] Run `python -m pytest tests/test_code_analyzer.py -q` and confirm all analyzer tests pass.

### Task 3: Enforce analysis in Durable execution [x]

**Files:**
- Modify: `backend/app/workflows/handlers/cad_execution.py`
- Test: `backend/tests/test_durable_static_analysis.py`

- [x] Add a failing handler-level test with a fake backend and the known bad source, asserting the backend is not invoked and the handler raises `static_analysis_failed` with category `validation` and runtime type `StaticAnalysis`.
- [x] Call `analyze_code(source_code)` after persisted hash verification and before runtime execution.
- [x] Raise a structured, non-retryable `ApplicationError` whose details match `_execution_failure()` and `decide_repair()` expectations.
- [x] Run the new handler test and existing durable repair tests.

### Task 4: Improve generation and repair guidance [x]

**Files:**
- Modify: `backend/app/agent/code_gen.py`
- Test: `backend/tests/test_durable_agent_repair.py`

- [x] Add a failing prompt test requiring guidance against subtracting a body-derived `cutBlind()` result twice.
- [x] Add the minimal generation and static-analysis repair wording.
- [x] Run the focused prompt tests and confirm they pass.

### Task 5: Verify and activate [-]

**Files:**
- No production file additions.

- [x] Run the focused analyzer, Durable workflow, execution, and repair test modules.
- [x] Run broader assembly and geometry validation tests.
- [x] Run `python -m compileall app` and `git diff --check`.
- [x] Stop the duplicate worker and start exactly one UTF-8 worker using the modified host code.
- [x] Confirm API health, worker process count, and Podman service state.
- [x] Submit a fresh equivalent generation task when an authenticated local request fixture is available; otherwise report the exact automated verification boundary without claiming end-to-end generation success.

Verification outcome on 2026-11-20:

- `197 passed` across the focused analyzer, Durable workflow, repair, assembly, and geometry suites.
- The first live task, `891b26b6-9ccd-4082-a040-238358d24dce`, exposed catastrophic backtracking in the pre-existing loop-level `show_object()` regex after the new execution-boundary analysis was activated. A subprocess timeout regression test reproduced it, and the check now uses bounded AST ancestry instead.
- The retry task, `645968a3-7464-43ff-9542-95068a8e8e36`, completed source analysis and entered the existing source-repair loop, confirming that the Durable execution path no longer hangs before Podman and that the original body-derived double-subtraction failure did not recur.
- End-to-end generation is not yet successful. The retry was blocked by a separate assembly validation gap: a repaired component containing multiple solids was accepted before combination, and the final artifact measured `153 x 60 x 35 mm` with `solid_count_mismatch`. This plan does not claim that independent assembly-component validation issue is fixed.
