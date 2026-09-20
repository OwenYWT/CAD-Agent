"""Reject a missing, empty, failed, or partially skipped mandatory suite."""
import sys
from pathlib import Path
import xml.etree.ElementTree as ET


def validate(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter('testcase'))
    if not cases:
        raise ValueError('mandatory suite executed no test cases')
    failed = [case.get('name') for case in cases if any(case.find(k) is not None for k in ('skipped', 'error', 'failure'))]
    if failed:
        raise ValueError('mandatory cases did not pass: ' + ', '.join(failed))
    return len(cases)


if __name__ == '__main__':
    for report in sys.argv[1:]:
        print(f'{Path(report).name}: {validate(report)} mandatory cases passed')
    if len(sys.argv) == 1:
        raise SystemExit('At least one report is required')
