"""Bind uploaded evidence to a specific commit and workflow attempt."""
import argparse
import json
import os
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('job')
parser.add_argument('report_root', type=Path)
args = parser.parse_args()
args.report_root.mkdir(parents=True, exist_ok=True)
sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
if os.getenv('GITHUB_SHA') and os.environ['GITHUB_SHA'] != sha:
    raise SystemExit('checked-out source is not the tested GitHub SHA')
(args.report_root / 'job.json').write_text(json.dumps({
    'schema_version': 'cad-ci-job.v1', 'job': args.job, 'source_sha': sha,
    'run_id': os.getenv('GITHUB_RUN_ID', 'local'), 'run_attempt': os.getenv('GITHUB_RUN_ATTEMPT', '1'),
}, indent=2) + '\n')
