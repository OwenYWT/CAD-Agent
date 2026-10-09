"""Stable MCAD workflow wire models; no dispatch, storage or Temporal imports."""
from __future__ import annotations
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer
from app.models.native_modification import FreeCADStructuredModificationV1
from app.domain.requirement_basis import RequirementBasisV1
from app.domain.selection import SelectionContextV1

class NativeImportV1(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    format: Literal['fcstd', 'step']
    artifact_id: UUID
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int = Field(ge=1, le=64*1024*1024, strict=True)
    filename: str = Field(min_length=1, max_length=240)

    def object_key(self, tenant_id: UUID, document_id: UUID) -> str:
        return f'tenants/{tenant_id}/document-imports/{document_id}/{self.sha256}.{self.format}'


class OperationContextV1(BaseModel):
    """Frozen record of how a public request became a durable CAD operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["mcad-operation-context.v1"] = (
        "mcad-operation-context.v1"
    )
    rule: Literal[
        "explicit_rest_operation",
        "explicit_modify_part",
        "explicit_parameter_edit",
        "explicit_history_restore",
        "explicit_native_import",
        "explicit_branch_fork",
        "explicit_branch_merge",
        "explicit_ui_intent",
        "legacy_editable_base_present",
        "legacy_empty_panel",
    ]
    source_channel: Literal["rest", "session_websocket"]
    panel_id: str | None = Field(default=None, min_length=1, max_length=500)
    requested_operation: Literal["generate", "modify"] | None = None
    resolved_operation: Literal["generate", "modify"]
    requested_modeling_backend: Literal["auto", "freecad", "cadquery"] | None = (
        None
    )
    submission_modeling_backend: Literal["auto", "freecad", "cadquery"]
    base_revision_id: UUID
    base_source_kind: Literal[
        "fcstd_artifact",
        "agent_generated_source",
        "revision_manifest_source",
        "request_code",
        "none",
        "native_import_artifact",
    ]
    base_source_id: UUID | None = None
    base_source_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    feature_lease_token: UUID | None = None
    client_request_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    rebased_from_revision_id: UUID | None = None
    rebased_from_state_version: int | None = Field(default=None, ge=0)
    rebase_evidence_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    selection_context: SelectionContextV1 | None = None
    requirement_basis: RequirementBasisV1 | None = None
    source_candidate_revision_id: UUID | None = None
    native_import: NativeImportV1 | None = None

    @model_serializer(mode="wrap")
    def preserve_historical_wire_identity(self, handler):
        payload = handler(self)
        # New optional context must not change hashes of existing durable inputs.
        for key in ('feature_lease_token','client_request_hash','rebased_from_revision_id',
                    'rebased_from_state_version','rebase_evidence_hash','selection_context','requirement_basis','source_candidate_revision_id','native_import'):
            if payload.get(key) is None:
                payload.pop(key, None)
        return payload

    @model_validator(mode="after")
    def validate_source_and_channel(self) -> "OperationContextV1":
        if self.native_import is not None and self.base_source_kind != 'native_import_artifact':
            raise ValueError('导入来源必须明确绑定到原生导入输入')
        if self.source_candidate_revision_id and (self.resolved_operation != "modify"
                or self.submission_modeling_backend != "freecad"
                or self.base_source_kind != "fcstd_artifact" or self.selection_context):
            raise ValueError("候选续改必须使用明确的原生来源，不能混用已保存版本的选择")
        if self.selection_context and (self.resolved_operation != "modify"
                or self.submission_modeling_backend != "freecad"
                or self.selection_context.revision_id != self.base_revision_id):
            raise ValueError("选择上下文只适用于同一基线的原生修改")
        rebase_fields=(self.rebased_from_revision_id,self.rebased_from_state_version,self.rebase_evidence_hash)
        if any(v is not None for v in rebase_fields) and not all(v is not None for v in rebase_fields):
            raise ValueError("rebase context requires original revision, version and evidence hash")
        if self.source_channel == "session_websocket" and self.panel_id is None:
            raise ValueError("session WebSocket operation context requires panel_id")
        if self.source_channel == "rest" and self.panel_id is not None:
            raise ValueError("REST operation context cannot include panel_id")
        if self.resolved_operation == "modify" and (
            self.submission_modeling_backend == "auto"
        ):
            raise ValueError("modify operation context requires a concrete backend")
        if self.base_source_kind == "fcstd_artifact":
            if self.submission_modeling_backend != "freecad":
                raise ValueError("FCStd source requires FreeCAD")
            if self.base_source_id is None or self.base_source_sha256 is None:
                raise ValueError("FCStd source requires id and SHA-256")
        elif self.base_source_kind in {
            "agent_generated_source",
            "revision_manifest_source",
        }:
            if self.submission_modeling_backend != "cadquery":
                raise ValueError("persisted source requires CadQuery")
            if self.base_source_sha256 is None:
                raise ValueError("persisted source requires SHA-256")
            if (
                self.base_source_kind == "agent_generated_source"
                and self.base_source_id is None
            ):
                raise ValueError("agent source requires source id")
        elif self.base_source_kind == "request_code":
            if self.source_channel != "rest":
                raise ValueError("request code is valid only for REST")
            if self.submission_modeling_backend != "cadquery":
                raise ValueError("request code requires CadQuery")
            if self.base_source_id is not None or self.base_source_sha256 is None:
                raise ValueError("request code requires only SHA-256 identity")
        elif self.base_source_kind == 'native_import_artifact':
            if (self.native_import is None or self.source_channel != 'rest' or self.rule != 'explicit_native_import'
                    or self.resolved_operation != 'generate' or self.submission_modeling_backend != 'freecad'
                    or self.base_source_id != self.native_import.artifact_id or self.base_source_sha256 != self.native_import.sha256):
                raise ValueError('原生导入仅接受服务端冻结的文件身份')
        elif self.base_source_kind == "none":
            if self.resolved_operation != "generate":
                raise ValueError("missing source is valid only for generate")
            if self.submission_modeling_backend not in {"auto", "cadquery"}:
                raise ValueError("source-free generation requires auto or CadQuery")
            if self.base_source_id is not None or self.base_source_sha256 is not None:
                raise ValueError("source-free generation cannot have source identity")
        return self

class FreeCADRevisionRestoreV1(BaseModel):
    """Server-resolved immutable input, distinct from the target branch head."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_revision_id: UUID
    source_artifact_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

class McadOutputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    media_type: str = Field(
        min_length=3,
        max_length=200,
        pattern=(
            r"^[A-Za-z0-9][A-Za-z0-9.+-]*/"
            r"[A-Za-z0-9][A-Za-z0-9.+-]*$"
        ),
    )
    required: bool = True
    max_size_bytes: int | None = Field(default=None, ge=1)

class McadExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_key: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    kind: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    capability: str = Field(
        default="mcad.local",
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    operation: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    mode: Literal["2d", "3d", "analysis"] = "3d"
    source_language: Literal["python", "javascript", "json"] = "python"
    source_code: str = Field(min_length=1, max_length=2_000_000)
    outputs: tuple[McadOutputRequest, ...] = ()
    timeout_seconds: int = Field(default=60, ge=1, le=3600)

    def model_post_init(self, __context: object) -> None:
        if self.outputs:
            return
        if self.mode == "2d":
            defaults = (
                McadOutputRequest(name="dxf", media_type="image/vnd.dxf"),
            )
        elif self.mode == "analysis":
            defaults = (
                McadOutputRequest(name="json", media_type="application/json"),
            )
        else:
            defaults = (
                McadOutputRequest(name="step", media_type="model/step"),
                McadOutputRequest(name="stl", media_type="model/stl"),
            )
        object.__setattr__(self, "outputs", defaults)

class McadSourcePreparationRequest(BaseModel):
    """LLM-backed source preparation executed by a Temporal activity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["generate", "modify"]
    prompt: str = Field(min_length=1, max_length=10_000)
    existing_code: str | None = Field(default=None, max_length=50_000)
    manufacturing_profile: dict[str, Any] | None = None
    output_formats: tuple[Literal["fcstd", "step", "stl", "dxf", "svg"], ...] = (
        "step",
        "stl",
    )

    @model_validator(mode="after")
    def validate_operation_inputs(self) -> "McadSourcePreparationRequest":
        if self.operation == "modify" and not self.existing_code:
            raise ValueError("modify source preparation requires existing_code")
        if self.operation == "generate" and self.existing_code is not None:
            raise ValueError("generate source preparation cannot include existing_code")
        if not self.output_formats:
            raise ValueError("output_formats cannot be empty")
        return self

class McadWorkflowRequest(BaseModel):
    """Serializable input whose workflow ID is derived from WorkflowRun."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_run_id: UUID
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID
    branch_id: UUID
    expected_base_revision_id: UUID
    document_queue: bool = False
    expected_state_version: int | None = Field(default=None, ge=0)
    objective: str = Field(min_length=1, max_length=4000)
    primary: McadExecutionRequest | None = None
    preparation: McadSourcePreparationRequest | None = None
    followup: McadExecutionRequest | None = None
    require_confirmation: bool = True
    confirmation_timeout_seconds: int = Field(default=3600, ge=1, le=604800)
    commit_after_confirmation: bool = True

    @model_validator(mode="after")
    def require_review_before_commit(self) -> "McadWorkflowRequest":
        if (self.primary is None) == (self.preparation is None):
            raise ValueError(
                "exactly one of primary or preparation must be provided"
            )
        if self.commit_after_confirmation and not self.require_confirmation:
            raise ValueError(
                "commit_after_confirmation requires explicit confirmation"
            )
        return self

    def temporal_payload(self) -> dict:
        return self.model_dump(mode="json")

class McadAgentWorkflowV2Request(BaseModel):
    """Planning-first input for the version-isolated durable Agent flow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_run_id: UUID
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID
    branch_id: UUID
    expected_base_revision_id: UUID
    document_queue: bool = False
    expected_state_version: int | None = Field(default=None, ge=0)
    operation: Literal["generate", "modify"]
    # Missing on historical V2 payloads, which must keep replaying through the
    # original source-code branch. New durable submissions set this explicitly.
    modeling_backend: Literal["auto", "cadquery", "freecad"] = "cadquery"
    objective: str = Field(min_length=1, max_length=4000)
    existing_code: str | None = Field(default=None, max_length=50_000)
    operation_context: OperationContextV1 | None = None
    structured_modification: FreeCADStructuredModificationV1 | None = None
    revision_restore: FreeCADRevisionRestoreV1 | None = None
    manufacturing_profile: dict[str, Any] | None = None
    output_formats: tuple[Literal["step", "stl", "dxf", "svg"], ...] = (
        "step",
        "stl",
    )
    confirmation_timeout_seconds: int = Field(default=3600, ge=1, le=604800)

    @model_validator(mode="after")
    def validate_operation_inputs(self) -> "McadAgentWorkflowV2Request":
        if self.revision_restore is not None and (
            self.operation != "modify"
            or self.modeling_backend != "freecad"
            or self.existing_code is not None
            or self.structured_modification is not None
        ):
            raise ValueError("revision restore requires code-free FreeCAD modify only")
        if self.structured_modification is not None and (
            self.operation != "modify"
            or self.modeling_backend != "freecad"
            or self.existing_code is not None
        ):
            raise ValueError(
                "structured modification requires code-free FreeCAD modify"
            )
        if (
            self.modeling_backend == "cadquery"
            and self.operation == "modify"
            and not self.existing_code
        ):
            raise ValueError("modify planning requires existing_code")
        if self.operation == "generate" and self.existing_code is not None:
            raise ValueError("generate planning cannot include existing_code")
        if not self.output_formats:
            raise ValueError("output_formats cannot be empty")
        if self.modeling_backend == "freecad" and set(self.output_formats) - {
            "fcstd",
            "step",
            "stl",
            "dxf",
        }:
            raise ValueError("FreeCAD output formats must be fcstd/step/stl/dxf")
        if self.operation_context is not None:
            if self.operation_context.resolved_operation != self.operation:
                raise ValueError("operation context does not match request operation")
            if (
                self.operation_context.submission_modeling_backend
                != self.modeling_backend
            ):
                raise ValueError("operation context does not match request backend")
        return self

    def temporal_payload(self) -> dict:
        return self.model_dump(mode="json")

class McadCheckRequest(BaseModel):
    """Serializable request for one durable, read-only engineering check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_run_id: UUID
    source_workflow_run_id: UUID
    source_revision_id: UUID
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID
    code: str = Field(default="", max_length=50_000)
    description: str = Field(default="", max_length=5_000)
    process: str | None = Field(default=None, max_length=200)
    material: str | None = Field(default=None, max_length=200)
    rule_configuration: dict | None = None
    timeout_seconds: int = Field(default=120, ge=1, le=3600)

    def temporal_payload(self) -> dict:
        # Old queued dispatches are content addressed without this new field.
        payload = self.model_dump(mode="json")
        if self.rule_configuration is None:
            payload.pop("rule_configuration")
        return payload
