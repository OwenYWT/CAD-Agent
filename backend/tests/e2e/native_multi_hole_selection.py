"""T11/T18: one measured Hole with four profiles; ambiguity never becomes a write.

Seed with seed_collaboration_document.py --four-holes. This test uses real
HTTP/Agent/Temporal/FreeCAD/S3. Invalid selector payloads are negative inputs,
not substituted service responses. Measure exported files independently with
freecad_hole_measurements.py after this script.
"""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from cloud_document_acceptance import PRIVATE, call, commit, wait_task


def main():
    person=json.loads(PRIVATE.read_text())
    fixture=json.loads(Path(os.environ['CAD_MULTI_HOLE_FIXTURE']).read_text())
    out=Path(os.environ['CAD_COEDIT_REPORT_DIR']);out.mkdir(parents=True,exist_ok=True)
    client=httpx.Client(headers={'Authorization':'Bearer '+person['owner']['token']})
    path='/api/documents/'+fixture['document_id']
    before=call(client,'GET',path)
    holes=[f for f in before['features'] if f['type']=='PartDesign::Hole']
    assert len(holes)==1
    hole=holes[0]
    profile=next(f for f in before['features'] if f['kernel_name']=='HoleSketch')
    assert profile['sketch']['geometry_count']==4
    selector=next(b for b in hole['topology_bindings'] if b['geometry']=='circular')
    selection={'revision_id':before['head_revision_id'],'state_version':before['state_version'],'feature_ids':[hole['id']]}
    def payload(objective, context):
        return {'action':'modify','objective':objective,'expected_base_revision_id':before['head_revision_id'],
            'expected_state_version':before['state_version'],'idempotency_key':str(uuid4()),'selection_context':context}
    previous=call(client,'GET',path+'/collaboration')['operations']
    invalid=[('multi_hole_parent','把这个孔的直径改成 8 mm',selection),
        ('missing_target','给这条边加 0.5 mm 圆角',None),
        ('stale_selection','修改整个孔特征',{**selection,'state_version':before['state_version']+1}),
        ('incomplete_symmetric_selector','给这条边加 0.5 mm 圆角',{**selection,'topology_selector':{k:v for k,v in selector.items() if k!='center'}}),
        ('invented_neighbor','给这条边加 0.5 mm 圆角',{**selection,'topology_selector':{**selector,'center':{**selector['center'],'x':selector['center']['x']+0.01}}}),
        ('edge_cannot_resize_entire_feature','把这个孔的直径改成 8 mm',{**selection,'topology_selector':selector})]
    rejected=[]
    from cloud_document_acceptance import BASE
    for name,objective,context in invalid:
        body=payload(objective,context)
        if context is None:body.pop('selection_context')
        response=client.post(BASE+path+'/operations',json=body,timeout=60)
        assert response.status_code in {400,409,422},(name,response.status_code,response.text)
        rejected.append({'case':name,'status_code':response.status_code,'detail':response.json()})
    assert call(client,'GET',path+'/collaboration')['operations']==previous
    assert call(client,'GET',path)['head_revision_id']==before['head_revision_id']
    (out/'rejections.json').write_text(json.dumps(rejected,ensure_ascii=False,indent=2))
    submitted=call(client,'POST',path+'/operations',expected=202,json=payload(
        '修改整个 Hole 特征的所有孔，将 Hole.Diameter 从 6 mm 改为 8 mm。保留四个孔的原位置、贯穿深度、Pad.Length=8 和全部已有特征名称。导出 FCStd、STEP 和 STL。',selection))
    task=wait_task(client,submitted['workflow_run_id']);change=commit(client,task)
    after=call(client,'GET',path)
    assert after['state_version']==before['state_version']+1
    current_hole=next(f for f in after['features'] if f['id']==hole['id'])
    assert next(p['value'] for p in current_hole['parameters'] if p['property_name']=='Diameter')==8
    assert next(f for f in after['features'] if f['id']==profile['id'])['sketch']['geometry_count']==4
    assert {f['id'] for f in after['features']}=={f['id'] for f in before['features']}
    source=call(client,'GET',path+'/revisions/'+after['head_revision_id'])
    files={}
    for kind in ('fcstd','step'):
        response=client.get(BASE+source['snapshot']['files'][kind],timeout=60);response.raise_for_status()
        target=out/('four-hole.'+kind);target.write_bytes(response.content)
        files[kind]={'path':str(target),'sha256':hashlib.sha256(response.content).hexdigest()}
    (out/'measurements.json').write_text(json.dumps([{'name':'one-Hole-four-profiles-explicit-whole-feature',
        'fcstd':files['fcstd']['path'],'step':files['step']['path'],'dimensions':[60,40,8],
        'holes':[{'x':x,'y':y,'diameter':8,'depth':8} for x,y in [(30,20),(15,10),(45,10),(15,30)]]}]))
    report={'document_id':fixture['document_id'],'source_revision':before['head_revision_id'],'revision':after['head_revision_id'],
        'workflow_id':submitted['workflow_run_id'],'change_set_id':change,'rejected_cases':rejected,
        'invalid_requests_created_no_operations':True,'whole_feature_modification_explicit':True,'files':files}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('CAD_MULTI_HOLE_SELECTION='+json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
