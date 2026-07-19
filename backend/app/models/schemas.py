from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.parameters import CADParameter, extract_parameters


class ManufacturingProfile(BaseModel):
    process: Literal["fdm", "sla", "cnc", "laser_cut", "generic"] = "fdm"
    material: str = "PLA"
    nozzle_diameter_mm: float | None = 0.4
    layer_height_mm: float | None = 0.2
    build_volume_mm: list[float] = Field(default_factory=lambda: [220, 220, 250])

    @classmethod
    def fdm_pla_default(cls) -> "ManufacturingProfile":
        return cls(
            process="fdm",
            material="PLA",
            nozzle_diameter_mm=0.4,
            layer_height_mm=0.2,
            build_volume_mm=[220, 220, 250],
        )

    def label(self) -> str:
        if self.process == "fdm":
            return f"FDM {self.material}".strip()
        if self.process == "sla":
            return f"SLA {self.material}".strip()
        if self.process == "cnc":
            return f"CNC {self.material}".strip()
        if self.process == "laser_cut":
            return f"Laser cut {self.material}".strip()
        return self.material or "Generic"

    def prompt_context(self) -> str:
        fields = [f"process={self.process}", f"material={self.material}"]
        if self.nozzle_diameter_mm is not None:
            fields.append(f"nozzle={self.nozzle_diameter_mm}mm")
        if self.layer_height_mm is not None:
            fields.append(f"layer_height={self.layer_height_mm}mm")
        if self.build_volume_mm:
            fields.append("build_volume=" + "x".join(f"{value:g}" for value in self.build_volume_mm) + "mm")
        return "Manufacturing profile: " + ", ".join(fields)


class ParamConfig(BaseModel):
    value: float
    comment: str


class StepUpdate(BaseModel):
    step: str
    message: str
    status: str = "running"
    stage_id: str | None = None
    attempt: int | None = None
    started_at: str | None = None
    duration_ms: int | None = None
    detail: dict | None = None
    part_name: str | None = None
    part_index: int | None = None
    total_parts: int | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        allowed = {"queued", "running", "success", "warn", "failed", "skipped"}
        if value not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}")
        return value

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


class RecoveryAction(BaseModel):
    label: str
    prompt: str
    reason: str = ""
    action_type: Literal["retry_simpler", "fix_printability", "clarify", "explain", "inspect"]


class RepairStep(BaseModel):
    attempt: int
    stage: str  # "validation" | "static_analysis" | "execution" | "geometry" | "vision"
    error_type: str
    message: str
    action: str = "fix_error"
    status: str = "repaired"  # "repaired" | "failed" | "skipped"


class InspectCheck(BaseModel):
    """One deterministic geometry check in the inspect manifest (forgecad-style)."""
    name: str  # "watertight"|"build_volume"|"min_wall"|"dimension_range"|"volume"|"expected_dimensions"|"vision"|...
    status: str  # "pass" | "warn" | "fail"
    message: str
    source: str = "geometry"  # "geometry" | "dfm" | "step" | "vision"


class InspectReport(BaseModel):
    """Aggregated, deterministic evidence report for a produced model.

    Pure aggregation of already-computed facts (GeometryValidation + optional DFM) 鈥?
    no geometry is recomputed. `verdict` is the worst status across `checks`.
    success = "model produced"; this report = honest per-check evidence.
    """
    verdict: str = "pass"  # "pass" | "warn" | "fail" = worst check status
    printable: bool | None = None  # honest 3D-print verdict, copied from geometry
    is_watertight: bool = False
    bounding_box: BoundingBox | None = None
    volume: float = 0.0
    min_wall_thickness: float | None = None
    checks: list[InspectCheck] = Field(default_factory=list)
    print_warnings: list[str] = Field(default_factory=list)
    # optional DFM enrichment (only populated when DFM ran)
    design_score: int | None = None
    dfm_violations: list["RuleViolationModel"] = Field(default_factory=list)
    available_exports: list[str] = Field(default_factory=list)
    repair_attempts: int = 0
    source: str = "geometry_validator"


class CriticalDimension(BaseModel):
    name: str
    value: float | None = None
    unit: str = "mm"
    reason: str = ""


class DesignBrief(BaseModel):
    intent_summary: str = ""
    artifact_type: str = "custom"
    manufacturing_posture: str = "printable"
    assumptions: list[str] = []
    critical_dimensions: list[CriticalDimension] = []
    functional_requirements: list[str] = []
    printability_targets: list[str] = []
    acceptance_criteria: list[str] = []
    open_questions: list[str] = []


class GenerationResult(BaseModel):
    success: bool
    needs_confirmation: bool = False
    manufacturing_profile: ManufacturingProfile | None = None
    request_id: str | None = None
    snapshot_id: str | None = None
    version: int | None = None
    files: dict[str, str] = {}
    code: str | None = None
    params: dict[str, ParamConfig] | None = None
    parameters: list[CADParameter] | None = None
    execution_time_ms: int = 0
    attempts: int = 0
    repair_history: list[RepairStep] = Field(default_factory=list)
    recovery_actions: list[RecoveryAction] = Field(default_factory=list)
    error: dict | None = None
    validation: ValidationResult | None = None
    assembly_parts: list[AssemblyPartInfo] | None = None
    inspect_report: InspectReport | None = None
    plan: "CADPlan | None" = None  # the understood requirement brief (A2)
    design_brief: DesignBrief | None = None

    @model_validator(mode="after")
    def populate_parameters(self):
        if self.parameters is None and self.code:
            parsed = extract_parameters(self.code)
            self.parameters = parsed or None
        return self


class CADPlan(BaseModel):
    description: str
    part_type: str  # "box"|"bracket"|"cylinder"|"plate"|"flange"|"enclosure"|"custom"|"profile_2d"|"revolution"|"swept"|"organic"|"assembly"
    dimensions: dict[str, float]
    features: list[str]
    constraints: list[str] = []
    ambiguities: list[str] = []
    modeling_hint: str = ""  # "revolve"|"sweep"|"loft"|"extrude_cut"|"boolean_combine"|""
    design_brief: DesignBrief | None = None
    manufacturing_profile: ManufacturingProfile | None = None


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
    manufacturing_profile: ManufacturingProfile | None = None
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
    needs_confirmation: bool = False
    manufacturing_profile: ManufacturingProfile | None = None
    snapshot_id: str | None = None
    version: int | None = None
    files: dict[str, str] = {}
    code: str | None = None
    params: dict[str, ParamConfig] | None = None
    parameters: list[CADParameter] | None = None
    execution_time_ms: int = 0
    attempts: int = 0
    repair_history: list[RepairStep] = Field(default_factory=list)
    recovery_actions: list[RecoveryAction] = Field(default_factory=list)
    error: dict | None = None
    validation: ValidationResult | None = None
    assembly_parts: list[AssemblyPartInfo] | None = None
    inspect_report: InspectReport | None = None
    plan: "CADPlan | None" = None  # the understood requirement brief (A2)
    design_brief: DesignBrief | None = None
    dfm_analysis: dict | None = None  # deprecated alias; kept for the analyze-endpoint contract

    @model_validator(mode="after")
    def populate_parameters(self):
        if self.parameters is None and self.code:
            parsed = extract_parameters(self.code)
            self.parameters = parsed or None
        return self


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
    label: str  # short label e.g. "澹佸帤 0.6mm"
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


# Resolve forward references: InspectReport鈫扲uleViolationModel, and the two response
# models鈫扖ADPlan (defined between them). All target types now exist.
InspectReport.model_rebuild()
GenerationResult.model_rebuild()
GenerateResponse.model_rebuild()

