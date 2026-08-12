"""Deterministic STEP/STL/DXF validation executed only inside the MCAD sandbox."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Iterable


_AXIS_GROUPS = (
    ("width", "depth", "height"),
    ("length", "width", "height"),
    ("length", "depth", "height"),
    ("length", "width", "thickness"),
    ("width", "height", "thickness"),
    ("x", "y", "z"),
    ("width", "height"),
    ("length", "width"),
    ("x", "y"),
)


def _bounds(values: Iterable[float]) -> dict[str, float]:
    numbers = tuple(float(item) for item in values)
    if len(numbers) != 6 or not all(math.isfinite(item) for item in numbers):
        raise ValueError("geometry returned invalid bounds")
    return dict(
        zip(("x_min", "x_max", "y_min", "y_max", "z_min", "z_max"), numbers)
    )


def _dimension_error(
    dimensions: tuple[float, ...],
    expected: dict[str, float],
) -> float | None:
    normalized = {str(key).strip().lower(): value for key, value in expected.items()}
    group = next(
        (group for group in _AXIS_GROUPS if all(key in normalized for key in group)),
        None,
    )
    if group is None:
        return None
    expected_values = sorted(float(normalized[key]) for key in group)
    if len(dimensions) != len(expected_values) or any(
        not math.isfinite(value) or value <= 0 for value in expected_values
    ):
        return 1.0
    return max(
        abs(actual - wanted) / wanted
        for actual, wanted in zip(sorted(dimensions), expected_values)
    )


def _common(path: Path, *, role: str, artifact_format: str) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "role": role,
        "filename": path.name,
        "format": artifact_format,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "parseable": True,
        "valid": False,
        "solid_count": None,
        "face_count": None,
        "is_watertight": None,
        "volume_mm3": None,
        "bounds_mm": None,
        "dimensions_mm": [],
        "max_dimension_error": None,
        "issues": [],
    }


def _validate_step(path: Path, *, role: str) -> dict[str, Any]:
    import cadquery as cq

    result = _common(path, role=role, artifact_format="step")
    model = cq.importers.importStep(str(path))
    shape = model.val()
    solids = tuple(shape.Solids())
    box = shape.BoundingBox()
    dimensions = (float(box.xlen), float(box.ylen), float(box.zlen))
    result.update(
        {
            "valid": bool(shape.isValid() and solids and min(dimensions) > 0),
            "solid_count": len(solids),
            "volume_mm3": max(0.0, sum(float(item.Volume()) for item in solids)),
            "bounds_mm": _bounds(
                (box.xmin, box.xmax, box.ymin, box.ymax, box.zmin, box.zmax)
            ),
            "dimensions_mm": dimensions,
        }
    )
    if not shape.isValid():
        result["issues"].append("invalid_topology")
    if not solids:
        result["issues"].append("no_solid")
    if min(dimensions) <= 0:
        result["issues"].append("degenerate_bounds")
    return result


def _validate_stl(path: Path, *, role: str) -> dict[str, Any]:
    import trimesh

    result = _common(path, role=role, artifact_format="stl")
    mesh = trimesh.load_mesh(path, force="mesh")
    dimensions = tuple(float(item) for item in mesh.bounding_box.extents)
    bounds = mesh.bounding_box.bounds
    valid = bool(
        not mesh.is_empty
        and len(mesh.faces) > 0
        and mesh.is_watertight
        and math.isfinite(float(mesh.volume))
        and abs(float(mesh.volume)) > 0.001
        and min(dimensions) > 0
    )
    result.update(
        {
            "valid": valid,
            "solid_count": len(mesh.split(only_watertight=False)),
            "face_count": len(mesh.faces),
            "is_watertight": bool(mesh.is_watertight),
            "volume_mm3": max(0.0, abs(float(mesh.volume))),
            "bounds_mm": _bounds(
                (
                    bounds[0][0],
                    bounds[1][0],
                    bounds[0][1],
                    bounds[1][1],
                    bounds[0][2],
                    bounds[1][2],
                )
            ),
            "dimensions_mm": dimensions,
        }
    )
    if mesh.is_empty or len(mesh.faces) == 0:
        result["issues"].append("empty_mesh")
    if not mesh.is_watertight:
        result["issues"].append("not_watertight")
    if result["volume_mm3"] <= 0.001:
        result["issues"].append("degenerate_volume")
    if min(dimensions) <= 0:
        result["issues"].append("degenerate_bounds")
    return result


def _validate_dxf(path: Path, *, role: str) -> dict[str, Any]:
    import ezdxf
    from ezdxf import bbox

    result = _common(path, role=role, artifact_format="dxf")
    document = ezdxf.readfile(path)
    entities = tuple(document.modelspace())
    extents = bbox.extents(entities, fast=False)
    if not extents.has_data:
        result["issues"].append("empty_profile")
        return result
    size = extents.size
    dimensions = (float(size.x), float(size.y))
    result.update(
        {
            "valid": bool(entities and min(dimensions) > 0),
            "face_count": len(entities),
            "bounds_mm": _bounds(
                (
                    extents.extmin.x,
                    extents.extmax.x,
                    extents.extmin.y,
                    extents.extmax.y,
                    0,
                    0,
                )
            ),
            "dimensions_mm": dimensions,
        }
    )
    if not entities:
        result["issues"].append("empty_profile")
    if min(dimensions) <= 0:
        result["issues"].append("degenerate_bounds")
    return result


def validate_geometry_files(
    artifacts: list[dict[str, str]],
    *,
    expected_dimensions: dict[str, float] | None = None,
    dimension_tolerance: float = 0.05,
) -> dict[str, Any]:
    expected = dict(expected_dimensions or {})
    reports: list[dict[str, Any]] = []
    issues: list[str] = []
    supported = {"step": _validate_step, "stl": _validate_stl, "dxf": _validate_dxf}
    for item in artifacts:
        role = str(item["role"])
        artifact_format = str(item["format"]).lower()
        path = Path(item["path"])
        try:
            report = supported[artifact_format](path, role=role)
            error = _dimension_error(tuple(report["dimensions_mm"]), expected)
            report["max_dimension_error"] = error
            if error is not None and error > dimension_tolerance:
                report["valid"] = False
                report["issues"].append("dimension_mismatch")
        except Exception as exc:
            report = _common(path, role=role, artifact_format=artifact_format)
            report["parseable"] = False
            report["issues"].append(f"parse_failed:{type(exc).__name__}")
        reports.append(report)
        issues.extend(f"{role}:{issue}" for issue in report["issues"])
    outcome = (
        "indeterminate"
        if any(not item["parseable"] for item in reports)
        else "passed"
        if reports and all(item["valid"] for item in reports)
        else "failed"
    )
    return {
        "schema_version": "durable-geometry-report.v1",
        "outcome": outcome,
        "artifact_kind": "profile"
        if reports and all(item["format"] == "dxf" for item in reports)
        else "solid",
        "expected_dimensions_mm": expected,
        "dimension_tolerance": dimension_tolerance,
        "artifacts": reports,
        "issues": issues,
    }
