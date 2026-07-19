# ForgeCAD-Style Inspect Report Design

## Background

CAD-Agent is being improved by selectively adapting proven ideas from CADAM and forgecad-public-kit. The previous adaptation added ForgeCAD-style curated printable examples. The next useful ForgeCAD idea is the inspect loop: after model code runs, the system should surface deterministic evidence about the produced CAD model instead of only returning files and preview geometry.

## Source Attribution

- Source project: `forgecad-public-kit`
- Borrowed idea: generated CAD should be accompanied by inspectable evidence from the run/inspect cycle, so the user and the agent can judge whether the model is printable and trustworthy.
- Local references:
  - `../forgecad-public-kit/README.md`
  - `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`
  - `../forgecad-public-kit/skills/forgecad-inspect-model/SKILL.md`

## Current CAD-Agent State

CAD-Agent already has most backend evidence available:

- `backend/app/models/schemas.py` defines `InspectCheck` and `InspectReport`.
- `backend/app/validation/inspect.py` aggregates `GeometryValidation` into an inspect report.
- `backend/app/validation/geometry_validator.py` computes bounding box, volume, watertight status, build-volume fit, minimum wall thickness, and print warnings.
- `backend/app/agent/orchestrator.py` already imports `build_inspect_report` and can attach `inspect_report` to generation results.
- The frontend currently exposes repair history and some validation snippets, but there is no dedicated, user-facing printable inspection panel.

The implementation should therefore productize and verify existing inspect evidence rather than creating a separate geometry engine.

## Goal

Add a clear ForgeCAD-style printability inspection report to CAD-Agent generation results and the web UI.

The report should answer:

- Was a usable model produced?
- Is it watertight when that evidence is available?
- What are the model dimensions and volume?
- Does it fit the configured printer build volume?
- Are there printability warnings such as thin walls or abnormal volume?
- Did automatic repair happen before the final model was accepted?
- Which export files are available for downstream use?

## Non-Goals

- Do not introduce the ForgeCAD DSL or runtime.
- Do not add slicing, G-code generation, or material-specific print simulation.
- Do not block successful generation only because the inspect report contains warnings.
- Do not recompute geometry in the report builder if the validator already computed it.
- Do not redesign the whole frontend analysis tab.

## Proposed Scope: B, Recommended Version

### Backend

1. Keep `InspectReport` as the canonical response field.
2. Ensure every successful generation and manual code execution returns `inspect_report` whenever validation data exists.
3. Enrich the report with lightweight delivery evidence:
   - `available_exports`: list of generated formats such as `stl`, `step`, `dxf`, `svg`.
   - `repair_attempts`: count of repair history items.
   - `source`: short string such as `geometry_validator` to make evidence provenance explicit.
4. Preserve existing `validation` and `repair_history` fields for backward compatibility.
5. Add tests for report construction and response serialization.

### Frontend

1. Add a compact `InspectReportPanel` component.
2. Render it in the existing analysis area near repair history.
3. Show verdict as pass/warn/fail using clear color states.
4. Show dimensions, volume, watertight, build-volume fit, minimum wall thickness, warnings, repair attempt count, and available exports.
5. Hide the panel when no report is available, so older responses still work.

### Documentation

1. Update `docs/case-study-adaptations/CAD-Agent-adaptation-report.md` with Adaptation 4.
2. Record source, implementation scope, verification commands, and user decision.
3. Keep this spec as design traceability.

## Data Model Sketch

Extend `InspectReport` conservatively:

```python
class InspectReport(BaseModel):
    verdict: str = "pass"
    printable: bool | None = None
    is_watertight: bool = False
    bounding_box: BoundingBox | None = None
    volume: float = 0.0
    min_wall_thickness: float | None = None
    checks: list[InspectCheck] = []
    print_warnings: list[str] = []
    design_score: int | None = None
    dfm_violations: list[RuleViolationModel] = []
    available_exports: list[str] = []
    repair_attempts: int = 0
    source: str = "geometry_validator"
```

This is additive and should not break existing API consumers.

## UI Behavior

The UI panel should be concise and action-oriented:

- Header: `Printability Inspection`
- Verdict badge: `Pass`, `Warning`, or `Fail`
- Core metrics: dimensions, volume, watertight, build-volume fit
- Printability section: warnings and minimum wall thickness if available
- Process evidence: repair attempts and available export formats
- Details list: each `InspectCheck` with status and message

The panel should avoid overpromising. For example, if a field is `null`, show `Not evaluated` instead of implying success.

## Testing Strategy

Backend tests:

- `build_inspect_report` maps geometry checks to `InspectReport` correctly.
- Generation/execute responses serialize new inspect fields with safe defaults.
- Existing repair-history and parameter tests still pass.

Frontend tests/build:

- TypeScript build passes.
- If no frontend unit test framework is configured for components, rely on `npm.cmd run build` for type and bundle validation.

## Acceptance Criteria

- A successful model result contains a structured `inspect_report` when validation exists.
- The frontend displays a printable inspection panel after generation.
- The panel degrades gracefully when older responses lack `inspect_report`.
- Existing `validation`, `repair_history`, and download behavior continue to work.
- The adaptation report records this as ForgeCAD-style Adaptation 4.

## Risks And Mitigations

- Risk: Users may interpret warnings as hard failures.
  - Mitigation: Use explicit pass/warn/fail wording and preserve generation success separately.
- Risk: Duplicating validation and inspect data could confuse future maintainers.
  - Mitigation: Treat `validation` as raw compatibility data and `inspect_report` as user-facing evidence aggregation.
- Risk: Existing code may already partially populate `inspect_report`.
  - Mitigation: Implement additively and keep tests focused on expected response shape.
