"""Read immutable validation evidence without attributing old checks to new geometry."""
from uuid import UUID

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.projects import Permission
from app.execution.canonical import canonical_sha256
from app.services.event_relay import workflow_project_id


_REPORT_FIELDS = frozenset({
    'schema_version', 'outcome', 'artifact_kind', 'expected_dimensions_mm',
    'dimension_tolerance', 'artifacts', 'process', 'material', 'metrics',
    'evaluated_rule_ids', 'unevaluated_rule_ids', 'policy_hash', 'policy_object',
    'violations', 'issues', 'judgment',
    'acceptance', 'acceptance_contract', 'request_sha256',
})


def project_evidence(row: dict, revision: dict | None) -> dict:
    report = row['evidence']
    if canonical_sha256(report) != row['evidence_hash']:
        raise ValueError('检查证据完整性校验失败')
    manifest = revision['manifest'] if revision else {}
    gates = (manifest.get('validation') or {}).get('gates', [])
    selected = {str(item['staging_manifest_id']) for item in manifest.get('selected_manifests', [])}
    sealed = str(row['staging_manifest_id']) in selected and any(
        str(gate.get('evidence_id')) == str(row['id'])
        and gate.get('evidence_hash') == row['evidence_hash'] for gate in gates)
    return {
        'evidence_id': str(row['id']), 'evidence_hash': row['evidence_hash'],
        'workflow_run_id': str(row['workflow_run_id']),
        'staging_manifest_id': str(row['staging_manifest_id']),
        'revision_id': str(revision['id']) if revision and sealed else None,
        'selected_for_revision': sealed,
        'gate': row['gate'], 'mode': row['mode'], 'outcome': row['outcome'],
        'report': {key: value for key, value in report.items() if key in _REPORT_FIELDS},
    }


async def get_validation_evidence(principal, workflow_id: UUID, evidence_id: UUID) -> dict:
    await workflow_project_id(principal, workflow_id, permission=Permission.VIEW_PROJECT)
    async with tenant_transaction(principal.tenant_id, principal.principal_id) as connection:
        row = (await connection.execute(text('''
            SELECT * FROM agent_validation_evidence
            WHERE tenant_id=:tenant AND workflow_run_id=:workflow AND id=:evidence
        '''), {'tenant': principal.tenant_id, 'workflow': workflow_id, 'evidence': evidence_id})).mappings().one_or_none()
        if row is None:
            raise KeyError(evidence_id)
        revision = (await connection.execute(text('''
            SELECT r.id, r.manifest FROM change_sets c
            JOIN project_revisions r ON r.id=c.candidate_revision_id AND r.tenant_id=c.tenant_id
            WHERE c.tenant_id=:tenant AND c.source_workflow_run_id=:workflow
        '''), {'tenant': principal.tenant_id, 'workflow': workflow_id})).mappings().one_or_none()
    return project_evidence(dict(row), dict(revision) if revision else None)
