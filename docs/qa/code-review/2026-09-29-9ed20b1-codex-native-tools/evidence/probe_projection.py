import json
from pathlib import Path
from uuid import uuid4
from app.execution.canonical import canonical_sha256
from app.services.task_evidence import project_evidence
from app.models.workflow_requests import McadAgentWorkflowV2Request
from app.freecad.operation_generator import FreeCADOperationGenerator

folder=Path(__file__).parent
report=json.loads((folder/'geometry-probe.json').read_text())[0]['final_step_report']
row={'id':uuid4(),'workflow_run_id':uuid4(),'staging_manifest_id':uuid4(),
     'evidence':report,'evidence_hash':canonical_sha256(report),
     'gate':'geometry','mode':'required','outcome':report['outcome']}
revision={'id':uuid4(),'manifest':{'selected_manifests':[{
    'staging_manifest_id':str(row['staging_manifest_id'])}],
    'validation':{'gates':[{'evidence_id':str(row['id']),'evidence_hash':row['evidence_hash']}]}}}
projected=project_evidence(row,revision)
assert projected['selected_for_revision']
result={'stored_report_keys':sorted(report),'public_report_keys':sorted(projected['report']),
    'dropped_report_keys':sorted(set(report)-set(projected['report'])),
    'selected_for_revision':projected['selected_for_revision']}
req=McadAgentWorkflowV2Request(**{key:uuid4() for key in ['workflow_run_id','tenant_id','project_id',
    'principal_id','branch_id','expected_base_revision_id']},operation='generate',
    modeling_backend='freecad',objective='20 mm cube',output_formats=('stl',))
result['stl_request_accepted']=True
result['stl_generated_formats']=FreeCADOperationGenerator._required_formats(req.output_formats)
(folder/'projection-probe.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
