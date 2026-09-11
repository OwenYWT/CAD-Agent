"""Bound Agent DFM context to the exact revision's selected, sealed evidence."""
from uuid import UUID

from sqlalchemy import text

from app.execution.canonical import canonical_sha256


async def revision_dfm_summary(connection, document_id: UUID, revision_id: UUID) -> dict:
    unavailable = {"status": "unavailable", "revision_id": str(revision_id),
                   "reason": "no_verified_dfm_evidence_for_revision"}
    manifest = await connection.scalar(text("SELECT manifest FROM project_revisions WHERE id=:rev AND branch_id=:doc"),
        {"rev": revision_id, "doc": document_id})
    if not manifest:
        return unavailable
    gates = [g for g in (manifest.get("validation") or {}).get("gates", []) if g.get("gate") == "dfm"]
    selected = {m["staging_manifest_id"] for m in manifest.get("selected_manifests", [])}
    if len(gates) != 1:
        return unavailable
    gate = gates[0]
    row = (await connection.execute(text("SELECT * FROM agent_validation_evidence WHERE id=:id AND gate='dfm'"),
        {"id": UUID(gate["evidence_id"])})).mappings().one_or_none()
    if row is None or str(row["staging_manifest_id"]) not in selected:
        raise ValueError("DFM evidence is not bound to the revision's selected model")
    if row["evidence_hash"] != gate["evidence_hash"] or canonical_sha256(row["evidence"]) != row["evidence_hash"]:
        raise ValueError("DFM evidence integrity mismatch")
    report = row["evidence"]
    return {"status": row["outcome"], "revision_id": str(revision_id), "evidence_id": str(row["id"]),
            "policy_hash": report.get("policy_hash"), "issues": list(report.get("issues") or [])[:12],
            "violations": list(report.get("violations") or [])[:12]}
