"""Native stepped bores, whole axial bands and independently checked evidence."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def fixtures():
    from app.contracts.acceptance_v2 import AcceptanceContractV2
    rows=[]
    variants={'good':'passed', 'rotated':'passed', 'scaled':'passed', 'reversed_axis':'passed',
        'wrong_radius':'failed', 'wrong_height':'failed', 'cavity':'indeterminate',
        'nurbs_cavity':'indeterminate', 'drill':'indeterminate', 'notch':'indeterminate',
        'blind':'indeterminate', 'boss':'indeterminate', 'bridge':'indeterminate', 'off_axis':'indeterminate',
        'shoulder_point':'indeterminate'}
    for name,expected in variants.items():
        scale=2 if name=='scaled' else 1
        checks=[]
        for i,(radius,height,z) in enumerate([(12,7,3.5),(8,11,12.5),(15,5,20.5)]):
            point=[0,0,z*scale];axis=[0,0,1]
            if name=='rotated':point=[21+z,-4,7];axis=[1,0,0]
            if name=='reversed_axis':axis=[0,0,-1]
            if name=='off_axis':point[0]=1
            if name=='shoulder_point' and i==0:point[2]=7
            for quantity,value in [('diameter',2*radius*scale),('height',height*scale)]:
                checks.append({'check_id':f'segment-{i}-{quantity}', 'kind':'annular_segment_'+quantity,
                    'description':'Complete annular segment '+quantity, 'binding_ref':'objective',
                    'source_quote':'Measure the specified coaxial annular segments.',
                    'comparison':{'op':'eq','value':float(value),'unit':'mm'},
                    'scope':{'axis':axis,'point_mm':point}})
        contract=AcceptanceContractV2(objective='Measure the specified coaxial annular segments.',checks=checks)
        rows.append({'name':name,'expected':expected,'contract':contract.model_dump(mode='json')})
    return rows


def native(cases):
    from geometry_validation import validate_geometry_files
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        program=root/'native.py'
        program.write_text(f'''
import FreeCAD as App, Part, json
root={str(root)!r}
bop={{}}
for name in { [c['name'] for c in cases]!r}:
    scale=2 if name=='scaled' else 1
    radii=[12,9 if name=='wrong_radius' else 8,15]
    heights=[7,12 if name=='wrong_height' else 11,5]
    z=0; pieces=[]
    for radius,height in zip(radii,heights):
        pieces.append(Part.makeCylinder(radius*scale,height*scale,App.Vector(0,0,z)))
        z+=height*scale
    shape=pieces[0].fuse(pieces[1]).fuse(pieces[2]).removeSplitter()
    shape=shape.cut(Part.makeCylinder(3*scale,z-(1 if name=='blind' else 0))).removeSplitter()
    if name in ('cavity','nurbs_cavity'):
        cavity=Part.makeSphere(.2,App.Vector(10,0,3))
        if name=='nurbs_cavity':cavity=cavity.toNurbs()
        shape=shape.cut(cavity)
    if name=='drill':shape=shape.cut(Part.makeCylinder(.3,30,App.Vector(-15,0,3),App.Vector(1,0,0)))
    if name=='notch':shape=shape.cut(Part.makeBox(2,2,2,App.Vector(11,-1,2)))
    if name=='boss':shape=shape.fuse(Part.makeBox(3,2,2,App.Vector(11,-1,2))).removeSplitter()
    if name=='bridge':
        shape=shape.fuse(Part.makeCylinder(20,1,App.Vector(0,0,-1))).fuse(
            Part.makeCylinder(1,7,App.Vector(17,0,0))).cut(Part.makeCylinder(3,24,App.Vector(0,0,-1))).removeSplitter()
    if name=='rotated':
        shape.rotate(App.Vector(),App.Vector(0,1,0),90);shape.translate(App.Vector(21,-4,7))
    assert shape.isValid() and len(shape.Solids)==1,name
    try:shape.check(True);bop[name]={{'status':'passed'}}
    except ValueError as error:
        bop[name]={{'status':'failed','diagnostic':str(error)}}
        assert name=='nurbs_cavity',bop[name]
    doc=App.newDocument(name);body=doc.addObject('PartDesign::Body','Body')
    body.newObject('PartDesign::Feature','Final').Shape=shape
    doc.recompute();doc.saveAs(root+'/'+name+'.FCStd');App.closeDocument(doc.Name)
    restored=App.openDocument(root+'/'+name+'.FCStd');restored.recompute()
    Part.export([restored.Body],root+'/'+name+'.step');App.closeDocument(restored.Name)
with open(root+'/bop.json','w') as stream:json.dump(bop,stream)
print('ANNULAR_NATIVE_SAVED')
''')
        result=subprocess.run(['/opt/freecad/bin/FreeCADCmd','-c',
            f'exec(compile(open({str(program)!r}).read(),{str(program)!r},"exec"))'],capture_output=True,text=True)
        if os.environ.get('CAD_INTAKE_EVIDENCE_DIR'):
            shutil.copytree(root,Path(os.environ['CAD_INTAKE_EVIDENCE_DIR'])/'saved-models')
        assert result.returncode==0 and 'ANNULAR_NATIVE_SAVED' in result.stdout and 'Traceback' not in result.stdout+result.stderr,result.stdout+result.stderr
        reports=[]
        for case in cases:
            path=root/(case['name']+'.step');digest=hashlib.sha256(path.read_bytes()).hexdigest()
            context={'schema_version':'measurement-context.v1','base_revision_id':'base',
                'checkpoint_manifest_id':'native-segment','artifact_sha256s':[digest]}
            report=validate_geometry_files([{'role':'model','format':'step','path':str(path)}],
                acceptance=case['contract'],measurement_context=context)
            assert report['outcome']==case['expected'],(case['name'],report)
            if case['name']=='bridge':
                assert 'excess material' in str(report['acceptance']['evidence'][0]['issues'])
            assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
            reports.append({**case,'report':report,'context':context,
                'native_sha256':hashlib.sha256((root/(case['name']+'.FCStd')).read_bytes()).hexdigest(),
                'native_bop':json.loads((root/'bop.json').read_text())[case['name']]})
        print('CAD_ANNULAR_SEGMENTS='+json.dumps(reports))


def verify(path):
    from app.contracts.acceptance_v2 import decode_acceptance
    from app.contracts.measurement_binding import binding_digest
    from app.validation.durable_geometry import DurableGeometryReport,verify_geometry_evidence
    rows=json.loads(next(line.split('=',1)[1] for line in Path(path).read_text().splitlines()
                         if line.startswith('CAD_ANNULAR_SEGMENTS=')))
    mutations=0
    for row in rows:
        kwargs=dict(request_sha256=row['report']['request_sha256'],acceptance=decode_acceptance(row['contract']),
                    expected_solid_count=None,measurement_context=row['context'])
        verify_geometry_evidence(DurableGeometryReport.model_validate(row['report']),**kwargs)
        for field in ['complete','measured','height','diameter','axis','interval','missing','excess','point','area']:
            tampered=deepcopy(row['report'])
            evidence=next((e for e in tampered['acceptance']['evidence'] if e['outcome']!='indeterminate'),None)
            if evidence is None:continue
            cert=evidence['details']['annular_segment']
            if field=='complete':cert['complete']=False
            elif field=='measured':evidence['measured'][0]+=1
            elif field in {'height','diameter'}:cert['dimensions_mm'][field]+=1
            elif field=='axis':cert['axis']=[0,0,0]
            elif field=='interval':cert['axial_interval_mm'][1]+=1
            elif field=='missing':cert['missing_material_mm3']=1
            elif field=='excess':cert['excess_material_mm3']=1
            elif field=='point':cert['axis_point_mm'][0]+=1
            elif field=='area':cert['outer_surface_area_mm2']/=2
            binding=evidence['details']['measurement_binding']
            binding['proof']['selection']['annular_segment']=deepcopy(cert)
            binding['binding_digest']=binding_digest({k:v for k,v in binding.items() if k!='binding_digest'})
            try:verify_geometry_evidence(DurableGeometryReport.model_validate(tampered),**kwargs)
            except ValueError:mutations+=1
            else:raise AssertionError('Accepted tampered annular certificate: '+field)
    print('CAD_ANNULAR_RECEIVER='+json.dumps({'cases':len(rows),'rejected_mutations':mutations}))


if __name__=='__main__':
    if sys.argv[1]=='--fixtures':Path(sys.argv[2]).write_text(json.dumps(fixtures()))
    elif sys.argv[1]=='--verify':verify(sys.argv[2])
    else:native(json.loads(Path(sys.argv[1]).read_text()))
