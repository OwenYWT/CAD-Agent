# ForgeCAD-Style Engineering Brief Design

## Summary

Add a structured `design_brief` to CAD-Agent generation results. The brief captures how the agent interpreted the user's natural-language request before code generation, with explicit assumptions, manufacturing posture, critical dimensions, printability targets, and acceptance criteria.

This adapts ForgeCAD's design-spec discipline into CAD-Agent without interrupting the current generation flow.

## Source Attribution

- Source project: `forgecad-public-kit`
- Borrowed idea: make a design brief the source of truth before CAD code is produced.
- Local references studied:
  - `../forgecad-public-kit/skills/forgecad-design-spec/SKILL.md`
  - `../forgecad-public-kit/skills/forgecad-design-spec/references/default-profiles.md`
  - `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`

ForgeCAD emphasizes these principles:

- The object narrative comes before dimensions.
- Every important number should have a reason.
- Manufacturing process is a design decision, not an implicit default.
- Vague requests should become honest assumptions and explicit open questions.
- Verification criteria should be known before modeling.

## Current CAD-Agent State

CAD-Agent already has several pieces that can support this feature:

- `backend/app/models/schemas.py` defines `CADPlan` with `description`, `part_type`, `dimensions`, `features`, `constraints`, `ambiguities`, and `modeling_hint`.
- `backend/app/agent/planner.py` produces `CADPlan` from user prompts.
- `backend/app/agent/code_gen.py` uses the plan and examples to produce CadQuery or ezdxf code.
- `GenerateResponse` and `GenerationResult` already include `plan` for the understood requirement brief.
- `frontend/src/types/index.ts` defines `CADPlanBrief` and exposes `plan` on `GenerationResult`.
- The analysis tab already contains inspection, version history, repair history, design analysis, and run timeline panels.

Current limitations:

- `CADPlan` is useful for code generation but too terse for user trust and engineering review.
- Assumptions are mixed with constraints and ambiguities, so users cannot quickly audit what the agent invented.
- Manufacturing posture is not first-class, even though the product goal is directly usable CAD for fabrication.
- Critical dimensions do not include reasons, so a user cannot tell why a value was chosen.
- Acceptance criteria are implicit in validation and prompts rather than visible before generation.
- Code generation can use the plan but not a richer brief that states printability and functional priorities.

## Goal

Add a ForgeCAD-style engineering brief that lets users see and audit the agent's interpretation of the request.

The feature should:

- Explain the requested artifact in plain engineering language.
- State the intended manufacturing posture, defaulting to 3D-printable prototype when no stronger signal exists.
- List assumptions separately from unresolved open questions.
- Attach reasons to critical dimensions.
- State functional requirements and printability targets.
- Provide acceptance criteria that the resulting model should satisfy.
- Flow into code generation so the model is built against the brief, not only displayed after the fact.
- Preserve the existing one-shot generation UX.

## Non-Goals

- Do not add a blocking pre-generation approval step in this phase.
- Do not add multi-turn requirement elicitation unless the existing planner already asks naturally through `ambiguities`.
- Do not implement production certification, simulation, finite element analysis, or guaranteed manufacturing readiness.
- Do not replace `CADPlan`; enrich it with a companion brief.
- Do not require new external services or web access.
- Do not expose hidden chain-of-thought.

## Proposed Scope: B, Recommended Version

### Backend Schema

Add new Pydantic models in `backend/app/models/schemas.py`:

```python
class CriticalDimension(BaseModel):
    name: str
    value: float | None = None
    unit: str = "mm"
    reason: str = ""


class DesignBrief(BaseModel):
    intent_summary: str = ""
    artifact_type: str = "custom"
    manufacturing_posture: str = "printable"
    assumptions: list[str] = []
    critical_dimensions: list[CriticalDimension] = []
    functional_requirements: list[str] = []
    printability_targets: list[str] = []
    acceptance_criteria: list[str] = []
    open_questions: list[str] = []
```

Add fields:

- `CADPlan.design_brief: DesignBrief | None = None`
- `GenerateResponse.design_brief: DesignBrief | None = None`
- `GenerationResult.design_brief: DesignBrief | None = None`

Compatibility rule:

- Existing clients that read `plan` continue working.
- If older planner output lacks `design_brief`, backend derives a safe fallback from `CADPlan`.

### Planner Contract

Extend the planner prompt so the LLM returns `design_brief` inside the JSON response.

The planner should produce concise, user-readable engineering statements, not hidden reasoning. Examples:

```json
{
  "description": "Desk cable clip with rounded base and snap slot",
  "part_type": "bracket",
  "dimensions": { "length": 60, "width": 22, "height": 16, "wall_thickness": 2.4 },
  "features": ["rounded base", "snap slot", "filleted edges"],
  "constraints": ["print without supports where possible"],
  "ambiguities": ["exact cable diameter not specified"],
  "modeling_hint": "extrude_cut",
  "design_brief": {
    "intent_summary": "A small 3D-printable clip that holds one cable against a desk edge.",
    "artifact_type": "clip",
    "manufacturing_posture": "printable",
    "assumptions": ["Cable diameter is approximately 6 mm", "Part is intended for indoor desk use"],
    "critical_dimensions": [
      { "name": "wall_thickness", "value": 2.4, "unit": "mm", "reason": "Six 0.4 mm nozzle lines give a sturdy printable wall" }
    ],
    "functional_requirements": ["Hold cable without pinching", "Avoid sharp user-facing edges"],
    "printability_targets": ["Minimum wall thickness at least 2.0 mm", "Fillets on exposed edges", "Avoid unsupported overhangs above 45 degrees"],
    "acceptance_criteria": ["Model exports STL and STEP", "Geometry is watertight", "Cable channel remains open"],
    "open_questions": ["Confirm exact cable diameter if fit is critical"]
  }
}
```

### Fallback Builder

Add a small backend fallback helper, likely in `backend/app/agent/design_brief.py`, to build a brief when planner output is missing or partial.

Fallback behavior:

- `intent_summary` comes from `CADPlan.description`.
- `artifact_type` comes from `CADPlan.part_type`.
- `manufacturing_posture` defaults to `printable` because CAD-Agent's stated primary target is 3D printing.
- `assumptions` include safe generic assumptions such as prototype use and metric dimensions when not already covered.
- `critical_dimensions` are built from `CADPlan.dimensions`; reasons use concise labels such as `User-provided or planner-inferred dimension`.
- `functional_requirements` are built from `CADPlan.features` and `CADPlan.constraints`.
- `printability_targets` include watertight geometry, minimum wall thickness, fillets on exposed edges, and build-volume awareness.
- `acceptance_criteria` include successful code execution and export availability.
- `open_questions` come from `CADPlan.ambiguities`.

### Code Generation Use

Update `backend/app/agent/code_gen.py` so the generated-code prompt includes the design brief when available.

The prompt should instruct the model to:

- Respect `intent_summary` and `functional_requirements`.
- Use `critical_dimensions` as the primary numeric constraints.
- Preserve `printability_targets` when choosing walls, holes, fillets, and supports.
- Avoid adding unrequested complex features when assumptions are explicit.
- Make parameters align with critical dimensions where practical.

### API Response Flow

Update orchestration so generated results carry `design_brief`:

- After `plan = await planner.plan_new(...)`, ensure `plan.design_brief` exists by using the fallback builder.
- When returning `GenerateResponse`, set `design_brief=plan.design_brief`.
- When converting to `GenerationResult`, preserve `design_brief`.
- For direct `execute_code`, `design_brief` may be `None` because there is no natural-language planning pass.
- For `modify_part`, either keep the previous brief if available in context or generate a new brief from the modification plan only if already easy. Recommended first pass: preserve previous brief when context stores it, otherwise return `None`.

### Frontend UI

Add `frontend/src/components/DesignBriefPanel.tsx`.

Display sections:

- Intent: short summary and artifact type.
- Manufacturing: posture badge, with clear copy that this is an intended posture, not certification.
- Assumptions: bullet list.
- Critical Dimensions: table with name, value, unit, reason.
- Requirements: functional requirements and printability targets.
- Acceptance Criteria: checklist-style list.
- Open Questions: warning-styled list when present.

Place `DesignBriefPanel` near the top of the analysis tab, directly below `AgentRunTimeline` and above `InspectReportPanel`.

Use English UI labels for the new component to avoid Windows console encoding issues in generated source files.

### Traceability

Add an `Adaptation 7` section to `docs/case-study-adaptations/CAD-Agent-adaptation-report.md` with:

- Source references.
- Borrowed idea.
- Backend schema and planner changes.
- Frontend panel changes.
- Verification commands and results.
- Non-goals kept.

## Acceptance Criteria

- A normal successful generated model includes `design_brief` in the backend response.
- Existing tests that only expect `plan` continue passing.
- If planner JSON omits `design_brief`, generation still succeeds with a fallback brief.
- Frontend builds successfully with `DesignBriefPanel` wired into the analysis tab.
- The brief is visible after generation and remains available in saved/restored result payloads because it is part of `GenerationResult`.
- The feature does not block generation for user confirmation.
- The adaptation report records the ForgeCAD source and CAD-Agent implementation details.

## Test Plan

Backend:

- Add `backend/tests/test_design_brief.py` for schema validation and fallback generation.
- Add planner parsing test for JSON that includes `design_brief`.
- Add response model test proving `GenerateResponse` and `GenerationResult` carry the brief.
- Run relevant existing orchestration/persistence tests to ensure result payloads still serialize.

Frontend:

- Extend `frontend/src/types/index.ts` with `DesignBrief` and `CriticalDimension`.
- Run `cd frontend && npm.cmd run build` for type and integration validation.

Expected verification commands:

- `python -m pytest backend\tests\test_design_brief.py backend\tests\test_deploy_websocket.py backend\tests\test_model_snapshots.py -q`
- `cd frontend && npm.cmd run build`

## Risks And Mitigations

- Risk: planner output becomes too verbose or invalid JSON.
  - Mitigation: keep `design_brief` concise and build fallback from existing `CADPlan` if missing.
- Risk: users treat the brief as certified manufacturing advice.
  - Mitigation: UI labels should say intended posture and acceptance criteria, not certification.
- Risk: prompt token usage increases.
  - Mitigation: cap brief fields to short lists in parser/fallback, and keep frontend display concise.
- Risk: direct code execution has no brief.
  - Mitigation: allow `design_brief` to be nullable and document that only natural-language planning produces it.

## Open Design Decisions

- Store brief in conversation context for modifications: recommended, but implementation can be minimal if context persistence is already straightforward.
- Add editing of brief before generation: explicitly deferred to a future phase.
- Add manufacturing processes beyond printable: schema supports strings, but the first implementation should default to `printable` unless the user prompt names another process.
