"""Public boundary for starting and controlling durable MCAD workflows."""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from temporalio.client import WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.db import tenant_transaction
from app.config import settings
from app.services.run_state import (
    IllegalTransition,
    create_workflow,
    request_workflow_cancellation,
)
from app.temporal_client import (
    get_temporal_client,
    temporal_agent_v2_worker_readiness,
)


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
    ]
    base_source_id: UUID | None = None
    base_source_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_source_and_channel(self) -> "OperationContextV1":
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
        elif self.base_source_kind == "none":
            if self.resolved_operation != "generate":
                raise ValueError("missing source is valid only for generate")
            if self.submission_modeling_backend not in {"auto", "cadquery"}:
                raise ValueError("source-free generation requires auto or CadQuery")
            if self.base_source_id is not None or self.base_source_sha256 is not None:
                raise ValueError("source-free generation cannot have source identity")
        return self


class FreeCADParameterUpdateV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parameter_id: str = Field(
        min_length=3,
        max_length=161,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,79}\.[A-Za-z_][A-Za-z0-9_]{0,79}$",
    )
    value: float = Field(allow_inf_nan=False)


class FreeCADStructuredModificationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["freecad-structured-modification.v1"] = (
        "freecad-structured-modification.v1"
    )
    expected_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameter_updates: tuple[FreeCADParameterUpdateV1, ...] = Field(
        min_length=1,
        max_length=100,
    )

    @model_validator(mode="after")
    def unique_updates(self) -> "FreeCADStructuredModificationV1":
        identifiers = [update.parameter_id for update in self.parameter_updates]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("parameter_duplicate_update")
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
    timeout_seconds: int = Field(default=120, ge=1, le=3600)

    def temporal_payload(self) -> dict:
        return self.model_dump(mode="json")


def temporal_workflow_id(workflow_run_id: UUID | str) -> str:
    return f"mcad-workflow-{workflow_run_id}"


def temporal_check_workflow_id(workflow_run_id: UUID | str) -> str:
    return f"mcad-check-workflow-{workflow_run_id}"


def temporal_agent_v2_workflow_id(workflow_run_id: UUID | str) -> str:
    return f"mcad-agent-v2-workflow-{workflow_run_id}"


def mcad_workflow_request_payload(
    *,
    branch_id: UUID,
    expected_base_revision_id: UUID,
    objective: str,
    primary: McadExecutionRequest | None,
    preparation: McadSourcePreparationRequest | None,
    followup: McadExecutionRequest | None,
    require_confirmation: bool,
    confirmation_timeout_seconds: int,
    commit_after_confirmation: bool,
) -> dict[str, Any]:
    """Canonical immutable payload used for idempotency comparisons."""
    return {
        "objective": objective,
        "branch_id": str(branch_id),
        "expected_base_revision_id": str(expected_base_revision_id),
        "primary": primary.model_dump(mode="json") if primary else None,
        "preparation": (
            preparation.model_dump(mode="json") if preparation else None
        ),
        "followup": followup.model_dump(mode="json") if followup else None,
        "require_confirmation": require_confirmation,
        "confirmation_timeout_seconds": confirmation_timeout_seconds,
        "commit_after_confirmation": commit_after_confirmation,
    }


def mcad_agent_v2_request_payload(
    *,
    branch_id: UUID,
    expected_base_revision_id: UUID,
    operation: Literal["generate", "modify"],
    objective: str,
    existing_code: str | None,
    manufacturing_profile: dict[str, Any] | None,
    output_formats: tuple[str, ...],
    confirmation_timeout_seconds: int,
    modeling_backend: Literal["auto", "cadquery", "freecad"] = "cadquery",
    operation_context: OperationContextV1 | None = None,
    structured_modification: FreeCADStructuredModificationV1 | None = None,
    revision_restore: FreeCADRevisionRestoreV1 | None = None,
) -> dict[str, Any]:
    """Canonical immutable V2 payload used by WorkflowRun idempotency."""
    payload = {
        "branch_id": str(branch_id),
        "expected_base_revision_id": str(expected_base_revision_id),
        "operation": operation,
        "objective": objective,
        "existing_code": existing_code,
        "manufacturing_profile": manufacturing_profile,
        "output_formats": list(output_formats),
        "confirmation_timeout_seconds": confirmation_timeout_seconds,
    }
    if modeling_backend != "cadquery":
        payload["modeling_backend"] = modeling_backend
    if operation_context is not None:
        payload["operation_context"] = operation_context.model_dump(mode="json")
    if structured_modification is not None:
        payload["structured_modification"] = structured_modification.model_dump(
            mode="json"
        )
    if revision_restore is not None:
        payload["revision_restore"] = revision_restore.model_dump(mode="json")
    return payload


async def start_mcad_agent_v2_workflow(
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID,
    branch_id: UUID,
    expected_base_revision_id: UUID,
    kind: str,
    idempotency_key: str,
    operation: Literal["generate", "modify"],
    objective: str,
    existing_code: str | None = None,
    manufacturing_profile: dict[str, Any] | None = None,
    output_formats: tuple[str, ...] = ("step", "stl"),
    confirmation_timeout_seconds: int = 3600,
    require_worker_ready: bool = True,
    modeling_backend: Literal["auto", "cadquery", "freecad"] = "cadquery",
    operation_context: OperationContextV1 | None = None,
    structured_modification: FreeCADStructuredModificationV1 | None = None,
    revision_restore: FreeCADRevisionRestoreV1 | None = None,
) -> tuple[UUID, WorkflowHandle]:
    """Fail closed on V2 readiness, persist once, then start V2 idempotently."""
    objective = objective.strip()
    kind = kind.strip()
    idempotency_key = idempotency_key.strip()
    request_payload = mcad_agent_v2_request_payload(
        branch_id=branch_id,
        expected_base_revision_id=expected_base_revision_id,
        operation=operation,
        objective=objective,
        existing_code=existing_code,
        manufacturing_profile=manufacturing_profile,
        output_formats=output_formats,
        confirmation_timeout_seconds=confirmation_timeout_seconds,
        modeling_backend=modeling_backend,
        operation_context=operation_context,
        structured_modification=structured_modification,
        revision_restore=revision_restore,
    )
    # New work is accepted only when V2 has pollers. An already-persisted
    # idempotent submission may re-enter this boundary without readiness so a
    # crash between the DB commit and Temporal start can still be repaired.
    if require_worker_ready:
        await temporal_agent_v2_worker_readiness()
    async with tenant_transaction(tenant_id, principal_id) as connection:
        created = await create_workflow(
            connection,
            tenant_id=tenant_id,
            project_id=project_id,
            requested_by_principal_id=principal_id,
            kind=kind,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
    durable_request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id=principal_id,
        branch_id=branch_id,
        expected_base_revision_id=expected_base_revision_id,
        operation=operation,
        modeling_backend=modeling_backend,
        operation_context=operation_context,
        structured_modification=structured_modification,
        revision_restore=revision_restore,
        objective=objective,
        existing_code=existing_code,
        manufacturing_profile=manufacturing_profile,
        output_formats=output_formats,
        confirmation_timeout_seconds=confirmation_timeout_seconds,
    )
    client = await get_temporal_client()
    workflow_id = temporal_agent_v2_workflow_id(created.workflow_id)
    try:
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            durable_request.temporal_payload(),
            id=workflow_id,
            task_queue=settings.temporal_agent_v2_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return created.workflow_id, handle


async def start_mcad_workflow(
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID,
    branch_id: UUID,
    expected_base_revision_id: UUID,
    kind: str,
    idempotency_key: str,
    objective: str,
    primary: McadExecutionRequest | None = None,
    preparation: McadSourcePreparationRequest | None = None,
    followup: McadExecutionRequest | None = None,
    require_confirmation: bool = True,
    confirmation_timeout_seconds: int = 3600,
    commit_after_confirmation: bool = True,
) -> tuple[UUID, WorkflowHandle]:
    """Persist WorkflowRun first, then idempotently start its Temporal history."""
    objective = objective.strip()
    kind = kind.strip()
    idempotency_key = idempotency_key.strip()
    if not objective or len(objective) > 4000:
        raise ValueError("objective must contain 1 to 4000 characters")
    if not kind or len(kind) > 200:
        raise ValueError("kind must contain 1 to 200 characters")
    if not idempotency_key or len(idempotency_key) > 500:
        raise ValueError(
            "idempotency_key must contain 1 to 500 characters"
        )
    if not 1 <= confirmation_timeout_seconds <= 604800:
        raise ValueError(
            "confirmation_timeout_seconds must be between 1 and 604800"
        )
    if commit_after_confirmation and not require_confirmation:
        raise ValueError(
            "commit_after_confirmation requires explicit confirmation"
        )
    if (primary is None) == (preparation is None):
        raise ValueError(
            "exactly one of primary or preparation must be provided"
        )
    request_payload = mcad_workflow_request_payload(
        branch_id=branch_id,
        expected_base_revision_id=expected_base_revision_id,
        objective=objective,
        primary=primary,
        preparation=preparation,
        followup=followup,
        require_confirmation=require_confirmation,
        confirmation_timeout_seconds=confirmation_timeout_seconds,
        commit_after_confirmation=commit_after_confirmation,
    )
    async with tenant_transaction(tenant_id, principal_id) as connection:
        created = await create_workflow(
            connection,
            tenant_id=tenant_id,
            project_id=project_id,
            requested_by_principal_id=principal_id,
            kind=kind,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )

    durable_request = McadWorkflowRequest(
        workflow_run_id=created.workflow_id,
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id=principal_id,
        branch_id=branch_id,
        expected_base_revision_id=expected_base_revision_id,
        objective=objective,
        primary=primary,
        preparation=preparation,
        followup=followup,
        require_confirmation=require_confirmation,
        confirmation_timeout_seconds=confirmation_timeout_seconds,
        commit_after_confirmation=commit_after_confirmation,
    )
    client = await get_temporal_client()
    workflow_id = temporal_workflow_id(created.workflow_id)
    try:
        handle = await client.start_workflow(
            "McadDurableWorkflow",
            durable_request.temporal_payload(),
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return created.workflow_id, handle


async def start_mcad_check_workflow(
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID,
    source_workflow_run_id: UUID,
    source_revision_id: UUID,
    idempotency_key: str,
    code: str = "",
    description: str = "",
    process: str | None = None,
    material: str | None = None,
    timeout_seconds: int = 120,
) -> tuple[UUID, WorkflowHandle]:
    """Persist and idempotently start a read-only engineering-check run."""
    idempotency_key = idempotency_key.strip()
    if not idempotency_key or len(idempotency_key) > 500:
        raise ValueError(
            "idempotency_key must contain 1 to 500 characters"
        )
    request_payload = {
        "source_workflow_run_id": str(source_workflow_run_id),
        "source_revision_id": str(source_revision_id),
        "code": code,
        "description": description,
        "process": process,
        "material": material,
        "timeout_seconds": timeout_seconds,
    }
    async with tenant_transaction(tenant_id, principal_id) as connection:
        created = await create_workflow(
            connection,
            tenant_id=tenant_id,
            project_id=project_id,
            requested_by_principal_id=principal_id,
            kind="mcad.check",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
    durable_request = McadCheckRequest(
        workflow_run_id=created.workflow_id,
        source_workflow_run_id=source_workflow_run_id,
        source_revision_id=source_revision_id,
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id=principal_id,
        code=code,
        description=description,
        process=process,
        material=material,
        timeout_seconds=timeout_seconds,
    )
    client = await get_temporal_client()
    workflow_id = temporal_check_workflow_id(created.workflow_id)
    try:
        handle = await client.start_workflow(
            "McadCheckWorkflow",
            durable_request.temporal_payload(),
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return created.workflow_id, handle


async def confirm_mcad_workflow(
    workflow_run_id: UUID,
    *,
    accepted: bool,
    note: str = "",
    workflow_kind: str | None = None,
) -> None:
    client = await get_temporal_client()
    workflow_id = (
        temporal_agent_v2_workflow_id(workflow_run_id)
        if workflow_kind and workflow_kind.startswith("mcad.agent.v2")
        else temporal_workflow_id(workflow_run_id)
    )
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal(
        "confirmation",
        {"accepted": accepted, "note": note[:4000]},
    )


async def cancel_mcad_workflow(
    *,
    tenant_id: UUID,
    principal_id: UUID,
    workflow_run_id: UUID,
    reason: str = "用户取消",
) -> None:
    """Persist cancellation intent before notifying Temporal."""
    async with tenant_transaction(tenant_id, principal_id) as connection:
        workflow_kind = await connection.scalar(
            text("SELECT kind FROM workflow_runs WHERE id=:id"),
            {"id": workflow_run_id},
        )
        if workflow_kind is None:
            raise KeyError(workflow_run_id)
        try:
            await request_workflow_cancellation(connection, workflow_run_id)
        except IllegalTransition:
            # A terminal WorkflowRun treats cancellation as an idempotent no-op.
            status = await connection.scalar(
                text("SELECT status FROM workflow_runs WHERE id=:id"),
                {"id": workflow_run_id},
            )
            if status in {"succeeded", "failed", "cancelled", "timed_out"}:
                return
            else:
                raise
        status = await connection.scalar(
            text("SELECT status FROM workflow_runs WHERE id=:id"),
            {"id": workflow_run_id},
        )
        if status in {"succeeded", "failed", "cancelled", "timed_out"}:
            return
    client = await get_temporal_client()
    workflow_id = (
        temporal_check_workflow_id(workflow_run_id)
        if workflow_kind == "mcad.check"
        else temporal_agent_v2_workflow_id(workflow_run_id)
        if str(workflow_kind).startswith("mcad.agent.v2")
        else temporal_workflow_id(workflow_run_id)
    )
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal("cancel_requested", reason[:4000])
