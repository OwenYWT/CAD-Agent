"""Reject absent mandatory identities, failures, skips and duplicate receipts."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]


def case_id(case):
    properties = case.find('properties')
    if properties is not None:
        identities = [p.get('value') for p in properties.findall('property') if p.get('name') == 'ci_nodeid']
        if len(set(identities)) == 1 and identities[0]:
            return identities[0]
        if identities:
            raise ValueError('ambiguous test identity')
    return case.get('classname', '') + '::' + case.get('name', '')


def validate(path, required=None, *, allow_skips=False, allowed_skips=None):
    root = ET.parse(path).getroot()
    cases = list(root.iter('testcase'))
    if not cases:
        raise ValueError('mandatory suite executed no test cases')
    if root.tag not in ('testsuite', 'testsuites'):
        raise ValueError('invalid JUnit root')
    identities = [case_id(case) for case in cases]
    if len(identities) != len(set(identities)):
        raise ValueError('duplicate test identities')
    required = set(required or ())
    allowed_skips = allowed_skips or {}
    missing = sorted(required - set(identities))
    if missing:
        raise ValueError('missing mandatory tests: ' + ', '.join(missing))
    failed = []
    for case in cases:
        identity, skipped = case_id(case), case.find('skipped')
        if case.find('error') is not None or case.find('failure') is not None:
            failed.append(identity)
        elif skipped is not None:
            if identity in allowed_skips:
                if not skipped.get('message', '').endswith(allowed_skips[identity]):
                    failed.append(identity + ' (unexpected skip reason)')
            elif not allow_skips or identity in required:
                failed.append(identity)
    if failed:
        raise ValueError('mandatory cases did not pass: ' + ', '.join(failed))
    return sum(case.find('skipped') is None for case in cases)


def required_cases(suite):
    config = json.loads((ROOT / 'docs/architecture/modules.json').read_text())
    cases = json.loads((ROOT/config['required_test_manifest']).read_text())[suite]['node_ids']
    if not cases or len(cases) != len(set(cases)):
        raise ValueError('mandatory test list is empty or duplicated: ' + suite)
    return cases


def allowed_skips(suite):
    config = json.loads((ROOT / 'docs/architecture/modules.json').read_text())
    return json.loads((ROOT/config['required_test_manifest']).read_text())[suite].get('allowed_skips', {})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('reports', nargs='+', type=Path)
    parser.add_argument('--suite')
    parser.add_argument('--allow-skips', action='store_true')
    args = parser.parse_args()
    if args.suite and len(args.reports) != 1:
        parser.error('--suite requires exactly one report')
    for report in args.reports:
        required = required_cases(args.suite) if args.suite else None
        exceptions = allowed_skips(args.suite) if args.suite else None
        print(f'{report.name}: {validate(report, required, allow_skips=args.allow_skips, allowed_skips=exceptions)} executed cases passed')
