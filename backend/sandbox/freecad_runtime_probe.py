"""Real FreeCAD 1.1.3 operation probe executed by the packaged FreeCADCmd."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from pathlib import Path

import Assembly  # noqa: F401 -- importing proves the native module is packaged
import FreeCAD as App
import Import
import Mesh
import Part
import Sketcher
import Spreadsheet  # noqa: F401 -- required by Assembly::BomObject


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cell(sheet, column: str, row: int) -> str:
    try:
        value = sheet.get(f"{column}{row}")
    except ValueError:
        return ""
    return str(value or "").lstrip("'")


def run_probe() -> dict:
    work = Path(tempfile.mkdtemp(prefix="freecad-runtime-probe-"))
    document = App.newDocument("RuntimeProbe")
    box = document.addObject("Part::Box", "ProbeBox")
    box.Label = "Probe Box"
    box.Length = 10
    box.Width = 8
    box.Height = 4

    sketch = document.addObject("Sketcher::SketchObject", "ProbeSketch")
    geometry_index = sketch.addGeometry(
        Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 2),
        False,
    )
    sketch.addConstraint(Sketcher.Constraint("Diameter", geometry_index, 4))
    sketch.addConstraint(
        Sketcher.Constraint("DistanceX", geometry_index, 3, 0.0)
    )
    sketch.addConstraint(
        Sketcher.Constraint("DistanceY", geometry_index, 3, 0.0)
    )
    document.recompute()

    fcstd = work / "runtime-probe.FCStd"
    document.saveAs(str(fcstd))
    object_count_before = len(document.Objects)
    App.closeDocument(document.Name)
    document = App.openDocument(str(fcstd))
    document.UndoMode = 1
    reopened_box = document.getObject("ProbeBox")
    if reopened_box is None:
        raise RuntimeError("saved FCStd did not retain ProbeBox")
    before_value = float(reopened_box.Length.Value)
    reopened_box.Length = "12 mm"
    document.recompute()
    after_value = float(reopened_box.Length.Value)
    if not math.isfinite(after_value) or before_value == after_value:
        raise RuntimeError("reopened property modification was not applied")
    document.save()

    transaction_count = len(document.Objects)
    document.openTransaction("probe-abort")
    aborted_name = document.addObject("Part::Feature", "MustAbort").Name
    document.abortTransaction()
    if document.getObject(aborted_name) is not None:
        raise RuntimeError("aborted transaction leaked an object")

    sketch = document.getObject("ProbeSketch")
    solver_status = int(sketch.solve())
    document.recompute()
    fully_constrained = bool(sketch.FullyConstrained)
    if solver_status != 0 or not fully_constrained:
        raise RuntimeError("probe sketch is not fully constrained")

    reopened_box = document.getObject("ProbeBox")
    shape_errors = list(reopened_box.Shape.check(True) or ())
    shape_valid = bool(reopened_box.Shape.isValid())
    solid_count = len(reopened_box.Shape.Solids)
    if not shape_valid or shape_errors or solid_count < 1:
        raise RuntimeError("probe solid failed shape validation")

    step_path = work / "runtime-probe.step"
    stl_path = work / "runtime-probe.stl"
    Import.export([reopened_box], str(step_path))
    Mesh.export([reopened_box], str(stl_path))
    if not step_path.is_file() or not stl_path.is_file():
        raise RuntimeError("FreeCAD export did not produce STEP and STL")
    step_document = App.newDocument("ReopenedStep")
    Import.insert(str(step_path), step_document.Name)
    step_document.recompute()
    reopened_step_solid_count = sum(
        len(getattr(obj, "Shape", None).Solids)
        for obj in step_document.Objects
        if getattr(obj, "Shape", None) is not None
    )
    App.closeDocument(step_document.Name)
    if reopened_step_solid_count < 1:
        raise RuntimeError("reopened STEP contains no solid")

    component = document.addObject("App::Part", "ProbeComponent")
    component.Label = "Probe Component"
    component_shape = document.addObject("Part::Feature", "ComponentShape")
    component_shape.Label = "Component Shape"
    component_shape.Shape = Part.makeBox(5, 4, 3)
    component.addObject(component_shape)
    bom = document.addObject("Assembly::BomObject", "CADAgentBOM")
    bom.columnsNames = ["Index", "Name", "Quantity", "File Name"]
    bom.detailParts = False
    bom.onlyParts = True
    document.recompute()
    columns = [_cell(bom, column, 1) for column in "ABCD"]
    rows: list[list[str]] = []
    for row in range(2, 10002):
        values = [_cell(bom, column, row) for column in "ABCD"]
        if not any(values):
            break
        rows.append(values)
    if columns != ["Index", "Name", "Quantity", "File Name"] or not rows:
        raise RuntimeError("native Assembly BOM did not produce rows")
    total_quantity = sum(int(row[2]) for row in rows)
    if total_quantity <= 0:
        raise RuntimeError("native Assembly BOM quantity is empty")

    checks = [
        {
            "id": "freecad.create_save_reopen_modify",
            "status": "passed",
            "evidence": {
                "object_count_before": object_count_before,
                "object_count_after": len(document.Objects),
                "property": "ProbeBox.Length",
                "before_value": before_value,
                "after_value": after_value,
                "fcstd_size_bytes": fcstd.stat().st_size,
                "fcstd_sha256": _sha256(fcstd),
            },
        },
        {
            "id": "freecad.transaction_abort",
            "status": "passed",
            "evidence": {
                "object_count_before": transaction_count,
                "object_count_after": transaction_count,
                "aborted_object_name": aborted_name,
            },
        },
        {
            "id": "freecad.sketch_solve",
            "status": "passed",
            "evidence": {
                "solver_status": solver_status,
                "fully_constrained": fully_constrained,
                "constraint_count": int(sketch.ConstraintCount),
            },
        },
        {
            "id": "freecad.shape_check",
            "status": "passed",
            "evidence": {
                "shape_valid": shape_valid,
                "shape_error_count": len(shape_errors),
                "solid_count": solid_count,
            },
        },
        {
            "id": "freecad.export_step_stl",
            "status": "passed",
            "evidence": {
                "step_size_bytes": step_path.stat().st_size,
                "stl_size_bytes": stl_path.stat().st_size,
                "step_sha256": _sha256(step_path),
                "stl_sha256": _sha256(stl_path),
                "reopened_step_solid_count": reopened_step_solid_count,
            },
        },
        {
            "id": "freecad.assembly_bom",
            "status": "passed",
            "evidence": {
                "native_type": str(bom.TypeId),
                "columns": columns,
                "row_count": len(rows),
                "total_quantity": total_quantity,
            },
        },
    ]
    return {
        "schema_version": "mcad-runtime-probe.v2",
        "freecad_capabilities_sha256": _sha256(Path("/opt/cad-agent/freecad-capabilities.json")),
        "runtime_lock_sha256": _sha256(
            Path("/opt/cad-agent/runtime-lock.json")
        ),
        "versions": {"freecad": ".".join(App.Version()[:3])},
        "checks": checks,
        "verified_operations": [
            check["id"] for check in checks if check["status"] == "passed"
        ],
    }


if __name__ == "__main__":
    try:
        print(json.dumps(run_probe(), sort_keys=True))
    except Exception as exc:
        print(json.dumps({
            "schema_version": "mcad-runtime-probe.v2",
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc)[:4000],
        }, sort_keys=True))
        raise
