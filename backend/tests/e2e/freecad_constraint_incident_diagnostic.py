"""Retain actual native failure evidence for a source-bound patch integration test."""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner


def main():
    fixture = json.loads(Path('/tests/fixtures/native_failures/headphone_hook_constraints.json').read_text())
    runner.INPUT_ROOT = Path('/tmp/diagnostic-input'); runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT = Path('/tmp/diagnostic-output'); runner.OUTPUT_ROOT.mkdir()
    def execute(plan, inputs):
        return runner.run_task({'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
            'operation': 'execute', 'params': {'plan': plan}, 'inputs': inputs})
    checkpoint = execute(fixture['checkpoint_plan'], {})
    shutil.copyfile(checkpoint['files']['fcstd'], runner.INPUT_ROOT / 'base.FCStd')
    try:
        execute(fixture['plan'], {'base': 'base.FCStd'})
    except runner.FreeCADRunnerError as exc:
        assert exc.code == 'sketch_redundant_constraints'
        data = {'snapshot': json.loads(exc.details['failure_snapshot_json']),
                'failure': {'error_code': exc.code, 'operation_id': exc.op_id,
                    'details': {k: v for k, v in exc.details.items() if not k.startswith('failure_snapshot')}},
                'base_state': json.loads(Path(checkpoint['files']['state']).read_text())}
        destination = Path('/evidence/incident-diagnostic.json')
        destination.write_text(json.dumps(data, ensure_ascii=False))
        shutil.copyfile(runner.INPUT_ROOT / 'base.FCStd', '/evidence/base.FCStd')
        print('CAD_CONSTRAINT_INCIDENT_DIAGNOSTIC=' + json.dumps({'code': exc.code,
            'logical_id': exc.op_id, 'constraint_count': len(data['snapshot']['sketches'][0]['constraints'])}))
    else:
        raise AssertionError('Retained production failure unexpectedly passed')


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        import os, traceback
        traceback.print_exc(); os._exit(1)
