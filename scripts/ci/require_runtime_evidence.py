"""Fail closed when a declared mandatory runtime suite did not actually pass."""
import json
from pathlib import Path
import sys
import argparse
import hashlib
import zipfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.ci.require_test_report import allowed_skips, required_cases, validate as validate_junit


def validate(root, manifest=None, *, job=None):
    config=json.loads((ROOT/'docs/architecture/modules.json').read_text())
    required=manifest if manifest is not None else config['required_runtime_reports']
    if job is not None:
        required={name:contract for name,contract in required.items() if contract.get('job') == job}
    if not required:
        raise ValueError('runtime evidence manifest is empty')
    for name,contract in required.items():
        path=root/name
        if not path.is_file():
            raise ValueError(f'missing mandatory evidence: {name}')
        if contract['kind'] == 'junit':
            expected=required_cases(contract['suite']) if contract.get('suite') else None
            exceptions=allowed_skips(contract['suite']) if contract.get('suite') else None
            validate_junit(path, expected, allow_skips=contract.get('allow_skips', False), allowed_skips=exceptions)
        elif contract['kind'] == 'json':
            value=json.loads(path.read_text())
            for key,wanted in contract['fields'].items():
                if type(value.get(key)) is not type(wanted) or value.get(key) != wanted:
                    raise ValueError(f'missing successful evidence {name}:{key}')
        elif contract['kind'] == 'marker':
            text=path.read_text()
            if contract['marker'] not in text or 'Traceback (most recent call last)' in text:
                raise ValueError(f'native suite did not finish: {name}')
        elif contract['kind'] == 'workflow-replay':
            value=json.loads(path.read_text())
            if not isinstance(value,list) or not value:
                raise ValueError(f'no real histories replayed: {name}')
            if any(row.get('replay') != 'passed' or not row.get('workflow_id') or not row.get('run_id')
                   or not isinstance(row.get('events'),int) or row['events'] < 2 for row in value):
                raise ValueError(f'invalid history evidence: {name}')
            if set(contract['families']) - {row.get('workflow_type') for row in value}:
                raise ValueError(f'missing workflow families: {name}')
            if contract.get('pre_post_model_jobs'):
                patches={('agent-v2-model-jobs-v1' in row.get('patches',[])) for row in value
                         if row.get('workflow_type') == 'McadAgentWorkflowV2'}
                if patches != {True,False}:
                    raise ValueError(f'missing pre/post model-job histories: {name}')
        elif contract['kind'] == 'released-replay':
            value=json.loads(path.read_text())
            fixture=json.loads((ROOT/contract['manifest']).read_text())
            wanted=[{key:row[key] for key in ('file','source_sha','sha256','cases')} for row in fixture['histories']]
            if value.get('passed') is not True or value.get('histories') != wanted:
                raise ValueError(f'frozen released histories omitted or changed: {name}')
        elif contract['kind'] == 'images':
            value=json.loads(path.read_text())
            from scripts.ci.image_manifest import IMAGES
            if value.get('source_sha') != json.loads((path.parent/'job.json').read_text())['source_sha'] or set(value.get('images',{})) != set(IMAGES):
                raise ValueError(f'incomplete or stale image evidence: {name}')
            for image,row in value['images'].items():
                if not row['tag'].endswith(':ci-'+value['source_sha']) or len(row['sha256']) != 64 or not row['image_id'].startswith('sha256:'):
                    raise ValueError(f'invalid image identity: {name}:{image}')
        elif contract['kind'] == 'runtime-probe':
            value=json.loads(path.read_text())
            if value.get('status') != 'success' or value.get('schema_version') != 'mcad-runtime-probe.v2':
                raise ValueError(f'actual runtime probe failed: {name}')
            checks=value.get('checks',[])
            if not checks or any(row.get('status') != 'passed' for row in checks) or set(contract['operations']) - {row.get('id') for row in checks}:
                raise ValueError(f'missing runtime operation probes: {name}')
            if value.get('versions',{}).get('freecad') != contract['freecad'] or not value.get('artifacts'):
                raise ValueError(f'unverified runtime version/artifacts: {name}')
        elif contract['kind'] == 'steps':
            value=json.loads(path.read_text())
            if not isinstance(value,list) or not value:
                raise ValueError(f'no executed steps: {name}')
            ids=[row['step'] for row in value]
            if len(ids) != len(set(ids)) or set(contract['steps']) - set(ids):
                raise ValueError(f'missing or duplicated execution steps: {name}')
            for row in value:
                log=path.parent/row['log']
                if log.resolve().parent != path.parent.resolve() or type(row['exit_code']) is not int or row['exit_code'] != 0 or not row['command']:
                    raise ValueError(f'invalid step receipt: {name}')
                if hashlib.sha256(log.read_bytes()).hexdigest() != row['sha256']:
                    raise ValueError(f'execution log changed: {name}')
        elif contract['kind'] == 'browser-trace':
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None or not any(p.endswith('trace.trace') for p in archive.namelist()):
                    raise ValueError(f'missing valid browser trace: {name}')
        else:
            raise ValueError(f'unknown evidence kind: {contract}')
    return len(required)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('root',type=Path)
    parser.add_argument('--job')
    args=parser.parse_args()
    print(f'{validate(args.root,job=args.job)} mandatory runtime reports verified')
