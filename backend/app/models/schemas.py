from pydantic import BaseModel, Field, field_validator


class ParamConfig(BaseModel):
    value: float
    comment: str


class StepUpdate(BaseModel):
    step: str  # "planning"|"retrieving_examples"|"generating_code"|"executing"|"fixing_error"|"assembly_part"|"complete"|"failed"
    message: str
    part_name: str | None = None
    part_index: int | None = None
    total_parts: int | None = None


class BoundingBox(BaseModel):
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float


class ValidationResult(BaseModel):
    is_watertight: bool = False
    bounding_box: BoundingBox | None = None
    volume: float = 0.0
    # 3D-printing feasibility (None = not evaluated)
    printable: bool | None = None
    fits_build_volume: bool | None = None
    min_wall_thickness: float | None = None
    print_warnings: list[str] = []


class AssemblyPartInfo(BaseModel):
    name: str
    description: str = ""
    code: str  # individual part code (make_xxx function)
    status: str = "success"  # "success" | "failed"
    position: list[float] = [0, 0, 0]
    color: str = "lightgray"


class InspectCheck(BaseModel):
    """One deterministic geometry check in the inspect manifest (forgecad-style)."""
    name: str  # "watertight"|"build_volume"|"min_wall"|"dimension_range"|"volume"|"expected_dimensions"|"vision"|...
    status: str  # "pass" | "warn" | "fail"
    message: str
    source: str = "geometry"  # "geometry" | "dfm" | "step" | "vision"


class InspectReport(BaseModel):
    """Aggregated, deterministic evidence report for a produced model.

    Pure aggregation of already-computed facts (GeometryValidation + optional DFM) —
    no geometry is recomputed. `verdict` is the worst status across `checks`.
    success = "model produced"; this report = honest per-check evidence.
    """
    verdict: str = "pass"  # "pass" | "warn" | "fail" = worst check status
    printable: bool | None = None  # honest 3D-print verdict, copied from geometry
    is_watertight: bool = False
    bounding_box: BoundingBox | None = None
    volume: float = 0.0
    min_wall_thickness: float | None = None
    checks: list[InspectCheck] = []
    print_warnings: list[str] = []
    # optional DFM enrichment (only populated when DFM ran)
    design_score: int | None = None
    dfm_violations: list["RuleViolationModel"] = []


class GenerationResult(BaseModel):
    success: bool
    request_id: str | None = None
    files: dict[str, str] = {}
    code: str | None = None
    params: dict[str, ParamConfig] | None = None
    execution_time_ms: int = 0
    attempts: int = 0
    error: dict | None = None
    validation: ValidationResult | None = None
    assembly_parts: list[AssemblyPartInfo] | None = None
    inspect_report: InspectReport | None = None
    plan: "CADPlan | None" = None  # the understood requirement brief (A2)


class CADPlan(BaseModel):
    description: str
    part_type: str  # "box"|"bracket"|"cylinder"|"plate"|"flange"|"enclosure"|"custom"|"profile_2d"|"revolution"|"swept"|"organic"|"assembly"
    dimensions: dict[str, float]
    features: list[str]
    constraints: list[str] = []
    ambiguities: list[str] = []
    modeling_hint: str = ""  # "revolve"|"sweep"|"loft"|"extrude_cut"|"boolean_combine"|""


class ModificationPlan(BaseModel):
    description: str
    modification_type: str  # "dimension_change"|"add_feature"|"remove_feature"|"redesign"
    target_params: dict[str, float] = {}
    new_features: list[str] = []


# === REST API Request/Response Models (Scheme C) ===


_VALID_FORMATS = {"step", "stl", "dxf", "svg"}


def _validate_output_formats(v: list[str]) -> list[str]:
    """Reject unknown output formats (was declared but never enforced)."""
    bad = [f for f in v if f not in _VALID_FORMATS]
    if bad:
        raise ValueError(
            f"Unsupported output_formats: {bad}. Allowed: {sorted(_VALID_FORMATS)}"
        )
    return v


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=10000)
    output_formats: list[str] = ["step", "stl"]

    _check_formats = field_validator("output_formats")(_validate_output_formats)


class ModifyRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=50000)
    prompt: str = Field(..., min_length=1, max_length=10000)
    output_formats: list[str] = ["step", "stl"]

    _check_formats = field_validator("output_formats")(_validate_output_formats)


class ExecuteRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=50000)
    output_formats: list[str] = ["step", "stl"]

    _check_formats = field_validator("output_formats")(_validate_output_formats)


class FeedbackRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=128)
    rating: str | None = Field(None, pattern="^(up|down)$")
    printed: str | None = Field(None, pattern="^(yes|no|not_yet)$")
    note: str | None = Field(None, max_length=2000)


class GenerateResponse(BaseModel):
    request_id: str
    success: bool
    files: dict[str, str] = {}
    code: str | None = None
    params: dict[str, ParamConfig] | None = None
    execution_time_ms: int = 0
    attempts: int = 0
    error: dict | None = None
    validation: ValidationResult | None = None
    assembly_parts: list[AssemblyPartInfo] | None = None
    inspect_report: InspectReport | None = None
    plan: "CADPlan | None" = None  # the understood requirement brief (A2)
    dfm_analysis: dict | None = None  # deprecated alias; kept for the analyze-endpoint contract


# === Design Analysis / DFM ===


class AnalyzeRequest(BaseModel):
    code: str = Field("", max_length=50000)
    description: str = Field("", max_length=5000)
    process: str | None = None  # filter rules by process; None = evaluate all
    material: str | None = None  # material hint for knowledge graph constraint lookup


class DFMIssueModel(BaseModel):
    category: str
    severity: str  # "critical" | "warning" | "info"
    description: str
    suggestion: str
    location: str = ""


class RuleViolationModel(BaseModel):
    rule_id: str
    process: str
    category: str
    severity: str
    source: str  # "geometric" | "heuristic"
    actual_value: float | None = None
    message: str = ""
    suggestion: str = ""


class Annotation3D(BaseModel):
    """A 3D annotation marking a DFM issue on the model."""
    id: str
    type: str  # "point_marker" | "face_highlight" | "dimension"
    severity: str  # "critical" | "warning" | "info"
    position: list[float]  # [x, y, z] world coordinates
    normal: list[float] | None = None
    label: str  # short label e.g. "壁厚 0.6mm"
    detail: str = ""  # full description
    category: str = ""  # DFM issue category
    face_ids: list[int] | None = None  # STEP face IDs for highlighting


class StepAnalysisSummary(BaseModel):
    """Summary of STEP-based precise analysis (for frontend display)."""
    available: bool = False
    face_count: int = 0
    edge_count: int = 0
    feature_count: int = 0
    min_wall_thickness: float | None = None
    min_fillet_radius: float | None = None
    min_hole_diameter: float | None = None
    min_draft_angle: float | None = None
    features: list[dict] = []  # simplified feature list for display


class DesignAnalysisResponse(BaseModel):
    design_score: int = 0
    design_summary: str = ""
    structural_issues: list[str] = []
    functional_notes: list[str] = []
    recommended_process: str = ""
    process_compatibility: dict[str, str] = {}
    dfm_issues: list[DFMIssueModel] = []
    estimated_difficulty: str = "medium"
    geometry: dict = {}
    rule_violations: list[RuleViolationModel] = []
    step_analysis: StepAnalysisSummary = StepAnalysisSummary()
    annotations: list[Annotation3D] = []


# Resolve forward references: InspectReport→RuleViolationModel, and the two response
# models→CADPlan (defined between them). All target types now exist.
InspectReport.model_rebuild()
GenerationResult.model_rebuild()
GenerateResponse.model_rebuild()
