# CADAM-Style Parameter Interaction Design

## Goal
Bring CADAM-style editable parameters into CAD-Agent for CadQuery models: generated code exposes top-level parameters, the backend parses them into UI metadata, and users can adjust values without another LLM call.

## Scope
- Parse numeric CadQuery/Python top-level assignments with CADAM-like range comments.
- Support group comments, description comments, units, min/step/max, default values, and current values.
- Return parsed parameters in generation/execution responses.
- Let the frontend render grouped sliders/number inputs from backend schema.
- Let parameter edits replace code values and re-execute the existing CadQuery code through the current sandbox pipeline.

## Parameter Format
```python
# [Body]
# 外壳宽度
width_mm = 80  # [40:1:160]

# 外壳深度
depth_mm = 50  # [30:1:120]
```

Interpretation:
- `# [Body]` starts a UI group.
- The immediately preceding normal `# ...` comment becomes the label/description.
- `name_mm` implies unit `mm`.
- `# [min:step:max]` defines slider bounds.
- `# [min:max]` defines slider bounds with inferred step.

## Out Of Scope For First Pass
- Boolean, enum, color, and string controls.
- Constraint solving and parameter sweep.
- LLM repair triggered by parameter edits.
- OpenSCAD/WASM integration.

## Acceptance Criteria
- Backend tests prove parameter parsing and code replacement work for integers, floats, negative values, grouped comments, and partial updates.
- Generated API responses include `parameters` when code contains supported parameter declarations.
- Frontend shows grouped parameter controls and uses backend range/step metadata.
- Changing a parameter re-executes code without calling the LLM.
