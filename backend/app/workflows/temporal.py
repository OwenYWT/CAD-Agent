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
from app.temporal_client import get_temporal_client


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
    output_formats: tuple[Literal["step", "stl", "dxf", "svg"], ...] = (
        "step",
        "stl",
    )
    manufacturing_profile: dict[str, Any] | None = None

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


def temporal_workflow_id(workflow_run_id: UUID | str) -> str:
    return f"mcad-workflow-{workflow_run_id}"


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


async def confirm_mcad_workflow(
    workflow_run_id: UUID,
    *,
    accepted: bool,
    note: str = "",
) -> None:
    client = await get_temporal_client()
    handle = client.get_workflow_handle(temporal_workflow_id(workflow_run_id))
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
    handle = client.get_workflow_handle(temporal_workflow_id(workflow_run_id))
    await handle.signal("cancel_requested", reason[:4000])
