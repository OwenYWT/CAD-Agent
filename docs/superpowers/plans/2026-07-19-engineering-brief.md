# Engineering Brief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a ForgeCAD-style `design_brief` that explains CAD-Agent's engineering interpretation before and after model generation.

**Architecture:** Extend existing response models additively with `DesignBrief` and `CriticalDimension`, then build a fallback brief from `CADPlan` so generation remains reliable even when the planner omits new fields. Feed the brief into code generation and display it in a focused React analysis panel without adding blocking approval flow.

**Tech Stack:** FastAPI, Pydantic, async orchestration, React, TypeScript, Zustand, Vite, Pytest.

---

## Source Attribution

- Source project: `forgecad-public-kit`
- Source references:
  - `../forgecad-public-kit/skills/forgecad-design-spec/SKILL.md`
  - `../forgecad-public-kit/skills/forgecad-design-spec/references/default-profiles.md`
  - `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`
- Borrowed principle: treat a concrete engineering brief as the source of truth before CAD code is produced.

## File Responsibilities

- `backend/app/models/schemas.py`: Define `CriticalDimension` and `DesignBrief`; add nullable `design_brief` fields to `CADPlan`, `GenerateResponse`, and `GenerationResult`.
- `backend/app/agent/design_brief.py`: Build safe fallback briefs from existing `CADPlan` objects and cap list lengths.
- `backend/tests/test_design_brief.py`: Cover schema compatibility, fallback behavior, and response serialization.
- `backend/app/agent/prompts.py`: Update planner and code-generation instructions so brief output is requested and respected.
- `backend/app/agent/planner.py`: Parse optional planner `design_brief` and backfill missing or partial values.
- `backend/app/agent/code_gen.py`: Include brief context in code-generation prompts.
- `backend/app/agent/orchestrator.py`: Ensure natural-language generation responses carry `design_brief`.
- `backend/app/api/websocket.py`: Preserve brief in WebSocket result payloads and snapshot persistence through existing result serialization.
- `frontend/src/types/index.ts`: Add `CriticalDimension` and `DesignBrief` types; expose `design_brief` on `CADPlanBrief` and `GenerationResult`.
- `frontend/src/components/DesignBriefPanel.tsx`: Display brief sections in the analysis tab.
- `frontend/src/App.tsx`: Render `DesignBriefPanel` below `AgentRunTimeline` and above `InspectReportPanel`.
- `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`: Record Adaptation 7 with source, changes, non-goals, and verification.

---

### Task 1: Backend Design Brief Schema

**Files:**
- Modify: `backend/app/models/schemas.py`
- Create: `backend/tests/test_design_brief.py`

- [ ] **Step 1: Write failing schema tests**

Create `backend/tests/test_design_brief.py`:

```python
from app.models.schemas import CADPlan, CriticalDimension, DesignBrief, GenerateResponse, GenerationResult


def test_design_brief_schema_serializes_engineering_fields():
    brief = DesignBrief(
        intent_summary="A printable desk cable clip.",
        artifact_type="clip",
        manufacturing_posture="printable",
        assumptions=["Cable diameter is approximately 6 mm"],
        critical_dimensions=[
            CriticalDimension(
                name="wall_thickness",
                value=2.4,
                unit="mm",
                reason="Six 0.4 mm nozzle lines make a sturdy printable wall",
            )
        ],
        functional_requirements=["Hold cable without pinching"],
        printability_targets=["Minimum wall thickness at least 2.0 mm"],
        acceptance_criteria=["Geometry is watertight"],
        open_questions=["Confirm exact cable diameter if fit is critical"],
    )

    payload = brief.model_dump()

    assert payload["intent_summary"] == "A printable desk cable clip."
    assert payload["artifact_type"] == "clip"
    assert payload["manufacturing_posture"] == "printable"
    assert payload["critical_dimensions"][0]["name"] == "wall_thickness"
    assert payload["critical_dimensions"][0]["value"] == 2.4


def test_cad_plan_accepts_optional_design_brief():
    brief = DesignBrief(intent_summary="A simple bracket", artifact_type="bracket")

    plan = CADPlan(
        description="L bracket",
        part_type="bracket",
        dimensions={"length": 40},
        features=["two mounting holes"],
        design_brief=brief,
    )

    assert plan.design_brief == brief
    assert plan.model_dump()["design_brief"]["intent_summary"] == "A simple bracket"


def test_generation_responses_carry_design_brief():
    brief = DesignBrief(intent_summary="A printable enclosure", artifact_type="enclosure")

    generate_response = GenerateResponse(request_id="req-brief", success=True, design_brief=brief)
    generation_result = GenerationResult(success=True, request_id="req-brief", design_brief=brief)

    assert generate_response.model_dump()["design_brief"]["artifact_type"] == "enclosure"
    assert generation_result.model_dump()["design_brief"]["artifact_type"] == "enclosure"
```

- [ ] **Step 2: Run schema tests and verify failure**

Run:

```powershell
python -m pytest backend\tests\test_design_brief.py -q
```

Expected: fails because `CriticalDimension`, `DesignBrief`, and `design_brief` fields do not exist.

- [ ] **Step 3: Add schema models and fields**

In `backend/app/models/schemas.py`, add these models before `CADPlan`:

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

Add to `CADPlan`:

```python
design_brief: DesignBrief | None = None
```

Add to `GenerateResponse` and `GenerationResult`:

```python
design_brief: DesignBrief | None = None
```

- [ ] **Step 4: Run schema tests and verify pass**

Run:

```powershell
python -m pytest backend\tests\test_design_brief.py -q
```

Expected: `3 passed`.

---

### Task 2: Backend Fallback Brief Builder

**Files:**
- Create: `backend/app/agent/design_brief.py`
- Modify: `backend/tests/test_design_brief.py`

- [ ] **Step 1: Add failing fallback tests**

Append to `backend/tests/test_design_brief.py`:

```python
from app.agent.design_brief import ensure_design_brief


def test_ensure_design_brief_builds_fallback_from_cad_plan():
    plan = CADPlan(
        description="Adjustable phone stand",
        part_type="stand",
        dimensions={"width": 70, "height": 120, "wall_thickness": 3},
        features=["tilted back support", "rounded base"],
        constraints=["print without support"],
        ambiguities=["phone thickness not specified"],
    )

    brief = ensure_design_brief(plan)

    assert brief.intent_summary == "Adjustable phone stand"
    assert brief.artifact_type == "stand"
    assert brief.manufacturing_posture == "printable"
    assert "Prototype use with metric dimensions" in brief.assumptions
    assert [item.name for item in brief.critical_dimensions] == ["width", "height", "wall_thickness"]
    assert brief.critical_dimensions[0].reason == "User-provided or planner-inferred dimension"
    assert "tilted back support" in brief.functional_requirements
    assert "print without support" in brief.functional_requirements
    assert "Geometry is watertight" in brief.acceptance_criteria
    assert brief.open_questions == ["phone thickness not specified"]
    assert plan.design_brief == brief


def test_ensure_design_brief_preserves_existing_brief_and_backfills_empty_lists():
    plan = CADPlan(
        description="Small hinge",
        part_type="hinge",
        dimensions={"pin_diameter": 4},
        features=[],
        design_brief=DesignBrief(intent_summary="A printable hinge", artifact_type="hinge"),
    )

    brief = ensure_design_brief(plan)

    assert brief.intent_summary == "A printable hinge"
    assert brief.artifact_type == "hinge"
    assert brief.printability_targets
    assert brief.acceptance_criteria
```

- [ ] **Step 2: Run fallback tests and verify failure**

Run:

```powershell
python -m pytest backend\tests\test_design_brief.py -q
```

Expected: fails with `ModuleNotFoundError` for `app.agent.design_brief`.

- [ ] **Step 3: Create fallback builder**

Create `backend/app/agent/design_brief.py`:

```python
from __future__ import annotations

from app.models.schemas import CADPlan, CriticalDimension, DesignBrief

_DEFAULT_PRINTABILITY_TARGETS = [
    "Geometry is watertight",
    "Minimum wall thickness should be practical for the intended scale",
    "Exposed edges should be softened where practical",
    "Model should remain aware of common desktop 3D printer build volume",
]

_DEFAULT_ACCEPTANCE_CRITERIA = [
    "Code executes successfully",
    "Model exports at least one fabrication-ready file",
    "Geometry is watertight",
]


def _limited_strings(values: list[str], limit: int = 8) -> list[str]:
    return [value.strip() for value in values if value and value.strip()][:limit]


def _dimensions_from_plan(plan: CADPlan) -> list[CriticalDimension]:
    return [
        CriticalDimension(
            name=name,
            value=value,
            unit="mm",
            reason="User-provided or planner-inferred dimension",
        )
        for name, value in list(plan.dimensions.items())[:10]
    ]


def ensure_design_brief(plan: CADPlan) -> DesignBrief:
    existing = plan.design_brief or DesignBrief()
    assumptions = _limited_strings(existing.assumptions) or ["Prototype use with metric dimensions"]
    critical_dimensions = existing.critical_dimensions or _dimensions_from_plan(plan)
    functional_requirements = _limited_strings(
        existing.functional_requirements or [*plan.features, *plan.constraints]
    )
    printability_targets = _limited_strings(existing.printability_targets) or _DEFAULT_PRINTABILITY_TARGETS.copy()
    acceptance_criteria = _limited_strings(existing.acceptance_criteria) or _DEFAULT_ACCEPTANCE_CRITERIA.copy()
    open_questions = _limited_strings(existing.open_questions or plan.ambiguities)

    brief = DesignBrief(
        intent_summary=existing.intent_summary or plan.description,
        artifact_type=existing.artifact_type or plan.part_type,
        manufacturing_posture=existing.manufacturing_posture or "printable",
        assumptions=assumptions,
        critical_dimensions=critical_dimensions,
        functional_requirements=functional_requirements,
        printability_targets=printability_targets,
        acceptance_criteria=acceptance_criteria,
        open_questions=open_questions,
    )
    plan.design_brief = brief
    return brief
```

- [ ] **Step 4: Run fallback tests and verify pass**

Run:

```powershell
python -m pytest backend\tests\test_design_brief.py -q
```

Expected: `5 passed`.

---

### Task 3: Planner Contract And Parsing

**Files:**
- Modify: `backend/app/agent/prompts.py`
- Modify: `backend/app/agent/planner.py`
- Modify: `backend/tests/test_design_brief.py`

- [ ] **Step 1: Add planner parsing test**

Append to `backend/tests/test_design_brief.py`:

```python
from app.agent.planner import Planner


def test_planner_parse_preserves_design_brief_from_json():
    payload = {
        "description": "Desk cable clip",
        "part_type": "clip",
        "dimensions": {"length": 60, "wall_thickness": 2.4},
        "features": ["snap slot"],
        "constraints": ["print without supports where possible"],
        "ambiguities": ["exact cable diameter not specified"],
        "modeling_hint": "extrude_cut",
        "design_brief": {
            "intent_summary": "A small 3D-printable clip that holds one cable against a desk edge.",
            "artifact_type": "clip",
            "manufacturing_posture": "printable",
            "assumptions": ["Cable diameter is approximately 6 mm"],
            "critical_dimensions": [
                {
                    "name": "wall_thickness",
                    "value": 2.4,
                    "unit": "mm",
                    "reason": "Six 0.4 mm nozzle lines give a sturdy printable wall",
                }
            ],
            "functional_requirements": ["Hold cable without pinching"],
            "printability_targets": ["Avoid unsupported overhangs above 45 degrees"],
            "acceptance_criteria": ["Cable channel remains open"],
            "open_questions": ["Confirm exact cable diameter if fit is critical"],
        },
    }

    plan = Planner._parse_plan_payload(payload)

    assert plan.design_brief is not None
    assert plan.design_brief.intent_summary.startswith("A small 3D-printable clip")
    assert plan.design_brief.critical_dimensions[0].reason.startswith("Six 0.4 mm")
```

- [ ] **Step 2: Run planner parsing test and verify failure**

Run:

```powershell
python -m pytest backend\tests\test_design_brief.py::test_planner_parse_preserves_design_brief_from_json -q
```

Expected: fails because `Planner._parse_plan_payload` does not exist.

- [ ] **Step 3: Add parser helper in planner**

In `backend/app/agent/planner.py`, add this static method to `Planner`:

```python
@staticmethod
def _parse_plan_payload(data: dict) -> CADPlan:
    plan = CADPlan(**data)
    from app.agent.design_brief import ensure_design_brief

    ensure_design_brief(plan)
    return plan
```

Update `plan_new` to use this helper after `json.loads(text)`:

```python
data = json.loads(text)
return self._parse_plan_payload(data)
```

Keep the existing heuristic fallback path, but after creating its `CADPlan`, call `ensure_design_brief(plan)` before returning.

- [ ] **Step 4: Extend planner prompt**

In `backend/app/agent/prompts.py`, update `PLANNER_SYSTEM_PROMPT` to require a `design_brief` JSON object with these keys:

```text
Also include design_brief with:
- intent_summary: concise engineering interpretation of the user's request.
- artifact_type: practical artifact family, such as bracket, enclosure, clip, gear, fixture, stand, assembly, custom.
- manufacturing_posture: printable unless the prompt clearly names another process.
- assumptions: concise assumptions the agent made because the user did not specify them.
- critical_dimensions: list of {name, value, unit, reason}; every important numeric value should have a reason.
- functional_requirements: what the part must do physically.
- printability_targets: targets such as wall thickness, watertight geometry, softened edges, overhang control, support awareness, and build-volume awareness.
- acceptance_criteria: checks the produced model should satisfy.
- open_questions: non-blocking questions for dimensions or fit details the user may later refine.
Do not expose chain-of-thought. Keep all lists short.
```

- [ ] **Step 5: Run design brief tests and verify pass**

Run:

```powershell
python -m pytest backend\tests\test_design_brief.py -q
```

Expected: all design brief tests pass.

---

### Task 4: Code Generation Uses Brief

**Files:**
- Modify: `backend/app/agent/code_gen.py`
- Modify: `backend/tests/test_design_brief.py`

- [ ] **Step 1: Add prompt formatting test**

Append to `backend/tests/test_design_brief.py`:

```python
from app.agent.code_gen import CodeGenerator


def test_code_generator_formats_design_brief_context():
    plan = CADPlan(
        description="Desk cable clip",
        part_type="clip",
        dimensions={"wall_thickness": 2.4},
        features=["snap slot"],
        design_brief=DesignBrief(
            intent_summary="A printable cable clip for a desk edge",
            artifact_type="clip",
            manufacturing_posture="printable",
            assumptions=["Cable diameter is approximately 6 mm"],
            critical_dimensions=[CriticalDimension(name="wall_thickness", value=2.4, unit="mm", reason="Printable sturdy wall")],
            functional_requirements=["Hold cable without pinching"],
            printability_targets=["Avoid unsupported overhangs above 45 degrees"],
            acceptance_criteria=["Cable channel remains open"],
            open_questions=["Confirm cable diameter"],
        ),
    )

    context = CodeGenerator._format_design_brief_context(plan)

    assert "Engineering Brief" in context
    assert "A printable cable clip for a desk edge" in context
    assert "wall_thickness=2.4 mm" in context
    assert "Printable sturdy wall" in context
    assert "Cable channel remains open" in context
```

- [ ] **Step 2: Run formatting test and verify failure**

Run:

```powershell
python -m pytest backend\tests\test_design_brief.py::test_code_generator_formats_design_brief_context -q
```

Expected: fails because `_format_design_brief_context` does not exist.

- [ ] **Step 3: Add code generator formatter**

In `backend/app/agent/code_gen.py`, add this static method to `CodeGenerator`:

```python
@staticmethod
def _format_design_brief_context(plan: CADPlan) -> str:
    brief = plan.design_brief
    if not brief:
        return ""

    dimension_lines = [
        f"- {item.name}={item.value} {item.unit}: {item.reason}"
        if item.value is not None
        else f"- {item.name}: {item.reason}"
        for item in brief.critical_dimensions
    ]
    sections = [
        "Engineering Brief:",
        f"Intent: {brief.intent_summary}",
        f"Artifact type: {brief.artifact_type}",
        f"Manufacturing posture: {brief.manufacturing_posture}",
    ]
    if brief.assumptions:
        sections.append("Assumptions:\n" + "\n".join(f"- {item}" for item in brief.assumptions))
    if dimension_lines:
        sections.append("Critical dimensions:\n" + "\n".join(dimension_lines))
    if brief.functional_requirements:
        sections.append("Functional requirements:\n" + "\n".join(f"- {item}" for item in brief.functional_requirements))
    if brief.printability_targets:
        sections.append("Printability targets:\n" + "\n".join(f"- {item}" for item in brief.printability_targets))
    if brief.acceptance_criteria:
        sections.append("Acceptance criteria:\n" + "\n".join(f"- {item}" for item in brief.acceptance_criteria))
    return "\n\n".join(sections)
```

- [ ] **Step 4: Include context in generation prompts**

In `CodeGenerator.generate` and `CodeGenerator.generate_2d`, add the formatted brief context to the user prompt or plan summary. Use this pattern:

```python
brief_context = self._format_design_brief_context(plan)
if brief_context:
    prompt_parts.append(brief_context)
```

If the current code builds a single f-string instead of `prompt_parts`, append:

```python
if brief_context:
    user_prompt += f"\n\n{brief_context}"
```

Do not add brief context to hidden reasoning. This is explicit user-readable design context.

- [ ] **Step 5: Run formatting tests and existing codegen tests**

Run:

```powershell
python -m pytest backend\tests\test_design_brief.py backend\tests\test_codegen.py::TestCodeGeneratorNoLLM -q
```

Expected: design brief tests pass. If `TestCodeGeneratorNoLLM` does not exist, run only `backend\tests\test_design_brief.py` and note the missing focused codegen group in the final handoff.

---

### Task 5: Orchestrator Response Flow

**Files:**
- Modify: `backend/app/agent/orchestrator.py`
- Modify: `backend/tests/test_design_brief.py`

- [ ] **Step 1: Add response flow unit test**

Append to `backend/tests/test_design_brief.py`:

```python
from app.agent.design_brief import ensure_design_brief


def test_generate_response_can_embed_plan_and_brief_together():
    plan = CADPlan(
        description="Printable wall hook",
        part_type="hook",
        dimensions={"width": 30},
        features=["rounded hook"],
    )
    brief = ensure_design_brief(plan)

    response = GenerateResponse(
        request_id="req-flow",
        success=True,
        code="result = cq.Workplane('XY').box(1, 1, 1)",
        plan=plan,
        design_brief=brief,
    )

    payload = response.model_dump()

    assert payload["plan"]["design_brief"]["artifact_type"] == "hook"
    assert payload["design_brief"]["artifact_type"] == "hook"
```

- [ ] **Step 2: Run response flow test**

Run:

```powershell
python -m pytest backend\tests\test_design_brief.py::test_generate_response_can_embed_plan_and_brief_together -q
```

Expected: passes after Task 1 and Task 2. If it fails, fix schema or fallback flow before touching orchestrator.

- [ ] **Step 3: Ensure natural-language generation has brief**

In `backend/app/agent/orchestrator.py`, import:

```python
from app.agent.design_brief import ensure_design_brief
```

After every `plan = await self.planner.plan_new(...)`, add:

```python
design_brief = ensure_design_brief(plan)
```

When returning `GenerateResponse` or converting into `GenerationResult`, include:

```python
design_brief=design_brief,
```

For helper functions that only receive `plan`, use:

```python
design_brief=plan.design_brief if plan else None,
```

- [ ] **Step 4: Preserve context brief for modifications if available**

In `ConversationContext`, add:

```python
current_design_brief: DesignBrief | None = None
```

After a successful natural-language generation, set:

```python
context.current_design_brief = response.design_brief
```

When `modify_part` returns a result and no new brief exists, include the previous context brief:

```python
design_brief=context.current_design_brief,
```

- [ ] **Step 5: Run backend response tests**

Run:

```powershell
python -m pytest backend\tests\test_design_brief.py backend\tests\test_deploy_websocket.py backend\tests\test_model_snapshots.py -q
```

Expected: selected tests pass and snapshot serialization remains compatible.

---

### Task 6: Frontend Types And DesignBriefPanel

**Files:**
- Modify: `frontend/src/types/index.ts`
- Create: `frontend/src/components/DesignBriefPanel.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Extend frontend types**

In `frontend/src/types/index.ts`, add:

```ts
export interface CriticalDimension {
  name: string;
  value?: number | null;
  unit: string;
  reason: string;
}

export interface DesignBrief {
  intent_summary: string;
  artifact_type: string;
  manufacturing_posture: string;
  assumptions: string[];
  critical_dimensions: CriticalDimension[];
  functional_requirements: string[];
  printability_targets: string[];
  acceptance_criteria: string[];
  open_questions: string[];
}
```

Add to `CADPlanBrief` and `GenerationResult`:

```ts
design_brief?: DesignBrief | null;
```

- [ ] **Step 2: Create `DesignBriefPanel` component**

Create `frontend/src/components/DesignBriefPanel.tsx`:

```tsx
import type { DesignBrief } from "../types";

interface DesignBriefPanelProps {
  brief?: DesignBrief | null;
}

function Section({ title, items }: { title: string; items?: string[] }) {
  if (!items?.length) return null;
  return (
    <div>
      <h4 className="text-xs font-semibold text-gray-700 mb-1">{title}</h4>
      <ul className="space-y-1">
        {items.map((item, index) => (
          <li key={`${title}-${index}`} className="text-xs text-gray-600 flex gap-2">
            <span className="text-gray-300">?</span>
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function DesignBriefPanel({ brief }: DesignBriefPanelProps) {
  if (!brief) return null;

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-100">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold text-gray-900">Engineering Brief</h3>
          <span className="text-[10px] uppercase tracking-wide rounded-full border border-blue-200 bg-blue-50 text-blue-700 px-2 py-0.5">
            {brief.manufacturing_posture || "printable"}
          </span>
        </div>
        <p className="text-xs text-gray-500 mt-1">Visible design assumptions before CAD generation, not manufacturing certification.</p>
      </div>

      <div className="p-4 space-y-4">
        <div>
          <h4 className="text-xs font-semibold text-gray-700 mb-1">Intent</h4>
          <p className="text-xs text-gray-600">{brief.intent_summary}</p>
          <p className="text-[10px] text-gray-400 mt-1">Artifact type: {brief.artifact_type}</p>
        </div>

        {brief.critical_dimensions?.length ? (
          <div>
            <h4 className="text-xs font-semibold text-gray-700 mb-1">Critical Dimensions</h4>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-gray-400">
                  <tr>
                    <th className="text-left font-medium py-1">Name</th>
                    <th className="text-left font-medium py-1">Value</th>
                    <th className="text-left font-medium py-1">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {brief.critical_dimensions.map((dimension, index) => (
                    <tr key={`${dimension.name}-${index}`} className="border-t border-gray-100">
                      <td className="py-1 pr-2 text-gray-700">{dimension.name}</td>
                      <td className="py-1 pr-2 text-gray-600 tabular-nums">
                        {dimension.value ?? "?"} {dimension.unit || "mm"}
                      </td>
                      <td className="py-1 text-gray-500">{dimension.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}

        <Section title="Assumptions" items={brief.assumptions} />
        <Section title="Functional Requirements" items={brief.functional_requirements} />
        <Section title="Printability Targets" items={brief.printability_targets} />
        <Section title="Acceptance Criteria" items={brief.acceptance_criteria} />
        <Section title="Open Questions" items={brief.open_questions} />
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Wire panel into analysis tab**

In `frontend/src/App.tsx`, import:

```ts
import DesignBriefPanel from "./components/DesignBriefPanel";
```

Render below `AgentRunTimeline`:

```tsx
<DesignBriefPanel brief={result?.design_brief || result?.plan?.design_brief || null} />
```

- [ ] **Step 4: Run frontend build**

Run:

```powershell
cd frontend
npm.cmd run build
```

Expected: build succeeds. Existing Vite chunk-size warnings are acceptable.

---

### Task 7: Traceability Report

**Files:**
- Modify: `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`

- [ ] **Step 1: Add Adaptation 7 entry**

Append this section:

```markdown
## Adaptation 7: ForgeCAD-Style Engineering Brief

### Source

- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-design-spec/SKILL.md`
- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-design-spec/references/default-profiles.md`
- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`

### Borrowed Idea

ForgeCAD treats the design brief as the source of truth before CAD code is produced. CAD-Agent adapts this by returning a visible `design_brief` with intent, manufacturing posture, assumptions, critical dimensions, requirements, printability targets, acceptance criteria, and open questions.

### CAD-Agent Changes

- Added `CriticalDimension` and `DesignBrief` schemas.
- Added fallback brief generation from `CADPlan` when planner output is missing or partial.
- Extended planner instructions to produce user-readable engineering briefs.
- Included brief context in code-generation prompts.
- Returned `design_brief` in generation results and preserved it through snapshots.
- Added `DesignBriefPanel` in the frontend analysis tab.

### Non-Goals Kept

- No blocking pre-generation approval step was added.
- No production certification or simulation claims were added.
- No hidden chain-of-thought was exposed.
- No external services were introduced.

### Verification

- `python -m pytest backend\tests\test_design_brief.py backend\tests\test_deploy_websocket.py backend\tests\test_model_snapshots.py -q`
- `cd frontend && npm.cmd run build`
```

- [ ] **Step 2: Verify report entry**

Run:

```powershell
Select-String -Path docs\case-study-adaptations\CAD-Agent-adaptation-report.md -Pattern "Adaptation 7|Engineering Brief|forgecad-design-spec"
```

Expected: Adaptation 7 appears with source references and verification commands.

---

### Task 8: Final Verification

**Files:**
- Read only: test outputs

- [ ] **Step 1: Run focused backend suite**

Run:

```powershell
python -m pytest backend\tests\test_design_brief.py backend\tests\test_deploy_websocket.py backend\tests\test_model_snapshots.py -q
```

Expected: selected tests pass.

- [ ] **Step 2: Run frontend build**

Run:

```powershell
cd frontend
npm.cmd run build
```

Expected: TypeScript and Vite build pass. Bundle-size warnings are acceptable.

- [ ] **Step 3: Manual smoke test**

Run backend and frontend in separate terminals:

```powershell
cd backend
python -m uvicorn app.main:app --reload --port 8000
```

```powershell
cd frontend
npm.cmd run dev
```

Expected manual result:

- Generate a normal model from a natural-language prompt.
- Open the analysis tab.
- `Agent Run Timeline` remains visible.
- `Engineering Brief` appears below the timeline.
- Brief shows intent, manufacturing posture, assumptions, dimensions with reasons, requirements, printability targets, acceptance criteria, and open questions when available.
- Existing `InspectReportPanel`, `VersionHistoryPanel`, `RepairHistory`, and `DesignAnalysis` still render.
- Direct code execution still works when no `design_brief` is available.

---

## Self-Review Notes

- Spec coverage: backend schema, fallback builder, planner contract, code-generation use, response flow, frontend display, traceability, and verification are all represented.
- Compatibility: all new response fields are nullable or additive; existing `plan` readers continue working.
- Non-goals: the plan avoids blocking approval, production certification claims, simulation, external services, and chain-of-thought exposure.
- Test strategy: backend behavior is covered with Pytest; frontend integration is covered with the existing `npm.cmd run build` script because this project has no frontend test script.
- Traceability: Adaptation 7 is explicitly added to the comprehensive report.
