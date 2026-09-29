"""Real Temporal/native generation -> review -> HTTP parameter edit -> persistence.

Deterministic geometry exercises new parameter types without model generation;
configured workflow visual checks still run. This is not a model-quality
evaluation. The edited STEP bytes are retained
for independent kernel measurement; no service responses are substituted.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import httpx

SEED = '''import asyncio,json,sys
from uuid import uuid4
from app.domain.identity import user_principal
from app.services.durable_submission import ensure_workspace_identity
from app.freecad.contracts import FreeCADOperationPlan
from app.workflows.temporal import start_mcad_workflow,McadExecutionRequest,McadOutputRequest
from app.db import close_database
async def main():
    owner=user_principal(sys.argv[1]);key=str(uuid4())
    workspace=await ensure_workspace_identity(owner,session_id=key,panel_id=str(uuid4()),title='Native parameter contract',user_id=sys.argv[1])
    plan=FreeCADOperationPlan.model_validate_json(sys.argv[2])
    task={'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute','params':{'plan':plan.model_dump(mode='json')},'inputs':{}}
    outputs={'fcstd':'application/vnd.freecad.fcstd','state':'application/json','step':'model/step','stl':'model/stl'}
    primary=McadExecutionRequest(step_key='native-parameter-contract',kind='mcad_model',capability='mcad.freecad',operation='execute',source_language='json',source_code=json.dumps(task),outputs=tuple(McadOutputRequest(name=k,media_type=v) for k,v in outputs.items()))
    workflow,handle=await start_mcad_workflow(tenant_id=owner.tenant_id,project_id=workspace.project_id,principal_id=owner.principal_id,branch_id=workspace.branch_id,expected_base_revision_id=workspace.head_revision_id,kind='mcad.execute',idempotency_key=key,primary=primary,objective='Deterministic native parameter contract',require_confirmation=False,commit_after_confirmation=False)
    result=await handle.result();assert result['status']=='succeeded',result
    print('NATIVE_PARAMETER_SEED='+json.dumps({'document_id':str(workspace.branch_id),'workflow_id':str(workflow),'change_set_id':result['change_set_id']}))
    await close_database()
asyncio.run(main())
'''


def case_plan(kind):
    ops = []
    def add(action, **args):
        ops.append({'op_id': f'op-{len(ops)}', 'action': action, 'args': args})
    def circle(sketch, radius, x=0):
        add('sketch.add_profile', sketch=sketch, geometry={
            'kind': 'circle', 'center': {'x': x, 'y': 0}, 'radius_mm': radius})
    if kind == 'revolve':
        add('sketch.create', name='Section', frame={
            'origin': {'x': 0, 'y': 0, 'z': 0}, 'normal': [0,-1,0], 'x_axis': [1,0,0]})
        circle('Section', 4, 40); circle('Section', 2, 40)
        add('feature.revolve', name='Ring', profile='Section', axis='z', angle_deg=90)
        edit = ('Ring.Angle', 90, 120)
        volume = lambda v: math.pi*12*40*math.radians(v)
    else:
        add('sketch.create', name='Base'); circle('Base', 50)
        add('feature.pad', name='Pad', profile='Base', length_mm=12)
        add('sketch.create', name='Profile', offset_mm=12)
        circle('Profile', 2, 25 if kind == 'pattern' else 0)
        if kind == 'pattern':
            add('feature.pocket', name='Hole', profile='Profile', through_all=True)
            add('feature.polar_pattern', name='Pattern', originals=['Hole'], axis='z', occurrences=6, angle_deg=360)
            edit = ('Pattern.Occurrences', 6, 7)
            volume = lambda v: math.pi*(2500-v*4)*12
        else:
            add('feature.hole', name='Hole', profile='Profile', diameter_mm=4,
                through_all=True, cut={'kind':'counterbore','diameter_mm':8,'depth_mm':3})
            edit = ('Hole.HoleCutDiameter', 8, 9)
            volume = lambda v: math.pi*(2500*12-4*9-(v/2)**2*3)
    add('document.export', formats=['fcstd','step','stl'], basename=kind)
    return {'operations': ops}, edit, volume


def main():
    out = Path(os.environ['CAD_NATIVE_PARAMETER_REPORT']); out.mkdir(parents=True, exist_ok=False)
    account = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())['owner']
    command = json.loads(os.environ['CAD_NATIVE_E2E_COMMAND'])
    evidence = []
    with httpx.Client(base_url=os.environ['CAD_NATIVE_E2E_URL'], timeout=None, trust_env=False) as client:
        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs); response.raise_for_status()
            return response.json()
        auth = call('POST','/api/auth/login/password',json={k:account[k] for k in ('phone','password')})
        client.headers['Authorization'] = 'Bearer '+auth['token']
        def commit(change):
            note={'note':'Native parameter regression: exact persisted value and exported geometry are independently checked.'}
            call('POST',f'/api/change-sets/{change}/accept',json=note)
            call('POST',f'/api/change-sets/{change}/commit',json=note)
        for kind in ['pattern','revolve','counterbore']:
            plan, (parameter, initial, updated), volume = case_plan(kind)
            result = subprocess.run(command+['-',account['user']['id'],json.dumps(plan)],input=SEED,text=True,capture_output=True)
            (out/(kind+'-seed.log')).write_text(result.stdout+result.stderr)
            assert result.returncode == 0, kind+' seed failed; see retained log'
            seed=json.loads(next(line.split('=',1)[1] for line in result.stdout.splitlines() if line.startswith('NATIVE_PARAMETER_SEED=')))
            commit(seed['change_set_id']); path='/api/documents/'+seed['document_id']
            before=call('GET',path)
            params={p['id']:p for f in before['features'] for p in f['parameters']}
            assert params[parameter]['value']==initial and params[parameter]['editable']
            request={'action':'parameters.update','expected_base_revision_id':before['head_revision_id'],
                'expected_state_version':before['state_version'],'idempotency_key':str(uuid4()),
                'modification':{'expected_state_sha256':before['parameter_state_sha256'],
                    'parameter_updates':[{'parameter_id':parameter,'value':updated}]}}
            task=call('POST',path+'/operations',json=request)['workflow_run_id']
            assert call('POST',path+'/operations',json=request)['workflow_run_id']==task
            confirmed=False
            while True:
                snapshot=call('GET',f'/api/tasks/{task}/snapshot')
                if snapshot['status']=='waiting_confirmation' and not confirmed:
                    call('POST',f'/api/tasks/{task}/confirmation',json={'accepted':True});confirmed=True
                if snapshot['status'] in {'succeeded','failed','cancelled','timed_out'}: break
                time.sleep(.5)
            (out/(kind+'-task.json')).write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
            assert snapshot['status']=='succeeded', (kind,snapshot.get('error_code'),snapshot.get('error_message'))
            commit(snapshot['change_set']['id']); after=call('GET',path)
            after_params={p['id']:p for f in after['features'] for p in f['parameters']}
            assert after_params[parameter]['value']==updated
            assert {k:p['value'] for k,p in params.items() if k!=parameter} == {k:p['value'] for k,p in after_params.items() if k!=parameter}
            assert after['state_version']==before['state_version']+1
            assert {f['id'] for f in after['features']}=={f['id'] for f in before['features']}
            assert client.post(path+'/operations',json={**request,'idempotency_key':str(uuid4())}).status_code==409
            artifact=next(a for a in snapshot['artifacts'] if a['artifact_kind']=='step')
            response=client.get(artifact['download_url']);response.raise_for_status()
            assert hashlib.sha256(response.content).hexdigest()==artifact['sha256']
            step=out/(kind+'.step');step.write_bytes(response.content)
            evidence.append({'kind':kind,'task':task,'parameter':parameter,'before':initial,'after':updated,
                'step':str(step),'expected_volume':volume(updated),'state_version':after['state_version'],
                'stable_feature_ids':True,'unrelated_parameters_preserved':True,'idempotency_and_stale_base':True})
            print('NATIVE_PARAMETER_EDIT',kind,'passed',flush=True)
    (out/'measurements.json').write_text(json.dumps(evidence,indent=2))


if __name__ == '__main__':
    main()
