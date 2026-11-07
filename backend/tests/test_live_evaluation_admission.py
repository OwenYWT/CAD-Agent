from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_durable_submission_attaches_live_session_after_workflow_admission(monkeypatch):
    from app.services import durable_submission as submission_api

    identity = {
        "project_id": uuid4(),
        "branch_id": uuid4(),
        "expected_base_revision_id": uuid4(),
        "idempotency_key": "live-admission-1",
    }
    principal = SimpleNamespace(tenant_id=uuid4(), principal_id=uuid4())
    workflow_run_id = uuid4()
    observed = []

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(one_or_none=lambda: None)
            )

    async def start_v2(**_kwargs):
        return workflow_run_id, object()

    async def authorize(*_args, **_kwargs):
        return None

    def observe(payload, **kwargs):
        observed.append((payload, kwargs))
        return "live-workflow"

    monkeypatch.setattr(
        submission_api,
        "tenant_transaction",
        lambda *_args, **_kwargs: _Transaction(),
    )
    monkeypatch.setattr(submission_api, "_authorize_current_base", authorize)
    monkeypatch.setattr(submission_api, "start_mcad_agent_v2_workflow", start_v2)
    monkeypatch.setattr(submission_api, "ensure_live_session", observe, raising=False)

    result = await submission_api.submit_durable_workflow(
        principal,
        **identity,
        operation="generate",
        objective="create a cube",
        output_formats=["step", "stl"],
    )

    assert result.workflow_run_id == workflow_run_id
    assert len(observed) == 1
    assert observed[0][0]["workflow_run_id"] == str(workflow_run_id)
    assert observed[0][0]["operation"] == "generate"
    assert observed[0][0]["objective"] == "create a cube"
    assert observed[0][0]["expected_base_revision_id"] == str(
        identity["expected_base_revision_id"]
    )


@pytest.mark.asyncio
async def test_durable_submission_preserves_benchmark_source_identity(monkeypatch):
    from app.services import durable_submission as submission_api

    identity = {
        "project_id": uuid4(),
        "branch_id": uuid4(),
        "expected_base_revision_id": uuid4(),
        "idempotency_key": "benchmark-admission-1",
    }
    principal = SimpleNamespace(tenant_id=uuid4(), principal_id=uuid4())
    workflow_run_id = uuid4()
    observed = []

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(one_or_none=lambda: None)
            )

    async def start_v2(**_kwargs):
        return workflow_run_id, object()

    async def authorize(*_args, **_kwargs):
        return None

    def observe(payload, **kwargs):
        observed.append((payload, kwargs))

    monkeypatch.setattr(
        submission_api,
        "tenant_transaction",
        lambda *_args, **_kwargs: _Transaction(),
    )
    monkeypatch.setattr(submission_api, "_authorize_current_base", authorize)
    monkeypatch.setattr(submission_api, "start_mcad_agent_v2_workflow", start_v2)
    monkeypatch.setattr(submission_api, "ensure_live_session", observe)

    await submission_api.submit_durable_workflow(
        principal,
        **identity,
        operation="generate",
        objective="create a cube",
        output_formats=["step", "stl"],
        source_kind="benchmark",
        source_id="P01",
    )

    assert observed[0][1] == {"source_kind": "benchmark", "source_id": "P01"}


def test_live_submission_adapter_is_best_effort(monkeypatch):
    from app.services import durable_submission as submission_api

    def fail(*_args, **_kwargs):
        raise OSError("report storage unavailable")

    monkeypatch.setattr(submission_api, "ensure_live_session", fail)

    submission_api._record_live_submission(
        uuid4(),
        operation="generate",
        objective="create a cube",
        idempotency_key="submission-1",
        expected_base_revision_id=uuid4(),
    )
