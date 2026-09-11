from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.execution.contracts import ExecutionError
from app.parameters import CADParameter, extract_parameters
from app.config import settings


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
    part_id: str | None = None
    name: str
    description: str = ""
    code: str  # individual part code (make_xxx function)
    code_hash: str | None = None
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
    project_id: UUID | None = None
    branch_id: UUID | None = None
    expected_base_revision_id: UUID | None = None
    revision_id: UUID | None = None
    workflow_run_id: UUID | None = None
    change_set_id: UUID | None = None
    task_status: str | None = None
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


class DurableRequestIdentity(BaseModel):
    """Optimistic-concurrency identity shared by every public write request.

    Every public MCAD write is durable. Accepting an incomplete identity would
    silently lose stale-base and idempotency guarantees, so validation fails
    before execution in every environment.
    """

    project_id: UUID
    branch_id: UUID
    expected_base_revision_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=500)

    @model_validator(mode="before")
    @classmethod
    def require_durable_identity_after_cutover(cls, value):
        if not isinstance(value, dict):
            return value
        missing = [
            name
            for name in (
                "project_id",
                "branch_id",
                "expected_base_revision_id",
                "idempotency_key",
            )
            if value.get(name) is None
        ]
        if missing:
            raise ValueError(
                "durable MCAD writes require " + ", ".join(missing)
            )
        return value


DurablePrompt = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]


class GenerateRequest(DurableRequestIdentity):
    prompt: DurablePrompt
    manufacturing_profile: ManufacturingProfile | None = None
    output_formats: list[str] = ["step", "stl"]

    _check_formats = field_validator("output_formats")(_validate_output_formats)


class ModifyRequest(DurableRequestIdentity):
    code: str | None = Field(default=None, min_length=1, max_length=50000)
    modeling_backend: Literal["auto", "freecad", "cadquery"] = "auto"
    prompt: DurablePrompt
    output_formats: list[str] = ["step", "stl"]

    _check_formats = field_validator("output_formats")(_validate_output_formats)


class ExecuteRequest(DurableRequestIdentity):
    code: str = Field(..., min_length=1, max_length=50000)
    output_formats: list[str] = ["step", "stl"]

    _check_formats = field_validator("output_formats")(_validate_output_formats)


class FeedbackRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=128)
    rating: str | None = Field(None, pattern="^(up|down)$")
    printed: str | None = Field(None, pattern="^(yes|no|not_yet)$")
    note: str | None = Field(None, max_length=2000)


class DurableAgentEventProjection(BaseModel):
    stage: str
    label: str
    status: str
    message: str
    step_key: str | None = None
    step_kind: str | None = None
    attempt_number: int | None = None
    gate: str | None = None
    mode: str | None = None
    outcome: str | None = None
    evidence_id: UUID | None = None
    evidence_hash: str | None = None
    risk_count: int | None = None


class DurableAgentValidationProjection(BaseModel):
    evidence_id: UUID
    evidence_hash: str
    gate: str
    mode: str
    outcome: str
    issues: list[str] = Field(default_factory=list)
    violations: list[dict[str, Any]] = Field(default_factory=list)


class DurableBOMProjection(BaseModel):
    status: Literal[
        "pending",
        "running",
        "succeeded",
        "not_applicable",
        "missing",
        "failed",
        "unsupported",
        "cancelled",
    ]
    revision_id: UUID | None = None
    evidence_id: UUID | None = None
    json_download_url: str | None = None
    csv_download_url: str | None = None
    error: ExecutionError | None = None


class DurableAgentSnapshotProjection(BaseModel):
    current_stage: str
    current_step_key: str | None = None
    current_step_kind: str | None = None
    current_status: str
    candidate_build_id: UUID | None = None
    candidate_status: str | None = None
    repair_count: int = 0
    plan: dict[str, Any] | None = None
    validations: list[DurableAgentValidationProjection] = Field(default_factory=list)
    risk_summary: dict[str, Any] | None = None
    bom: DurableBOMProjection | None = None


class DurableAffectedObject(BaseModel):
    object_id: str
    object_type: Literal["part", "assembly", "feature", "profile", "file"]
    label: str
    change: Literal["create", "modify", "remove", "inspect"]


class DurableConfirmationProjection(BaseModel):
    status: Literal["waiting"] = "waiting"
    workflow_run_id: UUID
    reason: str
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    affected_objects: list[DurableAffectedObject]


class DurableTaskEvent(BaseModel):
    id: UUID
    workflow_run_id: UUID
    sequence: int
    event_type: str
    payload: dict[str, Any]
    projection: DurableAgentEventProjection | None = None
    occurred_at: datetime


class DurableTaskEventPage(BaseModel):
    workflow_run_id: UUID
    events: list[DurableTaskEvent]
    after_sequence: int
    next_cursor: int
    earliest_sequence: int
    current_sequence: int
    has_more: bool


class DurableAttemptSnapshot(BaseModel):
    id: UUID
    step_run_id: UUID
    attempt_number: int
    status: str
    worker_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    error: ExecutionError | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class DurableStepSnapshot(BaseModel):
    id: UUID
    step_key: str
    step_index: int
    kind: str
    status: str
    attempt_count: int
    error_code: str | None = None
    error_message: str | None = None
    error: ExecutionError | None = None
    attempts: list[DurableAttemptSnapshot] = Field(default_factory=list)


class DurableArtifactSnapshot(BaseModel):
    id: UUID
    revision_id: UUID
    artifact_kind: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    download_url: str
    created_at: datetime


class DurableChangeSetSummary(BaseModel):
    id: UUID
    status: str
    base_revision_id: UUID
    candidate_revision_id: UUID
    objective: str
    updated_at: datetime


class DurableTaskSnapshot(BaseModel):
    id: UUID
    project_id: UUID
    requested_by_principal_id: UUID
    kind: str
    status: str
    request_payload: dict[str, Any]
    last_event_sequence: int
    cancellation_requested_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    error: ExecutionError | None = None
    created_at: datetime
    started_at: datetime | None = None
    updated_at: datetime
    completed_at: datetime | None = None
    steps: list[DurableStepSnapshot] = Field(default_factory=list)
    artifacts: list[DurableArtifactSnapshot] = Field(default_factory=list)
    change_set: DurableChangeSetSummary | None = None
    agent: DurableAgentSnapshotProjection | None = None
    confirmation: DurableConfirmationProjection | None = None
    parameters: list[CADParameter] = Field(default_factory=list)
    parameter_state_sha256: str | None = None


class TaskConfirmationRequest(BaseModel):
    accepted: bool
    note: str = Field(default="", max_length=4000)


class TaskCancellationRequest(BaseModel):
    reason: str = Field(default="用户取消", min_length=1, max_length=4000)


class ChangeSetReviewRequest(BaseModel):
    note: str = Field(default="", max_length=4000)


class ChangeSetRequiredNoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=4000)


class ChangeSetActionResponse(BaseModel):
    change_set_id: UUID
    status: str
    replayed: bool


class OnshapeCreateDocumentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    description: str | None = Field(None, max_length=2000)
    is_public: bool | None = None


class OnshapeDocumentResponse(BaseModel):
    id: str
    name: str = ""
    default_workspace_id: str | None = None
    web_url: str | None = None
    raw: dict = Field(default_factory=dict)


class OnshapeDocumentsResponse(BaseModel):
    documents: list[dict] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


class OnshapePublishRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    document_id: str | None = Field(None, min_length=1, max_length=128)
    workspace_id: str | None = Field(None, min_length=1, max_length=128)
    document_name: str | None = Field(None, min_length=1, max_length=256)
    step_filename: str | None = Field(None, min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9._-]+$")
    wait_for_completion: bool = False
    poll_interval_s: float = Field(2.0, ge=0.5, le=10.0)
    timeout_s: float = Field(60.0, ge=1.0, le=300.0)


class OnshapePublishResponse(BaseModel):
    request_id: str
    status: str
    onshape_url: str
    document_id: str
    workspace_id: str
    element_id: str | None = None
    translation_id: str | None = None
    document_name: str = ""
    step_filename: str = ""
    raw: dict = Field(default_factory=dict)


class OnshapeLink(BaseModel):
    request_id: str
    status: str
    onshape_url: str
    document_id: str
    workspace_id: str
    element_id: str | None = None
    translation_id: str | None = None
    document_name: str = ""
    step_filename: str = ""
    mode: str = "import_step"
    created_at: str
    updated_at: str


class GenerateResponse(BaseModel):
    request_id: str
    success: bool
    needs_confirmation: bool = False
    manufacturing_profile: ManufacturingProfile | None = None
    snapshot_id: str | None = None
    version: int | None = None
    project_id: UUID | None = None
    branch_id: UUID | None = None
    expected_base_revision_id: UUID | None = None
    revision_id: UUID | None = None
    workflow_run_id: UUID | None = None
    change_set_id: UUID | None = None
    task_status: str | None = None
    files: dict[str, str] = {}
    code: str | None = None
    params: dict[str, ParamConfig] | None = None
    parameters: list[CADParameter] | None = None
    parameter_state_sha256: str | None = None
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


# Resolve forward references: InspectReport -> RuleViolationModel, and the two response
# models -> CADPlan (defined between them). All target types now exist.
InspectReport.model_rebuild()
GenerationResult.model_rebuild()
GenerateResponse.model_rebuild()
