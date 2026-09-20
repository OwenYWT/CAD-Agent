"""Authorized document submission, independent of HTTP routing."""
from __future__ import annotations
from uuid import UUID
from sqlalchemy import text
from app.services.feature_leases import assert_operation_lease_access
from app.services.document_rebase import resolve_parameter_rebase
from app.execution.canonical import canonical_sha256
from app.db import tenant_transaction
from app.domain.identity import PrincipalContext
from app.domain.projects import Permission
from app.freecad.state_contract import compile_parameter_operation_plan, read_verified_state_artifact
from app.services.cloud_documents import DocumentConflict, authorized_document, checkpoint
from app.services.durable_submission import submit_durable_workflow
from app.services.operation_resolution import load_revision_source_inventory, resolve_rest_generate_submission, resolve_rest_modify_submission
from app.services.run_state import IdempotencyConflict

from app.models.document_requests import OperationRequest

async def submit_document_operation(document_id: UUID, body: OperationRequest, principal: PrincipalContext):
    client_hash = canonical_sha256(body.model_dump(mode="json", exclude={"lease_token"}))

    legacy_replay = False

    async with tenant_transaction(principal.tenant_id, principal.principal_id) as conn:
        doc = await authorized_document(conn, principal, document_id, Permission.MODIFY_DESIGN)
        prior = (await conn.execute(text("""SELECT w.id,w.requested_by_principal_id,w.request_payload
            FROM workflow_runs w JOIN cad_operations o ON o.id=w.id
            WHERE o.document_id=:doc AND o.idempotency_key=:key"""),
            {"doc":document_id,"key":body.idempotency_key})).mappings().one_or_none()

    if prior is not None:
        previous_context = prior["request_payload"].get("operation_context") or {}
        if prior["requested_by_principal_id"] != principal.principal_id:
            raise IdempotencyConflict("请求 ID 属于其他提交者")
        if previous_context.get("client_request_hash"):
            if previous_context["client_request_hash"] != client_hash:
                raise IdempotencyConflict("请求 ID 已用于其他文档操作")
            return {"operation_id":str(prior["id"]),"workflow_run_id":str(prior["id"]),
                "document_id":str(document_id),"task_url":f"/api/tasks/{prior['id']}/snapshot",
                "rebased_from_revision_id":previous_context.get("rebased_from_revision_id"),"replayed":True}
        legacy_replay = True

    rebase = None

    if not legacy_replay:
        body, rebase = await resolve_parameter_rebase(principal, document_id, doc, body)

    base = await checkpoint(principal, document_id, body.expected_base_revision_id)

    if not legacy_replay:
        # Reject expired/conflicting edits before provider/worker readiness
        # probes. Enqueue repeats this check in its atomic write transaction.
        async with tenant_transaction(principal.tenant_id, principal.principal_id) as conn:
            current = await authorized_document(conn, principal, document_id, Permission.MODIFY_DESIGN, lock=True)
            if current['head_revision_id'] != body.expected_base_revision_id or current['state_version'] != body.expected_state_version:
                raise DocumentConflict('文档已更新，请读取当前版本后重新提交')
            await assert_operation_lease_access(conn,document_id,principal.principal_id,{
                'structured_modification':body.modification.model_dump(mode='json') if body.modification else None,
                'operation_context':{'feature_lease_token':body.lease_token},
            })

    if body.action == "generate":
        if base["modeling_backend"]:
            raise ValueError("已有模型的文档应使用修改操作，新模型请新建会话")
        if body.modification is not None:
            raise ValueError("生成操作不能携带参数修改")
        resolution = resolve_rest_generate_submission(base_revision_id=body.expected_base_revision_id, output_formats=["step", "stl"])
    else:
        inventory = await load_revision_source_inventory(principal, project_id=doc["project_id"], revision_id=body.expected_base_revision_id)
        code = inventory.cadquery[0].code if not inventory.fcstd and len(inventory.cadquery) == 1 else None
        resolution = resolve_rest_modify_submission(requested_backend="auto", base_revision_id=body.expected_base_revision_id, inventory=inventory, request_code=code)
        if body.action in {"parameters.update", "native.update"}:
            if body.modification is None or resolution.modeling_backend != "freecad":
                raise ValueError("参数编辑需要 FreeCAD 检查点及有效参数值")
            if (body.action == 'native.update') != bool(body.modification.native_edits):
                raise ValueError('操作类型与结构化修改内容不一致')
            async with tenant_transaction(principal.tenant_id, principal.principal_id) as conn:
                artifacts = [dict(a) for a in (await conn.execute(text("SELECT * FROM artifacts WHERE revision_id=:rev AND artifact_kind='state'"), {"rev": body.expected_base_revision_id})).mappings()]
            state, _ = await read_verified_state_artifact(artifacts, require_v2=True, expected_sha256=body.modification.expected_state_sha256)
            compile_parameter_operation_plan(state, body.modification.model_dump(mode="json"), output_formats=("step", "stl"))
        elif body.modification is not None:
            raise ValueError("请使用参数编辑操作提交结构化参数")

    objective = body.objective.strip()

    if body.action == "parameters.update" and not objective:
        objective = "更新参数：" + ", ".join(f"{p.parameter_id}={p.value}" for p in body.modification.parameter_updates)
    elif body.action == 'native.update' and not objective:
        objective = '修改原生特征：' + ', '.join(f"{edit.action} {edit.args.get('object') or edit.args.get('sketch')}" for edit in body.modification.native_edits)

    operation_context = resolution.operation_context

    if not legacy_replay:
        operation_context = operation_context.model_copy(update={"client_request_hash":client_hash,
            "feature_lease_token":body.lease_token, **(rebase or {})})

    submission = await submit_durable_workflow(principal, project_id=doc["project_id"], branch_id=document_id,
        expected_base_revision_id=body.expected_base_revision_id, expected_state_version=body.expected_state_version,
        idempotency_key=body.idempotency_key, operation=resolution.operation, objective=objective,
        output_formats=["step", "stl"], code=resolution.existing_code, modeling_backend=resolution.modeling_backend,
        operation_context=operation_context, structured_modification=body.modification,
        selection_context=body.selection_context)

    return {"operation_id": str(submission.workflow_run_id), "workflow_run_id": str(submission.workflow_run_id),
            "document_id": str(document_id), "task_url": f"/api/tasks/{submission.workflow_run_id}/snapshot",
            "rebased_from_revision_id":str(rebase["rebased_from_revision_id"]) if rebase else None}
