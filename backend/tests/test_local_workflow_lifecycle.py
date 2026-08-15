import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio

from app.config import settings
from app.models.schemas import GenerateResponse, StepUpdate
from app.storage import history, local_runs
from app.workflows.local import LocalWorkflowManager


@pytest_asyncio.fixture(autouse=True)
async def isolated_history_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    await history.close_db()
    await local_runs.initialize()
    yield
    await history.close_db()


def successful_result(request_id: str = "request-1") -> GenerateResponse:
    return GenerateResponse(
        request_id=request_id,
        success=True,
        files={"step": f"/api/files/{request_id}/result.step"},
        code="result = cq.Workplane('XY').box(1, 1, 1)",
        attempts=1,
    )


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_running_workflow():
    manager = LocalWorkflowManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(on_progress):
        started.set()
        await release.wait()
        return successful_result()

    task_id = await manager.submit(
        kind="generate",
        owner=None,
        request={"prompt": "box"},
        runner=runner,
    )
    waiter = asyncio.create_task(manager.wait(task_id, owner=None))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release.set()
    result = await manager.wait(task_id, owner=None)
    assert result["success"] is True
    assert (await local_runs.get_run(task_id, owner=None))["state"] == "COMPLETED"


@pytest.mark.asyncio
async def test_progress_delivery_failure_is_isolated_and_terminal_is_committed():
    manager = LocalWorkflowManager()

    async def broken_delivery(_step):
        raise ConnectionError("client disconnected")

    async def runner(on_progress):
        await on_progress(StepUpdate(step="planning", message="planning"))
        return successful_result()

    task_id = await manager.submit(
        kind="generate",
        owner=None,
        request={"prompt": "box"},
        runner=runner,
        progress_delivery=broken_delivery,
    )
    result = await manager.wait(task_id, owner=None)
    persisted = await local_runs.get_run(task_id, owner=None)
    events = await local_runs.list_events(task_id)

    assert result["success"] is True
    assert persisted["state"] == "COMPLETED"
    assert persisted["terminal_result_committed"] is True
    assert persisted["result"]["request_id"] == "request-1"
    assert any(event["event_type"] == "progress" for event in events)


@pytest.mark.asyncio
async def test_new_manager_reads_persisted_result_after_reconnect():
    first_manager = LocalWorkflowManager()

    async def runner(_on_progress):
        return successful_result()

    task_id = await first_manager.submit(
        kind="generate",
        owner="api-key",
        request={"prompt": "box"},
        runner=runner,
    )
    await first_manager.wait(task_id, owner="api-key")

    reconnected_manager = LocalWorkflowManager()
    run = await reconnected_manager.get(task_id, owner="api-key")
    assert run["state"] == "COMPLETED"
    assert run["result"]["success"] is True
    assert await reconnected_manager.get(task_id, owner="wrong-key") is None


@pytest.mark.asyncio
async def test_structured_cad_failure_completes_but_thrown_failure_marks_run_failed():
    manager = LocalWorkflowManager()

    async def handled_failure(_on_progress):
        return GenerateResponse(
            request_id="handled-failure",
            success=False,
            error={"type": "InvalidCode", "message": "invalid"},
        )

    handled_id = await manager.submit(
        kind="generate",
        owner=None,
        request={"prompt": "bad geometry"},
        runner=handled_failure,
    )
    await manager.wait(handled_id, owner=None)
    assert (await manager.get(handled_id, owner=None))["state"] == "COMPLETED"

    async def thrown_failure(_on_progress):
        raise RuntimeError("backend unavailable")

    thrown_id = await manager.submit(
        kind="generate",
        owner=None,
        request={"prompt": "box"},
        runner=thrown_failure,
    )
    thrown_result = await manager.wait(thrown_id, owner=None)
    thrown_run = await manager.get(thrown_id, owner=None)
    assert thrown_run["state"] == "FAILED"
    assert thrown_result["success"] is False
    assert thrown_result["error"]["type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_restart_reconciliation_records_honest_failure_sequence():
    task_id = await local_runs.create_run(
        kind="generate",
        owner=None,
        request={"prompt": "box"},
    )
    await local_runs.mark_running(task_id)

    reconciled = await local_runs.reconcile_incomplete_runs()
    run = await local_runs.get_run(task_id, owner=None)
    states = [
        event["state"]
        for event in await local_runs.list_events(task_id)
        if event["event_type"] == "state"
    ]

    assert reconciled == [task_id]
    assert states == ["PENDING", "RUNNING", "INTERRUPTED", "RECONCILING", "FAILED"]
    assert run["state"] == "FAILED"
    assert run["error_code"] == "process_restarted"
    assert run["terminal_result_committed"] is True
    assert run["result"]["success"] is False


@pytest.mark.asyncio
async def test_restart_reconciliation_accepts_only_verifiable_committed_success():
    request_id = "committed-request"
    output_dir = Path(settings.file_storage_dir) / request_id
    output_dir.mkdir(parents=True)
    (output_dir / "result.step").write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;")

    task_id = await local_runs.create_run(
        kind="generate",
        owner=None,
        request={"prompt": "box"},
    )
    await local_runs.mark_running(task_id)
    db = await history.get_db()
    result = successful_result(request_id).model_dump(mode="json")
    await db.execute(
        """
        UPDATE local_workflow_runs
        SET result_json = ?, terminal_result_ref = ?,
            terminal_result_committed = 1
        WHERE id = ?
        """,
        (json.dumps(result), request_id, task_id),
    )
    await db.commit()

    await local_runs.reconcile_incomplete_runs()
    run = await local_runs.get_run(task_id, owner=None)
    assert run["state"] == "COMPLETED"
    assert run["error_code"] is None


@pytest.mark.asyncio
async def test_restart_rejects_committed_success_with_missing_artifact():
    task_id = await local_runs.create_run(
        kind="generate",
        owner=None,
        request={"prompt": "box"},
    )
    await local_runs.mark_running(task_id)
    db = await history.get_db()
    result = successful_result("missing-request").model_dump(mode="json")
    await db.execute(
        """
        UPDATE local_workflow_runs
        SET result_json = ?, terminal_result_ref = ?,
            terminal_result_committed = 1
        WHERE id = ?
        """,
        (json.dumps(result), "missing-request", task_id),
    )
    await db.commit()

    await local_runs.reconcile_incomplete_runs()
    run = await local_runs.get_run(task_id, owner=None)
    assert run["state"] == "FAILED"
    assert run["error_code"] == "process_restarted"
    assert run["result"]["success"] is False


@pytest.mark.asyncio
async def test_explicit_cancel_is_persisted_and_distinct_from_disconnect():
    manager = LocalWorkflowManager()
    started = asyncio.Event()

    async def runner(_on_progress):
        started.set()
        await asyncio.Event().wait()

    task_id = await manager.submit(
        kind="generate",
        owner=None,
        request={"prompt": "box"},
        runner=runner,
    )
    await started.wait()
    assert await manager.request_cancel(task_id, owner=None) is True

    run = await local_runs.get_run(task_id, owner=None)
    assert run["cancel_requested"] is True
    assert run["state"] == "CANCELLED"
    assert run["result"]["error"]["type"] == "ExecutionCancelled"


@pytest.mark.asyncio
async def test_manager_shutdown_leaves_uncommitted_run_for_restart_reconciliation():
    manager = LocalWorkflowManager()
    started = asyncio.Event()

    async def runner(_on_progress):
        started.set()
        await asyncio.Event().wait()

    task_id = await manager.submit(
        kind="generate",
        owner=None,
        request={"prompt": "box"},
        runner=runner,
    )
    await started.wait()
    await manager.shutdown()

    interrupted = await local_runs.get_run(task_id, owner=None)
    assert interrupted["state"] == "RUNNING"
    assert interrupted["terminal_result_committed"] is False

    await local_runs.reconcile_incomplete_runs()
    reconciled = await local_runs.get_run(task_id, owner=None)
    assert reconciled["state"] == "FAILED"
    assert reconciled["error_code"] == "process_restarted"
