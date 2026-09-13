"""PostgreSQL implementation of sessions, revisions, feedback, and Onshape links."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text

from app.agent.assembly_manifest import enrich_assembly_parts
from app.db import tenant_transaction
from app.domain.identity import (
    IDENTITY_NAMESPACE,
    PrincipalContext,
    user_principal,
)
from app.execution.canonical import canonical_sha256
from app.freecad.state_contract import (
    project_state_parameters,
    read_verified_state_artifact,
)
from app.parameters import extract_parameters
from app.principal_context import current_principal
from app.repositories.identity import ensure_principal


def _context(user_id: str | None = None) -> PrincipalContext:
    return user_principal(user_id) if user_id else current_principal()


def _iso(value: datetime | str | None) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _stable_uuid(kind: str, context: PrincipalContext, value: str) -> UUID:
    return uuid5(
        IDENTITY_NAMESPACE,
        f"workspace:{kind}:{context.tenant_id}:{value}",
    )


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _source_parameters(source_code: str | None) -> list[dict] | None:
    if not source_code:
        return None
    parameters = [
        parameter.model_dump(mode="json")
        for parameter in extract_parameters(source_code)
    ]
    return parameters or None


def _durable_revision_projection(
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    if manifest.get("schema_version") == "mcad-agent-revision-manifest.v1":
        files, _ = _revision_artifact_projection(
            manifest.get("artifacts") or [],
            manifest.get("workflow_run_id"),
        )
        operation = str(manifest.get("operation") or "")
        return {
            "source": {"generate": "generation", "modify": "modify_part"}.get(operation, operation),
            "prompt": str(manifest.get("objective") or ""),
            "code": "",
            "parameters": None,
            "files": files,
            "available_exports": list(dict.fromkeys(
                key.split(":", 1)[0] for key in files
                if key.split(":", 1)[0] in {"fcstd", "step", "stl", "dxf", "svg"}
            )),
        }
    if manifest.get("schema_version") != "mcad-revision-manifest.v1":
        return None
    executions = [
        execution
        for execution in manifest.get("executions") or []
        if isinstance(execution, dict)
    ]
    if not executions:
        return {
            "source": "initial",
            "prompt": "项目初始化",
            "code": "",
            "parameters": None,
            "available_exports": [],
        }
    latest = executions[-1]
    operation = str(latest.get("operation") or "")
    source = {
        "generate": "generation",
        "modify": "modify_part",
        "execute": "execute_code",
        "analyze": "dfm",
    }.get(operation, operation or "execute_code")
    source_code = str(latest.get("source_code") or "")
    available_exports = list(dict.fromkeys(
        str(output.get("name"))
        for execution in executions
        for output in execution.get("outputs") or []
        if isinstance(output, dict) and output.get("name")
    ))
    return {
        "source": source,
        "prompt": str(manifest.get("objective") or ""),
        "code": source_code,
        "parameters": _source_parameters(source_code),
        "available_exports": available_exports,
    }


def _revision_artifact_projection(
    artifacts: list[Any],
    workflow_run_id: str | None = None,
) -> tuple[dict[str, str], dict[str, str] | None]:
    """Project authenticated URLs separately from immutable comparison identity."""
    rows = [dict(item) for item in artifacts if isinstance(item, dict) or hasattr(item, "keys")]
    counts = Counter(str(item.get("artifact_kind") or "") for item in rows)
    files: dict[str, str] = {}
    fingerprints: dict[str, str] = {}
    for item in rows:
        kind = str(item.get("artifact_kind") or "")
        filename = str(item.get("filename") or "")
        workflow = str(item.get("workflow_run_id") or workflow_run_id or "")
        if not kind or not filename or not workflow:
            continue
        key = kind if counts[kind] == 1 else f"{kind}:{filename}"
        if key in files:
            raise ValueError("revision has ambiguous artifact identity")
        files[key] = f"/api/files/{quote(workflow, safe='')}/{quote(filename, safe='')}"
        digest = str(item.get("sha256") or "")
        if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
            fingerprints[f"{kind}:{filename}"] = digest
    return files, fingerprints if files and len(fingerprints) == len(files) else None


def _session(row: Any) -> dict:
    return {
        "id": row["id"],
        "project_id": str(row["project_id"]),
        "user_id": row.get("legacy_user_id"),
        "title": row["title"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def create_session(
    session_id: str,
    title: str = "",
    user_id: str | None = None,
) -> dict:
    context = _context(user_id)
    project_id = _stable_uuid("project", context, session_id)
    slug = f"workspace-{hashlib.sha256(session_id.encode()).hexdigest()[:20]}"
    now = datetime.now(timezone.utc)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await ensure_principal(connection, context)
        await connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, tenant_id, name, slug, created_by_principal_id,
                    created_at, updated_at
                ) VALUES (
                    :id, :tenant, :name, :slug, :principal, :now, :now
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": project_id,
                "tenant": context.tenant_id,
                "name": title.strip() or "未命名 CAD 项目",
                "slug": slug,
                "principal": context.principal_id,
                "now": now,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO project_memberships (
                    tenant_id, project_id, principal_id, role
                ) VALUES (:tenant, :project, :principal, 'owner')
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "tenant": context.tenant_id,
                "project": project_id,
                "principal": context.principal_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO workspace_sessions (
                    id, tenant_id, project_id, created_by_principal_id,
                    legacy_user_id, title, created_at, updated_at
                ) VALUES (
                    :id, :tenant, :project, :principal,
                    :user_id, :title, :now, :now
                )
                ON CONFLICT (tenant_id, id) DO UPDATE
                SET legacy_user_id=COALESCE(
                        workspace_sessions.legacy_user_id,
                        EXCLUDED.legacy_user_id
                    ),
                    title=CASE
                        WHEN BTRIM(workspace_sessions.title)='' AND
                             BTRIM(EXCLUDED.title)<>''
                        THEN EXCLUDED.title
                        ELSE workspace_sessions.title
                    END,
                    updated_at=CURRENT_TIMESTAMP
                """
            ),
            {
                "id": session_id,
                "tenant": context.tenant_id,
                "project": project_id,
                "principal": context.principal_id,
                "user_id": user_id,
                "title": title,
                "now": now,
            },
        )
        row = (
            await connection.execute(
                text(
                    """
                    SELECT id, project_id, legacy_user_id, title,
                           created_at, updated_at
                    FROM workspace_sessions
                    WHERE tenant_id=:tenant AND id=:id
                    """
                ),
                {"tenant": context.tenant_id, "id": session_id},
            )
        ).mappings().one()
    return _session(row)


async def list_sessions(user_id: str | None = None) -> list[dict]:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, project_id, legacy_user_id, title,
                           created_at, updated_at
                    FROM workspace_sessions
                    ORDER BY updated_at DESC LIMIT 50
                    """
                )
            )
        ).mappings().all()
    return [_session(row) for row in rows]


async def session_belongs_to_user(
    session_id: str,
    user_id: str | None,
) -> bool:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        return bool(
            await connection.scalar(
                text(
                    "SELECT 1 FROM workspace_sessions "
                    "WHERE tenant_id=:tenant AND id=:id"
                ),
                {"tenant": context.tenant_id, "id": session_id},
            )
        )


async def session_writable_by_user(
    session_id: str,
    user_id: str | None,
) -> bool:
    # Tenant RLS makes another customer's identically named session invisible.
    return await session_belongs_to_user(session_id, user_id) or not await _session_exists(
        session_id,
        _context(user_id),
    )


async def _session_exists(
    session_id: str,
    context: PrincipalContext,
) -> bool:
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        return bool(
            await connection.scalar(
                text(
                    "SELECT 1 FROM workspace_sessions "
                    "WHERE tenant_id=:tenant AND id=:id"
                ),
                {"tenant": context.tenant_id, "id": session_id},
            )
        )


async def panel_writable_by_session(
    panel_id: str,
    session_id: str,
    user_id: str | None,
) -> bool:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        existing = await connection.scalar(
            text(
                "SELECT session_id FROM workspace_panels "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": context.tenant_id, "id": panel_id},
        )
    return existing is None or existing == session_id


async def panel_belongs_to_user(
    panel_id: str,
    user_id: str | None,
) -> bool:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        return bool(
            await connection.scalar(
                text(
                    "SELECT 1 FROM workspace_panels "
                    "WHERE tenant_id=:tenant AND id=:id"
                ),
                {"tenant": context.tenant_id, "id": panel_id},
            )
        )


async def delete_session(
    session_id: str,
    user_id: str | None = None,
) -> None:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        project_id = await connection.scalar(
            text(
                "SELECT project_id FROM workspace_sessions "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": context.tenant_id, "id": session_id},
        )
        if project_id:
            await connection.execute(
                text(
                    "DELETE FROM workspace_sessions "
                    "WHERE tenant_id=:tenant AND id=:session"
                ),
                {"tenant": context.tenant_id, "session": session_id},
            )
            await connection.execute(
                text(
                    """
                    UPDATE projects SET status='deleted',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=:tenant AND id=:project
                    """
                ),
                {"tenant": context.tenant_id, "project": project_id},
            )


async def touch_session(session_id: str) -> None:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await connection.execute(
            text(
                """
                UPDATE workspace_sessions
                SET updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=:tenant AND id=:id
                """
            ),
            {"tenant": context.tenant_id, "id": session_id},
        )


async def create_panel(
    session_id: str,
    panel_id: str,
    title: str = "",
    user_id: str | None = None,
) -> dict:
    context = _context(user_id)
    await create_session(session_id, user_id=user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        existing = await connection.scalar(
            text(
                "SELECT session_id FROM workspace_panels "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": context.tenant_id, "id": panel_id},
        )
        if existing is not None and existing != session_id:
            raise ValueError("panel_id already belongs to another session")
        await connection.execute(
            text(
                """
                INSERT INTO workspace_panels (
                    id, tenant_id, session_id, title
                ) VALUES (:id, :tenant, :session, :title)
                ON CONFLICT (tenant_id, id) DO NOTHING
                """
            ),
            {
                "id": panel_id,
                "tenant": context.tenant_id,
                "session": session_id,
                "title": title,
            },
        )
        row = (
            await connection.execute(
                text(
                    """
                    SELECT id, session_id, title, created_at
                    FROM workspace_panels
                    WHERE tenant_id=:tenant AND id=:id
                    """
                ),
                {"tenant": context.tenant_id, "id": panel_id},
            )
        ).mappings().one()
    return {**dict(row), "created_at": _iso(row["created_at"])}


async def list_panels(session_id: str) -> list[dict]:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT p.id, p.session_id, p.title, p.current_code,
                           p.created_at, s.project_id
                    FROM workspace_panels p
                    JOIN workspace_sessions s
                      ON s.tenant_id=p.tenant_id AND s.id=p.session_id
                    WHERE p.tenant_id=:tenant AND p.session_id=:session
                    ORDER BY p.created_at
                    """
                ),
                {"tenant": context.tenant_id, "session": session_id},
            )
        ).mappings().all()
        branch_name_by_panel = {
            row["id"]: (
                "panel-"
                + hashlib.sha256(row["id"].encode()).hexdigest()[:16]
            )
            for row in rows
        }
        branch_rows = []
        if branch_name_by_panel:
            branch_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT id, project_id, name, head_revision_id
                        FROM project_branches
                        WHERE tenant_id=:tenant
                          AND name = ANY(CAST(:branch_names AS text[]))
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "branch_names": list(
                            branch_name_by_panel.values()
                        ),
                    },
                )
            ).mappings().all()
        branches = {
            (branch["project_id"], branch["name"]): branch
            for branch in branch_rows
        }
        branch_ids = [str(row["id"]) for row in branch_rows]
        latest_workflows = {}
        latest_code = {}
        if branch_ids:
            workflow_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT DISTINCT ON (
                            request_payload->>'branch_id'
                        )
                               id, status, request_payload, created_at
                        FROM workflow_runs
                        WHERE tenant_id=:tenant
                          AND request_payload->>'branch_id'
                              = ANY(CAST(:branch_ids AS text[]))
                        ORDER BY request_payload->>'branch_id',
                                 created_at DESC
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "branch_ids": branch_ids,
                    },
                )
            ).mappings().all()
            latest_workflows = {
                row["request_payload"]["branch_id"]: row
                for row in workflow_rows
            }
            code_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT DISTINCT ON (
                            w.request_payload->>'branch_id'
                        )
                               w.request_payload->>'branch_id' AS branch_id,
                               COALESCE(
                                   source.payload->>'source_code',
                                   w.request_payload
                                     ->'primary'->>'source_code'
                               ) AS source_code
                        FROM workflow_runs w
                        LEFT JOIN LATERAL (
                            SELECT payload
                            FROM task_events
                            WHERE workflow_run_id=w.id
                              AND event_type='source.prepared'
                              AND payload->>'source_code' IS NOT NULL
                            ORDER BY sequence DESC
                            LIMIT 1
                        ) source ON TRUE
                        WHERE w.tenant_id=:tenant
                          AND w.request_payload->>'branch_id'
                              = ANY(CAST(:branch_ids AS text[]))
                          AND COALESCE(
                              source.payload->>'source_code',
                              w.request_payload
                                ->'primary'->>'source_code'
                          ) IS NOT NULL
                        ORDER BY w.request_payload->>'branch_id',
                                 w.created_at DESC
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "branch_ids": branch_ids,
                    },
                )
            ).mappings().all()
            latest_code = {
                row["branch_id"]: row["source_code"]
                for row in code_rows
            }
        result = []
        for row in rows:
            branch_name = branch_name_by_panel[row["id"]]
            branch = branches.get((row["project_id"], branch_name))
            branch_id = str(branch["id"]) if branch else None
            workflow = latest_workflows.get(branch_id)
            result.append({
                **dict(row),
                "current_code": (
                    latest_code.get(branch_id)
                    or row["current_code"]
                ),
                "project_id": str(row["project_id"]),
                "branch_id": branch_id,
                "current_revision_id": (
                    str(branch["head_revision_id"])
                    if branch and branch["head_revision_id"]
                    else None
                ),
                "active_workflow_run_id": (
                    str(workflow["id"]) if workflow else None
                ),
                "active_workflow_status": (
                    workflow["status"] if workflow else None
                ),
                "created_at": _iso(row["created_at"]),
            })
    return result


async def update_panel_code(
    panel_id: str,
    code: str | None,
    params: dict | None = None,
) -> None:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await connection.execute(
            text(
                """
                UPDATE workspace_panels
                SET current_code=:code, current_params=CAST(:params AS jsonb)
                WHERE tenant_id=:tenant AND id=:id
                """
            ),
            {
                "code": code,
                "params": _json(params) if params is not None else None,
                "tenant": context.tenant_id,
                "id": panel_id,
            },
        )


async def save_message(
    panel_id: str,
    role: str,
    content: str,
    result: dict | None = None,
) -> None:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await connection.execute(
            text(
                """
                INSERT INTO workspace_messages (
                    tenant_id, panel_id, role, content, result
                ) VALUES (
                    :tenant, :panel, :role, :content, CAST(:result AS jsonb)
                )
                """
            ),
            {
                "tenant": context.tenant_id,
                "panel": panel_id,
                "role": role,
                "content": content,
                "result": _json(result) if result is not None else None,
            },
        )


async def get_messages(panel_id: str) -> list[dict]:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT role, content, result, created_at
                    FROM workspace_messages
                    WHERE tenant_id=:tenant AND panel_id=:panel
                    ORDER BY id
                    """
                ),
                {"tenant": context.tenant_id, "panel": panel_id},
            )
        ).mappings().all()
        branch = (
            await connection.execute(
                text(
                    """
                    SELECT b.id, b.project_id
                    FROM workspace_panels p
                    JOIN workspace_sessions s
                      ON s.tenant_id=p.tenant_id
                     AND s.id=p.session_id
                    JOIN project_branches b
                      ON b.tenant_id=s.tenant_id
                     AND b.project_id=s.project_id
                     AND b.name=:branch_name
                    WHERE p.tenant_id=:tenant AND p.id=:panel
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "panel": panel_id,
                    "branch_name": (
                        "panel-"
                        + hashlib.sha256(
                            panel_id.encode()
                        ).hexdigest()[:16]
                    ),
                },
            )
        ).mappings().one_or_none()
        workflows = []
        artifacts_by_workflow: dict[UUID, list[dict]] = {}
        if branch is not None:
            workflows = (
                await connection.execute(
                    text(
                        """
                        SELECT w.id, w.project_id, w.kind, w.status,
                               w.request_payload, w.error_code,
                               w.error_message, w.created_at,
                               w.updated_at,
                               source.payload AS source_payload,
                               c.id AS change_set_id,
                               c.base_revision_id,
                               c.candidate_revision_id
                        FROM workflow_runs w
                        LEFT JOIN LATERAL (
                            SELECT payload
                            FROM task_events
                            WHERE workflow_run_id=w.id
                              AND event_type='source.prepared'
                            ORDER BY sequence DESC
                            LIMIT 1
                        ) source ON TRUE
                        LEFT JOIN change_sets c
                          ON c.source_workflow_run_id=w.id
                        WHERE w.tenant_id=:tenant
                          AND w.project_id=:project_id
                          AND w.request_payload->>'branch_id'=:branch_id
                        ORDER BY w.created_at
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "project_id": branch["project_id"],
                        "branch_id": str(branch["id"]),
                    },
                )
            ).mappings().all()
            workflow_ids = [row["id"] for row in workflows]
            if workflow_ids:
                artifact_rows = (
                    await connection.execute(
                        text(
                            """
                            SELECT workflow_run_id, artifact_kind, filename
                            FROM artifacts
                            WHERE workflow_run_id
                                  = ANY(CAST(:workflow_ids AS uuid[]))
                            ORDER BY created_at, filename
                            """
                        ),
                        {"workflow_ids": workflow_ids},
                    )
                ).mappings().all()
                for artifact in artifact_rows:
                    artifacts_by_workflow.setdefault(
                        artifact["workflow_run_id"],
                        [],
                    ).append(dict(artifact))

    entries = [
        {
            "role": row["role"],
            "content": row["content"],
            "_created_at": row["created_at"],
            **(
                {"result": row["result"]}
                if row["result"] is not None
                else {}
            ),
        }
        for row in rows
    ]
    represented_workflows = {
        str(entry["result"].get("workflow_run_id"))
        for entry in entries
        if isinstance(entry.get("result"), dict)
        and entry["result"].get("workflow_run_id")
    }
    terminal = {"succeeded", "failed", "cancelled", "timed_out"}
    for workflow in workflows:
        workflow_id = str(workflow["id"])
        if workflow_id in represented_workflows:
            continue
        request_payload = dict(workflow["request_payload"])
        objective = str(request_payload.get("objective") or "")
        source = dict(workflow["source_payload"] or {})
        primary = dict(request_payload.get("primary") or {})
        source_code = (
            source.get("source_code")
            or primary.get("source_code")
        )
        entries.append({
            "role": "user",
            "content": objective,
            "_created_at": workflow["created_at"],
        })
        waiting_for_source = (
            workflow["status"] == "waiting_confirmation"
            and workflow["change_set_id"] is None
            and source.get("needs_confirmation") is True
        )
        if workflow["status"] not in terminal and not waiting_for_source:
            continue
        files = {
            artifact["artifact_kind"]: (
                f"/api/files/{workflow_id}/{artifact['filename']}"
            )
            for artifact in artifacts_by_workflow.get(
                workflow["id"],
                [],
            )
        }
        success = workflow["status"] == "succeeded"
        result = {
            "request_id": workflow_id,
            "success": success,
            "needs_confirmation": waiting_for_source,
            "project_id": str(workflow["project_id"]),
            "branch_id": request_payload.get("branch_id"),
            "expected_base_revision_id": request_payload.get(
                "expected_base_revision_id"
            ),
            "revision_id": (
                str(workflow["candidate_revision_id"])
                if workflow["candidate_revision_id"]
                else None
            ),
            "workflow_run_id": workflow_id,
            "change_set_id": (
                str(workflow["change_set_id"])
                if workflow["change_set_id"]
                else None
            ),
            "task_status": workflow["status"],
            "files": files,
            "code": source_code,
            "parameters": _source_parameters(source_code),
            "plan": source.get("plan"),
            "design_brief": source.get("design_brief"),
            "manufacturing_profile": source.get(
                "manufacturing_profile"
            ),
            "error": (
                None
                if success or waiting_for_source
                else {
                    "type": (
                        workflow["error_code"]
                        or "WorkflowFailed"
                    ),
                    "message": (
                        workflow["error_message"]
                        or "持久任务执行失败"
                    ),
                }
            ),
        }
        entries.append({
            "role": "assistant",
            "content": (
                "设计简报需要确认"
                if waiting_for_source
                else "CAD 模型已生成"
                if success
                else "CAD 任务执行失败"
            ),
            "result": result,
            "_created_at": workflow["updated_at"],
        })
    entries.sort(key=lambda item: item["_created_at"])
    return [
        {
            key: value
            for key, value in entry.items()
            if key != "_created_at"
        }
        for entry in entries
    ]


def _manifest(
    panel_id: str,
    result: dict,
    source: str,
    prompt: str,
    parent_snapshot_id: str | None,
) -> dict:
    return {
        "schema": "workspace-model-revision-v1",
        "panel_id": panel_id,
        "requested_parent_snapshot_id": parent_snapshot_id,
        "source": source,
        "prompt": prompt,
        "code": result.get("code") or "",
        "result": result,
        "files": result.get("files") or {},
        "params": result.get("params"),
        "parameters": result.get("parameters"),
        "validation": result.get("validation"),
        "inspect_report": result.get("inspect_report"),
        "repair_history": result.get("repair_history") or [],
        "status": _snapshot_status(result),
    }


def _snapshot_status(result: dict) -> str:
    if not result.get("success"):
        return "fail"
    verdict = (result.get("inspect_report") or {}).get("verdict")
    return verdict if verdict in {"pass", "warn", "fail"} else "unknown"


def _snapshot_from_revision(
    row: Any,
    *,
    snapshot_id: str | None = None,
    metadata: Any | None = None,
) -> dict:
    manifest = dict(row["manifest"])
    details = metadata or {}
    durable = _durable_revision_projection(manifest) or {}
    inspect_report = manifest.get("inspect_report")
    workflow_status = row.get("workflow_status")
    durable_status = (
        "pass"
        if workflow_status == "succeeded"
        else "fail"
        if workflow_status in {"failed", "cancelled", "timed_out"}
        else "unknown"
    )
    result = manifest.get("result") or {
        "success": workflow_status == "succeeded",
        "code": durable.get("code") or "",
        "parameters": durable.get("parameters"),
        "files": durable.get("files") or {},
        "validation": manifest.get("validation"),
    }
    assembly_parts = result.get("assembly_parts") or []
    projected_parts = (
        enrich_assembly_parts(assembly_parts) if assembly_parts else []
    )
    if projected_parts:
        result = {**result, "assembly_parts": projected_parts}
    return {
        "id": snapshot_id or str(row["id"]),
        "project_id": str(row["project_id"]),
        "branch_id": str(row["branch_id"]),
        "revision_id": str(row["id"]),
        "panel_id": manifest.get("panel_id") or details.get("panel_id"),
        "parent_snapshot_id": (
            str(row["parent_revision_id"])
            if row["parent_revision_id"]
            else manifest.get("legacy_parent_snapshot_id")
        ),
        "version": details.get("legacy_version") or row["revision_number"],
        "source": (
            details.get("source")
            or manifest.get("source")
            or durable.get("source")
            or "legacy"
        ),
        "prompt": (
            details.get("prompt")
            or manifest.get("prompt")
            or durable.get("prompt")
            or ""
        ),
        "code": manifest.get("code") or durable.get("code") or "",
        "result": result,
        "files": manifest.get("files") or durable.get("files") or {},
        "params": manifest.get("params"),
        "parameters": (
            manifest.get("parameters")
            or durable.get("parameters")
        ),
        "validation": manifest.get("validation"),
        "inspect_report": inspect_report,
        "repair_history": manifest.get("repair_history") or [],
        "status": (
            details.get("status")
            or manifest.get("status")
            or manifest.get("legacy_status")
            or durable_status
        ),
        "created_at": _iso(row["created_at"]),
        "available_exports": (
            (inspect_report or {}).get("available_exports")
            or durable.get("available_exports")
            or []
        ),
        "inspect_verdict": (inspect_report or {}).get("verdict"),
        "assembly_parts": projected_parts,
    }


async def _panel_project(connection, tenant_id: UUID, panel_id: str):
    return (
        await connection.execute(
            text(
                """
                SELECT s.project_id
                FROM workspace_panels p
                JOIN workspace_sessions s
                  ON s.tenant_id=p.tenant_id AND s.id=p.session_id
                WHERE p.tenant_id=:tenant AND p.id=:panel
                """
            ),
            {"tenant": tenant_id, "panel": panel_id},
        )
    ).mappings().one_or_none()


async def _panel_id_for_revision(
    connection,
    tenant_id: UUID,
    project_id: UUID,
    branch_id: UUID,
) -> str | None:
    branch_name = await connection.scalar(
        text(
            """
            SELECT name
            FROM project_branches
            WHERE tenant_id=:tenant AND id=:branch
            """
        ),
        {"tenant": tenant_id, "branch": branch_id},
    )
    if not branch_name:
        return None
    candidates = (
        await connection.execute(
            text(
                """
                SELECT p.id
                FROM workspace_panels p
                JOIN workspace_sessions s
                  ON s.tenant_id=p.tenant_id AND s.id=p.session_id
                WHERE p.tenant_id=:tenant AND s.project_id=:project
                """
            ),
            {"tenant": tenant_id, "project": project_id},
        )
    ).mappings().all()
    for row in candidates:
        panel_id = str(row["id"])
        candidate_branch_name = "panel-" + hashlib.sha256(panel_id.encode("utf-8")).hexdigest()[:16]
        if candidate_branch_name == branch_name:
            return panel_id
    return None


async def _workflow_files(connection, tenant_id: UUID, workflow_run_id: UUID | None) -> dict[str, str]:
    if workflow_run_id is None:
        return {}
    rows = (
        await connection.execute(
            text(
                """
                SELECT artifact_kind, filename
                FROM artifacts
                WHERE tenant_id=:tenant AND workflow_run_id=:workflow
                ORDER BY created_at, filename
                """
            ),
            {"tenant": tenant_id, "workflow": workflow_run_id},
        )
    ).mappings().all()
    files: dict[str, str] = {}
    for artifact in rows:
        kind = artifact.get("artifact_kind")
        filename = artifact.get("filename")
        if not kind or not filename:
            continue
        files[str(kind)] = f"/api/files/{workflow_run_id}/{filename}"
    return files


async def create_model_snapshot(
    panel_id: str,
    result: dict,
    source: str,
    prompt: str = "",
    parent_snapshot_id: str | None = None,
) -> dict:
    context = current_principal()
    revision_id = uuid4()
    branch_id = _stable_uuid("panel-branch", context, panel_id)
    manifest = _manifest(
        panel_id,
        result,
        source,
        prompt,
        parent_snapshot_id,
    )
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        panel = await _panel_project(connection, context.tenant_id, panel_id)
        if panel is None:
            raise ValueError("panel not found")
        branch = (
            await connection.execute(
                text(
                    """
                    SELECT head_revision_id, next_revision_number
                    FROM project_branches
                    WHERE tenant_id=:tenant AND id=:branch
                    FOR UPDATE
                    """
                ),
                {"tenant": context.tenant_id, "branch": branch_id},
            )
        ).mappings().one_or_none()
        if branch is None:
            revision_number = 1
            parent_revision_id = None
            await connection.execute(
                text(
                    """
                    INSERT INTO project_branches (
                        id, tenant_id, project_id, name,
                        next_revision_number, created_by_principal_id
                    ) VALUES (
                        :id, :tenant, :project, :name, 2, :principal
                    )
                    """
                ),
                {
                    "id": branch_id,
                    "tenant": context.tenant_id,
                    "project": panel["project_id"],
                    "name": "panel-"
                    + hashlib.sha256(panel_id.encode()).hexdigest()[:16],
                    "principal": context.principal_id,
                },
            )
            kind = "initial"
        else:
            revision_number = int(branch["next_revision_number"])
            parent_revision_id = branch["head_revision_id"]
            kind = "candidate"
        await connection.execute(
            text(
                """
                INSERT INTO project_revisions (
                    id, tenant_id, project_id, branch_id,
                    parent_revision_id, revision_number, kind,
                    content_hash, manifest, created_by_principal_id
                ) VALUES (
                    :id, :tenant, :project, :branch, :parent, :number,
                    :kind, :hash, CAST(:manifest AS jsonb), :principal
                )
                """
            ),
            {
                "id": revision_id,
                "tenant": context.tenant_id,
                "project": panel["project_id"],
                "branch": branch_id,
                "parent": parent_revision_id,
                "number": revision_number,
                "kind": kind,
                "hash": canonical_sha256(manifest),
                "manifest": _json(manifest),
                "principal": context.principal_id,
            },
        )
        await connection.execute(
            text(
                """
                UPDATE project_branches
                SET head_revision_id=:revision,
                    next_revision_number=:next,
                    updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=:tenant AND id=:branch
                """
            ),
            {
                "revision": revision_id,
                "next": revision_number + 1,
                "tenant": context.tenant_id,
                "branch": branch_id,
            },
        )
        await connection.execute(
            text(
                """
                UPDATE workspace_panels
                SET current_code=:code, current_params=CAST(:params AS jsonb)
                WHERE tenant_id=:tenant AND id=:panel
                """
            ),
            {
                "code": manifest["code"],
                "params": (
                    _json(manifest["params"])
                    if manifest["params"] is not None
                    else None
                ),
                "tenant": context.tenant_id,
                "panel": panel_id,
            },
        )
        row = (
            await connection.execute(
                text("SELECT * FROM project_revisions WHERE id=:id"),
                {"id": revision_id},
            )
        ).mappings().one()
    return _snapshot_from_revision(row)


async def list_model_snapshots(panel_id: str) -> list[dict]:
    context = current_principal()
    branch_name = (
        "panel-" + hashlib.sha256(panel_id.encode()).hexdigest()[:16]
    )
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        branch_id = await connection.scalar(
            text(
                """
                SELECT b.id
                FROM workspace_panels p
                JOIN workspace_sessions s
                  ON s.tenant_id=p.tenant_id AND s.id=p.session_id
                JOIN project_branches b
                  ON b.tenant_id=s.tenant_id
                 AND b.project_id=s.project_id
                 AND b.name=:branch_name
                WHERE p.tenant_id=:tenant AND p.id=:panel
                """
            ),
            {
                "tenant": context.tenant_id,
                "panel": panel_id,
                "branch_name": branch_name,
            },
        )
        live = (
            await connection.execute(
                text(
                    """
                    SELECT r.*, w.status AS workflow_status
                    FROM project_revisions r
                    LEFT JOIN change_sets c
                      ON c.tenant_id=r.tenant_id
                     AND c.candidate_revision_id=r.id
                    LEFT JOIN workflow_runs w
                      ON w.tenant_id=c.tenant_id
                     AND w.id=c.source_workflow_run_id
                    WHERE r.tenant_id=:tenant AND r.branch_id=:branch
                    ORDER BY revision_number DESC
                    """
                ),
                {"tenant": context.tenant_id, "branch": branch_id},
            )
        ).mappings().all() if branch_id else []
        imported = (
            await connection.execute(
                text(
                    """
                    SELECT r.*, m.legacy_snapshot_id, m.panel_id,
                           m.legacy_version, m.source, m.prompt, m.status
                    FROM legacy_snapshot_mappings m
                    JOIN project_revisions r
                      ON r.tenant_id=m.tenant_id
                     AND r.project_id=m.project_id
                     AND r.id=m.revision_id
                    WHERE m.tenant_id=:tenant AND m.panel_id=:panel
                    ORDER BY m.legacy_version DESC
                    """
                ),
                {"tenant": context.tenant_id, "panel": panel_id},
            )
        ).mappings().all()
    snapshots = [_snapshot_from_revision(row) for row in live]
    snapshots.extend(
        _snapshot_from_revision(
            row,
            snapshot_id=row["legacy_snapshot_id"],
            metadata=row,
        )
        for row in imported
    )
    return sorted(
        snapshots,
        key=lambda item: (item["created_at"], item["version"]),
        reverse=True,
    )


async def get_model_snapshot(
    snapshot_id: str,
    *,
    include_artifact_fingerprints: bool = False,
    context=None,
) -> dict | None:
    context = context or current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        try:
            revision_id = UUID(snapshot_id)
        except ValueError:
            revision_id = None
        row = None
        if revision_id is not None:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT r.*, w.status AS workflow_status
                        FROM project_revisions r
                        LEFT JOIN change_sets c
                          ON c.tenant_id=r.tenant_id
                         AND c.candidate_revision_id=r.id
                        LEFT JOIN workflow_runs w
                          ON w.tenant_id=c.tenant_id
                         AND w.id=c.source_workflow_run_id
                        WHERE r.tenant_id=:tenant AND r.id=:id
                        """
                    ),
                    {"tenant": context.tenant_id, "id": revision_id},
                )
            ).mappings().one_or_none()
        imported = None
        if row is None:
            imported = (
                await connection.execute(
                    text(
                        """
                        SELECT r.*, m.legacy_snapshot_id, m.panel_id,
                               m.legacy_version, m.source, m.prompt, m.status
                        FROM legacy_snapshot_mappings m
                        JOIN project_revisions r
                          ON r.tenant_id=m.tenant_id
                         AND r.project_id=m.project_id
                         AND r.id=m.revision_id
                        WHERE m.tenant_id=:tenant
                          AND m.legacy_snapshot_id=:snapshot
                        """
                    ),
                    {"tenant": context.tenant_id, "snapshot": snapshot_id},
                )
            ).mappings().one_or_none()
            row = imported
        if row is None:
            return None
        artifact_rows = (
            await connection.execute(
                text(
                    """
                    SELECT artifact_kind, filename, workflow_run_id,
                           size_bytes, sha256, object_key
                    FROM artifacts a
                    WHERE tenant_id=:tenant AND revision_id=:revision
                      AND NOT EXISTS(SELECT 1 FROM workflow_runs w WHERE w.id=a.workflow_run_id AND w.kind='mcad.scene')
                    ORDER BY created_at, filename
                    """
                ),
                {"tenant": context.tenant_id, "revision": row["id"]},
            )
        ).mappings().all()
        snapshot = _snapshot_from_revision(
            row,
            snapshot_id=snapshot_id if imported is not None else None,
            metadata=imported,
        )
        if not snapshot.get("panel_id"):
            snapshot["panel_id"] = await _panel_id_for_revision(
                connection,
                context.tenant_id,
                row["project_id"],
                row["branch_id"],
            )
        # Revision-bound artifacts are authoritative. Older history may only
        # carry a source workflow; preserve its download projection as fallback.
        if not artifact_rows:
            workflow_files = await _workflow_files(
                connection,
                context.tenant_id,
                row.get("source_workflow_run_id"),
            )
            if workflow_files:
                files = {**snapshot["files"], **workflow_files}
                snapshot["files"] = files
                snapshot["result"] = {**dict(snapshot["result"] or {}), "files": files}
    if artifact_rows:
        files, fingerprints = _revision_artifact_projection(artifact_rows)
        snapshot["files"] = files
        snapshot["result"] = {**dict(snapshot["result"] or {}), "files": files}
        if include_artifact_fingerprints:
            snapshot["_file_fingerprints"] = fingerprints
    state_artifacts = [
        dict(artifact)
        for artifact in artifact_rows
        if artifact["artifact_kind"] == "state"
    ]
    if len(state_artifacts) == 1:
        state, state_sha256 = await read_verified_state_artifact(
            state_artifacts,
        )
        parameters = [
            parameter.model_dump(mode="json")
            for parameter in project_state_parameters(state)
        ]
        snapshot["parameters"] = parameters or None
        snapshot["result"] = {
            **dict(snapshot["result"] or {}),
            "parameters": parameters or None,
            "parameter_state_sha256": state_sha256,
        }
    return snapshot


async def snapshot_belongs_to_user(
    snapshot_id: str,
    user_id: str | None,
) -> bool:
    context = _context(user_id)
    # Bind explicitly for callers that do not pass through an HTTP dependency.
    from app.principal_context import bind_principal

    bind_principal(context)
    return await get_model_snapshot(snapshot_id) is not None


def _restored_snapshot_result(snapshot: dict) -> dict:
    restored_result = dict(snapshot["result"] or {})
    restored_result["snapshot_id"] = snapshot["id"]
    restored_result["version"] = snapshot["version"]
    restored_result["panel_id"] = snapshot["panel_id"]
    if not restored_result.get("files"):
        restored_result["files"] = snapshot.get("files") or {}
    for field in (
        "params",
        "parameters",
        "validation",
        "inspect_report",
        "repair_history",
        "assembly_parts",
        "available_exports",
        "inspect_verdict",
    ):
        if not restored_result.get(field):
            value = snapshot.get(field)
            if value is not None:
                restored_result[field] = value
    if not restored_result.get("status") and snapshot.get("status") is not None:
        restored_result["status"] = snapshot["status"]
    return restored_result


async def restore_model_snapshot(
    snapshot_id: str,
    user_id: str | None = None,
) -> dict | None:
    context = _context(user_id)
    from app.principal_context import bind_principal

    bind_principal(context)
    snapshot = await get_model_snapshot(snapshot_id)
    if snapshot is None:
        return None
    target_revision_id: UUID | None
    try:
        target_revision_id = UUID(snapshot_id)
    except ValueError:
        target_revision_id = None
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        if target_revision_id is None:
            target_revision_id = await connection.scalar(
                text(
                    """
                    SELECT revision_id FROM legacy_snapshot_mappings
                    WHERE tenant_id=:tenant
                      AND legacy_snapshot_id=:snapshot
                    """
                ),
                {"tenant": context.tenant_id, "snapshot": snapshot_id},
            )
        target = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM project_revisions
                    WHERE tenant_id=:tenant AND id=:id
                    """
                ),
                {"tenant": context.tenant_id, "id": target_revision_id},
            )
        ).mappings().one_or_none()
        if target is None:
            return None
        panel_id = snapshot["panel_id"]
        if panel_id is None:
            panel_id = await _panel_id_for_revision(
                connection,
                context.tenant_id,
                target["project_id"],
                target["branch_id"],
            )
            if panel_id is None:
                return None
        branch = (
            await connection.execute(
                text(
                    """
                    SELECT head_revision_id, next_revision_number
                    FROM project_branches
                    WHERE tenant_id=:tenant AND id=:branch
                    FOR UPDATE
                    """
                ),
                {"tenant": context.tenant_id, "branch": target["branch_id"]},
            )
        ).mappings().one()
        revision_id = uuid4()
        manifest = {
            **dict(target["manifest"]),
            "schema": "workspace-restored-revision-v1",
            "restored_from_revision_id": str(target_revision_id),
            "panel_id": panel_id,
        }
        await connection.execute(
            text(
                """
                INSERT INTO project_revisions (
                    id, tenant_id, project_id, branch_id,
                    parent_revision_id, revision_number, kind,
                    content_hash, manifest, created_by_principal_id
                ) VALUES (
                    :id, :tenant, :project, :branch, :parent, :number,
                    'rollback', :hash, CAST(:manifest AS jsonb), :principal
                )
                """
            ),
            {
                "id": revision_id,
                "tenant": context.tenant_id,
                "project": target["project_id"],
                "branch": target["branch_id"],
                "parent": branch["head_revision_id"],
                "number": branch["next_revision_number"],
                "hash": canonical_sha256(manifest),
                "manifest": _json(manifest),
                "principal": context.principal_id,
            },
        )
        await connection.execute(
            text(
                """
                UPDATE project_branches SET head_revision_id=:revision,
                    next_revision_number=:next,
                    updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=:tenant AND id=:branch
                """
            ),
            {
                "revision": revision_id,
                "next": int(branch["next_revision_number"]) + 1,
                "tenant": context.tenant_id,
                "branch": target["branch_id"],
            },
        )
        await connection.execute(
            text(
                """
                UPDATE workspace_panels SET current_code=:code,
                    current_params=CAST(:params AS jsonb)
                WHERE tenant_id=:tenant AND id=:panel
                """
            ),
            {
                "code": snapshot["code"],
                "params": (
                    _json(snapshot["params"])
                    if snapshot["params"] is not None
                    else None
                ),
                "tenant": context.tenant_id,
                "panel": panel_id,
            },
        )
        restored_result = _restored_snapshot_result(snapshot)
        restored_result["revision_id"] = str(revision_id)
        restored_result["project_id"] = str(target["project_id"])
        restored_result["branch_id"] = str(target["branch_id"])
        await connection.execute(
            text(
                """
                INSERT INTO workspace_messages (
                    tenant_id, panel_id, role, content, result
                ) VALUES (
                    :tenant, :panel, 'assistant', :content,
                    CAST(:result AS jsonb)
                )
                """
            ),
            {
                "tenant": context.tenant_id,
                "panel": panel_id,
                "content": f"已恢复模型版本 v{snapshot['version']}",
                "result": _json(restored_result),
            },
        )
    snapshot["result"] = restored_result
    return snapshot


async def _request_project(connection, tenant_id: UUID, request_id: str):
    return await connection.scalar(
        text(
            """
            SELECT project_id FROM project_files
            WHERE tenant_id=:tenant AND request_id=:request
            ORDER BY created_at DESC LIMIT 1
            """
        ),
        {"tenant": tenant_id, "request": request_id},
    )


async def save_feedback(
    request_id: str,
    rating: str | None = None,
    printed: str | None = None,
    note: str | None = None,
) -> dict:
    context = current_principal()
    feedback_id = uuid4()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        project_id = await _request_project(
            connection,
            context.tenant_id,
            request_id,
        )
        row = (
            await connection.execute(
                text(
                    """
                    INSERT INTO product_feedback (
                        id, tenant_id, principal_id, project_id,
                        request_id, rating, printed, note
                    ) VALUES (
                        :id, :tenant, :principal, :project,
                        :request, :rating, :printed, :note
                    )
                    RETURNING request_id, rating, printed, note, created_at
                    """
                ),
                {
                    "id": feedback_id,
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "project": project_id,
                    "request": request_id,
                    "rating": rating,
                    "printed": printed,
                    "note": note,
                },
            )
        ).mappings().one()
    return {**dict(row), "created_at": _iso(row["created_at"])}


async def get_feedback(request_id: str) -> list[dict]:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT request_id, rating, printed, note, created_at
                    FROM product_feedback
                    WHERE tenant_id=:tenant AND request_id=:request
                    ORDER BY created_at
                    """
                ),
                {"tenant": context.tenant_id, "request": request_id},
            )
        ).mappings().all()
    return [
        {**dict(row), "created_at": _iso(row["created_at"])}
        for row in rows
    ]


def _link(row: Any, user_id: str | None = None) -> dict:
    return {
        **{
            key: row[key]
            for key in (
                "id",
                "request_id",
                "status",
                "document_id",
                "workspace_id",
                "element_id",
                "translation_id",
                "document_name",
                "mode",
            )
            if key in row
        },
        "user_id": user_id,
        "onshape_url": row["external_url"],
        "step_filename": row["source_filename"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def save_onshape_link(
    request_id: str,
    user_id: str | None,
    document_id: str,
    workspace_id: str,
    element_id: str | None,
    translation_id: str | None,
    status: str,
    onshape_url: str,
    document_name: str = "",
    step_filename: str = "",
    mode: str = "import_step",
    raw_response: dict | None = None,
) -> dict:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        project_id = await _request_project(
            connection,
            context.tenant_id,
            request_id,
        )
        row = (
            await connection.execute(
                text(
                    """
                    INSERT INTO connector_links (
                        tenant_id, principal_id, project_id, connector,
                        request_id, document_id, workspace_id, element_id,
                        translation_id, status, external_url, document_name,
                        source_filename, mode, raw_response
                    ) VALUES (
                        :tenant, :principal, :project, 'onshape',
                        :request, :document, :workspace, :element,
                        :translation, :status, :url, :name,
                        :filename, :mode, CAST(:raw AS jsonb)
                    )
                    RETURNING *
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "project": project_id,
                    "request": request_id,
                    "document": document_id,
                    "workspace": workspace_id,
                    "element": element_id,
                    "translation": translation_id,
                    "status": status,
                    "url": onshape_url,
                    "name": document_name,
                    "filename": step_filename,
                    "mode": mode,
                    "raw": (
                        _json(raw_response)
                        if raw_response is not None
                        else None
                    ),
                },
            )
        ).mappings().one()
    return _link(row, user_id)


async def get_onshape_links(
    request_id: str,
    user_id: str | None = None,
) -> list[dict]:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM connector_links
                    WHERE tenant_id=:tenant AND connector='onshape'
                      AND request_id=:request
                    ORDER BY updated_at DESC, id DESC
                    """
                ),
                {"tenant": context.tenant_id, "request": request_id},
            )
        ).mappings().all()
    return [_link(row, user_id) for row in rows]


async def get_latest_onshape_link(
    request_id: str,
    user_id: str | None = None,
) -> dict | None:
    rows = await get_onshape_links(request_id, user_id)
    return rows[0] if rows else None


async def get_onshape_link_by_translation(
    translation_id: str,
    user_id: str | None = None,
) -> dict | None:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM connector_links
                    WHERE tenant_id=:tenant AND connector='onshape'
                      AND translation_id=:translation
                    ORDER BY updated_at DESC, id DESC LIMIT 1
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "translation": translation_id,
                },
            )
        ).mappings().one_or_none()
    return _link(row, user_id) if row else None


async def update_onshape_link_status(
    link_id: int,
    status: str,
    element_id: str | None,
    onshape_url: str,
    raw_response: dict | None = None,
) -> dict | None:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        row = (
            await connection.execute(
                text(
                    """
                    UPDATE connector_links
                    SET status=:status,
                        element_id=COALESCE(:element, element_id),
                        external_url=:url,
                        raw_response=COALESCE(
                            CAST(:raw AS jsonb),
                            raw_response
                        ),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=:tenant AND id=:id
                    RETURNING *
                    """
                ),
                {
                    "status": status,
                    "element": element_id,
                    "url": onshape_url,
                    "raw": (
                        _json(raw_response)
                        if raw_response is not None
                        else None
                    ),
                    "tenant": context.tenant_id,
                    "id": link_id,
                },
            )
        ).mappings().one_or_none()
    return _link(row) if row else None
