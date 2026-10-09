"""Policy-snapshot-driven deterministic DFM checks inside the sandbox."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


_CONSTRAINT_RULE_KEYS = {
    "wall_thickness": ("min_wall", "min_wall_thickness"),
    "size": ("max_size",),
    "hole": ("min_hole_diameter",),
    "draft_angle": ("min_draft_angle", "draft_angle"),
    "fillet": ("min_internal_radius", "min_fillet"),
}


def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _metrics(stl_path: Path) -> tuple[dict[str, Any], str | None]:
    import numpy as np
    import trimesh

    mesh_source = stl_path
    analytic = {}
    if stl_path.suffix.lower() in {".step", ".stp"}:
        import cadquery as cq

        mesh_source = Path("/tmp/dfm-source.stl")
        model = cq.importers.importStep(str(stl_path))
        try:
            from dfm_brep import measure_brep
        except ModuleNotFoundError:
            from sandbox.dfm_brep import measure_brep
        analytic = measure_brep(model.val())
        cq.exporters.export(model, str(mesh_source), exportType="STL")
    try:
        from mesh_normalization import load_normalized_mesh
    except ModuleNotFoundError:
        from sandbox.mesh_normalization import load_normalized_mesh
    mesh = load_normalized_mesh(mesh_source)
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty or not len(mesh.faces):
        raise ValueError("DFM input contains no mesh")
    bounds = mesh.bounding_box.bounds
    extents = mesh.bounding_box.extents
    area = float(mesh.area)
    overhang = 0.0
    if area > 0:
        overhang = float(
            np.sum(mesh.area_faces[(mesh.face_normals[:, 2] < -math.cos(math.pi/4))
                & (mesh.triangles[:,:,2].max(axis=1) > bounds[0,2]+1e-5)]) / area
        )
    metrics: dict[str, Any] = {
        **analytic,
        "is_watertight": bool(mesh.is_watertight),
        "face_count": int(len(mesh.faces)),
        "surface_area_mm2": round(area, 6),
        "volume_mm3": round(abs(float(mesh.volume)), 6)
        if mesh.is_watertight
        else 0.0,
        "max_size_mm": round(float(max(extents)), 6),
        "overhang_ratio": round(overhang, 6),
        "material_ratio": round(
            abs(float(mesh.volume)) / float(np.prod(extents)), 6
        )
        if mesh.is_watertight and float(np.prod(extents)) > 0
        else 0.0,
    }
    thickness_issue: str | None = None
    try:
        sample_count = min(200, len(mesh.faces))
        rng = np.random.RandomState(42)
        indices = rng.choice(len(mesh.faces), size=sample_count, replace=False)
        origins = mesh.triangles_center[indices] + mesh.face_normals[indices] * 0.01
        locations, ray_indices, _ = mesh.ray.intersects_location(
            ray_origins=origins,
            ray_directions=-mesh.face_normals[indices],
        )
        values: list[float] = []
        for index in range(sample_count):
            hits = locations[ray_indices == index]
            if len(hits):
                distances = np.linalg.norm(hits - origins[index], axis=1)
                valid = distances[distances > 0.05]
                if len(valid):
                    # Rays start 0.01 mm outside the surface. That offset is
                    # not material and must not let a too-thin wall pass.
                    values.append(float(np.min(valid)) - 0.01)
        if values:
            metrics["min_wall_thickness_mm"] = round(
                float(np.percentile(values, 5)), 6
            )
        else:
            thickness_issue = "wall_thickness_unmeasurable"
    except Exception as exc:
        thickness_issue = f"wall_thickness_unavailable:{type(exc).__name__}"
    # A nearly filled bounding box cannot establish local wall thickness:
    # even a small pocket can have a thin floor. Missing measurements remain
    # indeterminate instead of manufacturing a passing wall measurement.
    if "min_feature_size_mm" in metrics and "min_wall_thickness_mm" in metrics:
        metrics["min_feature_size_mm"] = min(metrics["min_feature_size_mm"], metrics["min_wall_thickness_mm"])
    return metrics, thickness_issue


def _actual(category: str, metrics: dict[str, Any], unit: str) -> float | None:
    if {"wall_thickness": "mm", "overhang": "ratio", "size": "mm", "feature": "mm"}.get(category) != unit:
        return None
    return {
        "wall_thickness": metrics.get("min_wall_thickness_mm"),
        "overhang": metrics.get("overhang_ratio"),
        "size": metrics.get("max_size_mm"),
        "feature": metrics.get("min_feature_size_mm"),
    }.get(category)


def _thresholds(rule: dict[str, Any], constraints: dict[str, Any], *, configured_rules: bool = False):
    minimum = rule.get("threshold_min")
    maximum = rule.get("threshold_max")
    if configured_rules:
        return minimum, maximum
    for key in _CONSTRAINT_RULE_KEYS.get(str(rule["category"]), ()):
        if key not in constraints:
            continue
        if key.startswith("min") or key in {"draft_angle", "min_fillet"}:
            minimum = float(constraints[key])
        elif key.startswith("max"):
            maximum = float(constraints[key])
    return minimum, maximum


def _suggestion(rule, actual, minimum, maximum):
    template=str(rule.get("suggestion_template") or "")
    try:
        return template.format(actual=actual,min=minimum,max=maximum)
    except (KeyError,ValueError,TypeError,AttributeError):
        return f"{rule.get('description') or rule['id']}: measured {actual:g}; min={minimum}, max={maximum}"


def evaluate_dfm(
    stl_path: Path,
    policy_path: Path,
    *,
    expected_policy_hash: str,
) -> dict[str, Any]:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if _canonical_hash(policy) != expected_policy_hash:
        raise ValueError("DFM policy snapshot hash mismatch")
    if policy.get("schema_version") != "dfm-policy-snapshot.v1":
        raise ValueError("unsupported DFM policy snapshot")
    metrics, thickness_issue = _metrics(stl_path)
    evaluated: list[str] = []
    unevaluated: list[str] = []
    violations: list[dict[str, Any]] = []
    constraints = dict(policy.get("knowledge_constraints") or {})
    for rule in policy.get("rules") or []:
        rule_id = str(rule["id"])
        bridge_rule = rule_id == "fdm_bridge_distance"
        if rule["check_type"] == "heuristic" and not bridge_rule:
            unevaluated.append(rule_id)
            continue
        actual = metrics.get("bridge_span_mm") if bridge_rule and rule.get("unit") == "mm" else _actual(str(rule["category"]), metrics, str(rule.get("unit") or ""))
        if actual is None or not math.isfinite(float(actual)):
            unevaluated.append(rule_id)
            continue
        evaluated.append(rule_id)
        minimum, maximum = _thresholds(rule, constraints, configured_rules=policy.get("threshold_precedence") == "configured_rules")
        # Older tenant policy snapshots used a heuristic bridge rule without
        # a calibrated span. Preserve the policy: any measured bridge needs
        # review until the user supplies a tested positive threshold.
        if bridge_rule and maximum is None:
            maximum = 0.0
        violated = (
            minimum is not None and actual < float(minimum)
        ) or (
            maximum is not None and actual > float(maximum)
        )
        if violated:
            violations.append(
                {
                    "rule_id": rule_id,
                    "category": str(rule["category"]),
                    "severity": str(rule["severity"]),
                    "actual_value": float(actual),
                    "message": (
                        f"{rule.get('description') or rule_id}: measured {actual:g} "
                        f"{rule.get('unit') or ''}".strip()
                    ),
                    "suggestion": _suggestion(rule, actual, minimum, maximum),
                    "source": "geometric",
                }
            )
    issues: list[str] = []
    if not policy.get("rules"):
        issues.append("no_active_rules")
    if metrics.get("unanchored_roof_area_mm2",0)>1e-6:
        issues.append("horizontal_roof_requires_support")
    if not metrics["is_watertight"]:
        issues.append("mesh_not_watertight")
    if thickness_issue and any(
        rule["category"] == "wall_thickness"
        and rule["check_type"] == "geometric"
        for rule in policy.get("rules") or []
    ):
        issues.append(thickness_issue)
    blocking = any(item["severity"] != "info" for item in violations)
    required_unmeasured = [
        rule["id"]
        for rule in policy.get("rules") or []
        if rule["id"] in unevaluated
        and rule["severity"] != "info"
    ]
    if required_unmeasured:
        issues.append("unevaluated_rules:" + ",".join(sorted(required_unmeasured)))
    elif thickness_issue:
        issues = [item for item in issues if item != thickness_issue]
    outcome = "failed" if blocking else "indeterminate" if issues else "passed"
    return {
        "schema_version": "durable-dfm-report.v1",
        "outcome": outcome,
        "process": str(policy["process"]),
        "material": str(policy["material"]),
        "policy_hash": expected_policy_hash,
        "metrics": metrics,
        "evaluated_rule_ids": evaluated,
        "unevaluated_rule_ids": unevaluated,
        "violations": violations,
        "issues": issues,
    }
