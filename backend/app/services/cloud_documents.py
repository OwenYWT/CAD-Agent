"""Cloud document authority layered on immutable revisions and existing RBAC.

Kernel execution is serialized for each document, including Agent planning and
validation. Reviewable candidates release the kernel slot; only the existing
review/commit CAS advances the document. No implicit rebasing is performed.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.identity import PrincipalContext
from app.domain.projects import Permission
from app.freecad.semantic_state import project_semantic_state, semantic_delta
from app.freecad.state_contract import read_verified_state_artifact
from app.repositories.projects import principal_has_permission
from app.services.feature_annotations import annotation_context, annotate_projection


class DocumentConflict(ValueError):
    code = "document_state_stale"


async def _active_workspace_member(connection, tenant_id: UUID, principal_id: UUID) -> bool:
    member = await connection.scalar(text("""SELECT p.id FROM principals p
        JOIN tenant_memberships m ON m.tenant_id=p.tenant_id AND m.principal_id=p.id
        JOIN tenants t ON t.id=p.tenant_id
        WHERE p.id=:principal AND p.tenant_id=:tenant AND p.status='active' AND t.status='active'"""),
        {"principal": principal_id, "tenant": tenant_id})
    return member is not None


async def authorized_document(connection, context: PrincipalContext, document_id: UUID,
                              permission=Permission.VIEW_PROJECT, *, lock=False):
    row = (await connection.execute(text(
        "SELECT * FROM cloud_documents WHERE id=:id" + (" FOR UPDATE" if lock else "")
    ), {"id": document_id})).mappings().one_or_none()
    if row is None:
        raise KeyError(document_id)
    if not await _active_workspace_member(connection, context.tenant_id, context.principal_id):
        raise PermissionError("工作区授权已失效")
    allowed = await principal_has_permission(connection, tenant_id=context.tenant_id,
        project_id=row["project_id"], principal_id=context.principal_id, permission=permission)
    if not allowed:
        raise PermissionError("无权访问此文档")
    return dict(row)


async def enqueue_operation(connection, *, workflow_id: UUID, tenant_id: UUID,
                            principal_id: UUID, payload: dict, idempotency_key: str,
                            request_hash: str):
    """Called in the WorkflowRun creation transaction, never for read-only checks."""
    if not payload.get("branch_id") or not (
        payload.get("operation") in {"generate", "modify"} or payload.get("primary") or payload.get("preparation")
    ):
        return
    document_id = UUID(payload["branch_id"])
    doc = (await connection.execute(text(
        "SELECT * FROM cloud_documents WHERE id=:id FOR UPDATE"
    ), {"id": document_id})).mappings().one()
    expected = payload.get("expected_state_version")
    if str(doc["head_revision_id"]) != str(payload["expected_base_revision_id"]) or (
        expected is not None and int(expected) != doc["state_version"]
    ):
        raise DocumentConflict("文档已更新，请读取当前版本后重新提交")
    frozen_arguments = {**payload, "_feature_annotations": await annotation_context(connection, document_id)}
    selection = (payload.get("operation_context") or {}).get("selection_context")
    if selection is not None:
        from app.freecad.selection import SelectionContextV1, SelectionError, freeze_selection
        projection = await connection.scalar(text("""SELECT projection FROM document_checkpoints
            WHERE document_id=:doc AND revision_id=:revision ORDER BY projector_version DESC LIMIT 1"""),
            {"doc": document_id, "revision": doc["head_revision_id"]})
        if projection is None:
            raise SelectionError("目标检查点尚不可用，不能冻结选择")
        frozen_arguments["_selection_context"] = freeze_selection(dict(projection),
            SelectionContextV1.model_validate(selection), revision_id=doc["head_revision_id"],
            state_version=doc["state_version"], objective=payload["objective"])
    from app.services.engineering_evidence import engineering_references
    engineering = await engineering_references(connection, document_id, doc['head_revision_id'])
    if engineering:
        frozen_arguments['_engineering_evidence'] = engineering
    from app.services.feature_leases import assert_operation_lease_access
    await assert_operation_lease_access(connection, document_id, principal_id, payload)
    await connection.execute(text("""
        INSERT INTO cad_operations(id, tenant_id, document_id, actor_principal_id, actor,
            base_revision_id, base_state_version, action, arguments, idempotency_key, request_hash)
        VALUES (:id, :tenant, :doc, :principal, :actor, :base, :version, :action, CAST(:args AS jsonb), :key, :hash)
    """), {"id": workflow_id, "tenant": tenant_id, "doc": document_id, "principal": principal_id,
            "actor": "user" if payload.get("structured_modification") or payload.get("primary") else "agent",
            "base": UUID(payload["expected_base_revision_id"]), "version": doc["state_version"],
            "action": ('native.update' if payload['structured_modification'].get('native_edits') else 'parameters.update') if payload.get("structured_modification") else payload.get("operation", "execute"),
            "args": json.dumps(frozen_arguments, ensure_ascii=False), "key": idempotency_key, "hash": request_hash})
    lease_token = (payload.get("operation_context") or {}).get("feature_lease_token")
    if lease_token:
        # Transfer protection to the persisted queued operation atomically.
        await connection.execute(text("DELETE FROM document_feature_leases WHERE token=:token AND document_id=:doc AND principal_id=:principal"),
            {"token":UUID(str(lease_token)),"doc":document_id,"principal":principal_id})


async def acquire_operation(tenant_id: UUID, principal_id: UUID, workflow_id: UUID) -> dict:
    async with tenant_transaction(tenant_id, principal_id) as conn:
        op = (await conn.execute(text("SELECT * FROM cad_operations WHERE id=:id"), {"id": workflow_id})).mappings().one()
        doc = (await conn.execute(text("SELECT * FROM cloud_documents WHERE id=:id FOR UPDATE"),
                                  {"id": op["document_id"]})).mappings().one()
        # An API/worker crash after terminal persistence cannot strand the queue.
        await conn.execute(text("""
            UPDATE cad_operations o SET status=CASE WHEN w.status='succeeded' THEN 'reviewable'
                WHEN w.status='cancelled' THEN 'cancelled' ELSE 'failed' END,
                finished_at=COALESCE(o.finished_at, CURRENT_TIMESTAMP), error_code=w.error_code
            FROM workflow_runs w WHERE w.id=o.id AND o.document_id=:doc
                AND o.status IN ('queued', 'running') AND w.status IN ('succeeded', 'failed', 'cancelled', 'timed_out')
        """), {"doc": op["document_id"]})
        current = await conn.scalar(text("SELECT status FROM cad_operations WHERE id=:id"), {"id": workflow_id})
        if current not in {"queued", "running"}:
            return {"status": current, "error_code": "document_operation_terminal"}
        if not await _active_workspace_member(conn, tenant_id, principal_id) or not await principal_has_permission(conn, tenant_id=tenant_id, project_id=doc["project_id"],
                principal_id=principal_id, permission=Permission.MODIFY_DESIGN):
            await conn.execute(text("UPDATE cad_operations SET status='rejected', error_code='permission_denied', finished_at=CURRENT_TIMESTAMP WHERE id=:id"), {"id": workflow_id})
            return {"status": "rejected", "error_code": "permission_denied"}
        if current == "running":
            return {"status": "acquired"}
        if doc["head_revision_id"] != op["base_revision_id"] or doc["state_version"] != op["base_state_version"]:
            await conn.execute(text("UPDATE cad_operations SET status='rejected', error_code='document_state_stale', finished_at=CURRENT_TIMESTAMP WHERE id=:id"), {"id": workflow_id})
            return {"status": "rejected", "error_code": "document_state_stale"}
        first = await conn.scalar(text("SELECT id FROM cad_operations WHERE document_id=:doc AND status IN ('queued', 'running') ORDER BY queue_number LIMIT 1"), {"doc": op["document_id"]})
        if first != workflow_id:
            return {"status": "waiting"}
        await conn.execute(text("UPDATE cad_operations SET status='running', started_at=COALESCE(started_at, CURRENT_TIMESTAMP) WHERE id=:id"), {"id": workflow_id})
        return {"status": "acquired"}


async def finish_operation(tenant_id: UUID, principal_id: UUID, workflow_id: UUID):
    async with tenant_transaction(tenant_id, principal_id) as conn:
        await conn.execute(text("""
            UPDATE cad_operations o SET status=CASE WHEN w.status='succeeded' THEN 'reviewable'
                WHEN w.status='cancelled' THEN 'cancelled' ELSE 'failed' END,
                finished_at=COALESCE(o.finished_at, CURRENT_TIMESTAMP), error_code=w.error_code,
                result_revision_id=(SELECT candidate_revision_id FROM change_sets WHERE source_workflow_run_id=o.id LIMIT 1)
            FROM workflow_runs w WHERE w.id=o.id AND o.id=:id AND o.status IN ('queued', 'running')
        """), {"id": workflow_id})
        status = await conn.scalar(text("SELECT status FROM cad_operations WHERE id=:id"), {"id": workflow_id})
        if status is None:
            raise KeyError(workflow_id)
    return {"status": status}


async def checkpoint(context: PrincipalContext, document_id: UUID, revision_id: UUID) -> dict:
    """Project verified ancestry iteratively; preserve old immutable cache versions.

    A cached parent makes the common case one revision. Forks inherit the frozen
    source's feature history, never the source branch's mutable current head.
    """
    pending, visited = [], set()
    cursor = (document_id, revision_id)
    previous = {"features": []}
    while cursor is not None:
        current_doc, current_rev = cursor
        if cursor in visited:
            raise ValueError("revision ancestry contains a cycle")
        visited.add(cursor)
        async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
            doc = await authorized_document(conn, context, current_doc)
            cached = await conn.scalar(text("""SELECT projection FROM document_checkpoints
                WHERE document_id=:doc AND revision_id=:revision AND projector_version=4"""),
                {"doc": current_doc, "revision": current_rev})
            if cached is not None:
                previous = dict(cached)
                break
            revision = (await conn.execute(text("SELECT id, parent_revision_id, manifest FROM project_revisions WHERE id=:rev AND branch_id=:doc"),
                {"rev": current_rev, "doc": current_doc})).mappings().one_or_none()
            if revision is None:
                raise KeyError(current_rev)
            fork = (await conn.execute(text("SELECT * FROM document_forks WHERE document_id=:doc"),
                {"doc": current_doc})).mappings().one_or_none()
            lineage_id = fork["lineage_id"] if fork else current_doc
            artifacts = [dict(r) for r in (await conn.execute(text("""
                SELECT id, artifact_kind, sha256, size_bytes, object_key, runtime_metadata
                FROM artifacts WHERE project_id=:project AND revision_id=:revision ORDER BY created_at, id
            """), {"project": doc["project_id"], "revision": current_rev})).mappings()]
        pending.append((current_doc, current_rev, lineage_id, artifacts))
        parent = revision["parent_revision_id"]
        if fork and parent == fork["seed_revision_id"]:
            cursor = (fork["source_document_id"], fork["source_revision_id"])
        else:
            cursor = (current_doc, parent) if parent else None
    for current_doc, current_rev, lineage_id, artifacts in reversed(pending):
        state_rows = [a for a in artifacts if a["artifact_kind"] == "state"]
        state, state_hash = ({"objects": []}, None)
        if state_rows:
            state, state_hash = await read_verified_state_artifact(state_rows)
        projection = project_semantic_state(lineage_id, state, revision_id=current_rev, previous=previous)

        def ref(kind):
            a = next((a for a in artifacts if a["artifact_kind"] == kind), None)
            return {"artifact_id": str(a["id"]), "sha256": a["sha256"], "size_bytes": a["size_bytes"],
                    "url": f"/api/documents/{current_doc}/artifacts/{a['id']}", "runtime": a["runtime_metadata"]} if a else None

        projection.update(projector_version=4, revision_id=str(current_rev), parameter_state_sha256=state_hash,
            lineage_id=str(lineage_id), fcstd=ref("fcstd"), mesh=ref("stl"), state=ref("state"),
            modeling_backend="freecad" if any(a["artifact_kind"] == "fcstd" for a in artifacts) else "cadquery" if artifacts else None)
        async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
            await authorized_document(conn, context, current_doc)
            await conn.execute(text("""INSERT INTO document_checkpoints(tenant_id, document_id, revision_id, projector_version, projection)
                VALUES(:tenant, :doc, :rev, 4, CAST(:projection AS jsonb)) ON CONFLICT DO NOTHING"""),
                {"tenant": context.tenant_id, "doc": current_doc, "rev": current_rev, "projection": json.dumps(projection)})
        previous = projection
    return previous


async def document_snapshot(context: PrincipalContext, document_id: UUID) -> dict:
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        doc = await authorized_document(conn, context, document_id)
        can_edit = await principal_has_permission(conn, tenant_id=context.tenant_id, project_id=doc["project_id"],
            principal_id=context.principal_id, permission=Permission.MODIFY_DESIGN)
        can_share = await principal_has_permission(conn, tenant_id=context.tenant_id, project_id=doc["project_id"],
            principal_id=context.principal_id, permission=Permission.MANAGE_MEMBERS)
        can_commit = await principal_has_permission(conn, tenant_id=context.tenant_id, project_id=doc["project_id"],
            principal_id=context.principal_id, permission=Permission.COMMIT_VERSION)
        capabilities = {name: await principal_has_permission(conn, tenant_id=context.tenant_id,
            project_id=doc["project_id"], principal_id=context.principal_id, permission=permission)
            for name, permission in {"can_review": Permission.REVIEW_CHANGE,
                "can_export": Permission.EXPORT_ARTIFACT, "can_rollback": Permission.ROLLBACK_VERSION}.items()}
        annotations = await annotation_context(conn, document_id, through_sequence=doc["event_sequence"])
    projection = await checkpoint(context, document_id, doc["head_revision_id"])
    projection = annotate_projection(projection, annotations)
    return {"document_id": str(document_id), "project_id": str(doc["project_id"]), "active_branch_id": str(document_id),
            "head_revision_id": str(doc["head_revision_id"]), "state_version": doc["state_version"],
            "event_sequence": doc["event_sequence"], "can_edit": can_edit, "can_share": can_share,
            "can_commit":can_commit, **capabilities, **projection}


async def document_revision_view(context: PrincipalContext, document_id: UUID, revision_id: UUID) -> dict:
    """One immutable view projection, with current head metadata kept explicit."""
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        doc = await authorized_document(conn, context, document_id)
        revision = (await conn.execute(text("""SELECT r.revision_number, c.id AS change_set_id,
            c.status AS review_status, c.validation_summary, c.base_state_version, c.source_workflow_run_id
            FROM project_revisions r LEFT JOIN change_sets c ON c.candidate_revision_id=r.id
            WHERE r.branch_id=:doc AND r.id=:revision"""),
            {"doc": document_id, "revision": revision_id})).mappings().one_or_none()
        if revision is None:
            raise KeyError(revision_id)
        artifacts = (await conn.execute(text("""SELECT id, artifact_kind, filename FROM artifacts a WHERE revision_id=:revision
            AND NOT EXISTS(SELECT 1 FROM workflow_runs w WHERE w.id=a.workflow_run_id AND w.kind='mcad.scene') ORDER BY filename, id"""),
                                       {"revision": revision_id})).mappings().all()
        can_export = await principal_has_permission(conn, tenant_id=context.tenant_id, project_id=doc["project_id"],
            principal_id=context.principal_id, permission=Permission.EXPORT_ARTIFACT)
    projection = await checkpoint(context, document_id, revision_id)
    from app.storage.postgres_history import get_model_snapshot
    snapshot = await get_model_snapshot(str(revision_id), context=context)
    if snapshot:
        from collections import Counter
        counts = Counter(a["artifact_kind"] for a in artifacts)
        files = {(a["artifact_kind"] if counts[a["artifact_kind"]] == 1 else f"{a['artifact_kind']}:{a['filename']}"):
                 f"/api/documents/{document_id}/artifacts/{a['id']}" for a in artifacts
                 if can_export or a["artifact_kind"] in {"stl", "state"}}
        result = {**snapshot["result"], "files": files, "revision_id": str(revision_id),
                  "snapshot_id": snapshot["id"], "project_id": str(doc["project_id"]),
                  "branch_id": str(document_id), "change_set_id": str(revision["change_set_id"]) if revision["change_set_id"] else None}
        if revision["source_workflow_run_id"]:
            result.update(request_id=str(revision["source_workflow_run_id"]),
                          workflow_run_id=str(revision["source_workflow_run_id"]))
        if revision["validation_summary"]:
            result["validation"] = {**(result.get("validation") or {}), **dict(revision["validation_summary"])}
        snapshot = {**snapshot, "files": files, "result": result}
    return {"document_id": str(document_id), "project_id": str(doc["project_id"]),
            "head_revision_id": str(doc["head_revision_id"]), "head_state_version": doc["state_version"],
            **projection, **dict(revision), "snapshot": snapshot, "can_export": can_export}


async def document_events(context: PrincipalContext, document_id: UUID, after: int) -> list[dict]:
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        await authorized_document(conn, context, document_id)
        rows = (await conn.execute(text("SELECT sequence, event_type, payload FROM document_events WHERE document_id=:doc AND sequence>:after ORDER BY sequence LIMIT 100"),
                                   {"doc": document_id, "after": after})).mappings().all()
    events = []
    for row in rows:
        event = dict(row)
        if row["event_type"] == "state_delta":
            payload = dict(row["payload"])
            before = await checkpoint(context, document_id, UUID(payload["base_revision_id"]))
            after_state = await checkpoint(context, document_id, UUID(payload["revision_id"]))
            async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
                annotations = await annotation_context(conn, document_id, through_sequence=row["sequence"])
            before = annotate_projection(before, annotations)
            after_state = annotate_projection(after_state, annotations)
            event["payload"] = {**payload, **semantic_delta(before, after_state),
                **{k: v for k, v in after_state.items() if k not in {"features", "roots"}}}
        events.append(event)
    return events


async def collaboration_snapshot(context: PrincipalContext, document_id: UUID) -> dict:
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        await authorized_document(conn, context, document_id)
        operations = [dict(r) for r in (await conn.execute(text("""
            SELECT o.id, o.actor, o.actor_principal_id, o.action, o.base_revision_id, o.base_state_version,
                o.created_at, o.started_at, o.finished_at, o.error_code, o.arguments->>'objective' AS objective,
                COALESCE(c.candidate_revision_id, o.result_revision_id) AS result_revision_id,
                c.id AS change_set_id, CASE WHEN c.status IN ('committed', 'rejected', 'rolled_back') THEN c.status
                    WHEN c.status='changes_requested' THEN 'rejected' ELSE o.status END AS status
            FROM cad_operations o LEFT JOIN change_sets c ON c.source_workflow_run_id=o.id
            WHERE o.document_id=:doc ORDER BY o.queue_number DESC LIMIT 50
        """), {"doc": document_id})).mappings()]
        comments = [dict(r) for r in (await conn.execute(text("""
            SELECT c.id, c.principal_id, p.display_name, c.revision_id, c.feature_id, c.body, c.created_at
            FROM document_comments c JOIN principals p ON p.id=c.principal_id
            WHERE c.document_id=:doc ORDER BY c.created_at DESC, c.id LIMIT 100
        """), {"doc": document_id})).mappings()]
        presence = [dict(r) for r in (await conn.execute(text("""
            SELECT s.client_id, s.principal_id, p.display_name, s.selected_feature_id
            FROM document_presence s JOIN principals p ON p.id=s.principal_id
            WHERE s.document_id=:doc AND s.last_seen_at>CURRENT_TIMESTAMP-INTERVAL '45 seconds'
            ORDER BY s.client_id LIMIT 100
        """), {"doc": document_id})).mappings()]
        leases = [dict(r) for r in (await conn.execute(text("""SELECT l.feature_id,l.principal_id,p.display_name,l.expires_at
            FROM document_feature_leases l JOIN principals p ON p.id=l.principal_id
            WHERE l.document_id=:doc AND l.expires_at>CURRENT_TIMESTAMP ORDER BY l.feature_id"""),
            {"doc":document_id})).mappings()]
    return {"operations": operations, "comments": comments, "presence": presence, "leases": leases}


async def touch_presence(context: PrincipalContext, document_id: UUID, client_id: UUID, feature: UUID | None):
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        await authorized_document(conn, context, document_id)
        await conn.execute(text("DELETE FROM document_presence WHERE document_id=:doc AND last_seen_at<CURRENT_TIMESTAMP-INTERVAL '2 minutes'"), {"doc": document_id})
        result = await conn.execute(text("""INSERT INTO document_presence(tenant_id, document_id, client_id, principal_id, selected_feature_id)
            VALUES(:tenant, :doc, :client, :principal, :feature)
            ON CONFLICT(tenant_id, document_id, client_id) DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP,
                selected_feature_id=EXCLUDED.selected_feature_id
            WHERE document_presence.principal_id=EXCLUDED.principal_id RETURNING client_id"""),
            {"tenant": context.tenant_id, "doc": document_id, "client": client_id, "principal": context.principal_id, "feature": feature})
        if result.scalar_one_or_none() is None:
            raise PermissionError("在线会话属于其他用户")


async def add_comment(context: PrincipalContext, document_id: UUID, *, revision_id: UUID, body: str, feature_id: UUID | None, comment_id: UUID):
    snapshot = await checkpoint(context, document_id, revision_id)
    if feature_id and not any(f["id"] == str(feature_id) for f in snapshot["features"]):
        raise ValueError("特征不属于指定文档版本")
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        await authorized_document(conn, context, document_id, lock=True)
        previous = (await conn.execute(text("SELECT * FROM document_comments WHERE id=:id"), {"id": comment_id})).mappings().one_or_none()
        if previous:
            if (previous["document_id"], previous["principal_id"], previous["revision_id"], previous["feature_id"], previous["body"]) != (document_id, context.principal_id, revision_id, feature_id, body):
                raise DocumentConflict("评论 ID 已用于其他内容")
            return {"id": str(comment_id), "replayed": True}
        await conn.execute(text("""INSERT INTO document_comments(id, tenant_id, document_id, principal_id, revision_id, feature_id, body)
            VALUES(:id, :tenant, :doc, :principal, :revision, :feature, :body)"""),
            {"id": comment_id, "tenant": context.tenant_id, "doc": document_id, "principal": context.principal_id, "revision": revision_id, "feature": feature_id, "body": body})
        seq = await conn.scalar(text("UPDATE cloud_documents SET event_sequence=event_sequence+1 WHERE id=:doc RETURNING event_sequence"), {"doc": document_id})
        await conn.execute(text("""INSERT INTO document_events(tenant_id, document_id, sequence, event_type, payload)
            VALUES(:tenant, :doc, :seq, 'comment.created', CAST(:payload AS jsonb))"""),
            {"tenant": context.tenant_id, "doc": document_id, "seq": seq, "payload": json.dumps({"comment_id": str(comment_id)})})
    return {"id": str(comment_id), "replayed": False}
