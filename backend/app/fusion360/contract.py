"""Pydantic source of truth for the Fusion connector public and IPC contracts."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Generic, Literal, Protocol, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)
from typing_extensions import TypeAliasType

CONTRACT_VERSION = "1.0.0"
PROTOCOL_VERSION = 1
MAX_TIMEOUT_MS = 300_000
MAX_SKETCH_PRIMITIVES = 500
MAX_EDGE_IDS = 500
MAX_VERIFY_CHECKS = 100

JsonValue = TypeAliasType(
    "JsonValue",
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"],
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CommonRequest(StrictModel):
    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    timeout_ms: int = Field(default=30_000, ge=1_000, le=MAX_TIMEOUT_MS)
    execution_mode: Literal["preview", "execute"] = "execute"
    approval_id: uuid.UUID | None = None
    connector_instance_id: uuid.UUID | None = None


class DocumentTarget(StrictModel):
    document_id: str = Field(min_length=1, max_length=1024)


class ComponentTarget(DocumentTarget):
    component_id: str = Field(min_length=1, max_length=4096)


class ParameterTarget(ComponentTarget):
    parameter_id: str = Field(min_length=1, max_length=4096)


class FeatureParameterTarget(ParameterTarget):
    feature_id: str = Field(min_length=1, max_length=4096)


class EntityTarget(DocumentTarget):
    component_id: str | None = Field(default=None, max_length=4096)
    entity_id: str = Field(min_length=1, max_length=4096)
    entity_kind: Literal["component", "occurrence", "body"]


_UNIT_RE = re.compile(r"^[A-Za-z0-9°µμ%/^*(). _-]{1,32}$")
_SAFE_FILENAME_RE = re.compile(r"^[^/\\\x00-\x1f]{1,255}$")


class DimensionValue(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["expression"],
                    "not": {
                        "anyOf": [
                            {"required": ["amount"]},
                            {"required": ["unit"]},
                        ]
                    },
                },
                {
                    "required": ["amount", "unit"],
                    "not": {"required": ["expression"]},
                },
            ]
        },
    )

    amount: float | None = None
    unit: str | None = None
    expression: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def exactly_one_form(self) -> "DimensionValue":
        dimensional = self.amount is not None or self.unit is not None
        expression = self.expression is not None
        if dimensional == expression:
            raise ValueError("provide exactly one of amount/unit or expression")
        if dimensional and (self.amount is None or self.unit is None):
            raise ValueError("amount and unit must be provided together")
        if self.amount is not None and not (-1e300 < self.amount < 1e300):
            raise ValueError("amount must be finite")
        if self.unit is not None and not _UNIT_RE.fullmatch(self.unit):
            raise ValueError("unit contains unsupported characters")
        return self


class Point2(StrictModel):
    x: float
    y: float


class OriginPlane(StrictModel):
    kind: Literal["origin"] = "origin"
    plane: Literal["xy", "xz", "yz"]


class EntityPlane(StrictModel):
    kind: Literal["entity"] = "entity"
    entity_id: str = Field(min_length=1, max_length=4096)


SketchPlane = Annotated[OriginPlane | EntityPlane, Field(discriminator="kind")]


class LinePrimitive(StrictModel):
    kind: Literal["line"] = "line"
    start: Point2
    end: Point2


class CirclePrimitive(StrictModel):
    kind: Literal["circle"] = "circle"
    center: Point2
    radius: float = Field(gt=0)


class RectanglePrimitive(StrictModel):
    kind: Literal["rectangle"] = "rectangle"
    corner1: Point2
    corner2: Point2


SketchPrimitive = Annotated[
    LinePrimitive | CirclePrimitive | RectanglePrimitive,
    Field(discriminator="kind"),
]


class UpdateParameterAction(CommonRequest):
    action: Literal["cad.update_parameter"] = "cad.update_parameter"
    target: ParameterTarget
    value: DimensionValue


class UpdateFeatureParameterAction(CommonRequest):
    action: Literal["cad.update_feature_parameter"] = "cad.update_feature_parameter"
    target: FeatureParameterTarget
    value: DimensionValue


class CreateSketchAction(CommonRequest):
    action: Literal["cad.create_sketch"] = "cad.create_sketch"
    target: ComponentTarget
    plane: SketchPlane
    unit: Literal["mm", "cm", "m", "in", "ft"] = "mm"
    name: str | None = Field(default=None, min_length=1, max_length=255)
    primitives: list[SketchPrimitive] = Field(min_length=1, max_length=MAX_SKETCH_PRIMITIVES)


class CreateExtrudeAction(CommonRequest):
    action: Literal["cad.create_extrude"] = "cad.create_extrude"
    target: ComponentTarget
    profile_id: str = Field(min_length=1, max_length=4096)
    operation: Literal["new_body", "new_component", "join", "cut", "intersect"] = "new_body"
    distance: DimensionValue
    direction: Literal["positive", "negative", "symmetric"] = "positive"
    participant_body_ids: list[str] = Field(default_factory=list, max_length=MAX_EDGE_IDS)

    @model_validator(mode="after")
    def participants_apply_to_boolean(self) -> "CreateExtrudeAction":
        if self.participant_body_ids and self.operation not in {"join", "cut", "intersect"}:
            raise ValueError("participant_body_ids apply only to boolean operations")
        return self


class DistanceExtent(StrictModel):
    kind: Literal["distance"] = "distance"
    distance: DimensionValue


class ThroughAllExtent(StrictModel):
    kind: Literal["through_all"] = "through_all"
    direction: Literal["positive", "negative"] = "positive"


HoleExtent = Annotated[DistanceExtent | ThroughAllExtent, Field(discriminator="kind")]


class CreateHoleAction(CommonRequest):
    action: Literal["cad.create_hole"] = "cad.create_hole"
    target: ComponentTarget
    sketch_point_ids: list[str] = Field(min_length=1, max_length=100)
    diameter: DimensionValue
    extent: HoleExtent


class CreateFilletAction(CommonRequest):
    action: Literal["cad.create_fillet"] = "cad.create_fillet"
    target: ComponentTarget
    edge_ids: list[str] = Field(min_length=1, max_length=MAX_EDGE_IDS)
    radius: DimensionValue
    tangent_chain: bool = True


class CreateChamferAction(CommonRequest):
    action: Literal["cad.create_chamfer"] = "cad.create_chamfer"
    target: ComponentTarget
    edge_ids: list[str] = Field(min_length=1, max_length=MAX_EDGE_IDS)
    distance: DimensionValue
    tangent_chain: bool = True


class MaterialRef(StrictModel):
    library_id: str = Field(min_length=1, max_length=1024)
    material_id: str = Field(min_length=1, max_length=1024)


class EntityProperties(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    part_number: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    material: MaterialRef | None = None

    @model_validator(mode="after")
    def at_least_one(self) -> "EntityProperties":
        if not any(v is not None for v in (self.name, self.part_number, self.description, self.material)):
            raise ValueError("at least one property is required")
        return self


class UpdateEntityPropertiesAction(CommonRequest):
    action: Literal["cad.update_entity_properties"] = "cad.update_entity_properties"
    target: EntityTarget
    properties: EntityProperties


class SaveDocumentAction(CommonRequest):
    action: Literal["cad.save_document"] = "cad.save_document"
    target: DocumentTarget
    version_description: str = Field(default="", max_length=1024)


class SaveAsAction(CommonRequest):
    action: Literal["cad.save_as"] = "cad.save_as"
    target: DocumentTarget
    data_folder_id: str = Field(min_length=1, max_length=2048)
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=1024)
    tag: str = Field(default="", max_length=255)


class ExportOptions(StrictModel):
    component_id: str | None = Field(default=None, max_length=4096)
    body_id: str | None = Field(default=None, max_length=4096)
    sketch_id: str | None = Field(default=None, max_length=4096)
    mesh_refinement: Literal["low", "medium", "high"] | None = None
    binary: bool | None = None
    width: int | None = Field(default=None, ge=64, le=4096)
    height: int | None = Field(default=None, ge=64, le=4096)


class ExportAction(CommonRequest):
    action: Literal["cad.export"] = "cad.export"
    target: DocumentTarget
    format: Literal["step", "stl", "dxf", "f3d", "png"]
    filename: str = Field(min_length=1, max_length=255)
    options: ExportOptions = Field(default_factory=ExportOptions)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if value in {".", ".."} or not _SAFE_FILENAME_RE.fullmatch(value):
            raise ValueError("filename must be a safe basename")
        return value

    @model_validator(mode="after")
    def format_specific_options(self) -> "ExportAction":
        o = self.options
        suffix = self.filename.rsplit(".", 1)[-1].lower() if "." in self.filename else ""
        allowed_suffixes = {
            "step": {"step", "stp"}, "stl": {"stl"}, "dxf": {"dxf"},
            "f3d": {"f3d"}, "png": {"png"},
        }
        if suffix not in allowed_suffixes[self.format]:
            raise ValueError(f"filename extension must match {self.format} export format")
        if self.format == "dxf" and not o.sketch_id:
            raise ValueError("DXF export requires sketch_id")
        if self.format != "dxf" and o.sketch_id:
            raise ValueError("sketch_id applies only to DXF")
        if self.format != "stl" and any(v is not None for v in (o.body_id, o.mesh_refinement, o.binary)):
            raise ValueError("STL options apply only to STL")
        if self.format != "png" and any(v is not None for v in (o.width, o.height)):
            raise ValueError("PNG dimensions apply only to PNG")
        return self


CadAction = Annotated[
    UpdateParameterAction
    | UpdateFeatureParameterAction
    | CreateSketchAction
    | CreateExtrudeAction
    | CreateHoleAction
    | CreateFilletAction
    | CreateChamferAction
    | UpdateEntityPropertiesAction
    | SaveDocumentAction
    | SaveAsAction
    | ExportAction,
    Field(discriminator="action"),
]
CAD_ACTION_ADAPTER = TypeAdapter(CadAction)

ContextSection = Literal[
    "application", "document", "design", "components", "occurrences",
    "selection", "parameters", "materials", "mass_properties", "sketches",
    "features", "timeline", "bodies", "assembly", "cloud",
]


class ContextQuery(StrictModel):
    sections: list[ContextSection] = Field(default_factory=lambda: ["application", "document", "design"])
    max_depth: int = Field(default=8, ge=0, le=32)
    include_suppressed: bool = False
    connector_instance_id: uuid.UUID | None = None


class ContextRequest(StrictModel):
    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    timeout_ms: int = Field(default=30_000, ge=1_000, le=MAX_TIMEOUT_MS)
    query: ContextQuery = Field(default_factory=ContextQuery)


class ApplicationContext(StrictModel):
    name: str
    version: str
    language: str | None = None
    user_name: str | None = None


class DocumentContext(StrictModel):
    document_id: str
    name: str
    document_type: str
    is_saved: bool
    is_modified: bool
    is_read_only: bool


class DesignContext(StrictModel):
    design_type: Literal["parametric", "direct", "unknown"]
    root_component_id: str | None = None
    units: str | None = None


class EntitySummary(StrictModel):
    id: str
    name: str
    kind: str
    component_id: str | None = None
    parent_id: str | None = None
    suppressed: bool | None = None
    health_state: str | None = None
    vendor_extensions: dict[str, JsonValue] = Field(default_factory=dict)


class ParameterSummary(StrictModel):
    id: str
    name: str
    expression: str
    value: float | None = None
    unit: str | None = None
    is_user_parameter: bool
    component_id: str | None = None
    created_by_id: str | None = None


class MaterialSummary(StrictModel):
    id: str
    name: str
    library_id: str | None = None
    component_id: str | None = None


class MassPropertiesSummary(StrictModel):
    target_id: str
    mass: float | None = None
    volume: float | None = None
    density: float | None = None
    center_of_mass: list[float] | None = None


class CloudContext(StrictModel):
    data_file_id: str | None = None
    version_id: str | None = None
    version_number: int | None = None
    project_id: str | None = None
    folder_id: str | None = None
    is_complete: bool | None = None
    is_read_only: bool | None = None


class TimelineParentSummary(StrictModel):
    index: int
    name: str


class TimelineSummary(StrictModel):
    index: int
    name: str
    kind: str
    entity_id: str | None = None
    health_state: str | None = None
    is_group: bool = False
    is_rolled_back: bool = False
    is_suppressed: bool | None = None
    parent: TimelineParentSummary | None = None
    error_or_warning: str | None = None


class ContextData(StrictModel):
    application: ApplicationContext | None = None
    document: DocumentContext | None = None
    design: DesignContext | None = None
    components: list[EntitySummary] = Field(default_factory=list)
    occurrences: list[EntitySummary] = Field(default_factory=list)
    selection: list[EntitySummary] = Field(default_factory=list)
    parameters: list[ParameterSummary] = Field(default_factory=list)
    materials: list[MaterialSummary] = Field(default_factory=list)
    mass_properties: list[MassPropertiesSummary] = Field(default_factory=list)
    sketches: list[EntitySummary] = Field(default_factory=list)
    features: list[EntitySummary] = Field(default_factory=list)
    timeline: list[TimelineSummary] = Field(default_factory=list)
    bodies: list[EntitySummary] = Field(default_factory=list)
    assembly: list[EntitySummary] = Field(default_factory=list)
    cloud: CloudContext | None = None
    truncated: bool = False
    vendor_extensions: dict[str, JsonValue] = Field(default_factory=dict)


class ParameterEqualsCheck(StrictModel):
    check: Literal["parameter_equals"] = "parameter_equals"
    target_id: str
    expected: DimensionValue
    tolerance: float = Field(default=1e-6, ge=0)


class EntityResolvesCheck(StrictModel):
    check: Literal["entity_resolves"] = "entity_resolves"
    target_id: str


class NoFeatureErrorsCheck(StrictModel):
    check: Literal["no_feature_errors"] = "no_feature_errors"


class NoNewFeatureErrorsCheck(StrictModel):
    check: Literal["no_new_feature_errors"] = "no_new_feature_errors"
    snapshot_id: str | None = None


class LocalSaveAcceptedCheck(StrictModel):
    check: Literal["local_save_accepted"] = "local_save_accepted"


class CloudVersionCompleteCheck(StrictModel):
    check: Literal["cloud_version_complete"] = "cloud_version_complete"


class ArtifactValidCheck(StrictModel):
    check: Literal["artifact_valid"] = "artifact_valid"
    artifact_id: str


VerifyCheck = Annotated[
    ParameterEqualsCheck | EntityResolvesCheck | NoFeatureErrorsCheck
    | NoNewFeatureErrorsCheck | LocalSaveAcceptedCheck | CloudVersionCompleteCheck
    | ArtifactValidCheck,
    Field(discriminator="check"),
]


class VerifySpecification(StrictModel):
    document_id: str
    baseline_request_id: uuid.UUID | None = None
    source_request_id: uuid.UUID | None = None
    checks: list[VerifyCheck] = Field(min_length=1, max_length=MAX_VERIFY_CHECKS)

    @model_validator(mode="after")
    def references_are_present(self) -> "VerifySpecification":
        for check in self.checks:
            if isinstance(check, NoNewFeatureErrorsCheck) and not (check.snapshot_id or self.baseline_request_id):
                raise ValueError("no_new_feature_errors requires a baseline or snapshot")
            if isinstance(check, (LocalSaveAcceptedCheck, CloudVersionCompleteCheck, ArtifactValidCheck)) and not self.source_request_id:
                raise ValueError(f"{check.check} requires source_request_id")
        return self


class VerifyRequest(StrictModel):
    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    timeout_ms: int = Field(default=30_000, ge=1_000, le=MAX_TIMEOUT_MS)
    connector_instance_id: uuid.UUID | None = None
    specification: VerifySpecification


class Change(StrictModel):
    target_id: str
    path: str
    kind: Literal["created", "updated", "deleted"]
    before: JsonValue = None
    after: JsonValue = None


class VerificationCheckResult(StrictModel):
    check: str
    passed: bool
    target_id: str | None = None
    expected: JsonValue = None
    actual: JsonValue = None
    message: str | None = None


class VerificationData(StrictModel):
    passed: bool
    checks: list[VerificationCheckResult] = Field(default_factory=list)
    compute_completed: bool = False
    new_feature_errors: list[str] = Field(default_factory=list)


class PreviewData(StrictModel):
    kind: Literal["preview"] = "preview"
    planned_changes: list[Change]
    verification_plan: list[str]


class MutationData(StrictModel):
    kind: Literal["mutation"] = "mutation"
    snapshot_id: str
    created_or_updated_entity_ids: list[str] = Field(default_factory=list)
    compensation: str | None = None


class SaveData(StrictModel):
    kind: Literal["save"] = "save"
    local_save_accepted: bool
    cloud_version_processing: Literal["not_applicable", "pending", "complete", "failed"]
    before_version: str | None = None
    after_version: str | None = None


class ExportData(StrictModel):
    kind: Literal["export"] = "export"
    format: Literal["step", "stl", "dxf", "f3d", "png"]
    artifact_ids: list[str]


ActionData = Annotated[PreviewData | MutationData | SaveData | ExportData, Field(discriminator="kind")]


class CadError(StrictModel):
    code: str
    message: str
    category: str
    retryable: bool
    details: dict[str, JsonValue] = Field(default_factory=dict)


class LocalArtifact(StrictModel):
    artifact_id: str
    kind: str
    filename: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: str


class CadArtifact(StrictModel):
    artifact_id: str
    kind: str
    filename: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    download_url: str


class Approval(StrictModel):
    approval_id: uuid.UUID
    intent_hash: str
    risk: Literal["medium", "high"]
    expires_at: datetime
    connector_instance_id: uuid.UUID
    document_id: str


T = TypeVar("T")


class CadResult(StrictModel, Generic[T]):
    request_id: uuid.UUID
    status: Literal[
        "queued", "running", "success", "failed", "cancelled", "timeout",
        "approval_required", "offline", "indeterminate",
    ]
    action: str
    data: T | None = None
    changes: list[Change] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    verification: VerificationData | None = None
    artifacts: list[CadArtifact] = Field(default_factory=list)
    approval: Approval | None = None
    error: CadError | None = None


class CadCapabilities(StrictModel):
    adapter: Literal["fusion360"] = "fusion360"
    contract_version: str = CONTRACT_VERSION
    protocol_versions: list[int] = Field(default_factory=lambda: [PROTOCOL_VERSION])
    available: bool = False
    runtime_online: bool = False
    connector_online: bool = False
    fusion_running: bool | None = None
    last_heartbeat_at: datetime | None = None
    fusion_version: str | None = None
    actions: list[str] = Field(default_factory=list)
    context_sections: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class RegisterRequest(StrictModel):
    connector_instance_id: uuid.UUID
    protocol_versions: list[int] = Field(min_length=1)
    fusion_version: str
    addin_version: str
    platform: Literal["windows", "macos"]
    user_name: str | None = None
    artifact_root_fingerprint: str
    capabilities: CadCapabilities


class RegisterResponse(StrictModel):
    protocol_version: int = PROTOCOL_VERSION
    heartbeat_interval_ms: int = 5_000
    heartbeat_ttl_ms: int = 15_000
    lease_duration_ms: int = 30_000


class ExecutionContext(StrictModel):
    artifact_dir: str
    artifact_root_fingerprint: str


class TaskLease(StrictModel):
    protocol_version: int = PROTOCOL_VERSION
    request_id: uuid.UUID
    operation: Literal["context", "execute", "verify"]
    intent_hash: str
    lease_id: uuid.UUID
    attempt: int = Field(ge=1)
    leased_until: float
    deadline: float
    payload: dict[str, JsonValue]
    execution_context: ExecutionContext


class LeaseIdentity(StrictModel):
    connector_instance_id: uuid.UUID
    request_id: uuid.UUID
    lease_id: uuid.UUID
    attempt: int = Field(ge=1)
    intent_hash: str


class TaskResultEnvelope(LeaseIdentity):
    result: dict[str, JsonValue]
    local_artifacts: list[LocalArtifact] = Field(default_factory=list)
    # Executor-only pre-mutation evidence. Runtime validates and persists it,
    # then removes it from every public CadResult.
    snapshot_evidence: dict[str, JsonValue] | None = None


class TaskControl(StrictModel):
    cancel_requested: bool
    deadline: float
    lease_valid: bool


class CadAdapter(Protocol):
    def get_capabilities(self) -> CadCapabilities: ...
    async def get_context(self, query: ContextRequest) -> CadResult[ContextData]: ...
    async def execute(self, action: CadAction) -> CadResult[ActionData]: ...
    async def verify(self, specification: VerifyRequest) -> CadResult[VerificationData]: ...


class JsonCadAdapter:
    """Strict dict-compatible facade for Web/Agent tool registries."""

    def __init__(self, adapter: CadAdapter):
        self._adapter = adapter

    def get_capabilities(self) -> dict[str, Any]:
        return self._adapter.get_capabilities().model_dump(mode="json")

    async def get_context(self, query: dict[str, Any]) -> dict[str, Any]:
        result = await self._adapter.get_context(ContextRequest.model_validate(query))
        return result.model_dump(mode="json")

    async def execute(self, action: dict[str, Any]) -> dict[str, Any]:
        parsed = CAD_ACTION_ADAPTER.validate_python(action)
        result = await self._adapter.execute(parsed)
        return result.model_dump(mode="json")

    async def verify(self, specification: dict[str, Any]) -> dict[str, Any]:
        result = await self._adapter.verify(VerifyRequest.model_validate(specification))
        return result.model_dump(mode="json")


ACTION_NAMES = [
    "cad.update_parameter", "cad.update_feature_parameter", "cad.create_sketch",
    "cad.create_extrude", "cad.create_hole", "cad.create_fillet",
    "cad.create_chamfer", "cad.update_entity_properties", "cad.save_document",
    "cad.save_as", "cad.export",
]
CONTEXT_SECTIONS = list(ContextSection.__args__)
