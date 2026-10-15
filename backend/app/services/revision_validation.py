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


async def revision_geometry_baseline(connection, document_id: UUID, revision_id: UUID) -> dict | None:
    """Read the sealed revision's independent measurements, never model labels."""
    manifest = await connection.scalar(text('SELECT manifest FROM project_revisions WHERE id=:rev AND branch_id=:doc'),
                                      {'rev': revision_id, 'doc': document_id})
    if not manifest:
        return None
    selected = {m['staging_manifest_id'] for m in manifest.get('selected_manifests', [])}
    gates = [g for g in (manifest.get('validation') or {}).get('gates', []) if g.get('gate') == 'geometry']
    # An assembly needs part-specific identities, not a guessed aggregate value.
    if len(gates) != 1:
        return None
    gate = gates[0]
    row = (await connection.execute(text("SELECT * FROM agent_validation_evidence WHERE id=:id AND gate='geometry'"),
                                    {'id': UUID(gate['evidence_id'])})).mappings().one_or_none()
    if row is None or str(row['staging_manifest_id']) not in selected:
        raise ValueError('Geometry baseline does not belong to the selected revision')
    if row['evidence_hash'] != gate['evidence_hash'] or canonical_sha256(row['evidence']) != row['evidence_hash']:
        raise ValueError('Geometry baseline evidence integrity mismatch')
    report = row['evidence']
    contract, measurements = report.get('acceptance_contract'), report.get('acceptance')
    if row['outcome'] != 'passed' or not contract or not measurements:
        return None
    from app.contracts.acceptance_v2 import decode_acceptance
    from app.contracts.acceptance import AcceptanceMeasurements, acceptance_outcome
    parsed = decode_acceptance(contract)
    measured = AcceptanceMeasurements.model_validate(measurements)
    if measured.contract_sha256 != parsed.digest() or acceptance_outcome(parsed, measured.evidence) != 'passed':
        raise ValueError('Geometry baseline is not bound to its sealed acceptance contract')
    by_id = {c.check_id: c for c in parsed.checks}
    return {'revision_id': str(revision_id), 'evidence_id': str(row['id']),
        'evidence_sha256': row['evidence_hash'], 'acceptance_digest': parsed.digest(),
        'artifact_sha256s': [a['sha256'] for a in report.get('artifacts', [])],
        'measurements': [{'kind': by_id[r.check_id].kind, 'scope': by_id[r.check_id].scope.model_dump(mode='json'),
                          'check_id': r.check_id, 'values': list(r.measured)}
                         for r in measured.evidence if r.outcome == 'passed' and by_id[r.check_id].required]}
