"""DFM rule data models and STEP analysis result types."""

import math
from pydantic import BaseModel


def rule_process(process: str | None) -> str | None:
    if process is None:
        return None
    value = process.strip()
    return {'cnc':'CNC','fdm':'FDM','sla':'SLA','laser_cut':'sheet_metal'}.get(value.lower(), value)


def validate_rule_thresholds(rule: dict) -> None:
    minimum, maximum = rule.get('threshold_min'), rule.get('threshold_max')
    if any(value is not None and not math.isfinite(float(value)) for value in (minimum, maximum)):
        raise ValueError('规则阈值必须是有限数值')
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError('规则最小阈值不能大于最大阈值')


class DFMRule(BaseModel):
    id: str
    process: str  # "CNC" | "FDM" | "SLA" | "injection_mold" | "sheet_metal"
    category: str  # "wall_thickness" | "draft_angle" | "undercut" | "overhang" | "fillet" | "hole" | "tolerance" | "size" | "feature"
    check_type: str  # "geometric" | "heuristic"
    threshold_min: float | None = None
    threshold_max: float | None = None
    unit: str = "mm"
    severity: str = "warning"  # "critical" | "warning" | "info"
    description: str = ""
    suggestion_template: str = ""
    enabled: bool = True


class DFMRuleSet(BaseModel):
    id: str
    name: str
    process: str
    rules: list[DFMRule] = []


class RuleViolation(BaseModel):
    rule_id: str
    rule: DFMRule
    actual_value: float | None = None
    message: str = ""
    suggestion: str = ""
    severity: str = "warning"
    source: str = "geometric"  # "geometric" | "heuristic"


# --- STEP Analysis Result Types (Phase 2) ---


class FaceInfo(BaseModel):
    face_id: int
    face_type: str  # "plane" | "cylinder" | "cone" | "sphere" | "torus" | "bspline" | "other"
    area: float
    normal: list[float]  # [x, y, z] average normal
    draft_angle: float | None = None  # degrees, relative to pull direction
    center: list[float]  # [x, y, z] face centroid
    # Extra (type-specific)
    radius: float | None = None  # cylinder/sphere
    semi_angle: float | None = None  # cone
    major_radius: float | None = None  # torus
    minor_radius: float | None = None  # torus


class EdgeInfo(BaseModel):
    edge_id: int
    edge_type: str  # "line" | "arc" | "curve"
    length: float
    radius: float | None = None  # arc radius


class FeatureInfo(BaseModel):
    feature_type: str  # "hole" | "fillet" | "fillet_face" | "pocket" | "slot" | "boss" | "chamfer"
    dimensions: dict  # e.g. {"diameter": 10, "depth": 15}
    center: list[float]  # [x, y, z]
    face_ids: list[int] = []


class WallThicknessSample(BaseModel):
    point: list[float]  # [x, y, z]
    thickness: float  # mm


class DraftAngleSample(BaseModel):
    face_id: int
    angle: float | None  # degrees


class StepGlobalProperties(BaseModel):
    volume: float = 0.0
    surface_area: float = 0.0
    bounding_box: dict = {}
    face_count: int = 0
    edge_count: int = 0


class StepDerivedMetrics(BaseModel):
    min_wall_thickness: float | None = None
    min_fillet_radius: float | None = None
    min_hole_diameter: float | None = None
    min_draft_angle: float | None = None


class StepAnalysisResult(BaseModel):
    faces: list[FaceInfo] = []
    edges: list[EdgeInfo] = []
    features: list[FeatureInfo] = []
    wall_thicknesses: list[WallThicknessSample] = []
    draft_angles: list[DraftAngleSample] = []
    global_properties: StepGlobalProperties = StepGlobalProperties()
    derived_metrics: StepDerivedMetrics = StepDerivedMetrics()
    error: str | None = None
