"""Deterministic public-operation and trusted-base resolution.

This module is deliberately side-effect free. Database/object-store discovery
produces a ``RevisionSourceInventory`` first; these functions then make the
single decision that is persisted in the durable workflow request.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.identity import PrincipalContext
from app.domain.projects import Permission
from app.object_store import sha256_object
from app.repositories.projects import principal_has_permission
from app.workflows.temporal import OperationContextV1


BaseSourceKind = Literal[
    "fcstd_artifact",
    "agent_generated_source",
    "revision_manifest_source",
]


_MESSAGES = {
    "modify_code_required": "CadQuery modification requires source code.",
    "modify_code_not_allowed": "FreeCAD modification does not accept source code.",
    "modify_base_fcstd_missing": "The base revision has no editable FCStd artifact.",
    "modify_base_fcstd_ambiguous": "The base revision has multiple FCStd artifacts.",
    "modify_base_source_missing": "The base revision has no editable model source.",
    "modify_base_source_ambiguous": "The base revision has multiple editable model sources.",
    "modify_source_code_mismatch": "The supplied source does not match the committed revision.",
    "new_conversation_required": "Start a new conversation to generate a different model.",
}


class OperationResolutionError(ValueError):
    """Stable, public pre-workflow failure."""

    def __init__(
        self,
        code: str,
        *,
        details: dict[str, str | int | bool | list[str] | list[int]] | None = None,
    ) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(_MESSAGES[code])

    def public_error(self) -> dict[str, object]:
        return {
            "type": self.code,
            "message": str(self),
            "details": self.details,
            "retryable": False,
        }


@dataclass(frozen=True, slots=True)
class TrustedBaseSource:
    kind: BaseSourceKind
    source_id: UUID | None
    sha256: str
    code: str | None = None


@dataclass(frozen=True, slots=True)
class RevisionSourceInventory:
    fcstd: tuple[TrustedBaseSource, ...] = ()
    cadquery: tuple[TrustedBaseSource, ...] = ()


@dataclass(frozen=True, slots=True)
class SubmissionResolution:
    operation: Literal["generate", "modify"]
    modeling_backend: Literal["auto", "freecad", "cadquery"]
    existing_code: str | None
    operation_context: OperationContextV1


def _base_details(base_revision_id: UUID) -> dict[str, str]:
    return {"base_revision_id": str(base_revision_id)}


def _one_fcstd(
    inventory: RevisionSourceInventory,
    base_revision_id: UUID,
    *,
    missing_code: str,
) -> TrustedBaseSource:
    if len(inventory.fcstd) > 1:
        raise OperationResolutionError(
            "modify_base_fcstd_ambiguous",
            details=_base_details(base_revision_id),
        )
    if not inventory.fcstd:
        raise OperationResolutionError(
            missing_code,
            details=_base_details(base_revision_id),
        )
    return inventory.fcstd[0]


def resolve_rest_generate_submission(
    *,
    base_revision_id: UUID,
    output_formats: list[str],
) -> SubmissionResolution:
    backend: Literal["auto", "cadquery"] = (
        "cadquery"
        if set(output_formats).issubset({"dxf", "svg"})
        else "auto"
    )
    return SubmissionResolution(
        operation="generate",
        modeling_backend=backend,
        existing_code=None,
        operation_context=OperationContextV1(
            rule="explicit_rest_operation",
            source_channel="rest",
            panel_id=None,
            requested_operation="generate",
            resolved_operation="generate",
            requested_modeling_backend=None,
            submission_modeling_backend=backend,
            base_revision_id=base_revision_id,
            base_source_kind="none",
            base_source_id=None,
            base_source_sha256=None,
        ),
    )


def resolve_rest_modify_submission(
    *,
    requested_backend: Literal["auto", "freecad", "cadquery"],
    base_revision_id: UUID,
    inventory: RevisionSourceInventory,
    request_code: str | None,
) -> SubmissionResolution:
    """Resolve the exact public REST modify matrix before workflow creation."""
    if requested_backend == "cadquery" and request_code is None:
        raise OperationResolutionError(
            "modify_code_required",
            details=_base_details(base_revision_id),
        )
    if requested_backend == "freecad" and request_code is not None:
        raise OperationResolutionError(
            "modify_code_not_allowed",
            details=_base_details(base_revision_id),
        )

    if request_code is not None:
        source_sha = hashlib.sha256(request_code.encode("utf-8")).hexdigest()
        backend: Literal["freecad", "cadquery"] = "cadquery"
        source_kind = "request_code"
        source_id = None
        existing_code = request_code
    else:
        source = _one_fcstd(
            inventory,
            base_revision_id,
            missing_code="modify_base_fcstd_missing",
        )
        backend = "freecad"
        source_kind = "fcstd_artifact"
        source_id = source.source_id
        source_sha = source.sha256
        existing_code = None

    return SubmissionResolution(
        operation="modify",
        modeling_backend=backend,
        existing_code=existing_code,
        operation_context=OperationContextV1(
            rule="explicit_rest_operation",
            source_channel="rest",
            panel_id=None,
            requested_operation="modify",
            resolved_operation="modify",
            requested_modeling_backend=requested_backend,
            submission_modeling_backend=backend,
            base_revision_id=base_revision_id,
            base_source_kind=source_kind,
            base_source_id=source_id,
            base_source_sha256=source_sha,
        ),
    )


def resolve_browser_submission(
    *,
    requested_operation: Literal["generate", "modify"] | None,
    rule: Literal[
        "explicit_modify_part",
        "explicit_parameter_edit",
        "explicit_ui_intent",
        "legacy_editable_base_present",
        "legacy_empty_panel",
    ],
    panel_id: str,
    base_revision_id: UUID,
    inventory: RevisionSourceInventory,
    browser_code: str | None,
    generation_backend: Literal["auto", "cadquery"] = "auto",
) -> SubmissionResolution:
    """Resolve browser intent only from the authorized persisted base."""
    if len(inventory.fcstd) > 1:
        raise OperationResolutionError(
            "modify_base_fcstd_ambiguous",
            details=_base_details(base_revision_id),
        )
    if not inventory.fcstd and len(inventory.cadquery) > 1:
        raise OperationResolutionError(
            "modify_base_source_ambiguous",
            details=_base_details(base_revision_id),
        )

    source = (
        inventory.fcstd[0]
        if inventory.fcstd
        else (inventory.cadquery[0] if inventory.cadquery else None)
    )
    if requested_operation == "generate" and source is not None:
        raise OperationResolutionError(
            "new_conversation_required",
            details=_base_details(base_revision_id),
        )
    operation: Literal["generate", "modify"] = (
        requested_operation
        if requested_operation is not None
        else ("modify" if source is not None else "generate")
    )

    if operation == "generate":
        return SubmissionResolution(
            operation="generate",
            modeling_backend=generation_backend,
            existing_code=None,
            operation_context=OperationContextV1(
                rule=rule,
                source_channel="session_websocket",
                panel_id=panel_id,
                requested_operation=requested_operation,
                resolved_operation="generate",
                requested_modeling_backend=None,
                submission_modeling_backend=generation_backend,
                base_revision_id=base_revision_id,
                base_source_kind="none",
                base_source_id=None,
                base_source_sha256=None,
            ),
        )

    if source is None:
        raise OperationResolutionError(
            "modify_base_source_missing",
            details=_base_details(base_revision_id),
        )
    if source.kind == "fcstd_artifact":
        backend: Literal["freecad", "cadquery"] = "freecad"
        existing_code = None
    else:
        backend = "cadquery"
        if source.code is None:
            raise OperationResolutionError(
                "modify_base_source_missing",
                details=_base_details(base_revision_id),
            )
        if browser_code is not None and hashlib.sha256(
            browser_code.encode("utf-8")
        ).hexdigest() != source.sha256:
            raise OperationResolutionError(
                "modify_source_code_mismatch",
                details=_base_details(base_revision_id),
            )
        existing_code = source.code

    return SubmissionResolution(
        operation="modify",
        modeling_backend=backend,
        existing_code=existing_code,
        operation_context=OperationContextV1(
            rule=rule,
            source_channel="session_websocket",
            panel_id=panel_id,
            requested_operation=requested_operation,
            resolved_operation="modify",
            requested_modeling_backend=None,
            submission_modeling_backend=backend,
            base_revision_id=base_revision_id,
            base_source_kind=source.kind,
            base_source_id=source.source_id,
            base_source_sha256=source.sha256,
        ),
    )


def _valid_source_hash(source_code: str, source_hash: str) -> bool:
    return hashlib.sha256(source_code.encode("utf-8")).hexdigest() == source_hash


async def load_revision_source_inventory(
    principal: PrincipalContext,
    *,
    project_id: UUID,
    revision_id: UUID,
) -> RevisionSourceInventory:
    """Load integrity-verified editable sources owned by one authorized revision."""
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
        revision = (
            await connection.execute(
                text(
                    """
                    SELECT manifest
                    FROM project_revisions
                    WHERE tenant_id=:tenant_id
                      AND project_id=:project_id
                      AND id=:revision_id
                    """
                ),
                {
                    "tenant_id": principal.tenant_id,
                    "project_id": project_id,
                    "revision_id": revision_id,
                },
            )
        ).mappings().one_or_none()
        if revision is None:
            raise KeyError(revision_id)
        manifest = dict(revision["manifest"] or {})
        artifact_rows = [
            dict(row)
            for row in (
                await connection.execute(
                    text(
                        """
                        SELECT id, artifact_kind, object_key, size_bytes, sha256
                        FROM artifacts
                        WHERE tenant_id=:tenant_id
                          AND project_id=:project_id
                          AND revision_id=:revision_id
                          AND lower(artifact_kind)='fcstd'
                        ORDER BY created_at, id
                        """
                    ),
                    {
                        "tenant_id": principal.tenant_id,
                        "project_id": project_id,
                        "revision_id": revision_id,
                    },
                )
            ).mappings().all()
        ]

        staging_rows: list[dict] = []
        source_rows: list[dict] = []
        if manifest.get("schema_version") == "mcad-agent-revision-manifest.v1":
            selected = list(manifest.get("selected_manifests") or ())
            selected_ids: list[UUID] = []
            for item in selected:
                try:
                    selected_ids.append(UUID(str(item["staging_manifest_id"])))
                except (KeyError, TypeError, ValueError):
                    continue
            if selected_ids:
                staging_rows = [
                    dict(row)
                    for row in (
                        await connection.execute(
                            text(
                                """
                                SELECT id, candidate_build_id, workflow_run_id,
                                       status, manifest_hash, manifest
                                FROM agent_staging_manifests
                                WHERE tenant_id=:tenant_id AND id=ANY(:ids)
                                """
                            ),
                            {
                                "tenant_id": principal.tenant_id,
                                "ids": selected_ids,
                            },
                        )
                    ).mappings().all()
                ]
                source_ids: list[UUID] = []
                for row in staging_rows:
                    try:
                        source_ids.append(UUID(str(dict(row["manifest"])["source_id"])))
                    except (KeyError, TypeError, ValueError):
                        continue
                if source_ids:
                    source_rows = [
                        dict(row)
                        for row in (
                            await connection.execute(
                                text(
                                    """
                                    SELECT id, candidate_build_id, workflow_run_id,
                                           source_hash, source_code
                                    FROM agent_generated_sources
                                    WHERE tenant_id=:tenant_id AND id=ANY(:ids)
                                    """
                                ),
                                {
                                    "tenant_id": principal.tenant_id,
                                    "ids": source_ids,
                                },
                            )
                        ).mappings().all()
                    ]

    valid_fcstd: list[TrustedBaseSource] = []
    for artifact in artifact_rows:
        try:
            measured = await sha256_object(str(artifact["object_key"]))
        except Exception:
            continue
        if (
            int(measured["size_bytes"]) == int(artifact["size_bytes"])
            and str(measured["sha256"]) == str(artifact["sha256"])
        ):
            valid_fcstd.append(
                TrustedBaseSource(
                    kind="fcstd_artifact",
                    source_id=artifact["id"],
                    sha256=str(artifact["sha256"]),
                )
            )

    cadquery_by_identity: dict[tuple[UUID | None, str], TrustedBaseSource] = {}
    if manifest.get("schema_version") == "mcad-agent-revision-manifest.v1":
        selected_by_id = {
            str(item.get("staging_manifest_id")): item
            for item in manifest.get("selected_manifests") or ()
            if isinstance(item, dict)
        }
        sources_by_id = {str(row["id"]): row for row in source_rows}
        for row in staging_rows:
            supplied = selected_by_id.get(str(row["id"]))
            staged = dict(row.get("manifest") or {})
            source = sources_by_id.get(str(staged.get("source_id")))
            if (
                supplied is None
                or source is None
                or row.get("status") != "accepted"
                or row.get("manifest_hash") != supplied.get("manifest_hash")
                or str(row.get("candidate_build_id"))
                != str(manifest.get("candidate_build_id"))
                or str(row.get("workflow_run_id"))
                != str(manifest.get("workflow_run_id"))
                or source.get("candidate_build_id") != row.get("candidate_build_id")
                or source.get("workflow_run_id") != row.get("workflow_run_id")
                or source.get("source_hash") != staged.get("source_hash")
            ):
                continue
            source_code = str(source.get("source_code") or "")
            source_hash = str(source.get("source_hash") or "")
            if not source_code or not _valid_source_hash(source_code, source_hash):
                continue
            source_id = UUID(str(source["id"]))
            cadquery_by_identity[(source_id, source_hash)] = TrustedBaseSource(
                kind="agent_generated_source",
                source_id=source_id,
                sha256=source_hash,
                code=source_code,
            )
    elif manifest.get("schema_version") == "mcad-revision-manifest.v1":
        for execution in reversed(list(manifest.get("executions") or ())):
            if not isinstance(execution, dict) or not execution.get("outputs"):
                continue
            source_code = str(execution.get("source_code") or "")
            source_hash = str(execution.get("source_sha256") or "")
            if source_code and _valid_source_hash(source_code, source_hash):
                cadquery_by_identity[(None, source_hash)] = TrustedBaseSource(
                    kind="revision_manifest_source",
                    source_id=None,
                    sha256=source_hash,
                    code=source_code,
                )
            break

    return RevisionSourceInventory(
        fcstd=tuple(valid_fcstd),
        cadquery=tuple(cadquery_by_identity.values()),
    )
