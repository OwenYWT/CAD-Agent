"""Execute backend-composed patch against its real FCStd baseline, for chain tests."""
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner


def main():
    task = json.loads(Path('/evidence/next-task.json').read_text())
    runner.INPUT_ROOT = Path('/tmp/patch-input'); runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT = Path('/tmp/patch-output'); runner.OUTPUT_ROOT.mkdir()
    if task['inputs']:
        shutil.copyfile('/evidence/base.FCStd', runner.INPUT_ROOT / 'base.FCStd')
    try:
        result = runner.run_task(task)
        for kind, path in result['files'].items():
            output = Path('/evidence') / ('candidate.' + Path(path).suffix.lstrip('.'))
            if kind == 'state': output = Path('/evidence/candidate-state.json')
            shutil.copyfile(path, output)
        report = {'status': 'succeeded', 'validations': result['validations'],
                  'runtime': result['runtime']}
    except runner.FreeCADRunnerError as exc:
        report = {'status': 'failed', 'failure': {'error_code': exc.code,
            'operation_id': exc.op_id, 'details': {k: v for k, v in exc.details.items()
                if not k.startswith('failure_snapshot')}}, 'message': str(exc),
            'snapshot': json.loads(exc.details['failure_snapshot_json']) if exc.details.get('failure_snapshot_json') else None}
    Path('/evidence/next-result.json').write_text(json.dumps(report, ensure_ascii=False))
    print('CAD_CONSTRAINT_PATCH_EXECUTE=' + json.dumps({k: v for k, v in report.items() if k not in {'snapshot', 'validations'}}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        import os, traceback
        traceback.print_exc(); os._exit(1)
