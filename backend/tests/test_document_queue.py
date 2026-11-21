import pytest
from temporalio.exceptions import ApplicationError

from app.workflows import document_queue


class _WaitingWorkflow:
    def __init__(self):
        self._phase = None
        self._cancel_reason = None
        self.acquire_calls = 0

    async def _activity(self, name, request, *, suffix):
        assert name == "document.acquire"
        self.acquire_calls += 1
        return {"status": "waiting"}


@pytest.mark.asyncio
async def test_revision_restore_fails_fast_when_document_is_busy(monkeypatch):
    async def timeout(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(document_queue.workflow, "patched", lambda name: name == "document-restore-busy-v1")
    monkeypatch.setattr(document_queue.workflow, "wait_condition", timeout)
    workflow = _WaitingWorkflow()

    with pytest.raises(ApplicationError) as caught:
        await document_queue.wait_for_document(
            workflow,
            {"revision_restore": {"source_revision_id": "revision"}},
        )

    assert caught.value.type == "document_operation_in_progress"
    assert workflow.acquire_calls == 1


@pytest.mark.asyncio
async def test_pre_patch_revision_restore_replays_its_wait(monkeypatch):
    class QueuedRestore(_WaitingWorkflow):
        async def _activity(self, name, request, *, suffix):
            result = await super()._activity(name, request, suffix=suffix)
            return result if self.acquire_calls == 1 else {"status": "acquired"}

    async def expire_timer(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(document_queue.workflow, "patched", lambda name: False)
    monkeypatch.setattr(document_queue.workflow, "wait_condition", expire_timer)
    instance = QueuedRestore()
    await document_queue.wait_for_document(instance, {"revision_restore": {"source_revision_id": "revision"}})
    assert instance.acquire_calls == 2
