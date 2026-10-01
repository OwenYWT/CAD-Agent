"""Server-owned, integrity-checked inputs for incremental FreeCAD tool turns.

The Agent never chooses an object-store key or a checkpoint belonging to another
candidate. Accepted manifests are the durable boundary, not a model's claim that
an operation succeeded.
"""
from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import text
from temporalio.exceptions import ApplicationError

from app.db import tenant_transaction
from app.execution.canonical import canonical_sha256
from app.object_store import get_object


def rejected(message):
    return ApplicationError(message, type="freecad_checkpoint_rejected", non_retryable=True)


async def checkpoint_manifest(request, candidate_build_id, checkpoint_id):
    async with tenant_transaction(request.tenant_id, request.principal_id) as conn:
        row = (await conn.execute(text("""
            SELECT manifest, manifest_hash FROM agent_staging_manifests
            WHERE tenant_id=:tenant AND candidate_build_id=:candidate
              AND workflow_run_id=:workflow AND id=:id AND status='accepted'
        """), {"tenant": request.tenant_id, "candidate": candidate_build_id,
               "workflow": request.workflow_run_id, "id": UUID(str(checkpoint_id))})).mappings().one_or_none()
    if row is None:
        raise rejected("checkpoint is not accepted by this workflow and candidate")
    manifest = dict(row["manifest"])
    if (canonical_sha256(manifest) != row["manifest_hash"]
            or manifest.get("base_revision_id") != str(request.expected_base_revision_id)
            or manifest.get("modeling_backend") != "freecad"
            or manifest.get("execution_mode") != "checkpoint"):
        raise rejected("checkpoint identity, base revision or execution mode does not match")
    return manifest


def checkpoint_artifact(manifest, kind):
    matches = [item for item in manifest["outputs"] if item["format"] == kind]
    if len(matches) != 1:
        raise rejected(f"checkpoint requires exactly one {kind} artifact")
    return matches[0]


async def checkpoint_context(request, candidate_build_id, checkpoint_id, original_state=None):
    manifest = await checkpoint_manifest(request, candidate_build_id, checkpoint_id)
    artifact = checkpoint_artifact(manifest, "state")
    raw = await get_object(artifact["object_key"])
    if len(raw) != artifact["size_bytes"] or hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
        raise rejected("checkpoint state bytes do not match the accepted manifest")
    state = json.loads(raw)
    if not isinstance(state, dict) or state.get("schema_version") not in {"freecad-state.v1", "freecad-state.v2"}:
        raise rejected("checkpoint has an unsupported document state")
    # Selection/annotations are frozen user inputs. Prior engineering results
    # describe the old geometry and must not be copied to a changed checkpoint.
    for key in ("selection_context", "feature_annotations"):
        if original_state and key in original_state:
            state[key] = original_state[key]
    state["revision_id"] = str(request.expected_base_revision_id)
    return state, {
        "status": "executed", "checkpoint_id": str(checkpoint_id),
        "source_id": manifest["source_id"], "source_hash": manifest["source_hash"],
        "state_sha256": artifact["sha256"],
        "engineering_validation": "pending", "saved_revision": None,
    }
