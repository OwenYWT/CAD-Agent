"""Replay recorded real histories with current workflow definitions; no activity runs.

Use CAD_TEMPORAL_REPLAY_INPUTS as a JSON array of {workflow_id, path} records.
An optional path is an archived Temporal JSON history; otherwise fetch it from
the configured Temporal service. Keep histories private: they contain user input.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from app.temporal_client import get_temporal_client
from app.workflows.agent_v2 import McadAgentWorkflowV2
from app.workflows.definitions import McadCheckWorkflow, McadDurableWorkflow


async def main():
    records = json.loads(Path(os.environ['CAD_TEMPORAL_REPLAY_INPUTS']).read_text())
    assert records, 'explicit real histories are required'
    out = Path(os.environ['CAD_TEMPORAL_REPLAY_REPORT_DIR'])
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    replayer = Replayer(workflows=[McadAgentWorkflowV2, McadDurableWorkflow, McadCheckWorkflow])
    report = []
    for item in records:
        if item.get('path'):
            raw = Path(item['path']).read_text()
            history = WorkflowHistory.from_json(item['workflow_id'], raw)
        else:
            client = await get_temporal_client()
            history = await client.get_workflow_handle(item['workflow_id']).fetch_history()
            raw = history.to_json()
        record = {'workflow_id':item['workflow_id'], 'events':len(history.events),
            'history_sha256':hashlib.sha256(raw.encode()).hexdigest(), 'status':'failed'}
        (out / (item['workflow_id']+'.json')).write_text(raw)
        try:
            await replayer.replay_workflow(history)
            record['status'] = 'passed'
        except Exception as error:
            record['error'] = str(error)
            raise
        finally:
            report.append(record)
            (out/'report.json').write_text(json.dumps(report,indent=2))
            print(json.dumps(record),flush=True)


if __name__ == '__main__':
    asyncio.run(main())
