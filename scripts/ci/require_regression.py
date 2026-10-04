"""Accept only complete, current-run evidence from every mandatory CI job."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.ci.require_runtime_evidence import validate


def validate_jobs(results, required):
    if not required or len(required) != len(set(required)):
        raise ValueError('invalid mandatory job list')
    missing = set(required) - set(results)
    failed = {job: results[job].get('result') for job in required if job in results and results[job].get('result') != 'success'}
    if missing or failed:
        raise ValueError(f'incomplete regression: missing={sorted(missing)}, unsuccessful={failed}')


def validate_metadata(root, jobs, expected):
    for job in jobs:
        metadata = json.loads((root / job / 'job.json').read_text())
        if metadata.get('job') != job:
            raise ValueError('wrong evidence owner: ' + job)
        for field in ('source_sha', 'run_id', 'run_attempt'):
            if str(metadata.get(field)) != str(expected[field]):
                raise ValueError(f'stale or unrelated evidence: {job}:{field}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    config = json.loads((ROOT / 'docs/architecture/modules.json').read_text())
    jobs = config['required_ci_jobs']
    validate_jobs(json.loads(os.environ['RESULTS']), jobs)
    validate_metadata(args.root, jobs, {
        'source_sha': os.environ['GITHUB_SHA'], 'run_id': os.environ['GITHUB_RUN_ID'],
        'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
    })
    count = validate(args.root)
    print(f'Complete regression passed: {len(jobs)} mandatory jobs, {count} mandatory reports.')


if __name__ == '__main__':
    main()
