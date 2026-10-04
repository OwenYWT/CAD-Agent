"""Reject missing jobs, weak gates, mutable Actions and silently omitted cases."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[2]


def audit(root=ROOT):
    config = json.loads((root / 'docs/architecture/modules.json').read_text())
    workflow = yaml.safe_load((root / '.github/workflows/ci.yml').read_text())
    jobs = workflow['jobs']
    required = set(config['required_ci_jobs'])
    errors = []
    functions_by_path = {}
    if required - set(jobs):
        errors.append('missing mandatory jobs: ' + str(sorted(required - set(jobs))))
    gate = jobs.get('regression-gate', {})
    if gate.get('name') != 'Required regression gate' or gate.get('if') != 'always()' or set(gate.get('needs', [])) != required:
        errors.append('full regression gate must always require every mandatory job')
    for name in required & set(jobs):
        if jobs[name].get('if'):
            errors.append('mandatory job has a skip condition: ' + name)
    if 'pull_request' not in workflow.get('on', {}) or 'main' not in workflow.get('on', {}).get('push', {}).get('branches', []):
        errors.append('full regression must run before merging and after main push')
    for path in (root / '.github/workflows').glob('*.yml'):
        value = yaml.safe_load(path.read_text())
        if value.get('permissions') != {'contents': 'read'}:
            errors.append('workflow default permissions must be contents: read: ' + path.name)
        triggers = value.get('on', {})
        if path.name == 'ci-live.yml' and set(triggers) != {'workflow_dispatch'}:
            errors.append('paid model evaluation must be optional and manual only')
        if 'pull_request_target' in triggers:
            errors.append('untrusted code must not run with pull_request_target')
        for trigger in triggers.values():
            if isinstance(trigger, dict) and ('paths' in trigger or 'paths-ignore' in trigger):
                errors.append('initial complete regression must not use path skips')
        for name, job in value.get('jobs', {}).items():
            if path.name != 'ci.yml' and job.get('name') == 'Required regression gate':
                errors.append('only full regression may emit the required gate')
            for step in job.get('steps', []):
                action = step.get('uses')
                if action and not action.startswith('./') and not re.fullmatch(r'[^@]+@[0-9a-f]{40}', action):
                    errors.append('Action must use an immutable commit: ' + action)
    for path in (root / '.github/actions').glob('*/action.yml'):
        for step in yaml.safe_load(path.read_text()).get('runs', {}).get('steps', []):
            action = step.get('uses')
            if action and not action.startswith('./') and not re.fullmatch(r'[^@]+@[0-9a-f]{40}', action):
                errors.append('Composite Action must use an immutable commit: ' + action)
    contracts = json.loads((root/config['required_test_manifest']).read_text())
    contracts['live'] = json.loads((root/'docs/architecture/ci-live-cases.json').read_text())
    for suite, contract in contracts.items():
        nodes = contract['node_ids']
        if not nodes or len(nodes) != len(set(nodes)):
            errors.append('empty or duplicate required cases: ' + suite)
        if set(contract.get('allowed_skips',{})) - set(nodes):
            errors.append('skip exception does not identify a collected case: ' + suite)
        if contract.get('engine') == 'node':
            continue
        for node in nodes:
            path, *names = node.split('::')
            target = root / ('backend/' + path if path.startswith('tests/') else path)
            if not target.is_file():
                errors.append('missing mandatory test file: ' + node)
                continue
            if target not in functions_by_path:
                functions_by_path[target] = {item.name for item in ast.walk(ast.parse(target.read_text(encoding='utf-8-sig'))) if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
            functions = functions_by_path[target]
            function = node.split('[', 1)[0].split('::')[-1]
            if function not in functions:
                errors.append('missing mandatory test function: ' + node)
    owners = {contract.get('job') for contract in config['required_runtime_reports'].values()}
    if required - owners:
        errors.append('jobs without mandatory evidence: ' + str(sorted(required - owners)))
    if owners - required:
        errors.append('evidence assigned to unknown jobs: ' + str(sorted(owners - required)))
    return errors


if __name__ == '__main__':
    findings = audit()
    if findings:
        raise SystemExit('\n'.join(findings))
    print('CI policy passed: every pre-merge job and evidence contract remains mandatory.')
