from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_closed_change_set_keeps_source_workflow_identity(monkeypatch):
    from app.services import event_relay

    workflow_id = uuid4()
    project_id = uuid4()
    context = SimpleNamespace(tenant_id=uuid4(), principal_id=uuid4())
    row = {
        "project_id": project_id,
        "source_workflow_run_id": workflow_id,
        "workflow_status": "succeeded",
    }

    class Result:
        def mappings(self):
            return self

        def one_or_none(self):
            return row

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, *_args, **_kwargs):
            return Result()

    async def allow(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        event_relay,
        "tenant_transaction",
        lambda *_args, **_kwargs: Transaction(),
    )
    monkeypatch.setattr(event_relay, "_require_project_permission", allow)

    assert await event_relay.change_set_workflow_id(
        context,
        uuid4(),
    ) == workflow_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelling"])
async def test_closed_change_set_review_does_not_signal_temporal(monkeypatch, status):
    from app.api import changes

    confirmations = []

    async def snapshot(*_args, **_kwargs):
        return {"status": status, "kind": "mcad.modify"}

    async def confirm(*_args, **_kwargs):
        confirmations.append((_args, _kwargs))

    monkeypatch.setattr(changes, "get_task_snapshot", snapshot)
    monkeypatch.setattr(changes, "confirm_mcad_workflow", confirm)

    await changes._signal_review(
        SimpleNamespace(),
        uuid4(),
        accepted=True,
        note="reviewed",
    )

    assert confirmations == []


@pytest.mark.asyncio
async def test_commit_records_evaluation_from_authoritative_result_identity(monkeypatch):
    from app.api import changes
    from app.services.change_sets import ChangeSetOperationResult

    workflow_id = uuid4()
    change_set_id = uuid4()
    captured = []

    async def no_secondary_identity(*_args, **_kwargs):
        return None

    async def commit(*_args, **_kwargs):
        return ChangeSetOperationResult(
            change_set_id=change_set_id,
            status="committed",
            replayed=True,
            source_workflow_run_id=workflow_id,
        )

    def record(payload, action, result):
        captured.append((payload, action, result))

    monkeypatch.setattr(changes, "change_set_workflow_id", no_secondary_identity)
    monkeypatch.setattr(changes, "commit_change_set", commit)
    monkeypatch.setattr(changes, "record_change_set_transition", record)

    result = await changes.commit_change(
        change_set_id,
        principal=SimpleNamespace(tenant_id=uuid4(), principal_id=uuid4()),
    )

    assert result.status == "committed"
    assert captured[0][0] == {"workflow_run_id": str(workflow_id)}
    assert captured[0][1] == "committed"
