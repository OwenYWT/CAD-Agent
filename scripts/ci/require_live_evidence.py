"""Verify an optional bounded live run; never a required PR/release check."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.ci.require_test_report import validate


def require(root, sha, model):
    receipt=json.loads((root/'usage.json').read_text())
    if receipt.get('passed') is not True or receipt.get('monthly_settled') is not True or receipt.get('source_sha') != sha or receipt.get('model') != model or receipt.get('budget_failures'):
        raise ValueError('missing successful live evaluation for release source/model')
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(receipt['finished_at'])).total_seconds()
    if age < 0 or age > 86400:
        raise ValueError('live release evidence is older than 24 hours or has a future clock')
    if not receipt.get('calls') or Decimal(receipt['charged_or_reserved']) > Decimal(receipt['run_limit']):
        raise ValueError('missing provider calls or live cost budget exceeded')
    nodes=json.loads((ROOT/'docs/architecture/ci-live-cases.json').read_text())['node_ids']
    validate(root/'live.xml',nodes)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path)
    parser.add_argument('--sha',required=True);parser.add_argument('--model',required=True)
    args=parser.parse_args();require(args.root,args.sha,args.model)
    print('Optional source/model live evidence verified.')
