"""Measure saved native and STEP geometry against independent hole expectations.

Run under real FreeCADCmd with CAD_HOLE_MEASUREMENTS pointing to a JSON list of
{name, fcstd, step, dimensions:[x,y,z], holes:[{x,y,diameter,depth}]}.
Coordinates are relative to the plate's lower XY corner, depths measured from top.
"""
import json
import math
import os
from pathlib import Path

import FreeCAD as App
import Part


def measure(shape, case):
    assert shape.isValid() and len(shape.Solids) == 1
    b = shape.BoundBox
    dims = [b.XLength, b.YLength, b.ZLength]
    assert all(abs(a - e) < 1e-5 for a, e in zip(dims, case["dimensions"])), dims
    expected_volume = math.prod(dims)
    measured_holes = []
    cylinders = {}
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", None) != "Part::GeomCylinder" or abs(abs(surface.Axis.z) - 1) > 1e-6:
            continue
        x, y = surface.Center.x - b.XMin, surface.Center.y - b.YMin
        key = (round(x, 5), round(y, 5), round(surface.Radius * 2, 5))
        cylinders.setdefault(key, []).append((face.BoundBox.ZMin, face.BoundBox.ZMax))
    expected_keys = {(h["x"], h["y"], h["diameter"]) for h in case["holes"]}
    assert set(cylinders) == expected_keys, {"actual": list(cylinders), "expected": list(expected_keys)}
    for hole in case["holes"]:
        key = (hole["x"], hole["y"], hole["diameter"])
        low = min(a for a, _ in cylinders[key])
        high = max(z for _, z in cylinders[key])
        depth = high - low
        assert abs(high - b.ZMax) < 1e-5
        if "depth" in hole:
            assert abs(depth - hole["depth"]) < 1e-5, depth
        else:
            assert hole["depth_min"] - 1e-5 <= depth <= hole["depth_max"] + 1e-5, depth
        expected_volume -= math.pi * (hole["diameter"] / 2)**2 * depth
        measured_holes.append({"x": key[0], "y": key[1], "diameter": key[2], "depth_mm": depth,
                               "floor_mm": dims[2] - depth})
        center = App.Vector(b.XMin + hole["x"], b.YMin + hole["y"], b.ZMax - depth / 2)
        assert not shape.isInside(center, 1e-7, True)
        if depth < dims[2] - 1e-5:
            center.z = b.ZMin + (dims[2] - depth) / 2
            assert shape.isInside(center, 1e-7, True), "blind hole broke through remaining floor"
    assert abs(shape.Volume - expected_volume) < 1e-4, (shape.Volume, expected_volume)
    return {"dimensions_mm": dims, "volume_mm3": shape.Volume, "expected_volume_mm3": expected_volume,
            "hole_count": len(cylinders), "holes": measured_holes}


def main():
    result = []
    for case in json.loads(Path(os.environ["CAD_HOLE_MEASUREMENTS"]).read_text()):
        doc = App.openDocument(case["fcstd"])
        bodies = [o for o in doc.Objects if o.TypeId == "PartDesign::Body"]
        assert len(bodies) == 1
        native = measure(bodies[0].Shape, case)
        step = measure(Part.read(case["step"]), case)
        result.append({"name": case["name"], "native": native, "step": step})
        App.closeDocument(doc.Name)
    print("CAD_HOLE_MEASUREMENTS=" + json.dumps(result), flush=True)


try:
    main()
except BaseException:
    import traceback
    traceback.print_exc()
    os._exit(1)
