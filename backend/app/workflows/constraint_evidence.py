"""Private, source-bound constraint evidence; never a staging manifest."""
from __future__ import annotations

import hashlib
import json
import logging
from uuid import UUID

from sqlalchemy import text

from app.db import tenant_transaction
from app.config import settings
from app.object_store import get_object, put_object
from app.repositories.runs import append_workflow_event

logger = logging.getLogger(__name__)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def evidence_key(request, sha256):
    if len(sha256) != 64 or any(c not in '0123456789abcdef' for c in sha256):
        raise ValueError('invalid constraint evidence digest')
    return f'tenants/{request.tenant_id}/workflow-evidence/{request.workflow_run_id}/{sha256}.json'


async def persist_failure_snapshot(request, payload, attempt_id, error, operation_plan, checkpoint_hash, *, validate_snapshot):
    """Storage errors preserve the original execution failure, never a false success."""
    if error is None or 'failure_snapshot_json' not in error.details:
        return error
    details = dict(error.details)
    raw = details.pop('failure_snapshot_json')
    claimed = details.pop('failure_snapshot_sha256', None)
    try:
        if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != claimed:
            raise ValueError('failure diagnostic bytes failed integrity verification')
        snapshot = validate_snapshot(json.loads(raw), operation_plan.model_dump(mode='json'),
                                     checkpoint_hash, error.operation_id)
        envelope = {'schema_version': 'constraint-evidence.v1',
            'workflow_run_id': str(request.workflow_run_id), 'execution_attempt_id': str(attempt_id),
            'source_id': str(payload['source_id']), 'source_hash': str(payload['source_hash']),
            'snapshot': snapshot}
        encoded = canonical(envelope).encode()
        sha256 = hashlib.sha256(encoded).hexdigest()
        stored = await put_object(evidence_key(request, sha256), encoded, content_type='application/json')
        if stored['sha256'] != sha256:
            raise ValueError('stored failure diagnostic failed integrity verification')
        async with tenant_transaction(request.tenant_id, request.principal_id) as conn:
            await append_workflow_event(conn, tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id, event_type='agent.freecad.constraint_diagnostic',
                payload={k: envelope[k] for k in ('source_id', 'source_hash', 'execution_attempt_id')} |
                        {'sha256': sha256, 'valid_checkpoint': False})
        details.update(constraint_diagnostic_sha256=sha256, constraint_diagnostic_attempt_id=str(attempt_id),
                       constraint_repair_max_attempts=settings.constraint_repair_max_attempts)
        if error.code == 'profile_geometry_invalid':
            details['profile_replan_max_attempts'] = settings.profile_replan_max_attempts
    except Exception as exc:
        logger.warning('Constraint diagnostic unavailable for attempt %s (%s)', attempt_id, type(exc).__name__)
        details['failure_snapshot_unavailable'] = type(exc).__name__
    return error.model_copy(update={'details': details})


async def load_failure_snapshot(request, *, source_id, source_hash, failure):
    sha256 = (failure.get('details') or {}).get('constraint_diagnostic_sha256')
    if not sha256:
        return None
    # A client/model-provided object path is never accepted. Membership in this
    # workflow's immutable evidence events is checked before reading any bytes.
    async with tenant_transaction(request.tenant_id, request.principal_id) as conn:
        row = (await conn.execute(text('''SELECT payload FROM task_events
            WHERE tenant_id=:tenant AND workflow_run_id=:workflow
              AND event_type='agent.freecad.constraint_diagnostic'
              AND payload->>'sha256'=:hash AND payload->>'source_id'=:source
              AND payload->>'source_hash'=:source_hash
            ORDER BY sequence DESC LIMIT 1'''), {'tenant': request.tenant_id,
            'workflow': request.workflow_run_id, 'hash': sha256, 'source': str(source_id),
            'source_hash': source_hash})).mappings().one_or_none()
    if row is None:
        raise ValueError('constraint diagnostic is not owned by this task and source')
    raw = await get_object(evidence_key(request, sha256))
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError('constraint diagnostic integrity check failed')
    envelope = json.loads(raw)
    if (envelope['workflow_run_id'] != str(request.workflow_run_id)
            or envelope['source_id'] != str(source_id) or envelope['source_hash'] != source_hash
            or envelope['execution_attempt_id'] != (failure.get('execution_attempt_id') or
                (failure.get('details') or {}).get('constraint_diagnostic_attempt_id'))):
        raise ValueError('constraint diagnostic has a stale execution identity')
    return envelope


async def persist_repair_contract(connection, request, *, source_id, source_hash, contract):
    """Commit ownership alongside the generated source, never a candidate artifact."""
    envelope = {'schema_version': 'constraint-contract-evidence.v1',
        'workflow_run_id': str(request.workflow_run_id), 'source_id': str(source_id),
        'source_hash': source_hash, 'contract': contract}
    raw = canonical(envelope).encode()
    sha256 = hashlib.sha256(raw).hexdigest()
    stored = await put_object(evidence_key(request, sha256), raw, content_type='application/json')
    if stored['sha256'] != sha256:
        raise ValueError('stored constraint contract failed integrity verification')
    await append_workflow_event(connection, tenant_id=request.tenant_id,
        workflow_id=request.workflow_run_id, event_type='agent.freecad.constraint_patch',
        payload={'source_id': str(source_id), 'source_hash': source_hash, 'sha256': sha256,
                 'acceptance_hash': contract['acceptance_hash'], 'patch_count': len(contract['patches'])})


async def persist_repair_verification(request, *, source_id, source_hash, attempt_id, report):
    # Candidate sealing consumes temporary staging objects. Retain native
    # relationship/perturbation evidence independently for later audit.
    envelope = {'schema_version': 'constraint-verification-evidence.v1',
        'workflow_run_id': str(request.workflow_run_id), 'source_id': str(source_id),
        'source_hash': source_hash, 'execution_attempt_id': str(attempt_id), 'report': report}
    raw = canonical(envelope).encode()
    sha256 = hashlib.sha256(raw).hexdigest()
    stored = await put_object(evidence_key(request, sha256), raw, content_type='application/json')
    if stored['sha256'] != sha256:
        raise ValueError('native repair verification storage integrity failure')
    async with tenant_transaction(request.tenant_id, request.principal_id) as conn:
        await append_workflow_event(conn, tenant_id=request.tenant_id, workflow_id=request.workflow_run_id,
            event_type='agent.freecad.constraint_verified', payload={'source_id': str(source_id),
                'source_hash': source_hash, 'execution_attempt_id': str(attempt_id),
                'sha256': sha256, 'contract_hash': report['contract_hash'],
                'parameter_probes': len(report['parameter_probes'])})


async def load_repair_contract(request, *, source_id, source_hash):
    async with tenant_transaction(request.tenant_id, request.principal_id) as conn:
        source = (await conn.execute(text('''SELECT generator_kind, source_hash
            FROM agent_generated_sources WHERE tenant_id=:tenant AND workflow_run_id=:workflow AND id=:source'''),
            {'tenant': request.tenant_id, 'workflow': request.workflow_run_id,
             'source': UUID(str(source_id))})).mappings().one_or_none()
        if source is None or source['source_hash'] != source_hash:
            raise ValueError('repair contract source is not owned by this task')
        row = (await conn.execute(text('''SELECT payload FROM task_events
            WHERE tenant_id=:tenant AND workflow_run_id=:workflow
              AND event_type='agent.freecad.constraint_patch'
              AND payload->>'source_id'=:source AND payload->>'source_hash'=:source_hash
            ORDER BY sequence DESC LIMIT 1'''), {'tenant': request.tenant_id,
            'workflow': request.workflow_run_id, 'source': str(source_id),
            'source_hash': source_hash})).mappings().one_or_none()
    if row is None:
        if 'constraint_patch' in source['generator_kind']:
            raise ValueError('constraint repair proof is missing; execution is forbidden')
        return None
    sha256 = row['payload']['sha256']
    raw = await get_object(evidence_key(request, sha256))
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError('constraint repair proof integrity check failed')
    envelope = json.loads(raw)
    if (envelope.get('schema_version') != 'constraint-contract-evidence.v1'
            or envelope['workflow_run_id'] != str(request.workflow_run_id)
            or envelope['source_id'] != str(source_id) or envelope['source_hash'] != source_hash):
        raise ValueError('constraint repair proof ownership mismatch')
    return envelope['contract']


async def persist_profile_contract(connection, request, *, source_id, source_hash, contract):
    envelope = {'workflow_run_id': str(request.workflow_run_id), 'source_id': str(source_id),
                'source_hash': source_hash, 'contract': contract}
    raw = canonical(envelope).encode()
    sha256 = hashlib.sha256(raw).hexdigest()
    stored = await put_object(evidence_key(request, sha256), raw, content_type='application/json')
    if stored['sha256'] != sha256:
        raise ValueError('profile replan proof storage integrity failure')
    await append_workflow_event(connection, tenant_id=request.tenant_id, workflow_id=request.workflow_run_id,
        event_type='agent.freecad.profile_replan', payload={'source_id': str(source_id), 'source_hash': source_hash,
            'sha256': sha256, 'acceptance_hash': contract['acceptance_hash'], 'sketch': contract['sketch']})


async def load_profile_contract(request, *, source_id, source_hash):
    async with tenant_transaction(request.tenant_id, request.principal_id) as conn:
        source = (await conn.execute(text('''SELECT generator_kind,source_hash FROM agent_generated_sources
            WHERE tenant_id=:tenant AND workflow_run_id=:workflow AND id=:source'''),
            {'tenant': request.tenant_id, 'workflow': request.workflow_run_id, 'source': UUID(str(source_id))})).mappings().one_or_none()
        if source is None or source['source_hash'] != source_hash:
            raise ValueError('profile proof source is not owned by this task')
        row = (await conn.execute(text('''SELECT payload FROM task_events WHERE tenant_id=:tenant
            AND workflow_run_id=:workflow AND event_type='agent.freecad.profile_replan'
            AND payload->>'source_id'=:source AND payload->>'source_hash'=:source_hash ORDER BY sequence DESC LIMIT 1'''),
            {'tenant': request.tenant_id, 'workflow': request.workflow_run_id,
             'source': str(source_id), 'source_hash': source_hash})).mappings().one_or_none()
    if row is None:
        if 'profile_replan' in source['generator_kind']:
            raise ValueError('profile replan proof is missing; execution is forbidden')
        return None
    raw = await get_object(evidence_key(request, row['payload']['sha256']))
    if hashlib.sha256(raw).hexdigest() != row['payload']['sha256']:
        raise ValueError('profile replan proof integrity failure')
    envelope = json.loads(raw)
    if (envelope['workflow_run_id'] != str(request.workflow_run_id) or envelope['source_id'] != str(source_id)
            or envelope['source_hash'] != source_hash):
        raise ValueError('profile replan proof ownership mismatch')
    return envelope['contract']


async def persist_profile_verification(request, *, source_id, source_hash, attempt_id, report):
    raw = canonical({'source_id': str(source_id), 'source_hash': source_hash,
        'execution_attempt_id': str(attempt_id), 'report': report}).encode()
    sha256 = hashlib.sha256(raw).hexdigest()
    stored = await put_object(evidence_key(request, sha256), raw, content_type='application/json')
    if stored['sha256'] != sha256:
        raise ValueError('profile replan verification storage integrity failure')
    async with tenant_transaction(request.tenant_id, request.principal_id) as conn:
        await append_workflow_event(conn, tenant_id=request.tenant_id, workflow_id=request.workflow_run_id,
            event_type='agent.freecad.profile_verified', payload={'source_id': str(source_id),
                'source_hash': source_hash, 'execution_attempt_id': str(attempt_id), 'sha256': sha256,
                'contract_hash': report['contract_hash'], 'parameter_probes': len(report['parameter_probes'])})
