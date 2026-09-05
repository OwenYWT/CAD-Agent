"""Independent fail-closed native Assembly BOM runner for FreeCADCmd."""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any

import Assembly  # noqa: F401
import FreeCAD as App
import Import
import Part


INPUT_ROOT = Path("/sandbox/input")
OUTPUT_ROOT = Path("/sandbox/output")
SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


class BOMError(RuntimeError):
    def __init__(self, code: str, message: str, *, details=None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _input(task: dict[str, Any], role: str) -> Path:
    raw = task["inputs"].get(role)
    if not isinstance(raw, str) or Path(raw).name != raw:
        raise BOMError("bom_input_missing", f"BOM input is missing: {role}")
    path = INPUT_ROOT / raw
    if not path.is_file() or path.is_symlink():
        raise BOMError("bom_input_missing", f"BOM input is missing: {role}")
    return path


def _shapes(document) -> list[Any]:
    solid_objects = [
        obj for obj in document.Objects
        if getattr(obj, "Shape", None) is not None
        and not obj.Shape.isNull()
        and len(obj.Shape.Solids) > 0
    ]
    solid_names = {str(obj.Name) for obj in solid_objects}
    # FreeCAD's STEP importer exposes an assembly compound and its component
    # features simultaneously. Counting both doubles the solid count and
    # volume. Geometry evidence must therefore use the leaf shapes that carry
    # the actual component solids, while retaining a lone root for flat STEP.
    leaves = [
        obj
        for obj in solid_objects
        if not any(
            str(getattr(child, "Name", "")) in solid_names
            and child is not obj
            for child in getattr(
                obj,
                "OutListRecursive",
                getattr(obj, "OutList", ()),
            )
        )
    ]
    return [obj.Shape for obj in leaves]


def _measure(shapes: list[Any]) -> tuple[int, tuple[float, float, float], float]:
    compound = Part.makeCompound(shapes)
    box = compound.BoundBox
    return (
        sum(len(shape.Solids) for shape in shapes),
        (float(box.XLength), float(box.YLength), float(box.ZLength)),
        sum(float(shape.Volume) for shape in shapes),
    )


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-6, 1e-6 * max(abs(a), abs(b)))


def _cell(sheet, column: int, row: int) -> str:
    letters = ""
    number = column + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    try:
        value = sheet.get(f"{letters}{row + 1}")
    except ValueError:
        return ""
    return str(value or "").lstrip("'")


def run_bom(task: dict[str, Any]) -> dict[str, Any]:
    params = task.get("params")
    request = params.get("request") if isinstance(params, dict) else None
    if (
        task.get("schema_version") != "mcad-capability-task.v1"
        or task.get("capability") != "freecad"
        or task.get("operation") != "bom"
        or not isinstance(request, dict)
        or request.get("schema_version") != "freecad-bom-request.v1"
        or not isinstance(task.get("inputs"), dict)
    ):
        raise BOMError("bom_generation_failed", "invalid FreeCAD BOM task")
    components = request.get("components")
    columns = ["Index", "Name", "Quantity", "File Name"]
    properties = request.get("property_columns")
    if not isinstance(components, list) or not components:
        raise BOMError("bom_source_not_assembly", "BOM has no components")
    if not isinstance(properties, list) or len(properties) > 60:
        raise BOMError("bom_generation_failed", "invalid BOM property columns")
    if any(not isinstance(item, str) or SAFE_NAME.fullmatch(item) is None for item in properties):
        raise BOMError("bom_generation_failed", "invalid BOM property column")
    columns.extend(f".{item}" for item in properties)

    document = App.newDocument("AssemblyBOM")
    component_shapes: list[Any] = []
    expected_labels: list[str] = []
    try:
        for component in components:
            step_key = str(component.get("step_key") or "")
            label = str(component.get("label") or "")
            role = str(component.get("artifact_id") or "")
            position = component.get("position_mm")
            if (
                SAFE_NAME.fullmatch(step_key.replace("-", "_")) is None
                or not label
                or not isinstance(position, list)
                or len(position) != 3
                or any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in position)
            ):
                raise BOMError("bom_generation_failed", "invalid BOM component")
            imported = App.newDocument(f"Import_{step_key.replace('-', '_')}")
            Import.insert(str(_input(task, role)), imported.Name)
            imported.recompute()
            shapes = _shapes(imported)
            if not shapes:
                raise BOMError("bom_source_not_assembly", f"component has no solid: {step_key}")
            local_box = Part.makeCompound(shapes).BoundBox
            # Match the assembly-combiner contract: ``position_mm`` targets
            # the centre of the component's bottom bounding-box face.  The
            # source STEP may have any local origin, so normalise that anchor
            # before applying the requested assembly placement.
            local_anchor = App.Vector(
                (float(local_box.XMin) + float(local_box.XMax)) / 2.0,
                (float(local_box.YMin) + float(local_box.YMax)) / 2.0,
                float(local_box.ZMin),
            )
            target_anchor = App.Vector(*(float(value) for value in position))
            placement_offset = target_anchor.sub(local_anchor)
            container = document.addObject("App::Part", f"Part_{step_key.replace('-', '_')}")
            container.Label = label
            container.Placement.Base = placement_offset
            for index, shape in enumerate(shapes, start=1):
                feature = document.addObject("Part::Feature", f"Shape_{step_key.replace('-', '_')}_{index}")
                feature.Shape = shape.copy()
                container.addObject(feature)
                placed = shape.copy()
                placed.translate(placement_offset)
                component_shapes.append(placed)
            expected_labels.append(label)
            App.closeDocument(imported.Name)

        combined = App.newDocument("CombinedCheck")
        Import.insert(str(_input(task, "assembly")), combined.Name)
        combined.recompute()
        combined_shapes = _shapes(combined)
        if not combined_shapes:
            raise BOMError("bom_source_not_assembly", "combine STEP has no solid")
        measured_components = _measure(component_shapes)
        measured_combine = _measure(combined_shapes)
        App.closeDocument(combined.Name)
        if (
            measured_components[0] != measured_combine[0]
            or any(not _close(a, b) for a, b in zip(measured_components[1], measured_combine[1]))
            or not _close(measured_components[2], measured_combine[2])
        ):
            raise BOMError(
                "bom_source_geometry_mismatch",
                "component tree geometry differs from combine STEP",
                details={
                    "components_solid_count": measured_components[0],
                    "components_bounds_mm": list(measured_components[1]),
                    "components_volume_mm3": measured_components[2],
                    "combine_solid_count": measured_combine[0],
                    "combine_bounds_mm": list(measured_combine[1]),
                    "combine_volume_mm3": measured_combine[2],
                },
            )

        assembly_path = OUTPUT_ROOT / "assembly.FCStd"
        document.recompute()
        document.saveAs(str(assembly_path))
        bom = document.addObject("Assembly::BomObject", "CADAgentBOM")
        bom.columnsNames = columns
        bom.detailParts = False
        bom.onlyParts = True
        document.recompute()
        native_columns = [_cell(bom, col, 0) for col in range(len(columns))]
        if native_columns != [column.lstrip(".") for column in columns]:
            raise BOMError(
                "bom_generation_failed",
                "native BOM columns differ from the requested columns",
            )
        rows = []
        for row_index in range(1, 10_001):
            cells = [_cell(bom, col, row_index) for col in range(len(columns))]
            if not any(cells):
                break
            try:
                quantity = int(cells[2])
            except (TypeError, ValueError) as exc:
                raise BOMError("bom_generation_failed", "native BOM quantity is invalid") from exc
            file_name = Path(cells[3]).name
            if Path(file_name).name != file_name or not file_name:
                raise BOMError("bom_generation_failed", "native BOM file name is unsafe")
            rows.append({
                "index": cells[0],
                "name": cells[1],
                "quantity": quantity,
                "file_name": file_name,
                "properties": {
                    properties[index]: cells[index + 4]
                    for index in range(len(properties))
                },
            })
        if not rows:
            raise BOMError("bom_empty", "native Assembly BOM returned no rows")
        if [row["name"] for row in rows] != expected_labels or sum(row["quantity"] for row in rows) != len(components):
            raise BOMError("bom_source_hierarchy_lost", "native BOM rows differ from the component plan")
        document_payload = {
            "schema_version": "freecad-bom-runner.v1",
            "generator": {
                "freecad_version": ".".join(App.Version()[:3]),
                "runtime_image_digest": request["runtime_image_digest"],
                "native_type": str(bom.TypeId),
            },
            "columns": [column.lstrip(".") for column in native_columns],
            "rows": rows,
        }
        json_path = OUTPUT_ROOT / "bom.json"
        csv_path = OUTPUT_ROOT / "bom.csv"
        json_path.write_text(json.dumps(document_payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(document_payload["columns"])
            for row in rows:
                writer.writerow([row["index"], row["name"], row["quantity"], row["file_name"], *(row["properties"].get(item, "") for item in properties)])
        assembly_path.unlink(missing_ok=True)
        return {
            "schema_version": "freecad-operation-result.v1",
            "status": "succeeded",
            "operations": [],
            "files": {"bom-json": str(json_path), "bom-csv": str(csv_path)},
            "validations": [{"native_bom_row_count": len(rows)}],
            "runtime": {"freecad": ".".join(App.Version()[:3])},
        }
    finally:
        App.closeDocument(document.Name)
