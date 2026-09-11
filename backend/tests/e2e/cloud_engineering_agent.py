"""Real solver evidence -> real Agent modification -> reviewed native revision."""
import json
from pathlib import Path
from uuid import uuid4
import httpx
from cloud_document_acceptance import PRIVATE,call,wait_task,commit


def main():
    private=json.loads(PRIVATE.read_text());client=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    source=call(client,'GET','/api/documents/'+private['document_id'])
    fork=call(client,'POST','/api/documents/'+source['document_id']+'/branches',expected=202,json={
        'name':'engineering-agent-'+uuid4().hex[:8],'expected_revision_id':source['head_revision_id'],
        'expected_state_version':source['state_version'],'idempotency_key':str(uuid4())})
    commit(client,wait_task(client,fork['workflow_run_id']))
    path='/api/documents/'+fork['document_id'];before=call(client,'GET',path)
    component=next(f['kernel_name'] for f in before['features'] if f['type']=='PartDesign::Body')
    parameter=next(p for f in before['features'] if f['type']=='PartDesign::Pad' for p in f['parameters'] if p['property_name']=='Length' and p['editable'])
    analysis=call(client,'POST',path+'/engineering',expected=202,json={'expected_revision_id':before['head_revision_id'],
        'expected_state_version':before['state_version'],'idempotency_key':str(uuid4()),'task':{'kind':'linear_static',
            'component_name':component,'material':{'name':'Agent evidence acceptance material','young_modulus_mpa':210000,'poisson_ratio':0.3},
            'mesh_size_mm':4,'fixed_face':{'axis':'z','side':'min'},'loaded_face':{'axis':'z','side':'max'},'force_n':[0,0,1000]}})
    assert wait_task(client,analysis['workflow_run_id'])['status']=='succeeded'
    measured=call(client,'GET',path+'/engineering/'+analysis['workflow_run_id'])
    target=parameter['value']+1
    submitted=call(client,'POST',path+'/operations',expected=202,json={'action':'modify','expected_base_revision_id':before['head_revision_id'],
        'expected_state_version':before['state_version'],'idempotency_key':str(uuid4()),
        'objective':f"Use the recorded finite-element result as reference. Inspect the existing {parameter['object_name']} feature and its dependencies, then change only {parameter['id']} from {parameter['value']} mm to {target} mm. Keep the hole diameter, all other dimensions and native feature identities unchanged. Keep dependent through-hole geometry valid. Do not claim this change has a new validated solver result; the analysis must be rerun on the changed revision."})
    task=wait_task(client,submitted['workflow_run_id']);commit(client,task)
    after=call(client,'GET',path)
    assert next(p['value'] for f in after['features'] for p in f['parameters'] if p['id']==parameter['id'])==target
    assert after['head_revision_id']!=before['head_revision_id']
    assert call(client,'GET',path+'/engineering/'+analysis['workflow_run_id'])==measured
    assert all(t['source_revision_id']!=after['head_revision_id'] for t in call(client,'GET',path+'/engineering')['tasks'])
    evidence={'document_id':fork['document_id'],'source_revision_id':before['head_revision_id'],
        'analysis_workflow_id':analysis['workflow_run_id'],'agent_workflow_id':submitted['workflow_run_id'],
        'report_sha256':measured['artifacts']['engineering_report']['sha256'],'native_parameter':parameter['id'],
        'before_mm':parameter['value'],'after_mm':target,'new_revision_id':after['head_revision_id'],
        'old_solver_evidence_remains_bound_to_source':True,'no_fabricated_analysis_for_new_revision':True}
    Path('/tmp/cad-expansion-engineering-agent.json').write_text(json.dumps(evidence,indent=2))
    print('CAD_ENGINEERING_AGENT='+json.dumps(evidence),flush=True)


if __name__=='__main__':main()
