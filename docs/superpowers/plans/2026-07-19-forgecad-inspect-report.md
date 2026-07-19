# ForgeCAD-Style Inspect Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a user-facing printability inspection report that exposes deterministic model evidence after generation.

**Architecture:** Reuse the existing `InspectReport` pipeline instead of recomputing geometry. Extend backend response metadata additively, then add a focused frontend panel in the existing analysis tab. Preserve `validation`, `repair_history`, and download behavior for compatibility.

**Tech Stack:** Python 3.11, FastAPI/Pydantic, pytest, React 19, TypeScript, Tailwind CSS, Vite.

---

## File Structure

- `backend/app/models/schemas.py`: extend `InspectReport` with delivery/provenance fields and safe list defaults.
- `backend/app/validation/inspect.py`: add optional report construction arguments for exports, repair attempts, and source.
- `backend/app/agent/orchestrator.py`: populate final report delivery evidence before returning generation results.
- `backend/app/api/websocket.py`: ensure manual `execute_code` responses include an inspect report when validation exists.
- `backend/tests/test_inspect_report.py`: extend existing inspect tests with new fields and serialization expectations.
- `frontend/src/types/index.ts`: mirror backend `InspectReport` fields.
- `frontend/src/components/InspectReportPanel.tsx`: new compact inspection panel.
- `frontend/src/App.tsx`: render the panel in the analysis tab above repair history.
- `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`: record Adaptation 4 source, scope, and verification.

---

### Task 1: Backend Inspect Metadata Contract

**Files:**
- Modify: `backend/app/models/schemas.py`
- Modify: `backend/tests/test_inspect_report.py`

- [x] **Step 1: Write the failing schema serialization test**

Append this test to `backend/tests/test_inspect_report.py`:

```python
def test_inspect_report_serializes_delivery_evidence():
    report = InspectReport(
        verdict="warn",
        available_exports=["stl", "step"],
        repair_attempts=2,
        source="geometry_validator",
    )

    dumped = report.model_dump()

    assert dumped["available_exports"] == ["stl", "step"]
    assert dumped["repair_attempts"] == 2
    assert dumped["source"] == "geometry_validator"
```

- [x] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest backend\tests\test_inspect_report.py::test_inspect_report_serializes_delivery_evidence -q
```

Expected: fail because `InspectReport` does not yet expose all delivery evidence fields.

- [x] **Step 3: Extend `InspectReport` additively**

In `backend/app/models/schemas.py`, update `InspectReport` to use safe defaults and add these fields:

```python
class InspectReport(BaseModel):
    verdict: str = "pass"
    printable: bool | None = None
    is_watertight: bool = False
    bounding_box: BoundingBox | None = None
    volume: float = 0.0
    min_wall_thickness: float | None = None
    checks: list[InspectCheck] = Field(default_factory=list)
    print_warnings: list[str] = Field(default_factory=list)
    design_score: int | None = None
    dfm_violations: list["RuleViolationModel"] = Field(default_factory=list)
    available_exports: list[str] = Field(default_factory=list)
    repair_attempts: int = 0
    source: str = "geometry_validator"
```

- [x] **Step 4: Run schema test to verify it passes**

Run:

```powershell
python -m pytest backend\tests\test_inspect_report.py::test_inspect_report_serializes_delivery_evidence -q
```

Expected: pass.

---

### Task 2: Report Builder Enrichment

**Files:**
- Modify: `backend/app/validation/inspect.py`
- Modify: `backend/tests/test_inspect_report.py`

- [x] **Step 1: Write the failing builder test**

Append this test to `backend/tests/test_inspect_report.py`:

```python
@pytest.mark.asyncio
async def test_build_inspect_report_includes_exports_repairs_and_source():
    geo = await GeometryValidator().validate(make_stl("printable"))

    report = build_inspect_report(
        geo,
        available_exports=["stl", "step"],
        repair_attempts=1,
        source="geometry_validator",
    )

    assert report.available_exports == ["stl", "step"]
    assert report.repair_attempts == 1
    assert report.source == "geometry_validator"
```

- [x] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest backend\tests\test_inspect_report.py::test_build_inspect_report_includes_exports_repairs_and_source -q
```

Expected: fail because `build_inspect_report` does not accept the new keyword arguments.

- [x] **Step 3: Extend `build_inspect_report` signature and construction**

In `backend/app/validation/inspect.py`, change the function signature and report construction to:

```python
def build_inspect_report(
    geo: GeometryValidation,
    dfm: dict | None = None,
    *,
    available_exports: list[str] | None = None,
    repair_attempts: int = 0,
    source: str = "geometry_validator",
) -> InspectReport:
    checks = [_check_from_rule(r) for r in geo.rules]

    report = InspectReport(
        printable=geo.printable,
        is_watertight=geo.is_watertight,
        bounding_box=BoundingBox(**geo.bounding_box) if geo.bounding_box else None,
        volume=geo.volume,
        min_wall_thickness=geo.min_wall_thickness,
        checks=checks,
        print_warnings=list(geo.print_warnings),
        available_exports=list(available_exports or []),
        repair_attempts=repair_attempts,
        source=source,
    )
```

Keep the existing DFM enrichment and `report.verdict = _worst_status(report.checks)` logic after this block.

- [x] **Step 4: Run builder tests**

Run:

```powershell
python -m pytest backend\tests\test_inspect_report.py -q
```

Expected: all inspect report tests pass.

---

### Task 3: Orchestrator Report Population

**Files:**
- Modify: `backend/app/agent/orchestrator.py`
- Modify: `backend/tests/test_inspect_report.py`

- [x] **Step 1: Write failing generation evidence test**

Extend `test_inspect_report_on_generate_response` in `backend/tests/test_inspect_report.py` with these assertions after `assert r.inspect_report.is_watertight == r.validation.is_watertight`:

```python
    assert sorted(r.inspect_report.available_exports) == ["stl", "step"]
    assert r.inspect_report.repair_attempts == 0
    assert r.inspect_report.source == "geometry_validator"
```

- [x] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest backend\tests\test_inspect_report.py::test_inspect_report_on_generate_response -q
```

Expected: fail because the report is not yet populated with export evidence.

- [x] **Step 3: Populate final report evidence in `_execute_with_retry`**

In `backend/app/agent/orchestrator.py`, where `build_inspect_report(geo_validation)` is called, compute generated export formats from the `files` keys and pass repair count:

```python
available_exports = sorted(files.keys())
inspect_report = build_inspect_report(
    geo_validation,
    available_exports=available_exports,
    repair_attempts=len(repair_history),
    source="geometry_validator",
)
```

Keep DFM and vision enrichment behavior unchanged.

- [x] **Step 4: Run generation evidence test**

Run:

```powershell
python -m pytest backend\tests\test_inspect_report.py::test_inspect_report_on_generate_response -q
```

Expected: pass.

---

### Task 4: Manual Execute WebSocket Parity

**Files:**
- Modify: `backend/app/api/websocket.py`
- Modify: `backend/tests/test_repair_history.py`

- [x] **Step 1: Inspect the manual execute response path**

Open `backend/app/api/websocket.py` and locate the `execute_code` branch. Identify where the response currently builds `validation`, `files`, `params`, and `repair_history`.

- [x] **Step 2: Write failing parity test**

Add a test beside the existing WebSocket/manual execution tests if a helper exists. If no helper exists, add this focused schema-level fallback to `backend/tests/test_repair_history.py`:

```python
def test_generate_response_execute_code_can_include_inspect_report():
    response = GenerateResponse(
        request_id="req-1",
        success=True,
        inspect_report=InspectReport(
            verdict="pass",
            available_exports=["stl"],
            repair_attempts=0,
            source="geometry_validator",
        ),
    )

    dumped = response.model_dump()

    assert dumped["inspect_report"]["available_exports"] == ["stl"]
    assert dumped["inspect_report"]["repair_attempts"] == 0
```

Import `InspectReport` from `app.models.schemas` if needed.

- [x] **Step 3: Add inspect report construction to manual execute path**

In `backend/app/api/websocket.py`, after manual execution geometry validation succeeds, build an inspect report with:

```python
inspect_report = build_inspect_report(
    geo_validation,
    available_exports=sorted(files.keys()),
    repair_attempts=0,
    source="geometry_validator",
)
```

Include `inspect_report=inspect_report` in the `GenerateResponse` or response payload used for `generation_result`.

- [x] **Step 4: Run parity-related tests**

Run:

```powershell
python -m pytest backend\tests\test_repair_history.py backend\tests\test_inspect_report.py -q
```

Expected: pass.

---

### Task 5: Frontend Types And Inspection Panel

**Files:**
- Modify: `frontend/src/types/index.ts`
- Create: `frontend/src/components/InspectReportPanel.tsx`

- [x] **Step 1: Extend frontend `InspectReport` type**

In `frontend/src/types/index.ts`, add these fields to `InspectReport`:

```ts
  available_exports?: string[];
  repair_attempts?: number;
  source?: string;
```

- [x] **Step 2: Create `InspectReportPanel` component**

Create `frontend/src/components/InspectReportPanel.tsx` with this structure:

```tsx
import type { InspectCheck, InspectReport } from "../types";

interface InspectReportPanelProps {
  report?: InspectReport | null;
}

const VERDICT_META: Record<string, { label: string; className: string }> = {
  pass: { label: "Pass", className: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  warn: { label: "Warning", className: "bg-amber-50 text-amber-700 border-amber-200" },
  fail: { label: "Fail", className: "bg-red-50 text-red-700 border-red-200" },
};

const CHECK_META: Record<string, string> = {
  pass: "text-emerald-700 bg-emerald-50",
  warn: "text-amber-700 bg-amber-50",
  fail: "text-red-700 bg-red-50",
};

function formatMaybeNumber(value: number | null | undefined, unit = "") {
  if (value === null || value === undefined) return "Not evaluated";
  return `${Number(value).toFixed(2)}${unit}`;
}

function formatDimensions(report: InspectReport) {
  const box = report.bounding_box;
  if (!box) return "Not evaluated";
  const x = box.x_max - box.x_min;
  const y = box.y_max - box.y_min;
  const z = box.z_max - box.z_min;
  return `${x.toFixed(1)} x ${y.toFixed(1)} x ${z.toFixed(1)} mm`;
}

function CheckRow({ check }: { check: InspectCheck }) {
  return (
    <div className="flex items-start justify-between gap-3 rounded-lg bg-gray-50 px-3 py-2">
      <div>
        <div className="text-xs font-medium text-gray-700">{check.name}</div>
        <div className="mt-0.5 text-xs text-gray-500">{check.message}</div>
      </div>
      <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${CHECK_META[check.status] || "bg-gray-100 text-gray-600"}`}>
        {check.status}
      </span>
    </div>
  );
}

export default function InspectReportPanel({ report }: InspectReportPanelProps) {
  if (!report) return null;

  const verdict = VERDICT_META[report.verdict] || VERDICT_META.warn;
  const warnings = report.print_warnings || [];
  const exports = report.available_exports || [];

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">Printability Inspection</h3>
          <p className="text-xs text-gray-500">Based on geometry validation, exported files, and repair history</p>
        </div>
        <span className={`rounded-full border px-2.5 py-1 text-xs font-semibold ${verdict.className}`}>
          {verdict.label}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="rounded-lg bg-gray-50 p-2">
          <div className="text-gray-500">Dimensions</div>
          <div className="mt-1 font-medium text-gray-900">{formatDimensions(report)}</div>
        </div>
        <div className="rounded-lg bg-gray-50 p-2">
          <div className="text-gray-500">Volume</div>
          <div className="mt-1 font-medium text-gray-900">{formatMaybeNumber(report.volume, " mm3")}</div>
        </div>
        <div className="rounded-lg bg-gray-50 p-2">
          <div className="text-gray-500">Watertight</div>
          <div className="mt-1 font-medium text-gray-900">{report.is_watertight ? "Pass" : "Failed or not evaluated"}</div>
        </div>
        <div className="rounded-lg bg-gray-50 p-2">
          <div className="text-gray-500">Min wall thickness</div>
          <div className="mt-1 font-medium text-gray-900">{formatMaybeNumber(report.min_wall_thickness, " mm")}</div>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-2 text-xs text-gray-600">
        <span className="rounded-full bg-indigo-50 px-2 py-1 text-indigo-700">Repairs: {report.repair_attempts || 0}</span>
        <span className="rounded-full bg-slate-100 px-2 py-1">Source: {report.source || "geometry_validator"}</span>
        {exports.length > 0 && <span className="rounded-full bg-blue-50 px-2 py-1 text-blue-700">Exports: {exports.join(", ")}</span>}
      </div>

      {warnings.length > 0 && (
        <div className="mt-3 rounded-lg bg-amber-50 p-3 text-xs text-amber-800">
          <div className="mb-1 font-semibold">Print warnings</div>
          <ul className="list-disc space-y-1 pl-4">
            {warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}
          </ul>
        </div>
      )}

      {report.checks.length > 0 && (
        <div className="mt-3 space-y-2">
          {report.checks.map((check, index) => <CheckRow key={`${check.name}-${index}`} check={check} />)}
        </div>
      )}
    </div>
  );
}
```

- [x] **Step 3: Run frontend type build to verify current compile state**

Run:

```powershell
cd frontend
npm.cmd run build
```

Expected: pass after the component and type changes.

---

### Task 6: Frontend Integration In Analysis Tab

**Files:**
- Modify: `frontend/src/App.tsx`

- [x] **Step 1: Import the panel**

Add this import near `RepairHistory`:

```ts
import InspectReportPanel from "./components/InspectReportPanel";
```

- [x] **Step 2: Render the panel before repair history**

In the `activeTab === "analysis"` block, render:

```tsx
<InspectReportPanel report={result?.inspect_report || null} />
<RepairHistory steps={result?.repair_history || null} attempts={result?.attempts} />
```

This keeps inspection evidence above the repair timeline.

- [x] **Step 3: Run frontend build**

Run:

```powershell
cd frontend
npm.cmd run build
```

Expected: pass. Existing Vite bundle-size warnings are acceptable if the build exits successfully.

---

### Task 7: Adaptation Report And Final Verification

**Files:**
- Modify: `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`
- Modify: `docs/superpowers/plans/2026-07-19-forgecad-inspect-report.md`

- [x] **Step 1: Append Adaptation 4 to the case-study report**

Append this section to `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`:

```markdown
## Adaptation 4: ForgeCAD-Style Inspect Report

### Source
- Project: forgecad-public-kit
- Borrowed idea: CAD generation should produce inspectable evidence from the run/inspect loop, not only final files.
- Local reference files studied:
  - `../forgecad-public-kit/README.md`
  - `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`
  - `../forgecad-public-kit/skills/forgecad-inspect-model/SKILL.md`

### CAD-Agent Implementation
- Extended `InspectReport` with export availability, repair attempt count, and evidence source.
- Ensured generated models expose printability evidence through the response contract.
- Added a frontend printability inspection panel in the analysis tab.
- Preserved raw validation and repair history fields for compatibility.

### Verification
- `python -m pytest backend\tests\test_inspect_report.py backend\tests\test_repair_history.py -q` passed.
- `cd frontend && npm.cmd run build` passed.

### Decision
- Implemented recommended version B: structured inspect report plus UI panel, without adding a separate ForgeCAD runtime or slicer.
```

- [x] **Step 2: Run backend verification**

Run:

```powershell
python -m pytest backend\tests\test_inspect_report.py backend\tests\test_repair_history.py backend\tests\test_parameters.py -q
```

Expected: pass.

- [x] **Step 3: Run frontend verification**

Run:

```powershell
cd frontend
npm.cmd run build
```

Expected: pass. Bundle-size warnings are acceptable if the command exits with code 0.

- [x] **Step 4: Mark completed checkboxes**

After all verification passes, update every checkbox in this plan from `[ ]` to `[x]`.

---

## Execution Notes

- Do not run `git commit` unless the user explicitly requests it.
- Keep changes additive and avoid removing existing `validation`, `repair_history`, or `files` fields.
- If `backend/tests/test_codegen.py::TestLazyClientInit::test_client_raises_without_api_key` fails during broader test runs, treat it as unrelated because it checks an older API-key wording contract.
- If Chroma telemetry prints warnings, treat them as non-blocking unless a test exits non-zero.
