"""Opt-in bounded C10 acceptance. Isolated services must already be configured."""
import argparse
from dataclasses import asdict
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time

import httpx
import uvicorn

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.ci.live_budget import Limits, RunBudget
from scripts.ci.live_gateway import create_app


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--ledger',type=Path,required=True)
    args=parser.parse_args()
    if os.environ.get('CAD_C10_LIVE_AUTHORIZED')!='1':
        raise ValueError('explicit paid evaluation authorization required')
    if not os.environ.get('CAD_CI_SCOPE','').startswith('cad-ci-c10-'):
        raise ValueError('use an isolated C10 service scope')
    if '/cad_live_c10_' not in os.environ.get('DATABASE_URL',''):
        raise ValueError('use an isolated C10 database')
    limits=Limits.environment()
    if not os.environ['CAD_EVAL_PROVIDER_URL'].startswith('https://'):
        raise ValueError('HTTPS provider required')
    if limits.reservation>limits.run:
        raise ValueError('one provider reservation exceeds the authorized run')
    args.report.mkdir(parents=True,exist_ok=False)
    args.ledger.parent.mkdir(parents=True,exist_ok=True)
    owner=(args.ledger.parent/(args.ledger.name+'.lock')).open('a')
    fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
    budget=RunBudget(limits)
    bounds={k:str(v) if isinstance(v,Decimal) else v for k,v in asdict(limits).items()}
    if args.ledger.exists():
        previous=json.loads(args.ledger.read_text())
        if previous['limits']!=bounds:
            raise ValueError('cannot change the original authorization limits')
        budget.records=previous['calls']
        budget.charged=sum((Decimal(row['cost']) for row in budget.records),Decimal(0))
        if len(budget.records)>limits.calls or budget.charged>limits.run:
            raise ValueError('original authorization already exhausted')
    failures=[]
    def changed():
        data={'schema_version':'cad-c10-budget.v1','limits':bounds,'calls':budget.records,
            'charged_or_reserved':str(budget.charged),'failures':failures}
        temporary=args.ledger.with_suffix('.pending')
        with temporary.open('w') as stream:
            os.chmod(temporary,0o600)
            json.dump(data,stream,indent=2);stream.flush();os.fsync(stream.fileno())
        temporary.replace(args.ledger)
        (args.report/'usage-progress.json').write_text(json.dumps(data,indent=2))
    changed()
    def port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));return sock.getsockname()[1]
    gateway_port,api_port,web_port=port(),port(),port()
    app=create_app(budget,os.environ['CAD_EVAL_PROVIDER_URL'],os.environ['MOONSHOT_API_KEY'],failures,changed=changed)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=gateway_port,log_level='warning',access_log=False))
    thread=threading.Thread(target=server.run,daemon=True)
    runtime_private=args.ledger.with_suffix('.runtime-private.json')
    if not runtime_private.exists():
        with runtime_private.open('x') as stream:
            os.chmod(runtime_private,0o600)
            json.dump({'AUTH_TOKEN_SECRET':secrets.token_hex(32),'ADMIN_PASSWORD':secrets.token_urlsafe(24)},stream)
    env={**os.environ,**json.loads(runtime_private.read_text()),'APP_ENVIRONMENT':'test','AUTH_REQUIRED':'true','DURABLE_CONTROL_PLANE_ENABLED':'true',
        'MOONSHOT_API_KEY':'local-budget-gateway','DASHSCOPE_API_KEY':'','AZURE_OPENAI_API_KEY':'','ANTHROPIC_API_KEY':'',
        'LLM_PROVIDER':'moonshot','LLM_BASE_URL':f'http://127.0.0.1:{gateway_port}/v1',
        'LLM_MODEL':limits.model,'VISION_MODEL':limits.model,
        'CAD_NATIVE_E2E_URL':f'http://127.0.0.1:{api_port}','CAD_NATIVE_E2E_WEB':f'http://127.0.0.1:{web_port}',
        'CAD_API_TARGET':f'http://127.0.0.1:{api_port}','CAD_C10_REPORT':str(args.report),
        'CAD_NATIVE_E2E_PRIVATE':str(Path(os.environ['RUNNER_TEMP'])/'c10-private.json')}
    processes=[]; logs=[]; passed=False
    def start(name,command,cwd):
        log=(args.report/(name+'.log')).open('w');logs.append(log)
        process=subprocess.Popen(command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT)
        processes.append(process)
        return process
    try:
        with (args.report/'setup.log').open('w') as setup:
            subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=ROOT/'backend',env=env,stdout=setup,stderr=subprocess.STDOUT,check=True)
            subprocess.run([sys.executable,'tests/e2e/create_ci_account.py'],cwd=ROOT/'backend',env=env,stdout=setup,stderr=subprocess.STDOUT,check=True)
        thread.start()
        start('api',[sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(api_port)],ROOT/'backend')
        start('worker',[sys.executable,'-m','app.workers.workflow_worker'],ROOT/'backend')
        start('web',['node','node_modules/vite/bin/vite.js','--host','127.0.0.1','--strictPort','--port',str(web_port)],ROOT/'frontend')
        for _ in range(90):
            if any(p.poll() is not None for p in processes):
                raise RuntimeError('owned service exited; see service logs')
            try:
                ready=httpx.get(env['CAD_NATIVE_E2E_URL']+'/ready',trust_env=False).json()
                if server.started and ready.get('durable_control_plane',{}).get('status')=='ready' and httpx.get(env['CAD_NATIVE_E2E_WEB'],trust_env=False).status_code==200:
                    break
            except (httpx.HTTPError,ValueError):
                # Readiness only: bounded retries cannot authorize model calls.
                pass
            time.sleep(1)
        else: raise RuntimeError('owned services did not become ready')
        with (args.report/'browser.log').open('w') as log:
            result=subprocess.run([sys.executable,'tests/e2e/requirement_revision_live.py'],cwd=ROOT/'backend',env=env,stdout=log,stderr=subprocess.STDOUT)
        current_paid=[row for row in budget.records if row.get('request_sent') is not False]
        if result.returncode or failures or not current_paid or not all(r['usage_verified'] for r in current_paid):
            raise RuntimeError('C10 did not pass with complete provider cost evidence; see browser.log and usage-progress.json')
        report=json.loads((args.report/'report.json').read_text())
        if report.get('passed') is not True: raise RuntimeError('missing complete product evidence')
        passed=True
    finally:
        for process in processes:
            if process.poll() is None:process.terminate()
        for process in processes:
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        if thread.ident is not None:
            server.should_exit=True;thread.join(timeout=10)
        if thread.is_alive():
            failures.append('provider gateway did not stop')
        passed=passed and not failures and not thread.is_alive()
        changed()
        (args.report/'usage.json').write_text(json.dumps({'passed':passed,'limits':bounds,
            'calls':budget.records,'charged_or_reserved':str(budget.charged),'budget_failures':failures,
            'prices_are_configured_not_invoices':True,'provider_default_output_tokens':limits.output},indent=2))
        for log in logs:log.close()
        private=Path(env['CAD_NATIVE_E2E_PRIVATE'])
        if private.exists():private.unlink()
        owner.close()
    if not passed:
        raise RuntimeError('C10 teardown or provider evidence failed; see usage.json')
    print('C10 live acceptance passed; actual usage:',str(budget.charged),limits.currency,'calls:',len(budget.records))


if __name__=='__main__':main()
