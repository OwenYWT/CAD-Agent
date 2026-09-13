"""A real unsafe local target fails visibly; repairing it creates a new immutable attempt."""
import importlib.util
import json
import os
from pathlib import Path
from acceptance_paths import evidence_path
import subprocess
from uuid import uuid4
import httpx
from cloud_document_acceptance import BASE,PRIVATE,call


def main():
    private=json.loads(PRIVATE.read_text());owner=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    evidence=json.loads(evidence_path('cad-expansion-release-http.json').read_text());path='/api/documents/'+private['document_id']
    root=Path('/tmp')/('cad-bridge-failure-'+uuid4().hex[:10]);root.mkdir(mode=0o700)
    target=root/'target';target.mkdir();outside=root/'outside';outside.mkdir()
    script=Path(__file__).resolve().parents[2]/'app/integrations/local_bridge_client.py';config=root/'private.json'
    paired=call(owner,'POST',path+'/bridges/pair',json={'label':'Local failure recovery'},expected=201)
    actual=subprocess.run([os.sys.executable,str(script),'pair','--server',BASE,'--directory',str(target),'--config',str(config)],
        input=paired['pairing_code']+'\n',text=True,capture_output=True,timeout=20)
    assert actual.returncode==0,(actual.stdout,actual.stderr)
    (target/private['project_id']).symlink_to(outside,target_is_directory=True)
    post=path+'/releases/'+evidence['release_id']+'/deliveries';body={'bridge_id':paired['bridge_id']}
    first=call(owner,'POST',post,json=body,expected=202)['delivery_id']
    cmd=[os.sys.executable,str(script),'run','--config',str(config),'--once']
    failed=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
    assert failed.returncode!=0 and not list(outside.iterdir())
    row=next(d for d in call(owner,'GET',path+'/bridges')['deliveries'] if d['id']==first)
    assert row['status']=='failed' and '符号链接' in row['error_message'] and row['receipt'] is None,row
    (target/private['project_id']).unlink()
    second=call(owner,'POST',post,json=body,expected=202)['delivery_id'];assert second!=first
    succeeded=subprocess.run(cmd,capture_output=True,text=True,timeout=60);assert succeeded.returncode==0,(succeeded.stdout,succeeded.stderr)
    rows=call(owner,'GET',path+'/bridges')['deliveries'];assert next(d for d in rows if d['id']==second)['status']=='delivered'
    assert next(d for d in rows if d['id']==first)==row
    call(owner,'DELETE',path+'/bridges/'+paired['bridge_id'],expected=204)
    print('CAD_BRIDGE_FAILURE='+json.dumps({'bridge_id':paired['bridge_id'],'failed_delivery':first,'recovered_delivery':second,
        'unsafe_target_rejected_without_writes':True,'failure_visible_without_false_success':True,'repair_retries_as_new_delivery':True,
        'failed_record_unchanged':True}),flush=True)


if __name__=='__main__':main()
