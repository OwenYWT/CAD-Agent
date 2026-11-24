"""Replay the two unmarked pre-merge decisions from their recorded Temporal commands."""
from collections.abc import Callable
from contextlib import asynccontextmanager

from temporalio import workflow
from temporalio.client import Client, WorkflowHistory
from temporalio.converter import DataConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import Interceptor, Replayer, WorkflowInterceptorClassInput

Decision = tuple[str, int, str]  # Run, WorkflowTaskStarted event, business error code.


class RecordedDecisions(Interceptor):
    def __init__(self, lookup: Callable[[Decision], bool]) -> None:
        self.lookup = lookup

    def workflow_interceptor_class(self, input: WorkflowInterceptorClassInput):
        input.unsafe_extern_functions["cad.recorded_merge_decision"] = self.lookup
        return None


async def _recorded_failures(history: WorkflowHistory, converter: DataConverter, run_id: str) -> set[Decision]:
    started = {}
    decisions = set()
    for event in history.events:
        if event.HasField("workflow_task_completed_event_attributes"):
            started[event.event_id] = event.workflow_task_completed_event_attributes.started_event_id
        elif event.HasField("activity_task_scheduled_event_attributes"):
            command = event.activity_task_scheduled_event_attributes
            if command.activity_type.name in {"agent_v2.terminate_candidate", "mcad.record_failure"}:
                request, = await converter.decode(command.input.payloads)
                code = request.get("error_code")
                if code in {"document_operation_in_progress", "agent_visual_validation_failed"}:
                    decisions.add((run_id, started[command.workflow_task_completed_event_id], code))
    return decisions


def agent_history_plugin(client: Client, task_queue: str) -> SimplePlugin:
    decisions: set[Decision] = set()

    @asynccontextmanager
    async def prepare():
        # The release procedure quiesces admissions and old workers before handoff.
        async for execution in client.list_workflows("ExecutionStatus = 'Running' AND WorkflowType = 'McadAgentWorkflowV2'"):
            if execution.task_queue == task_queue:
                handle = client.get_workflow_handle(execution.id, run_id=execution.run_id)
                history = await handle.fetch_history()
                decisions.update(await _recorded_failures(history, client.data_converter, execution.run_id))
        yield

    return SimplePlugin("cad-recorded-agent-decisions", interceptors=[RecordedDecisions(decisions.__contains__)],
                        run_context=prepare)


async def replay_history(replayer: Replayer, history: WorkflowHistory) -> set[Decision]:
    config = replayer.config()
    converter = config["data_converter"]
    recorded = await _recorded_failures(history, converter, history.run_id)
    consulted: list[Decision] = []

    def lookup(key: Decision) -> bool:
        consulted.append(key)
        return key in recorded

    async def verify():
        probe = Replayer(**{**config, "interceptors": [*config["interceptors"], RecordedDecisions(lookup)]})
        await probe.replay_workflow(history)

    try:
        await verify()
    except workflow.NondeterminismError:
        # A workflow invoking the gate in isolation can return the business
        # error directly. Require that outcome and a full SDK command replay.
        end = history.events[-1]
        if not consulted or not end.HasField("workflow_execution_completed_event_attributes"):
            raise
        results = await converter.decode(end.workflow_execution_completed_event_attributes.result.payloads)
        key = consulted[-1]
        if key in recorded or results != [key[2]]:
            raise
        recorded.add(key)
        await verify()
    return recorded.intersection(consulted)
