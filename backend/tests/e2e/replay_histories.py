"""Replay real retained histories; emit identities/patches only, never payloads."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from temporalio.client import Client
from temporalio.worker import Replayer
from app.workflows.agent_v2 import McadAgentWorkflowV2
from app.workflows.definitions import McadDurableWorkflow, McadCheckWorkflow
from app.workflows.model_job import ModelJobWorkflow

async def main(output):
    types = {cls.__name__: cls for cls in (McadAgentWorkflowV2, McadDurableWorkflow, McadCheckWorkflow, ModelJobWorkflow)}
    client = await Client.connect(os.environ['TEMPORAL_TARGET'])
    seen = set(); report = []
    for name in types:
        count = 0
        async for item in client.list_workflows(query=f'WorkflowType = "{name}" AND ExecutionStatus = "Completed"'):
            if item.id.startswith('rollover-contract-'):
                continue  # This test deliberately accelerates timer durations.
            history = await client.get_workflow_handle(item.id, run_id=item.run_id).fetch_history()
            patches = set()
            for event in history.events:
                if not event.HasField('marker_recorded_event_attributes'):
                    continue
                marker = event.marker_recorded_event_attributes
                if marker.marker_name != 'core_patch':
                    continue
                for value in marker.details.values():
                    for payload in value.payloads:
                        try:
                            parsed = json.loads(payload.data)
                            if isinstance(parsed, dict) and 'id' in parsed: patches.add(parsed['id'])
                        except (ValueError, UnicodeDecodeError): pass
            key = (name, 'agent-v2-model-jobs-v1' in patches, 'agent-v2-backend-policy-v1' in patches)
            if key in seen:
                continue
            await Replayer(workflows=list(types.values())).replay_workflow(history)
            seen.add(key)
            report.append({'workflow_type':name,'workflow_id':item.id,'run_id':item.run_id,
                           'events':len(history.events),'patches':sorted(patches),'replay':'passed'})
            count += 1
            if name != 'McadAgentWorkflowV2' or count >= 3:
                break
    assert set(types) == {row['workflow_type'] for row in report}, 'missing workflow family'
    v2 = [row for row in report if row['workflow_type'] == 'McadAgentWorkflowV2']
    assert {('agent-v2-model-jobs-v1' in row['patches']) for row in v2} == {False, True}, 'missing pre/post model-job history'
    Path(output).write_text(json.dumps(report, indent=2))
    print(f'REPLAY PASSED: {len(report)} real histories across {len(types)} workflow families')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', required=True)
    asyncio.run(main(parser.parse_args().output))
