import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import verify_api_key
from app.config import settings
from app.storage.file_ownership import request_belongs_to
from app.models.schemas import (
    AnalyzeRequest,
    Annotation3D,
    DesignAnalysisResponse,
    DFMIssueModel,
    RuleViolationModel,
    StepAnalysisSummary,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _build_annotations(result) -> list[Annotation3D]:
    """Generate 3D annotations from analysis results."""
    annotations: list[Annotation3D] = []
    step_data = result.step_data

    # Use STEP face data if available for precise positions
    face_center_map: dict[str, list[float]] = {}
    if step_data and not step_data.error:
        for face in step_data.faces:
            face_center_map[str(face.face_id)] = face.center

    # Annotations from STEP features (holes, fillets)
    if step_data and not step_data.error:
        for i, feat in enumerate(step_data.features):
            if feat.feature_type == "hole":
                d = feat.dimensions.get("diameter", 0)
                annotations.append(Annotation3D(
                    id=f"feat_hole_{i}",
                    type="point_marker",
                    severity="info",
                    position=feat.center,
                    label=f"\u2300{d}mm",
                    detail=f"\u5B54\u5F84 {d}mm",
                    category="hole",
                    face_ids=feat.face_ids or None,
                ))

        # Wall thickness: annotate thinnest points
        if step_data.wall_thicknesses:
            sorted_wt = sorted(step_data.wall_thicknesses, key=lambda w: w.thickness)
            # Show up to 3 thinnest spots
            for i, wt in enumerate(sorted_wt[:3]):
                severity = "critical" if wt.thickness < 0.8 else "warning" if wt.thickness < 1.5 else "info"
                annotations.append(Annotation3D(
                    id=f"wt_{i}",
                    type="point_marker",
                    severity=severity,
                    position=wt.point,
                    label=f"\u58C1\u539A {wt.thickness:.2f}mm",
                    detail=f"\u8BE5\u5904\u58C1\u539A {wt.thickness:.2f}mm",
                    category="wall_thickness",
                ))

        # Draft angle: annotate faces with low draft angle
        for da in step_data.draft_angles:
            if da.angle is not None and da.angle < 2.0:
                fid = da.face_id
                pos = face_center_map.get(str(fid), [0, 0, 0])
                severity = "critical" if da.angle < 0.5 else "warning"
                annotations.append(Annotation3D(
                    id=f"draft_{fid}",
                    type="point_marker",
                    severity=severity,
                    position=pos,
                    label=f"\u62D4\u6A21\u89D2 {da.angle:.1f}\u00B0",
                    detail=f"face {fid} \u62D4\u6A21\u89D2\u4EC5 {da.angle:.1f}\u00B0\uFF0C\u6CE8\u5851\u5EFA\u8BAE >1\u00B0",
                    category="draft_angle",
                    face_ids=[fid],
                ))

    # Annotations from rule violations (use bounding box center as fallback position)
    bb = {}
    if result.geometry:
        bb = result.geometry.bounding_box or {}
    default_center = [
        (bb.get("x_max", 0) + bb.get("x_min", 0)) / 2,
        (bb.get("y_max", 0) + bb.get("y_min", 0)) / 2,
        (bb.get("z_max", 0) + bb.get("z_min", 0)) / 2,
    ]

    for i, rv in enumerate(result.rule_violations):
        # Skip if we already have an annotation for this category from STEP data
        existing_cats = {a.category for a in annotations}
        if rv.get("category") in existing_cats:
            continue

        annotations.append(Annotation3D(
            id=f"rv_{i}",
            type="point_marker",
            severity=rv.get("severity", "warning"),
            position=default_center,
            label=rv.get("message", "")[:30],
            detail=rv.get("suggestion", ""),
            category=rv.get("category", ""),
        ))

    return annotations


@router.post("/analyze/{request_id}", response_model=DesignAnalysisResponse)
async def analyze_design(
    request_id: str,
    body: AnalyzeRequest,
    credential=Depends(verify_api_key),
):
    if not _SAFE_ID_RE.match(request_id):
        raise HTTPException(400, "Invalid request_id")
    if not request_belongs_to(request_id, credential):
        raise HTTPException(404, "Request ID not found")

    # Find the STL file
    storage = Path(settings.file_storage_dir) / request_id
    if not storage.exists():
        raise HTTPException(404, "Request ID not found")

    stl_path = None
    step_path = None
    for f in storage.glob("*.stl"):
        stl_path = f
        break
    for f in storage.glob("*.step"):
        step_path = f
        break

    if stl_path is None:
        raise HTTPException(404, "No STL file found for this request")

    # Run analysis (prefer STEP for precision, STL always needed for rendering)
    from app.validation.dfm_analyzer import DFMAnalyzer

    analyzer = DFMAnalyzer()
    try:
        result = await analyzer.analyze(
            stl_path=stl_path,
            code=body.code,
            description=body.description,
            process=body.process,
            step_path=step_path,
            material=body.material,
        )
    except Exception as e:
        logger.error(f"Analysis failed for {request_id}: {e}", exc_info=True)
        raise HTTPException(500, f"Analysis failed: {e}")
    finally:
        # Clean up render images
        render_dir = stl_path.parent / "dfm_renders"
        if render_dir.exists():
            shutil.rmtree(render_dir, ignore_errors=True)

    # Build response
    geo_dict = {}
    if result.geometry:
        geo_dict = {
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

    # Build STEP analysis summary for frontend
    step_summary = StepAnalysisSummary()
    if result.step_data and not result.step_data.error:
        sd = result.step_data
        step_summary = StepAnalysisSummary(
            available=True,
            face_count=sd.global_properties.face_count,
            edge_count=sd.global_properties.edge_count,
            feature_count=len(sd.features),
            min_wall_thickness=sd.derived_metrics.min_wall_thickness,
            min_fillet_radius=sd.derived_metrics.min_fillet_radius,
            min_hole_diameter=sd.derived_metrics.min_hole_diameter,
            min_draft_angle=sd.derived_metrics.min_draft_angle,
            features=[
                {
                    "type": f.feature_type,
                    "dimensions": f.dimensions,
                    "center": f.center,
                }
                for f in sd.features
            ],
        )

    # Build 3D annotations
    annotations = _build_annotations(result)

    return DesignAnalysisResponse(
        design_score=result.design_score,
        design_summary=result.design_summary,
        structural_issues=result.structural_issues,
        functional_notes=result.functional_notes,
        recommended_process=result.recommended_process,
        process_compatibility=result.process_compatibility,
        dfm_issues=[
            DFMIssueModel(
                category=i.category,
                severity=i.severity,
                description=i.description,
                suggestion=i.suggestion,
                location=i.location,
            )
            for i in result.dfm_issues
        ],
        estimated_difficulty=result.estimated_difficulty,
        geometry=geo_dict,
        rule_violations=[
            RuleViolationModel(**rv) for rv in result.rule_violations
        ],
        step_analysis=step_summary,
        annotations=annotations,
    )
