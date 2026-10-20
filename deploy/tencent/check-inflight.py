"""Read-only payload and Temporal history probe in the actual target image.

Input comes from the existing deployment's trusted database reader. This probe
does not pause admission or switch services. Pause writes and drain active work
before using it. Unknown contracts and incompatible histories fail closed.
"""
import asyncio
import hashlib
import json
import sys
import subprocess
from pathlib import Path

RESULT_PREFIX = 'CAD_RELEASE_PREFLIGHT='


def parse_response(output):
    frames = [line[len(RESULT_PREFIX):] for line in output.splitlines() if line.startswith(RESULT_PREFIX)]
    if len(frames) != 1:
        raise RuntimeError('Target image did not return exactly one preflight result')
    result = json.loads(frames[0])
    if type(result.get('compatible')) is not bool:
        raise RuntimeError('Target image returned an invalid compatibility result')
    return result


def missing_fields(original, parsed, prefix=''):
    missing = []
    if isinstance(original, dict):
        if not isinstance(parsed, dict):
            return [prefix.rstrip('.') or '$']
        for key, value in original.items():
            name = prefix + key
            if key not in parsed:
                if value is not None:
                    missing.append(name)
            else:
                missing.extend(missing_fields(value, parsed[key], name + '.'))
    elif isinstance(original, list):
        if not isinstance(parsed, list):
            return [prefix.rstrip('.') or '$']
        for index, before in enumerate(original):
            name = prefix + str(index)
            if index >= len(parsed):
                missing.append(name)
            else:
                missing.extend(missing_fields(before, parsed[index], name + '.'))
    return missing


def inspect_requests(requests, *, require_histories=False, receipt_recovery_contract=None,
                     customer_billing_contract=None):
    from app.models.workflow_requests import McadAgentWorkflowV2Request, McadCheckRequest, McadWorkflowRequest
    blockers = []
    for item in requests:
        kind = item['kind']
        model = (McadAgentWorkflowV2Request if kind in {'mcad.agent.v2.generate', 'mcad.agent.v2.modify'} else
                 McadCheckRequest if kind == 'mcad.check' else
                 McadWorkflowRequest if kind in {'mcad.execute', 'mcad.generate', 'mcad.modify'} else None)
        if model is None:
            blockers.append({'workflow_id': item['id'], 'reason': 'unknown_workflow_contract'})
            continue
        try:
            parsed = model.model_validate(item['request_payload']).model_dump(mode='json')
            missing = missing_fields(item['request_payload'], parsed)
            if missing:
                blockers.append({'workflow_id': item['id'], 'reason': 'unsupported_fields', 'fields': missing})
        except Exception as exc:
            # No user objective, credentials or full payload is emitted.
            blockers.append({'workflow_id': item['id'], 'reason': type(exc).__name__})
    result = {'compatible': not blockers, 'checked': len(requests), 'blockers': blockers}
    if require_histories and not blockers:
        result['replayed_histories'] = asyncio.run(replay_histories(requests, blockers))
    # Budget enforcement lives in activities and the leased model runner, not
    # Temporal history. A successful replay alone cannot certify an old image
    # will honor the immutable policy stored beside the workflow dispatch.
    for item in requests:
        contract = item.get('provider_budget_contract')
        if contract is None:
            continue
        try:
            from app.contracts.provider_budget import SUPPORTED_PROVIDER_BUDGET_CONTRACTS
            supported = isinstance(contract, str) and contract in SUPPORTED_PROVIDER_BUDGET_CONTRACTS
        except ImportError:
            supported = False
        if not supported:
            blockers.append({'workflow_id': item['id'], 'reason': 'provider_budget_contract_unsupported'})
    # Completed/cancelled jobs can still have queued billing receipts. They
    # must not disappear from an application rollback's compatibility cohort.
    if receipt_recovery_contract is not None:
        try:
            from app.contracts.provider_budget import SUPPORTED_PROVIDER_RECEIPT_CONTRACTS
            supported = isinstance(receipt_recovery_contract, str) and receipt_recovery_contract in SUPPORTED_PROVIDER_RECEIPT_CONTRACTS
        except ImportError:
            supported = False
        if not supported:
            blockers.append({'reason': 'provider_receipt_recovery_unsupported'})
    # An idle tenant can still have an active plan or customer balance. Rolling
    # back to a provider runner without customer admission would bypass quotas.
    if customer_billing_contract is not None:
        try:
            from app.services.customer_billing import SUPPORTED_CUSTOMER_BILLING_CONTRACTS
            supported = isinstance(customer_billing_contract, str) and customer_billing_contract in SUPPORTED_CUSTOMER_BILLING_CONTRACTS
        except ImportError:
            supported = False
        if not supported:
            blockers.append({'reason': 'customer_billing_contract_unsupported'})
    result['compatible'] = not blockers
    return result


async def replay_histories(requests, blockers):
    from temporalio.client import WorkflowHistory
    from temporalio.worker import Replayer
    from app.workflows.agent_v2 import McadAgentWorkflowV2
    from app.workflows.definitions import McadDurableWorkflow, McadCheckWorkflow
    from app.workflows.model_job import ModelJobWorkflow

    replayer = Replayer(workflows=[McadAgentWorkflowV2, McadDurableWorkflow, McadCheckWorkflow, ModelJobWorkflow])
    completed = []
    for item in requests:
        try:
            raw = item['history']
            history = WorkflowHistory.from_json(item['temporal_id'], raw)
            if len(history.events) < 2:
                raise ValueError('Incomplete workflow history')
            await replayer.replay_workflow(history)
            completed.append({'workflow_id': item['id'], 'temporal_id': item['temporal_id'],
                'events': len(history.events), 'sha256': hashlib.sha256(raw.encode()).hexdigest(), 'replay': 'passed'})
        except Exception as exc:
            blockers.append({'workflow_id': item['id'], 'reason': 'workflow_history_incompatible',
                             'error_type': type(exc).__name__})
    return completed


def probe_deployment(source_container, target_image, runtime='docker'):
    """Inspect the complete cross-tenant cohort; never claim an empty RLS view."""
    reader = '''import asyncio,json,os
from sqlalchemy import text
from temporalio.client import Client
from app.db import get_database_engine,close_database
from app.workflows.temporal import temporal_agent_v2_workflow_id,temporal_check_workflow_id,temporal_workflow_id
async def read():
    async with get_database_engine().begin() as c:
        await c.execute(text('SET TRANSACTION READ ONLY'))
        allowed=await c.scalar(text('SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user'))
        if not allowed: raise RuntimeError('Deployment reader cannot inspect the complete cross-tenant cohort')
        rows=(await c.execute(text("SELECT w.id::text AS id,w.kind,w.status,d.payload AS request_payload FROM workflow_runs w LEFT JOIN workflow_dispatches d ON d.workflow_id=w.id WHERE w.status NOT IN ('succeeded','failed','cancelled','timed_out') ORDER BY w.id"))).mappings().all()
        jobs=await c.scalar(text("SELECT count(*) FROM model_jobs WHERE status IN ('queued','running')"))
        requests=[dict(row) for row in rows]
        pending_receipts=0
        customer_billing=False
        if await c.scalar(text("SELECT to_regclass('public.tenant_subscriptions') IS NOT NULL")):
            customer_billing=await c.scalar(text("""SELECT
                EXISTS(SELECT 1 FROM tenant_subscriptions s JOIN billing_plan_versions p ON p.id=s.plan_version_id
                    WHERE p.code<>'basic' OR p.version<>1 OR s.status<>'active' OR s.expires_at IS NOT NULL)
                OR EXISTS(SELECT 1 FROM tenant_wallet_entries)
                OR EXISTS(SELECT 1 FROM tenant_billing_events)
                OR EXISTS(SELECT 1 FROM billing_plan_versions WHERE version>1)"""))
        if await c.scalar(text("SELECT to_regclass('public.provider_budget_policies') IS NOT NULL")):
            policies=(await c.execute(text("SELECT p.workflow_run_id::text AS id,COALESCE(p.policy->>'schema_version','') AS contract FROM provider_budget_policies p JOIN workflow_runs w ON w.id=p.workflow_run_id WHERE w.status NOT IN ('succeeded','failed','cancelled','timed_out')"))).mappings().all()
            contracts={row['id']:row['contract'] for row in policies}
            for item in requests:
                if item['id'] in contracts: item['provider_budget_contract']=contracts[item['id']]
            if await c.scalar(text("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='provider_budget_reservations' AND column_name='settlement_next_attempt_at')")):
                pending_receipts=await c.scalar(text("SELECT count(*) FROM provider_budget_reservations WHERE settlement_next_attempt_at IS NOT NULL"))
    await close_database()
    if not jobs and requests and all(item['status']=='waiting_confirmation' for item in requests):
        client=await Client.connect(os.environ['TEMPORAL_TARGET'],namespace=os.getenv('TEMPORAL_NAMESPACE','default'))
        for item in requests:
            kind=item['kind']
            identity=(temporal_agent_v2_workflow_id if kind.startswith('mcad.agent.v2.') else temporal_check_workflow_id if kind=='mcad.check' else temporal_workflow_id)(item['id'])
            history=await client.get_workflow_handle(identity).fetch_history()
            item.update(temporal_id=identity,history=history.to_json())
    print(json.dumps({'requests':requests,'active_model_jobs':jobs,'receipt_recovery_contract':'provider-receipt-recovery.v1' if pending_receipts else None,
        'customer_billing_contract':'customer-billing.v1' if customer_billing else None}))
asyncio.run(read())
'''
    source = subprocess.run([runtime, 'exec', '-i', source_container, 'python', '-'], input=reader,
                            check=True, capture_output=True, text=True)
    cohort = json.loads(source.stdout)
    identity = subprocess.check_output([runtime, 'image', 'inspect', target_image, '--format', '{{.Id}}'], text=True).strip()
    if cohort['active_model_jobs']:
        return {'compatible': False, 'checked': len(cohort['requests']), 'target_image': identity,
                'blockers': [{'reason': 'active_model_jobs_require_drain', 'count': cohort['active_model_jobs']}]}
    executing = [item['id'] for item in cohort['requests'] if item['status'] != 'waiting_confirmation']
    if executing:
        # A currently job-free planning activity can still create a job after
        # this read. Drain it instead of treating that brief gap as quiescence.
        return {'compatible': False, 'checked': len(cohort['requests']), 'target_image': identity,
                'blockers': [{'reason': 'active_workflows_require_drain', 'workflow_ids': executing}]}
    wrapper = 'import io,json,sys\nv=json.load(sys.stdin);sys.stdin=io.StringIO(json.dumps({"requests":v["requests"],"require_histories":True,"receipt_recovery_contract":v["receipt_recovery_contract"],"customer_billing_contract":v["customer_billing_contract"],"framed":True}));exec(compile(v["probe"],"check-inflight.py","exec"))'
    target = subprocess.run([runtime, 'run', '--rm', '-i', '--network', 'none', '--read-only', '--entrypoint', 'python', identity, '-c', wrapper],
        input=json.dumps({'probe': Path(__file__).read_text(), 'requests': cohort['requests'],
                          'receipt_recovery_contract':cohort.get('receipt_recovery_contract'),
                          'customer_billing_contract':cohort.get('customer_billing_contract')}), capture_output=True, text=True)
    if target.returncode not in {0, 2}:
        raise RuntimeError('Target image compatibility probe did not execute')
    result = parse_response(target.stdout)
    if result['checked'] != len(cohort['requests']) or result['compatible'] != (target.returncode == 0):
        raise RuntimeError('Target image returned inconsistent compatibility evidence')
    return {**result, 'target_image': identity}


if __name__ == '__main__':
    framed = False
    if len(sys.argv) == 1:
        data = json.load(sys.stdin)
        framed = isinstance(data, dict) and data.get('framed', False)
        result = (inspect_requests(data['requests'], require_histories=data.get('require_histories', False),
                                   receipt_recovery_contract=data.get('receipt_recovery_contract'),
                                   customer_billing_contract=data.get('customer_billing_contract'))
                  if isinstance(data, dict) else inspect_requests(data))
    else:
        import argparse
        parser = argparse.ArgumentParser(description='Read-only release preflight; pause admission before using its result.')
        parser.add_argument('--source-container', required=True)
        parser.add_argument('--target-image', required=True)
        parser.add_argument('--runtime', default='docker')
        args = parser.parse_args()
        result = probe_deployment(args.source_container, args.target_image, args.runtime)
    # Rust's replay diagnostics can also use stdout. A single explicit frame
    # keeps that text separate from the machine-readable deployment decision.
    print((RESULT_PREFIX if framed else '') + json.dumps(result))
    sys.exit(0 if result['compatible'] else 2)
