"""Candidates use cases; Temporal names live in the adapter."""
from __future__ import annotations
from typing import Any
from temporalio.exceptions import ApplicationError
from app.agent.durable_plan import AgentPlan
from app.db import tenant_transaction
from app.repositories.agent_candidates import create_agent_candidate_build, transition_agent_candidate_build
from app.domain.revisions import CandidateBuildStatus
from app.services.artifact_commit import seal_agent_candidate
from app.workflows.inputs import _uuid, _agent_v2_request
from app.workflows.handlers.legacy_modeling import plan

async def agent_allocate_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    request = _agent_v2_request(payload)
    plan = AgentPlan.model_validate(payload["plan"])
    async with tenant_transaction(
        request.tenant_id,
        request.principal_id,
    ) as connection:
        created = await create_agent_candidate_build(
            connection,
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            branch_id=request.branch_id,
            base_revision_id=request.expected_base_revision_id,
            workflow_id=request.workflow_run_id,
            created_by_principal_id=request.principal_id,
            plan=plan.temporal_payload(),
        )
    return {
        "candidate_build_id": str(created.candidate_build_id),
        "status": created.status.value,
        "replayed": created.replayed,
    }


async def agent_terminate_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    request = _agent_v2_request(payload)
    target = CandidateBuildStatus(str(payload["target_status"]))
    if target not in {
        CandidateBuildStatus.FAILED,
        CandidateBuildStatus.CANCELLED,
        CandidateBuildStatus.ABANDONED,
    }:
        raise ApplicationError(
            "unsupported candidate terminal status",
            type="invalid_candidate_terminal_status",
            non_retryable=True,
        )
    async with tenant_transaction(
        request.tenant_id,
        request.principal_id,
    ) as connection:
        result = await transition_agent_candidate_build(
            connection,
            tenant_id=request.tenant_id,
            candidate_build_id=_uuid(payload, "candidate_build_id"),
            expected=CandidateBuildStatus.BUILDING,
            target=target,
            failure_code=str(payload.get("error_code") or "") or None,
            failure_message=(
                str(payload.get("error_message") or "")[:4000] or None
            ),
        )
    return {
        "candidate_build_id": str(result.candidate_build_id),
        "status": result.status.value,
        "replayed": result.replayed,
    }


async def agent_seal_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    request = _agent_v2_request(payload)
    plan = AgentPlan.model_validate(payload["plan"])
    sealed = await seal_agent_candidate(
        tenant_id=request.tenant_id,
        principal_id=request.principal_id,
        workflow_id=request.workflow_run_id,
        candidate_build_id=_uuid(payload, "candidate_build_id"),
        plan=plan.temporal_payload(),
        selected_manifests=[
            dict(item) for item in payload.get("selected_manifests") or ()
        ],
    )
    return {
        "status": "succeeded",
        "seal_id": str(sealed.seal_id),
        "candidate_build_id": str(sealed.candidate_build_id),
        "candidate_revision_id": str(sealed.candidate_revision_id),
        "change_set_id": str(sealed.change_set_id),
        "artifacts": [
            {
                "artifact_id": str(item.artifact_id),
                "filename": item.filename,
                "artifact_kind": item.artifact_kind,
                "object_key": item.object_key,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "content_type": item.content_type,
            }
            for item in sealed.artifacts
        ],
        "replayed": sealed.replayed,
    }
