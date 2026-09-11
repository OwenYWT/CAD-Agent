"""Trace L0 dimensions/DFM and feature history through a real Agent workflow.

Run inside the isolated API container: raw owner subject, engineering Agent
workflow ID. Uses actual Temporal results, immutable PostgreSQL evidence and S3
bytes; never fabricates a provider response or inserts completed workflow data.
"""
import asyncio
import hashlib
import json
import math
import sys
from uuid import UUID

from sqlalchemy import text

from app.db import close_database, tenant_transaction
from app.domain.identity import user_principal
from app.freecad.semantic_state import bounded_agent_context
from app.object_store import get_object
from app.services.cloud_documents import checkpoint
from app.services.revision_validation import revision_dfm_summary
from app.workflows.temporal import get_temporal_client, temporal_agent_v2_workflow_id


async def main():
    owner = user_principal(sys.argv[1])
    workflow_id = UUID(sys.argv[2])
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        operation = (await conn.execute(text("SELECT * FROM cad_operations WHERE id=:id"),
                                       {"id": workflow_id})).mappings().one()
        assert operation["result_revision_id"] is not None
        artifact = (await conn.execute(text("""SELECT * FROM artifacts
            WHERE revision_id=:id AND artifact_kind='state'"""),
            {"id": operation["base_revision_id"]})).mappings().one()
        expected_dfm = await revision_dfm_summary(conn, operation["document_id"], operation["base_revision_id"])
    payload = await get_object(artifact["object_key"])
    assert len(payload) == artifact["size_bytes"]
    assert hashlib.sha256(payload).hexdigest() == artifact["sha256"]
    native_state = json.loads(payload)
    temporal = await get_temporal_client()
    history = await temporal.get_workflow_handle(temporal_agent_v2_workflow_id(workflow_id)).fetch_history()
    witnessed = []
    for event in history.events:
        if not event.HasField("activity_task_completed_event_attributes"):
            continue
        results = await temporal.data_converter.decode(event.activity_task_completed_event_attributes.result.payloads)
        for result in results:
            if not isinstance(result, dict) or not result.get("engineering_context"):
                continue
            state, provenance = result["base_state"], result["engineering_context"]
            assert all(state[key] == value for key, value in native_state.items())
            assert state["revision_id"] == str(operation["base_revision_id"])
            assert state["dfm_summary"] == expected_dfm
            context = bounded_agent_context(state)
            encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            assert hashlib.sha256(encoded).hexdigest() == provenance["context_sha256"]
            assert provenance["provider"] not in {None, "mock", "deterministic"}
            assert provenance["request_hash"] and provenance["response_hash"]
            summary = context["summary"]
            assert summary["dimensions_status"] == "measured"
            assert all(math.isfinite(v) and v > 0 for v in summary["main_dimensions_mm"].values())
            assert summary["key_features"] and summary["dfm"]["revision_id"] == state["revision_id"]
            witnessed.append({"provider": provenance["provider"], "model": provenance["model"],
                              "context_sha256": provenance["context_sha256"], "summary": summary})
    assert witnessed, "no recorded real provider context matched verified native state"
    before = await checkpoint(owner, operation["document_id"], operation["base_revision_id"])
    after = await checkpoint(owner, operation["document_id"], operation["result_revision_id"])
    old = {f["id"]: f for f in before["features"]}
    assert old and old.keys() <= {f["id"] for f in after["features"]}
    changed, unchanged, bindings = [], [], 0
    for feature in after["features"]:
        previous = old.get(feature["id"])
        if previous:
            assert feature["revision_created"] == previous["revision_created"]
            if feature["content_sha256"] == previous["content_sha256"]:
                assert feature["last_modified"] == previous["last_modified"]
                unchanged.append(feature["kernel_name"])
            else:
                assert feature["last_modified"] == str(operation["result_revision_id"])
                changed.append(feature["kernel_name"])
        for binding in feature["topology_bindings"]:
            assert binding["revision_id"] == str(operation["result_revision_id"])
            assert binding["object_name"] == feature["kernel_name"]
            assert binding["geometry"] in {"planar", "circular"}
            assert "subelement_name" not in binding
            bindings += 1
    assert changed and unchanged and bindings
    print("CAD_SEMANTIC_PROVENANCE=" + json.dumps({
        "workflow_id": str(workflow_id), "verified_state_sha256": artifact["sha256"],
        "provider_contexts": witnessed, "changed_features": changed,
        "unchanged_features": unchanged, "current_revision_bindings": bindings,
        "feature_creation_history_preserved": True,
    }), flush=True)
    await close_database()


asyncio.run(main())
