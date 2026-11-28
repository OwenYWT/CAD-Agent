"""Compare planar DXF geometry with the case's independent reference drawing."""

import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import ezdxf


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UnsupportedDrawing(ValueError):
    pass


def _point(vector, tolerance):
    if not all(math.isfinite(value) for value in vector) or abs(vector.z) > tolerance:
        raise UnsupportedDrawing("expected finite XY-plane geometry")
    return (vector.x, vector.y)


def _primitive(entity, tolerance):
    kind = entity.dxftype()
    if kind == "LINE":
        start = _point(entity.dxf.start, tolerance)
        end = _point(entity.dxf.end, tolerance)
        if start == end:
            raise UnsupportedDrawing("zero-length line")
        return (kind, start, end)
    if kind not in {"CIRCLE", "ARC"}:
        raise UnsupportedDrawing(f"unsupported DXF entity: {kind}")
    center = _point(entity.dxf.center, tolerance)
    radius = entity.dxf.radius
    if not math.isfinite(radius) or radius <= 0:
        raise UnsupportedDrawing("invalid radius")
    if kind == "CIRCLE":
        return (kind, center, radius)
    start, end = entity.dxf.start_angle, entity.dxf.end_angle
    if not math.isfinite(start) or not math.isfinite(end) or (end - start) % 360 == 0:
        raise UnsupportedDrawing("invalid arc span")
    return (kind, center, radius, start % 360, (end - start) % 360)


def _drawing(path, tolerance):
    document = ezdxf.readfile(path)
    if document.units != ezdxf.units.MM:
        raise UnsupportedDrawing("DXF must use millimeters")
    groups = defaultdict(list)
    for entity in document.modelspace():
        kind = entity.dxftype()
        if tuple(entity.dxf.get("extrusion", (0, 0, 1))) != (0, 0, 1) or entity.dxf.get("thickness", 0):
            raise UnsupportedDrawing("expected planar geometry without thickness")
        if kind != "LWPOLYLINE":
            groups[kind].append(_primitive(entity, tolerance))
            continue
        if entity.has_width or not math.isfinite(entity.dxf.elevation) or abs(entity.dxf.elevation) > tolerance:
            raise UnsupportedDrawing("expected a planar polyline without width")
        edges = tuple(_primitive(edge, tolerance) for edge in entity.virtual_entities())
        if not edges:
            raise UnsupportedDrawing("empty polyline")
        groups[kind].append((entity.closed, edges))
    if not groups:
        raise UnsupportedDrawing("empty drawing")
    return groups


def _primitive_matches(actual, reference, tolerance):
    kind, first, second, *angles = actual
    reference_kind, reference_first, reference_second, *reference_angles = reference
    if kind != reference_kind:
        return False
    if kind == "LINE":
        return any(
            math.dist(first, start) <= tolerance and math.dist(second, end) <= tolerance
            for start, end in ((reference_first, reference_second), (reference_second, reference_first))
        )
    if math.dist(first, reference_first) > tolerance or abs(second - reference_second) > tolerance:
        return False
    if kind == "CIRCLE":
        return True
    start, sweep = angles
    ref_start, ref_sweep = reference_angles
    degree_tolerance = math.degrees(tolerance / max(second, reference_second))
    return (abs((start - ref_start + 180) % 360 - 180) <= degree_tolerance
            and abs(sweep - ref_sweep) <= degree_tolerance)


def _polyline_matches(actual, reference, tolerance):
    closed, edges = actual
    reference_closed, reference_edges = reference
    if closed != reference_closed or len(edges) != len(reference_edges):
        return False
    return any(
        all(_primitive_matches(edge, ordered[(index + offset) % len(edges)], tolerance)
            for index, edge in enumerate(edges))
        for ordered in (reference_edges, reference_edges[::-1])
        for offset in (range(len(edges)) if closed else (0,))
    )


def _matches_bijectively(actual, references, matches, tolerance):
    assigned = {}

    def assign(index, visited):
        for ref_index, reference in enumerate(references):
            if ref_index in visited or not matches(actual[index], reference, tolerance):
                continue
            visited.add(ref_index)
            if ref_index not in assigned or assign(assigned[ref_index], visited):
                assigned[ref_index] = index
                return True
        return False

    return all(assign(index, set()) for index in range(len(actual)))


def score(dxf: Path, case: Path) -> dict:
    contract = json.loads((case / "checks.json").read_text(encoding="utf-8"))
    if not isinstance(contract, dict) or set(contract) != {"schema_version", "tolerance_mm"} or (
        contract["schema_version"] != "cad-test-dxf-checks.v1"
    ):
        raise ValueError("unsupported DXF checks contract")
    tolerance = contract["tolerance_mm"]
    if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("invalid DXF tolerance")
    reference = _drawing(case / "reference.dxf", tolerance)
    expected_counts = Counter({kind: len(entities) for kind, entities in reference.items()})
    problem = None
    try:
        actual = _drawing(dxf, tolerance)
    except UnsupportedDrawing as exc:
        actual = {}
        problem = str(exc)
    actual_counts = Counter({kind: len(entities) for kind, entities in actual.items()})
    matched = actual_counts == expected_counts and all(
        _matches_bijectively(actual[kind], entities,
                             _polyline_matches if kind == "LWPOLYLINE" else _primitive_matches,
                             tolerance)
        for kind, entities in reference.items()
    )
    return {
        "schema_version": "cad-eval-score.v1",
        "passed": matched,
        "geometry_valid": problem is None,
        "requirements_passed": int(matched),
        "requirements_total": 1,
        "checks": [
            {
                "id": "dxf.reference_geometry.v1",
                "status": "pass" if matched else "fail",
                "role": "required",
                "observed": {"entity_counts": actual_counts, "problem": problem},
                "expected": {"entity_counts": expected_counts, "tolerance_mm": tolerance},
            }
        ],
        "scorer_error": False,
        "artifact_sha256": digest(dxf),
    }


def main(artifact: Path, case: Path, output: Path) -> int:
    try:
        result = score(artifact, case)
    except Exception as exc:
        result = {
            "schema_version": "cad-eval-score.v1",
            "passed": False,
            "geometry_valid": False,
            "requirements_passed": 0,
            "requirements_total": 1,
            "checks": [],
            "scorer_error": True,
            "error": f"{type(exc).__name__}: {exc}",
        }
        if artifact.is_file():
            result["artifact_sha256"] = digest(artifact)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "scorer_error": result["scorer_error"]}))
    return int(result["scorer_error"])


if __name__ == "__main__":
    raise SystemExit(main(*(Path(value) for value in sys.argv[1:])))
