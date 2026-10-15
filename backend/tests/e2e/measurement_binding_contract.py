"""Current native identity, rigid frames and STEP containment are all required."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,'/opt/cad-agent')
from geometry_validation import validate_geometry_files
from measurement_binding import verify_measurement_binding

process=subprocess.run(['/opt/freecad/bin/FreeCADCmd','-c',
    "exec(compile(open('/tests/freecad_measurement_fixture.py').read(),'/tests/freecad_measurement_fixture.py','exec'))"],
    capture_output=True,text=True)
assert process.returncode==0 and 'CAD_MEASUREMENT_FIXTURE=1' in process.stdout and 'Traceback' not in process.stdout+process.stderr,process.stdout+process.stderr
root=Path('/sandbox/input')
native=json.loads((root/'native-reference.json').read_text())
step_sha=hashlib.sha256((root/'final.step').read_bytes()).hexdigest()
artifact=[{'role':'artifact','format':'step','path':str(root/'final.step')}]
reports=[]
for name,type_id in [('Bracket','PartDesign::Body'),('BracketInstance','App::Link')]:
    reference={'revision_id':'base','feature_id':'confirmed-'+name,'kernel_name':name,
        'type_id':type_id,'parameter_state_sha256':'a'*64}
    context={'schema_version':'measurement-context.v2','base_revision_id':'base','checkpoint_manifest_id':'current',
        'artifact_sha256s':[step_sha],'native_reference':native,'selected_objects':[reference]}
    check={'check_id':'gap','kind':'planar_gap','required':True,
        'comparison':{'op':'eq','value':25.,'unit':'mm'},
        'scope':{'frame':'selected_object','object_ref':reference,'axis':[0,1,0]}}
    contract={'schema_version':'engineering-acceptance.v2','checks':[check],'comparison_policy_id':'occt-confusion-v1'}
    for target in (25.,24.):
        check['comparison']['value']=target
        report=validate_geometry_files(artifact,acceptance=contract,measurement_context=context,native_path=str(root/'selected.FCStd'))
        evidence=report['acceptance']['evidence'][0]
        assert evidence['outcome']==('passed' if target==25 else 'failed'),evidence
        assert abs(evidence['measured'][0]-25)<1e-7
        binding=evidence['details']['measurement_binding']
        verify_measurement_binding(binding,check=check,acceptance_digest=report['acceptance']['contract_sha256'],
            context=context,artifact_sha256s=[step_sha])
        assert binding['proof']['native_component']['contained_in_final_step']
        if target==25:
            selected_faces=binding['proof']['selection']['face_indices']
        else:
            assert binding['proof']['selection']['face_indices']==selected_faces
        reports.append({'object':name,'target':target,'measured':evidence['measured'],'outcome':evidence['outcome']})
    # An explicit world-axis request must stay world-axis even with local authority available.
    world=deepcopy(contract);world['checks'][0]['scope']={'frame':'world','axis':[0,1,0]}
    report=validate_geometry_files(artifact,acceptance=world,measurement_context=context,native_path=str(root/'selected.FCStd'))
    assert report['acceptance']['evidence'][0]['outcome']=='indeterminate',report
    if name=='Bracket':
        missing=deepcopy(context)
        missing['artifact_sha256s']=[hashlib.sha256((root/'missing-body.step').read_bytes()).hexdigest()]
        report=validate_geometry_files([{'role':'artifact','format':'step','path':str(root/'missing-body.step')}],
            acceptance=contract,measurement_context=missing,native_path=str(root/'selected.FCStd'))
        evidence=report['acceptance']['evidence'][0]
        assert evidence['outcome']=='indeterminate' and evidence['details']['cad_source_repair_allowed'] is False,evidence
        assert evidence['details']['reason']=='selected_component_not_in_final_step'
    assert hashlib.sha256((root/'selected.FCStd').read_bytes()).hexdigest()==native['sha256']
    assert hashlib.sha256((root/'final.step').read_bytes()).hexdigest()==step_sha
print('CAD_MEASUREMENT_BINDING_CONTRACT='+json.dumps(reports))
