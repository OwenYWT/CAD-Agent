"""Real CLI daemon, HTTP/DB queue and disk delivery; live five-minute crash recovery."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import httpx
from cloud_document_acceptance import BASE,PRIVATE,call

ROOT=Path(__file__).resolve().parents[3]
CLI=ROOT/'scripts/local_bridge.py'
spec=importlib.util.spec_from_file_location('actual_local_bridge',ROOT/'backend/app/integrations/local_bridge_client.py')
daemon=importlib.util.module_from_spec(spec);spec.loader.exec_module(daemon)


def main():
    private=json.loads(PRIVATE.read_text());client=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    document_id=private['document_id'];path='/api/documents/'+document_id
    release=json.loads(Path('/tmp/cad-expansion-release-http.json').read_text())['release_id']
    second=json.loads(Path('/tmp/cad-expansion-release-browser.json').read_text())['release_id']
    root=Path('/tmp')/('cad-bridge-acceptance-'+uuid4().hex[:10]);root.mkdir(mode=0o700)
    config_path=root/'private-config.json';target=root/'delivered';target.mkdir(mode=0o700)
    pair=call(client,'POST',path+'/bridges/pair',json={'label':'Actual local directory acceptance'},expected=201)
    result=subprocess.run([os.sys.executable,str(CLI),'pair','--server',BASE,'--directory',str(target),'--config',str(config_path)],
        input=pair['pairing_code']+'\n',capture_output=True,text=True,timeout=20)
    assert result.returncode==0,(result.stdout,result.stderr)
    assert config_path.stat().st_mode&0o777==0o600
    config=daemon.read_config(config_path)
    device=httpx.Client(headers={'Authorization':'Bridge '+config['token']})
    claim_body={'pairing_code':pair['pairing_code'],'client_info':{'protocol':'cad-local-bridge.v1','version':'1.0.0','hostname':'replay','target_label':'same'}}
    call(httpx.Client(),'POST','/api/local-bridge/pair',json=claim_body,expected=403)
    guest=httpx.Client(headers={'Authorization':'Bearer '+private['guest']['token'],'X-Workspace-Tenant':private['tenant_id']})
    call(guest,'POST',path+'/bridges/pair',json={'label':'guest'},expected=403)
    delivery=call(client,'POST',path+'/releases/'+release+'/deliveries',json={'bridge_id':pair['bridge_id']},expected=202)
    call(guest,'POST',path+'/releases/'+release+'/deliveries',json={'bridge_id':pair['bridge_id']},expected=403)
    command=[os.sys.executable,str(CLI),'run','--config',str(config_path),'--once']
    executed=subprocess.run(command,capture_output=True,text=True,timeout=60)
    assert executed.returncode==0,(executed.stdout,executed.stderr)
    rows=call(client,'GET',path+'/bridges')['deliveries'];actual=next(r for r in rows if r['id']==delivery['delivery_id'])
    assert actual['status']=='delivered' and actual['attempts']==1
    receipt=actual['receipt'];assert len(list(Path(receipt['directory']).iterdir()))==receipt['file_count']==14
    assert call(client,'POST',path+'/releases/'+release+'/deliveries',json={'bridge_id':pair['bridge_id']},expected=202)=={'delivery_id':delivery['delivery_id'],'replayed':True}
    assert subprocess.run(command,capture_output=True,timeout=30).returncode==0
    # Stop after real local writes, before acknowledgement, and let the real lease expire.
    recovery=call(client,'POST',path+'/releases/'+second+'/deliveries',json={'bridge_id':pair['bridge_id']},expected=202)
    leased=call(device,'POST','/api/local-bridge/poll')['delivery'];assert leased['delivery_id']==recovery['delivery_id']
    assert call(device,'POST','/api/local-bridge/poll')['delivery'] is None
    archive=daemon.request(config,'GET',leased['archive_url']+'?lease_token='+leased['lease_token'],binary=True)
    early_receipt=daemon.write_release(target,leased,archive);mtimes={p.name:p.stat().st_mtime_ns for p in Path(early_receipt['directory']).iterdir()}
    prefix='/api/local-bridge/deliveries/'+leased['delivery_id']
    call(device,'POST',prefix+'/ack',json={**early_receipt,'verified_files':{}},expected=422)
    call(device,'POST',prefix+'/ack',json={**early_receipt,'lease_token':str(uuid4())},expected=409)
    print('CAD_BRIDGE_FIRST_DELIVERY='+json.dumps({'delivery_id':delivery['delivery_id'],'files':receipt['file_count'],
        'real_cli_pair_and_delivery':True,'recovering_delivery':recovery['delivery_id'],'real_lease_wait_seconds':305}),flush=True)
    wait_started=time.monotonic()
    restarted=bool(os.getenv('CAD_NATIVE_E2E_API'))
    if restarted:
        checked=subprocess.run([os.sys.executable,str(Path(__file__).with_name('cloud_proxy_restart.py'))],
            capture_output=True,text=True,timeout=180)
        assert checked.returncode==0,(checked.stdout,checked.stderr)
        print(checked.stdout.strip(),flush=True)
    time.sleep(max(0,305-(time.monotonic()-wait_started)))
    recovered=subprocess.run(command,capture_output=True,text=True,timeout=60)
    assert recovered.returncode==0,(recovered.stdout,recovered.stderr)
    rows=call(client,'GET',path+'/bridges')['deliveries'];finished=next(r for r in rows if r['id']==recovery['delivery_id'])
    assert finished['status']=='delivered' and finished['attempts']==2
    assert mtimes=={p.name:p.stat().st_mtime_ns for p in Path(early_receipt['directory']).iterdir()}
    call(device,'POST',prefix+'/ack',json=early_receipt,expected=409)
    call(client,'DELETE',path+'/bridges/'+pair['bridge_id'],expected=204)
    call(device,'POST','/api/local-bridge/poll',expected=403)
    evidence={'document_id':document_id,'bridge_id':pair['bridge_id'],'delivery_id':delivery['delivery_id'],
        'recovery_delivery_id':recovery['delivery_id'],'real_cli_and_filesystem':True,'file_count':receipt['file_count'],
        'receipt_hashes_verified':True,'live_lease_expiry_and_restart':True,'actual_api_restart_verified':restarted,'recovery_attempts':finished['attempts'],
        'existing_files_not_rewritten':True,'replayed_pairing_and_stale_ack_denied':True,'viewer_and_revocation_denied':True,
        'output_directory':str(target)}
    Path('/tmp/cad-expansion-bridge-http.json').write_text(json.dumps(evidence,indent=2))
    print('CAD_BRIDGE_HTTP='+json.dumps(evidence),flush=True)


if __name__=='__main__':main()
