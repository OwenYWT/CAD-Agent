"""Compare transferred, placed solids whose aggregate BOM measurements coincide."""
import math
import sys
from pathlib import Path

import FreeCAD as App
import Part

sys.path.insert(0, "/opt/cad-agent")
from freecad_bom import BOMError, run_bom

root = Path("/sandbox/input")
hole_center = App.Vector(10, 10, 0)
stock = Part.makeBox(40, 20, 6)
plate = stock.cut(Part.makeCylinder(2, 6, hole_center))
spacer = Part.makeBox(4, 4, 4, App.Vector(60, 8, 0))
plate.exportStep(str(root / "plate.step"))
spacer.exportStep(str(root / "spacer.step"))
components = [
    {"step_key": "plate", "label": "Plate", "position_mm": [20, 10, 0], "artifact_id": "plate", "quantity": 1},
    {"step_key": "spacer", "label": "Spacer", "position_mm": [62, 10, 0], "artifact_id": "spacer", "quantity": 1},
]
task = {"schema_version": "mcad-capability-task.v1", "capability": "freecad", "operation": "bom",
        "inputs": {"plate": "plate.step", "spacer": "spacer.step", "assembly": "assembly.step"},
        "params": {"request": {"schema_version": "freecad-bom-request.v1", "components": components,
                              "property_columns": [], "runtime_image_digest": "sha256:" + "a" * 64}}}
assembly_path = str(root / "assembly.step")
Part.makeCompound([spacer, plate]).exportStep(assembly_path)
assert run_bom(task)["status"] == "succeeded"
reference = Part.read(assembly_path)
moved = stock.cut(Part.makeCylinder(2, 6, hole_center + App.Vector(20, 0, 0)))
Part.makeCompound([spacer, moved]).exportStep(assembly_path)
actual = Part.read(assembly_path)
assert math.isclose(reference.Volume, actual.Volume)
differences = [a.cut(b).Volume for a, b in ((reference, actual), (actual, reference))]
assert all(math.isclose(value, math.pi * 2**2 * 6, rel_tol=1e-8) for value in differences)
try:
    run_bom(task)
except BOMError as exc:
    assert exc.code == "bom_source_geometry_mismatch", exc.code
else:
    raise AssertionError("Moved hole passed the BOM")
print("CAD_BOM_GEOMETRY_CONTRACT=", differences, flush=True)
