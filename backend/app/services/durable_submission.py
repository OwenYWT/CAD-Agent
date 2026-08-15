"""Single durable write path used by REST and conversational WebSocket APIs."""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.identity import PrincipalContext
from app.domain.projects import Permission
from app.models.schemas import GenerateResponse, ManufacturingProfile
from app.execution.canonical import canonical_sha256
from app.repositories.projects import principal_has_permission
from app.repositories.revisions import (
    StaleBaseRevision,
    create_initial_branch,
)
from app.workflows.temporal import (
    McadExecutionRequest,
    McadOutputRequest,
    McadSourcePreparationRequest,
    mcad_workflow_request_payload,
    mcad_agent_v2_request_payload,
    start_mcad_agent_v2_workflow,
    start_mcad_workflow,
)
from app.services.run_state import IdempotencyConflict


_MEDIA_TYPES = {
    "step": "model/step",
    "stl": "model/stl",
    "dxf": "image/vnd.dxf",
    "svg": "image/svg+xml",
}


@dataclass(frozen=True, slots=True)
class DurableSubmission:
    workflow_run_id: UUID
    handle: Any
    project_id: UUID
    branch_id: UUID
    expected_base_revision_id: UUID
    operation: str


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    project_id: UUID
    branch_id: UUID
    head_revision_id: UUID


async def ensure_workspace_identity(
    principal: PrincipalContext,
    *,
    session_id: str,
    panel_id: str,
    title: str,
    user_id: str | None,
) -> WorkspaceIdentity:
    """Create the authenticated project/branch boundary for a first prompt."""
    from app.storage import history

    await history.create_session(session_id, title=title, user_id=user_id)
    await history.create_panel(
        session_id,
        panel_id,
        user_id=user_id,
    )
    branch_name = (
        "panel-" + hashlib.sha256(panel_id.encode("utf-8")).hexdigest()[:16]
    )
    async with tenant_transaction(
        principal.tenant_id,
        principal.principal_id,
    ) as connection:
        project_id = await connection.scalar(
            text(
                """
                SELECT project_id
                FROM workspace_sessions
                WHERE tenant_id=:tenant_id AND id=:session_id
                """
            ),
            {
                "tenant_id": principal.tenant_id,
                "session_id": session_id,
            },
        )
        if project_id is None:
            raise KeyError(session_id)
        existing = (
            await connection.execute(
                text(
                    """
                    SELECT id, head_revision_id
                    FROM project_branches
                    WHERE tenant_id=:tenant_id
                      AND project_id=:project_id
                      AND name=:branch_name
                    """
                ),
                {
                    "tenant_id": principal.tenant_id,
                    "project_id": project_id,
                    "branch_name": branch_name,
                },
            )
        ).mappings().one_or_none()
        if existing is None:
            initial = await create_initial_branch(
                connection,
                tenant_id=principal.tenant_id,
                project_id=project_id,
                created_by_principal_id=principal.principal_id,
                branch_name=branch_name,
                initial_manifest={
                    "schema_version": "mcad-revision-manifest.v1",
                    "state": "empty",
                    "session_id": session_id,
                    "panel_id": panel_id,
                },
            )
            branch_id = initial.branch_id
            head_revision_id = initial.revision_id
        else:
            branch_id = existing["id"]
            head_revision_id = existing["head_revision_id"]
            if head_revision_id is None:
                raise RuntimeError("workspace branch has no head revision")
    return WorkspaceIdentity(
        project_id=project_id,
        branch_id=branch_id,
        head_revision_id=head_revision_id,
    )


async def _authorize_current_base(
    principal: PrincipalContext,
    *,
    project_id: UUID,
    branch_id: UUID,
    expected_base_revision_id: UUID,
) -> None:
    async with tenant_transaction(
        principal.tenant_id,
        principal.principal_id,
    ) as connection:
        if not await principal_has_permission(
            connection,
            tenant_id=principal.tenant_id,
            project_id=project_id,
            principal_id=principal.principal_id,
            permission=Permission.MODIFY_DESIGN,
        ):
            raise PermissionError("principal cannot modify this project")
        head = await connection.scalar(
            text(
                """
                SELECT head_revision_id
                FROM project_branches
                WHERE tenant_id=:tenant_id
                  AND project_id=:project_id
                  AND id=:branch_id
                """
            ),
            {
                "tenant_id": principal.tenant_id,
                "project_id": project_id,
                "branch_id": branch_id,
            },
        )
        if head is None:
            raise KeyError(branch_id)
        if head != expected_base_revision_id:
            raise StaleBaseRevision(
                "expected_base_revision_id is not the current branch head"
            )


def _execution_for_code(
    *,
    operation: str,
    code: str,
    output_formats: list[str],
) -> McadExecutionRequest:
    is_2d = "ezdxf" in code or "result.dxf" in code
    formats = ["dxf"] if is_2d else output_formats
    return McadExecutionRequest(
        step_key="model",
        kind="mcad_model",
        operation=operation,
        mode="2d" if is_2d else "3d",
        source_code=code,
        outputs=tuple(
            McadOutputRequest(
                name=name,
                media_type=_MEDIA_TYPES[name],
            )
            for name in formats
        ),
    )


async def submit_durable_workflow(
    principal: PrincipalContext,
    *,
    project_id: UUID,
    branch_id: UUID,
    expected_base_revision_id: UUID,
    idempotency_key: str,
    operation: str,
    objective: str,
    output_formats: list[str],
    code: str | None = None,
    manufacturing_profile: ManufacturingProfile | dict | None = None,
    require_confirmation: bool = False,
) -> DurableSubmission:
    normalized_objective = objective.strip()
    normalized_idempotency_key = idempotency_key.strip()
    if not normalized_objective or len(normalized_objective) > 4000:
        raise ValueError("objective must contain 1 to 4000 characters")
    if (
        not normalized_idempotency_key
        or len(normalized_idempotency_key) > 500
    ):
        raise ValueError(
            "idempotency_key must contain 1 to 500 characters"
        )
    normalized_profile = None
    if operation == "execute":
        if not code:
            raise ValueError("execute requires source code")
        primary = _execution_for_code(
            operation=operation,
            code=code,
            output_formats=output_formats,
        )
        preparation = None
    elif operation in {"generate", "modify"}:
        normalized_profile = (
            ManufacturingProfile.model_validate(manufacturing_profile)
            if manufacturing_profile
            else None
        )
        primary = None
        preparation = McadSourcePreparationRequest(
            operation=operation,
            prompt=objective,
            existing_code=code if operation == "modify" else None,
            output_formats=tuple(output_formats),
            manufacturing_profile=(
                normalized_profile.model_dump(mode="json")
                if normalized_profile
                else None
            ),
        )
    else:
        raise ValueError(f"unsupported durable operation: {operation}")

    is_agent_v2 = operation in {"generate", "modify"}
    kind = (
        f"mcad.agent.v2.{operation}"
        if is_agent_v2
        else f"mcad.{operation}"
    )
    request_payload = (
        mcad_agent_v2_request_payload(
            branch_id=branch_id,
            expected_base_revision_id=expected_base_revision_id,
            operation=operation,
            objective=normalized_objective,
            existing_code=code if operation == "modify" else None,
            manufacturing_profile=(
                normalized_profile.model_dump(mode="json")
                if normalized_profile
                else None
            ),
            output_formats=tuple(output_formats),
            confirmation_timeout_seconds=3600,
        )
        if is_agent_v2
        else mcad_workflow_request_payload(
            branch_id=branch_id,
            expected_base_revision_id=expected_base_revision_id,
            objective=normalized_objective,
            primary=primary,
            preparation=preparation,
            followup=None,
            require_confirmation=require_confirmation,
            confirmation_timeout_seconds=3600,
            commit_after_confirmation=require_confirmation,
        )
    )
    payload_hash = canonical_sha256(request_payload)
    async with tenant_transaction(
        principal.tenant_id,
        principal.principal_id,
    ) as connection:
        existing = (
            await connection.execute(
                text(
                    """
                    SELECT id, project_id, requested_by_principal_id, kind,
                           request_payload_hash
                    FROM workflow_runs
                    WHERE tenant_id=:tenant_id
                      AND idempotency_key=:idempotency_key
                    """
                ),
                {
                    "tenant_id": principal.tenant_id,
                    "idempotency_key": normalized_idempotency_key,
                },
            )
        ).mappings().one_or_none()
    if existing is not None:
        expected = {
            "project_id": project_id,
            "requested_by_principal_id": principal.principal_id,
            "kind": kind,
            "request_payload_hash": payload_hash,
        }
        if any(
            existing[field] != value
            for field, value in expected.items()
        ):
            raise IdempotencyConflict(
                "workflow idempotency key was reused with a different payload"
            )
        # Re-enter the idempotent start boundary. This repairs the crash window
        # where WorkflowRun committed but the Temporal start call did not.
        if is_agent_v2:
            workflow_run_id, handle = await start_mcad_agent_v2_workflow(
                tenant_id=principal.tenant_id,
                project_id=project_id,
                principal_id=principal.principal_id,
                branch_id=branch_id,
                expected_base_revision_id=expected_base_revision_id,
                kind=kind,
                idempotency_key=normalized_idempotency_key,
                operation=operation,
                objective=normalized_objective,
                existing_code=code if operation == "modify" else None,
                manufacturing_profile=(
                    normalized_profile.model_dump(mode="json")
                    if normalized_profile
                    else None
                ),
                output_formats=tuple(output_formats),
                require_worker_ready=False,
            )
        else:
            workflow_run_id, handle = await start_mcad_workflow(
                tenant_id=principal.tenant_id,
                project_id=project_id,
                principal_id=principal.principal_id,
                branch_id=branch_id,
                expected_base_revision_id=expected_base_revision_id,
                kind=kind,
                idempotency_key=normalized_idempotency_key,
                objective=normalized_objective,
                primary=primary,
                preparation=preparation,
                require_confirmation=require_confirmation,
                commit_after_confirmation=require_confirmation,
            )
        return DurableSubmission(
            workflow_run_id=workflow_run_id,
            handle=handle,
            project_id=project_id,
            branch_id=branch_id,
            expected_base_revision_id=expected_base_revision_id,
            operation=operation,
        )

    await _authorize_current_base(
        principal,
        project_id=project_id,
        branch_id=branch_id,
        expected_base_revision_id=expected_base_revision_id,
    )
    if is_agent_v2:
        workflow_run_id, handle = await start_mcad_agent_v2_workflow(
            tenant_id=principal.tenant_id,
            project_id=project_id,
            principal_id=principal.principal_id,
            branch_id=branch_id,
            expected_base_revision_id=expected_base_revision_id,
            kind=kind,
            idempotency_key=normalized_idempotency_key,
            operation=operation,
            objective=normalized_objective,
            existing_code=code if operation == "modify" else None,
            manufacturing_profile=(
                normalized_profile.model_dump(mode="json")
                if normalized_profile
                else None
            ),
            output_formats=tuple(output_formats),
        )
    else:
        workflow_run_id, handle = await start_mcad_workflow(
            tenant_id=principal.tenant_id,
            project_id=project_id,
            principal_id=principal.principal_id,
            branch_id=branch_id,
            expected_base_revision_id=expected_base_revision_id,
            kind=kind,
            idempotency_key=normalized_idempotency_key,
            objective=normalized_objective,
            primary=primary,
            preparation=preparation,
            require_confirmation=require_confirmation,
            commit_after_confirmation=require_confirmation,
        )
    return DurableSubmission(
        workflow_run_id=workflow_run_id,
        handle=handle,
        project_id=project_id,
        branch_id=branch_id,
        expected_base_revision_id=expected_base_revision_id,
        operation=operation,
    )


async def _workflow_projection(
    principal: PrincipalContext,
    workflow_run_id: UUID,
) -> dict[str, Any]:
    async with tenant_transaction(
        principal.tenant_id,
        principal.principal_id,
    ) as connection:
        workflow = (
            await connection.execute(
                text(
                    """
                    SELECT project_id, kind, status, request_payload,
                           error_code, error_message
                    FROM workflow_runs
                    WHERE tenant_id=:tenant_id AND id=:workflow_run_id
                    """
                ),
                {
                    "tenant_id": principal.tenant_id,
                    "workflow_run_id": workflow_run_id,
                },
            )
        ).mappings().one()
        change_set = (
            await connection.execute(
                text(
                    """
                    SELECT id, base_revision_id, candidate_revision_id,
                           status
                    FROM change_sets
                    WHERE source_workflow_run_id=:workflow_run_id
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
        ).mappings().one_or_none()
        artifacts = (
            await connection.execute(
                text(
                    """
                    SELECT artifact_kind, filename
                    FROM artifacts
                    WHERE workflow_run_id=:workflow_run_id
                    ORDER BY created_at, filename
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
        ).mappings().all()
        source_event = (
            await connection.execute(
                text(
                    """
                    SELECT payload
                    FROM task_events
                    WHERE workflow_run_id=:workflow_run_id
                      AND event_type='source.prepared'
                    ORDER BY sequence DESC
                    LIMIT 1
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
        ).mappings().one_or_none()
        agent_plan_event = (
            await connection.execute(
                text(
                    """
                    SELECT payload
                    FROM task_events
                    WHERE workflow_run_id=:workflow_run_id
                      AND event_type='agent.plan.completed'
                    ORDER BY sequence DESC
                    LIMIT 1
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
        ).mappings().one_or_none()
        generated_source = (
            await connection.execute(
                text(
                    """
                    SELECT source_code, source_hash
                    FROM agent_generated_sources
                    WHERE workflow_run_id=:workflow_run_id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
        ).mappings().one_or_none()
        attempt_count = await connection.scalar(
            text(
                "SELECT count(*) FROM execution_attempts "
                "WHERE workflow_run_id=:workflow_run_id"
            ),
            {"workflow_run_id": workflow_run_id},
        )
    prepared = (
        dict(source_event["payload"])
        if source_event is not None
        else {}
    )
    if agent_plan_event is not None:
        agent_plan = dict(agent_plan_event["payload"])
        plan = dict(agent_plan.get("plan") or {})
        prepared.update(
            {
                "needs_confirmation": bool(
                    agent_plan.get("requires_confirmation")
                ),
                "design_brief": plan.get("design_brief"),
                "manufacturing_profile": workflow["request_payload"].get(
                    "manufacturing_profile"
                ),
                "output_formats": workflow["request_payload"].get(
                    "output_formats"
                ),
            }
        )
    if generated_source is not None:
        prepared["source_code"] = generated_source["source_code"]
        prepared["source_hash"] = generated_source["source_hash"]
    return {
        "workflow": dict(workflow),
        "change_set": dict(change_set) if change_set else None,
        "artifacts": [dict(item) for item in artifacts],
        "prepared": prepared or None,
        "attempt_count": int(attempt_count or 0),
    }


def _compatibility_response(
    submission: DurableSubmission,
    projection: dict[str, Any],
    *,
    workflow_result: dict[str, Any] | None = None,
) -> GenerateResponse:
    workflow = projection["workflow"]
    prepared = dict(
        (workflow_result or {}).get("preparation")
        or projection.get("prepared")
        or {}
    )
    if not prepared.get("source_code"):
        primary = dict(workflow["request_payload"].get("primary") or {})
        if primary.get("source_code"):
            prepared["source_code"] = primary["source_code"]
            prepared["output_formats"] = [
                item["name"] for item in primary.get("outputs") or []
            ]
    change_set = projection.get("change_set")
    status = str(workflow["status"])
    waiting_for_source = (
        status == "waiting_confirmation"
        and not change_set
        and bool(prepared.get("needs_confirmation"))
    )
    files = {
        str(item["artifact_kind"]): (
            f"/api/files/{submission.workflow_run_id}/{item['filename']}"
        )
        for item in projection["artifacts"]
        if str(item["artifact_kind"]) in _MEDIA_TYPES
    }
    success = status == "succeeded"
    error = None
    if not success and not waiting_for_source:
        error = {
            "type": str(workflow.get("error_code") or "WorkflowPending"),
            "message": str(
                workflow.get("error_message")
                or "持久任务仍在运行，请通过任务状态接口继续查询。"
            ),
        }
    return GenerateResponse(
        request_id=str(submission.workflow_run_id),
        success=success,
        needs_confirmation=waiting_for_source,
        manufacturing_profile=prepared.get("manufacturing_profile"),
        files=files,
        code=prepared.get("source_code"),
        plan=prepared.get("plan"),
        design_brief=prepared.get("design_brief"),
        error=error,
        project_id=submission.project_id,
        branch_id=submission.branch_id,
        expected_base_revision_id=submission.expected_base_revision_id,
        revision_id=(
            change_set["candidate_revision_id"] if change_set else None
        ),
        workflow_run_id=submission.workflow_run_id,
        change_set_id=change_set["id"] if change_set else None,
        task_status=status,
        attempts=projection["attempt_count"],
    )


async def wait_for_compatibility_response(
    principal: PrincipalContext,
    submission: DurableSubmission,
    *,
    timeout_seconds: float,
) -> GenerateResponse:
    """Wait for terminal output or return an actionable clarification state."""
    result_task = asyncio.create_task(submission.handle.result())
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        while True:
            if result_task.done():
                try:
                    workflow_result = result_task.result()
                except Exception:
                    workflow_result = None
                projection = await _workflow_projection(
                    principal,
                    submission.workflow_run_id,
                )
                return _compatibility_response(
                    submission,
                    projection,
                    workflow_result=workflow_result,
                )
            projection = await _workflow_projection(
                principal,
                submission.workflow_run_id,
            )
            if (
                projection["workflow"]["status"] == "waiting_confirmation"
                and projection["change_set"] is None
                and (projection.get("prepared") or {}).get(
                    "needs_confirmation"
                )
            ):
                return _compatibility_response(submission, projection)
            if asyncio.get_running_loop().time() >= deadline:
                return _compatibility_response(submission, projection)
            await asyncio.sleep(0.1)
    finally:
        if not result_task.done():
            # Cancelling this local await does not cancel the Temporal workflow.
            result_task.cancel()


async def get_compatibility_response(
    principal: PrincipalContext,
    workflow_run_id: UUID,
) -> GenerateResponse:
    projection = await _workflow_projection(principal, workflow_run_id)
    workflow = projection["workflow"]
    request_payload = dict(workflow["request_payload"])
    submission = DurableSubmission(
        workflow_run_id=workflow_run_id,
        handle=None,
        project_id=workflow["project_id"],
        branch_id=UUID(str(request_payload["branch_id"])),
        expected_base_revision_id=UUID(
            str(request_payload["expected_base_revision_id"])
        ),
        operation=str(workflow["kind"]).removeprefix("mcad."),
    )
    return _compatibility_response(submission, projection)
