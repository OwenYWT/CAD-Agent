"""Stable projection from deterministic DFM evidence to the public API."""
from __future__ import annotations

from typing import Any

from app.models.schemas import (
    Annotation3D,
    DesignAnalysisResponse,
    DFMIssueModel,
    RuleViolationModel,
    StepAnalysisSummary,
)


def build_annotations(result: Any) -> list[Annotation3D]:
    annotations: list[Annotation3D] = []
    step_data = result.step_data
    face_center_map: dict[str, list[float]] = {}
    if step_data and not step_data.error:
        for face in step_data.faces:
            face_center_map[str(face.face_id)] = face.center
        for index, feature in enumerate(step_data.features):
            if feature.feature_type == "hole":
                diameter = feature.dimensions.get("diameter", 0)
                annotations.append(Annotation3D(
                    id=f"feat_hole_{index}",
                    type="point_marker",
                    severity="info",
                    position=feature.center,
                    label=f"\u2300{diameter}mm",
                    detail=f"\u5B54\u5F84 {diameter}mm",
                    category="hole",
                    face_ids=feature.face_ids or None,
                ))
        if step_data.wall_thicknesses:
            for index, wall in enumerate(sorted(
                step_data.wall_thicknesses,
                key=lambda item: item.thickness,
            )[:3]):
                severity = (
                    "critical"
                    if wall.thickness < 0.8
                    else "warning"
                    if wall.thickness < 1.5
                    else "info"
                )
                annotations.append(Annotation3D(
                    id=f"wt_{index}",
                    type="point_marker",
                    severity=severity,
                    position=wall.point,
                    label=f"\u58C1\u539A {wall.thickness:.2f}mm",
                    detail=f"\u8BE5\u5904\u58C1\u539A {wall.thickness:.2f}mm",
                    category="wall_thickness",
                ))
        for draft in step_data.draft_angles:
            if draft.angle is not None and draft.angle < 2.0:
                face_id = draft.face_id
                annotations.append(Annotation3D(
                    id=f"draft_{face_id}",
                    type="point_marker",
                    severity=(
                        "critical" if draft.angle < 0.5 else "warning"
                    ),
                    position=face_center_map.get(str(face_id), [0, 0, 0]),
                    label=f"\u62D4\u6A21\u89D2 {draft.angle:.1f}\u00B0",
                    detail=(
                        f"face {face_id} \u62D4\u6A21\u89D2\u4EC5 "
                        f"{draft.angle:.1f}\u00B0\uFF0C\u6CE8\u5851\u5EFA\u8BAE >1\u00B0"
                    ),
                    category="draft_angle",
                    face_ids=[face_id],
                ))

    bounding_box = (
        result.geometry.bounding_box
        if result.geometry and result.geometry.bounding_box
        else {}
    )
    default_center = [
        (
            bounding_box.get("x_max", 0)
            + bounding_box.get("x_min", 0)
        ) / 2,
        (
            bounding_box.get("y_max", 0)
            + bounding_box.get("y_min", 0)
        ) / 2,
        (
            bounding_box.get("z_max", 0)
            + bounding_box.get("z_min", 0)
        ) / 2,
    ]
    for index, violation in enumerate(result.rule_violations):
        if violation.get("category") in {
            annotation.category for annotation in annotations
        }:
            continue
        annotations.append(Annotation3D(
            id=f"rv_{index}",
            type="point_marker",
            severity=violation.get("severity", "warning"),
            position=default_center,
            label=violation.get("message", "")[:30],
            detail=violation.get("suggestion", ""),
            category=violation.get("category", ""),
        ))
    return annotations


def build_design_analysis_response(result: Any) -> DesignAnalysisResponse:
    geometry = {}
    if result.geometry:
        geometry = {
            "min_wall_thickness": result.geometry.min_wall_thickness,
            "has_thin_walls": result.geometry.has_thin_walls,
            "overhang_ratio": result.geometry.overhang_ratio,
            "has_sharp_edges": result.geometry.has_sharp_edges,
            "sharp_edge_count": result.geometry.sharp_edge_count,
            "surface_area": result.geometry.surface_area,
            "volume": result.geometry.volume,
            "material_ratio": result.geometry.material_ratio,
            "face_count": result.geometry.face_count,
            "is_watertight": result.geometry.is_watertight,
            "bounding_box": result.geometry.bounding_box,
            "issues": result.geometry.issues,
        }
    step_summary = StepAnalysisSummary()
    if result.step_data and not result.step_data.error:
        step_data = result.step_data
        step_summary = StepAnalysisSummary(
            available=True,
            face_count=step_data.global_properties.face_count,
            edge_count=step_data.global_properties.edge_count,
            feature_count=len(step_data.features),
            min_wall_thickness=(
                step_data.derived_metrics.min_wall_thickness
            ),
            min_fillet_radius=step_data.derived_metrics.min_fillet_radius,
            min_hole_diameter=step_data.derived_metrics.min_hole_diameter,
            min_draft_angle=step_data.derived_metrics.min_draft_angle,
            features=[
                {
                    "type": feature.feature_type,
                    "dimensions": feature.dimensions,
                    "center": feature.center,
                }
                for feature in step_data.features
            ],
        )
    return DesignAnalysisResponse(
        design_score=result.design_score,
        evaluation_status=getattr(result, "evaluation_status", "indeterminate"),
        evaluated_rules=getattr(result, "evaluated_rules", []),
        analysis_errors=getattr(result, "analysis_errors", []),
        design_summary=result.design_summary,
        structural_issues=result.structural_issues,
        functional_notes=result.functional_notes,
        recommended_process=result.recommended_process,
        process_compatibility=result.process_compatibility,
        dfm_issues=[
            DFMIssueModel(
                category=issue.category,
                severity=issue.severity,
                description=issue.description,
                suggestion=issue.suggestion,
                location=issue.location,
            )
            for issue in result.dfm_issues
        ],
        estimated_difficulty=result.estimated_difficulty,
        geometry=geometry,
        rule_violations=[
            RuleViolationModel(**violation)
            for violation in result.rule_violations
        ],
        step_analysis=step_summary,
        annotations=build_annotations(result),
    )
