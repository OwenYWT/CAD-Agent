"""Read-only replay of frozen histories produced by unchanged released code."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from app.workflows.agent_v2 import McadAgentWorkflowV2
from app.workflows.definitions import McadDurableWorkflow
from app.workflows.model_job import ModelJobWorkflow

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures/released-histories'
REQUIRED_CASES = {
    'before-model-jobs', 'after-model-jobs', 'parent-cancellation',
    'worker-crash-recovery', 'model-job-cancellation', 'model-job-success',
}


def load_histories(root=FIXTURES):
    manifest = json.loads((root / 'manifest.json').read_text())
    rows = manifest['histories']
    cases = set()
    loaded = []
    for row in rows:
        if not re.fullmatch(r'[0-9a-f]{40}', row['source_sha']):
            raise ValueError('missing release source SHA')
        if row['patch_overrides'] is not False:
            raise ValueError('forced workflow patches are not released histories')
        path = root / row['file']
        if path.resolve().parent != root.resolve():
            raise ValueError('history path escapes fixture directory')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('frozen history digest changed: ' + row['file'])
        history = WorkflowHistory.from_json(row['workflow_id'], raw.decode())
        if len(history.events) != row['events']:
            raise ValueError('history event count changed')
        actual_type = history.events[0].workflow_execution_started_event_attributes.workflow_type.name
        if actual_type != row['workflow_type']:
            raise ValueError('workflow family differs from manifest')
        cases.update(row['cases'])
        loaded.append((row, history))
    if not REQUIRED_CASES <= cases:
        raise ValueError('missing released-history scenarios: ' + str(REQUIRED_CASES - cases))
    return loaded


async def main(output):
    replayer = Replayer(workflows=[McadAgentWorkflowV2, McadDurableWorkflow, ModelJobWorkflow])
    report = []
    for row, history in load_histories():
        await replayer.replay_workflow(history)
        report.append({key: row[key] for key in ('file', 'source_sha', 'sha256', 'cases')})
    Path(output).write_text(json.dumps({'passed': True, 'histories': report}, indent=2) + '\n')
    print(f'FROZEN RELEASE REPLAY PASSED: {len(report)} histories')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    asyncio.run(main(parser.parse_args().output))
