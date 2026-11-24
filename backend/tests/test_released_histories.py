import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from temporalio import workflow
from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter
from temporalio.worker import Replayer

from app.workers.history_replay import RecordedDecisions, replay_history
from app.workers.workflow_worker import build_agent_v2_workflow_worker
from app.workflows.agent_v2 import McadAgentWorkflowV2

spec = importlib.util.spec_from_file_location('released_replay', Path(__file__).parent / 'e2e/replay_released_histories.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def test_frozen_release_history_integrity_and_scenario_coverage():
    assert len(replay.load_histories()) >= 6


def test_corrupted_release_history_is_rejected(tmp_path):
    shutil.copytree(replay.FIXTURES, tmp_path / 'histories')
    root = tmp_path / 'histories'
    manifest = json.loads((root / 'manifest.json').read_text())
    path = root / manifest['histories'][0]['file']
    path.write_text(path.read_text() + ' ')
    with pytest.raises(ValueError, match='digest changed'):
        replay.load_histories(root)


def test_removed_release_scenario_is_rejected(tmp_path):
    shutil.copytree(replay.FIXTURES, tmp_path / 'histories')
    root = tmp_path / 'histories'
    path = root / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['histories'] = [row for row in manifest['histories'] if 'model-job-cancellation' not in row['cases']]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='missing released-history scenarios'):
        replay.load_histories(root)


@pytest.mark.asyncio
@pytest.mark.parametrize('case,expected', [
    ('tmp-busy-restore', ['document_operation_in_progress']),
    ('compare-busy-restore', []),
    ('main-busy-restore', []),
    ('tmp-advisory-visual', ['agent_visual_validation_failed']),
    ('compare-advisory-visual', []),
])
async def test_merge_decisions_replay_only_the_recorded_workflow_task(case, expected):
    history = WorkflowHistory.from_json(case, (replay.FIXTURES / f'merge-{case}.json').read_text())
    decisions = await replay_history(Replayer(workflows=[McadAgentWorkflowV2]), history)
    assert [decision for _, _, decision in decisions] == expected
    # Independently replay once, with only the resolved decisions available.
    await Replayer(workflows=[McadAgentWorkflowV2], interceptors=[RecordedDecisions(decisions.__contains__)]).replay_workflow(history)


@pytest.mark.asyncio
@pytest.mark.parametrize('conflict', ['activity_type', 'error_code'])
async def test_unrelated_divergence_still_fails_merge_replay(conflict):
    history = WorkflowHistory.from_json('busy', (replay.FIXTURES / 'merge-tmp-busy-restore.json').read_text())
    for event in history.events:
        command = event.activity_task_scheduled_event_attributes
        if conflict == 'activity_type' and command.activity_type.name == 'document.release':
            command.activity_type.name = 'unrelated.activity'
        if conflict == 'error_code' and command.activity_type.name == 'mcad.record_failure':
            request, = await DataConverter.default.decode(command.input.payloads)
            request['error_code'] = 'unrelated_failure'
            payload, = await DataConverter.default.encode([request])
            command.input.payloads[0].CopyFrom(payload)
    with pytest.raises(workflow.NondeterminismError):
        await replay_history(Replayer(workflows=[McadAgentWorkflowV2]), history)


@pytest.mark.asyncio
async def test_unfinished_workflow_task_has_no_historical_decision():
    history = WorkflowHistory.from_json('busy', (replay.FIXTURES / 'merge-tmp-busy-restore.json').read_text())
    first_failure = next(e for e in history.events if e.activity_task_scheduled_event_attributes.activity_type.name == 'mcad.record_failure')
    completion = first_failure.activity_task_scheduled_event_attributes.workflow_task_completed_event_id
    prefix = WorkflowHistory(history.workflow_id, [e for e in history.events if e.event_id < completion])
    assert not await replay_history(Replayer(workflows=[McadAgentWorkflowV2]), prefix)


@pytest.mark.asyncio
async def test_replacement_worker_loads_its_running_history_before_polling(monkeypatch):
    from app.config import settings

    queue = settings.temporal_agent_v2_task_queue
    history = WorkflowHistory.from_json('busy', (replay.FIXTURES / 'merge-tmp-busy-restore.json').read_text())
    scheduled = next(e for e in history.events if e.activity_task_scheduled_event_attributes.activity_type.name == 'mcad.record_failure')
    prefix = WorkflowHistory(history.workflow_id, [e for e in history.events if e.event_id <= scheduled.event_id])

    async def running(_query):
        yield SimpleNamespace(task_queue='other-queue', id='other', run_id='other-run')
        yield SimpleNamespace(task_queue=queue, id=history.workflow_id, run_id=history.run_id)

    handle = Mock(return_value=SimpleNamespace(fetch_history=AsyncMock(return_value=prefix)))
    client = SimpleNamespace(data_converter=DataConverter.default, list_workflows=running, get_workflow_handle=handle)
    monkeypatch.setattr('app.workers.workflow_worker.ModelJobWorker', lambda _client, **kwargs: kwargs)
    config = build_agent_v2_workflow_worker(client, backend=object())
    # The same startup hook and interceptor that the worker installs must
    # accept the remainder of the old run with no second fetch or retry.
    await Replayer(workflows=config['workflows'], plugins=config['plugins']).replay_workflow(history)
    handle.assert_called_once_with(history.workflow_id, run_id=history.run_id)
