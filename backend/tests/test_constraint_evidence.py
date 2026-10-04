"""Fault injection at storage boundaries; no fabricated CAD success evidence."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.execution.contracts import ExecutionError
from app.freecad.failure_snapshot import canonical, digest, validate_snapshot
from app.workflows import constraint_evidence
from tests.test_constraint_patch import context_and_patch


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_mode', ['unavailable', 'bad_integrity'])
async def test_diagnostic_storage_failure_preserves_original_model_error(monkeypatch, failure_mode):
    plan, context, _ = context_and_patch()
    snapshot = context['snapshot']
    raw = canonical(snapshot)
    original = ExecutionError(category='validation', code='sketch_redundant_constraints',
        message='native redundant constraints', operation_id=snapshot['failed_operation_id'],
        details={'object': context['sketch'], 'failure_snapshot_json': raw,
                 'failure_snapshot_sha256': digest(snapshot)})
    calls = []
    async def failing_store(key, data, **kwargs):
        calls.append(key)
        if failure_mode == 'unavailable':
            raise OSError('injected object store outage')
        return {'sha256': '0' * 64}
    monkeypatch.setattr(constraint_evidence, 'put_object', failing_store)
    request = SimpleNamespace(tenant_id=uuid4(), principal_id=uuid4(), workflow_run_id=uuid4())
    result = await constraint_evidence.persist_failure_snapshot(request,
        {'source_id': uuid4(), 'source_hash': 'a' * 64}, uuid4(), original, plan,
        context['checkpoint_hash'], validate_snapshot=validate_snapshot)
    assert len(calls) == 1 and '/workflow-evidence/' in calls[0]
    assert result.code == original.code and result.message == original.message
    assert result.operation_id == original.operation_id and result.retryable is False
    assert result.details['failure_snapshot_unavailable'] in {'OSError', 'ValueError'}
    assert 'failure_snapshot_json' not in result.details
    assert 'constraint_diagnostic_sha256' not in result.details


@pytest.mark.asyncio
async def test_native_verification_storage_failure_cannot_allow_candidate_publication(monkeypatch):
    async def failing_store(*args, **kwargs):
        raise OSError('injected evidence storage outage')
    monkeypatch.setattr(constraint_evidence, 'put_object', failing_store)
    request = SimpleNamespace(tenant_id=uuid4(), principal_id=uuid4(), workflow_run_id=uuid4())
    with pytest.raises(OSError, match='evidence storage outage'):
        await constraint_evidence.persist_repair_verification(request, source_id=uuid4(),
            source_hash='a' * 64, attempt_id=uuid4(), report={})
