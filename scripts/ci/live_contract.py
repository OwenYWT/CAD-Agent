"""Run frozen live cases through a bounded provider gateway; no PR secrets."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ci.live_budget import BudgetExceeded, Limits, MonthLedger, RunBudget, S3Store
from scripts.ci.live_gateway import create_app
from scripts.ci.require_test_report import validate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('preflight', 'run'))
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    limits = Limits.environment()
    for key in ('MOONSHOT_API_KEY', 'CAD_EVAL_PROVIDER_URL', 'CAD_EVAL_LEDGER_ENDPOINT',
                'CAD_EVAL_LEDGER_BUCKET', 'CAD_EVAL_LEDGER_ACCESS_KEY', 'CAD_EVAL_LEDGER_SECRET_KEY'):
        if not os.environ.get(key):
            raise ValueError('live evaluation is unconfigured: ' + key)
    if not os.environ['CAD_EVAL_PROVIDER_URL'].startswith('https://'):
        raise ValueError('HTTPS provider endpoint required')
    if limits.reservation > limits.run:
        raise BudgetExceeded('one conservative model call exceeds the run limit')
    if args.mode == 'preflight':
        print('Live limits/credentials configured; no model request made.')
        return
    if args.report is None:
        parser.error('--report required')
    args.report.mkdir(parents=True, exist_ok=False)
    identity = os.environ['GITHUB_RUN_ID']+'-'+os.environ['GITHUB_RUN_ATTEMPT']
    ledger = MonthLedger(S3Store(), 'cad-evals/budgets/'+datetime.now(timezone.utc).strftime('%Y-%m')+'.json', limits.currency, limits.month)
    ledger.update(identity, limits.run)
    budget = RunBudget(limits)
    passed = False
    import uvicorn
    failures = []
    app = create_app(budget, os.environ['CAD_EVAL_PROVIDER_URL'], os.environ['MOONSHOT_API_KEY'], failures)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=18777, log_level='warning', access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError('budget gateway failed to start')
            threading.Event().wait(.05)
        if not server.started:
            raise RuntimeError('budget gateway did not start')
        nodes = json.loads((ROOT/'docs/architecture/ci-live-cases.json').read_text())['node_ids']
        env = dict(os.environ, LLM_BASE_URL='http://127.0.0.1:18777/v1', LLM_PROVIDER='moonshot',
            LLM_MODEL=limits.model, VISION_MODEL=limits.model, CAD_AGENT_TEST_REAL_FREECAD_AGENT='1',
            CAD_CONSTRAINT_LIVE_REPAIR='1', RUN_REAL_FREECAD_AGENT='1', RUN_REAL_FREECAD='1',
            DASHSCOPE_API_KEY='', AZURE_OPENAI_API_KEY='', CAD_CONSTRAINT_REPORT=str(args.report/'constraint-chain'))
        with (args.report/'live.log').open('w') as log:
            process = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'scripts.ci.pytest_contract', *nodes,
                '-q', '--junitxml='+str(args.report/'live.xml')], cwd=ROOT/'backend', env=env, stdout=log, stderr=subprocess.STDOUT)
        if process.returncode:
            raise RuntimeError('live provider cases failed; inspect live.log')
        validate(args.report/'live.xml', nodes)
        if not budget.records or failures:
            raise RuntimeError('live cases did not complete within the provider budget')
        passed = True
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        settled = False
        if not thread.is_alive():
            try:
                ledger.update(identity, budget.charged, settle=True)
                settled = True
            except (ValueError, OSError, ClientError) as error:
                failures.append('monthly ledger: '+type(error).__name__)
        # If a gateway is still active, retain the complete monthly reservation.
        (args.report/'usage.json').write_text(json.dumps({'schema_version': 'cad-eval-usage.v1',
            'passed': passed and settled, 'monthly_settled': settled, 'source_sha': os.environ['GITHUB_SHA'],
            'finished_at': datetime.now(timezone.utc).isoformat(),
            'currency': limits.currency, 'run_id': identity, 'model': limits.model,
            'charged_or_reserved': str(budget.charged), 'run_limit': str(limits.run),
            'monthly_reservation': str(limits.run), 'calls': budget.records,
            'budget_failures': failures, 'prices_are_configured_not_invoices': True}, indent=2))
        if passed and not settled:
            raise RuntimeError('live cases passed but monthly budget evidence could not be settled')


if __name__ == '__main__':
    main()
