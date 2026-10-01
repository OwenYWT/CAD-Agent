"""Canonical application contracts -> actual kernel -> application evidence verifier."""
import ast
import json
import subprocess
from pathlib import Path
from app.contracts.acceptance import AcceptanceContract, acceptance_outcome
from app.contracts.geometry_request import geometry_request_digest
from app.validation.durable_geometry import DurableGeometryReport, verify_geometry_evidence

folder=Path(__file__).parent
rows=[]
for filename, marker, field, solids in [
    ('probe_geometry.py','GEOMETRY_REVIEW=','final_step_report',1),
    ('probe_formats.py','FORMAT_REVIEW=','with_acceptance',None)]:
    tree=ast.parse((folder/filename).read_text())
    assignment=next(n for n in tree.body if isinstance(n,ast.Assign)
        and any(isinstance(t,ast.Name) and t.id=='contract' for t in n.targets))
    contract=AcceptanceContract.model_validate(ast.literal_eval(assignment.value))
    assignment.value=ast.parse(repr(contract.model_dump(mode='json')),mode='eval').body
    source=ast.unparse(ast.fix_missing_locations(tree))
    command=['/private/tmp/cad-fixes-20260923/bin/docker','run','--rm','-i','--network','none',
        '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
        '--tmpfs','/tmp:rw,size=512m','-e','PYTHONPATH=/opt/cad-agent',
        '--entrypoint','python','cad-native-sandbox:whole-certified-test','-']
    result=subprocess.run(command,input=source,text=True,capture_output=True)
    (folder/(filename+'.canonical.log')).write_text(result.stdout+result.stderr)
    assert result.returncode==0,result.stderr
    data=json.loads(next(l[len(marker):] for l in result.stdout.splitlines() if l.startswith(marker)))
    digest=geometry_request_digest(expected_dimensions_mm={},dimension_tolerance=0.05,
        expected_solid_count=solids,acceptance=contract.model_dump(mode='json'))
    for item in data:
        report=DurableGeometryReport.model_validate(item[field])
        verify_geometry_evidence(report,request_sha256=digest,acceptance=contract,expected_solid_count=solids)
        rows.append({'probe':filename,'case':item.get('case',item.get('format')),
            'kernel_outcome':report.outcome,'server_required_check_outcome':acceptance_outcome(contract,report.acceptance.evidence),
            'evidence_verifier_accepted_report':True,'contract_digest':contract.digest(),
            'missing_material_mm3':item.get('missing_material_mm3')})
print(json.dumps(rows,indent=2))
(folder/'cross-validation.json').write_text(json.dumps(rows,indent=2)+'\n')
