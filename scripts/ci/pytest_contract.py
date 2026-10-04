"""Retain collected test identities in JUnit; never infer coverage from counts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def pytest_collection_finish(session):
    destination = os.environ.get("CAD_CI_COLLECTION_REPORT")
    if destination:
        Path(destination).write_text(json.dumps([item.nodeid for item in session.items], indent=2) + "\n")
    inventory = os.environ.get('CAD_CI_COLLECTION_POLICY_REPORT')
    if inventory:
        # Baseline maintenance only; CI never regenerates its expected contract.
        from _pytest.skipping import evaluate_skip_marks
        rows = []
        for item in session.items:
            skipped = evaluate_skip_marks(item)
            rows.append({'node_id': item.nodeid, 'skip_reason': skipped.reason if skipped else None})
        Path(inventory).write_text(json.dumps(rows, indent=2) + '\n')
    for item in session.items:
        item.user_properties.append(("ci_nodeid", item.nodeid))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == 'call' and report.failed and os.getenv('CAD_CI_RETAIN_FAILURE_PLANS') == '1':
        script = Path(__file__).with_name('retain_failure_plans.py')
        result = subprocess.run([sys.executable, str(script)], text=True, capture_output=True)
        if result.returncode:
            root = Path(os.environ['CAD_CI_REPORT_ROOT'])
            root.mkdir(parents=True, exist_ok=True)
            (root/'failure-retention-error.log').write_text(result.stdout+result.stderr)
