# ForgeCAD-Style Validation And Repair Loop Design

## Goal
Make CAD-Agent's existing generate/execute retry behavior visible and auditable, borrowing ForgeCAD's evidence-driven loop: build, run, inspect, repair, and report.

## Source Inspiration
- Project: `forgecad-public-kit`
- Borrowed idea: `agent edits .forge.js -> forgecad run -> forgecad inspect <evidence> -> iterate`
- Local references:
  - `../forgecad-public-kit/README.md` section "AI And Agent Workflows"
  - `../forgecad-public-kit/skills/forgecad-inspect-model/SKILL.md`
  - `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`

## CAD-Agent Scope: Medium Version B
- Preserve the existing repair behavior in `Orchestrator._execute_with_retry`.
- Add explicit `repair_history` metadata to responses.
- Record repair attempts for code validation, static analysis, geometry validation, vision mismatch, and sandbox execution errors.
- Show repair history in the frontend result area so users can see whether the model passed directly or was auto-repaired.
- Do not add a new multi-agent architecture or more LLM repair rounds in this pass.

## Response Shape
```json
{
  "repair_history": [
    {
      "attempt": 1,
      "stage": "execution",
      "error_type": "ExecutionError",
      "message": "CadQuery execution failed",
      "action": "fix_error",
      "status": "repaired"
    }
  ]
}
```

## Acceptance Criteria
- Successful results include empty `repair_history` when no repair was needed.
- Successful results include repair entries when a retry/fix path was used.
- Failed results preserve repair entries collected before final failure.
- WebSocket and REST responses expose the same metadata.
- Frontend renders repair history without requiring a separate analysis request.
