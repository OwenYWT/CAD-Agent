"""Process-local execution manager with durable M0 lifecycle state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.api.error_messages import public_generation_error
from app.storage import local_runs

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[Any], Awaitable[None]]
@dataclass(frozen=True)
class LocalWorkflowOutcome:
    result: BaseModel | dict
    state: str = "COMPLETED"


Runner = Callable[
    [ProgressCallback],
    Awaitable[BaseModel | dict | LocalWorkflowOutcome],
]


def _payload(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


class LocalWorkflowManager:
    """Run coroutines independently from request/WebSocket waiter lifetimes."""

    def __init__(self) -> None:
        self._active: dict[str, asyncio.Task] = {}
        self._progress_deliveries: dict[str, list[ProgressCallback]] = {}
        self._shutting_down = False

    async def submit(
        self,
        *,
        kind: str,
        owner: str | None,
        request: dict,
        runner: Runner,
        progress_delivery: ProgressCallback | None = None,
        scope_key: str | None = None,
    ) -> str:
        task_id = await local_runs.create_run(
            kind=kind,
            owner=owner,
            request=request,
            scope_key=scope_key,
        )
        if progress_delivery is not None:
            self._progress_deliveries[task_id] = [progress_delivery]
        task = asyncio.create_task(
            self._execute(
                task_id,
                runner=runner,
            ),
            name=f"local-workflow:{task_id}",
        )
        self._active[task_id] = task
        task.add_done_callback(
            lambda _task: (
                self._active.pop(task_id, None),
                self._progress_deliveries.pop(task_id, None),
            )
        )
        return task_id

    async def _execute(
        self,
        task_id: str,
        *,
        runner: Runner,
    ) -> dict:
        await local_runs.mark_running(task_id)

        async def durable_progress(update: Any) -> None:
            payload = _payload(update)
            await local_runs.append_progress(task_id, payload)
            deliveries = list(self._progress_deliveries.get(task_id, []))
            for delivery in deliveries:
                try:
                    await delivery(update)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    self.detach_progress_delivery(task_id, delivery)
                except Exception:
                    self.detach_progress_delivery(task_id, delivery)
                    logger.info(
                        "Progress delivery stopped for workflow %s; execution continues",
                        task_id,
                    )

        try:
            result = await runner(durable_progress)
        except asyncio.CancelledError:
            if self._shutting_down:
                # Keep RUNNING persisted.  Startup reconciliation will record the
                # honest process_restarted failure sequence.
                raise
            run = await local_runs.get_run_internal(task_id)
            if run and run["cancel_requested"]:
                cancelled = {
                    "request_id": task_id,
                    "success": False,
                    "files": {},
                    "attempts": 0,
                    "error": {
                        "type": "ExecutionCancelled",
                        "message": "用户取消了当前任务。",
                    },
                }
                return await local_runs.commit_result(
                    task_id,
                    cancelled,
                    state="CANCELLED",
                )
            # Event-loop/process shutdown is reconciled honestly on next startup.
            raise
        except Exception as exc:
            logger.error(
                "Local workflow %s failed with %s",
                task_id,
                type(exc).__name__,
            )
            failure = {
                "request_id": task_id,
                "success": False,
                "files": {},
                "attempts": 0,
                "error": public_generation_error(exc),
            }
            return await local_runs.commit_result(task_id, failure, state="FAILED")

        # This commit completes before any waiter receives the result.
        if isinstance(result, LocalWorkflowOutcome):
            return await local_runs.commit_result(
                task_id,
                result.result,
                state=result.state,
            )
        return await local_runs.commit_result(task_id, result)

    async def wait(self, task_id: str, owner: str | None) -> dict:
        owned_run = await local_runs.get_run(task_id, owner)
        if owned_run is None:
            raise KeyError(task_id)
        task = self._active.get(task_id)
        if task is not None:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Explicit cancellation commits a terminal record before the task
                # ends.  A cancelled caller, however, must remain cancelled.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
        run = await local_runs.get_run(task_id, owner)
        if run is None or run["result"] is None:
            raise KeyError(task_id)
        return run["result"]

    def attach_progress_delivery(
        self,
        task_id: str,
        delivery: ProgressCallback,
    ) -> bool:
        if task_id not in self._active:
            return False
        deliveries = self._progress_deliveries.setdefault(task_id, [])
        if delivery not in deliveries:
            deliveries.append(delivery)
        return True

    def detach_progress_delivery(
        self,
        task_id: str,
        delivery: ProgressCallback,
    ) -> None:
        deliveries = self._progress_deliveries.get(task_id)
        if deliveries is None:
            return
        try:
            deliveries.remove(delivery)
        except ValueError:
            pass
        if not deliveries:
            self._progress_deliveries.pop(task_id, None)

    async def get(self, task_id: str, owner: str | None) -> dict | None:
        return await local_runs.get_run(task_id, owner)

    async def get_latest_for_scope(
        self,
        scope_key: str,
        owner: str | None,
    ) -> dict | None:
        return await local_runs.find_latest_for_scope(scope_key, owner)

    async def request_cancel(self, task_id: str, owner: str | None) -> bool:
        requested = await local_runs.request_cancel(task_id, owner)
        if not requested:
            return False
        task = self._active.get(task_id)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return True

    async def shutdown(self) -> None:
        """Stop physical local work before storage and the event loop close."""
        self._shutting_down = True
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        self._progress_deliveries.clear()
        self._shutting_down = False


_manager: LocalWorkflowManager | None = None


def get_local_workflow_manager() -> LocalWorkflowManager:
    global _manager
    if _manager is None:
        _manager = LocalWorkflowManager()
    return _manager
