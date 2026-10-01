"""Explicit retry from immutable task input, with current permission/base checks."""
from uuid import UUID

from app.domain.projects import Permission
from app.services.event_relay import get_task_snapshot, workflow_project_id
from app.services.durable_submission import submit_durable_workflow
from app.models.workflow_requests import (OperationContextV1)


def retry_input(snapshot: dict) -> dict:
    if snapshot['status'] not in {'failed', 'timed_out', 'cancelled'}:
        raise ValueError('只有已失败、超时或取消的任务可以重试')
    if snapshot['kind'] not in {'mcad.agent.v2.generate', 'mcad.agent.v2.modify'}:
        raise ValueError('此任务不支持直接重试，请回到对应操作入口')
    payload = snapshot['request_payload']
    if payload.get('structured_modification') or payload.get('revision_restore'):
        raise ValueError('参数和恢复任务需要重新确认租约与版本，请回到属性或版本入口')
    context = OperationContextV1.model_validate(payload['operation_context']) if payload.get('operation_context') else None
    return dict(
        project_id=UUID(str(snapshot['project_id'])),
        branch_id=UUID(payload['branch_id']),
        expected_base_revision_id=UUID(payload['expected_base_revision_id']),
        expected_state_version=payload.get('expected_state_version'),
        operation=payload['operation'],
        objective=payload['objective'],
        output_formats=list(payload['output_formats']),
        code=payload.get('existing_code'),
        manufacturing_profile=payload.get('manufacturing_profile'),
        modeling_backend=payload.get('modeling_backend', 'cadquery'),
        operation_context=context,
        selection_context=context.selection_context if context else None,
    )


async def retry_task(principal, workflow_run_id: UUID, idempotency_key: str):
    await workflow_project_id(principal, workflow_run_id, permission=Permission.MODIFY_DESIGN)
    snapshot = await get_task_snapshot(principal, workflow_run_id)
    # submit_durable_workflow rechecks permission, immutable input and the exact
    # base generation. An old task must never silently retarget a newer Head.
    return await submit_durable_workflow(principal, idempotency_key=idempotency_key, **retry_input(snapshot))
