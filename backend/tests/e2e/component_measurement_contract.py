"""FreeCAD -> saved STEP: component selection must never clip to the target."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import cadquery as cq
from feature_verification import measure_checks
from geometry_validation import validate_geometry_files
from measurement_binding import verify_measurement_binding


def checks(region, diameter, count, length):
    scope = {'frame':'world', 'component_region':region}
    result = [{'check_id':'bounds', 'kind':'bounding_box',
               'comparison':{'op':'eq','value':0.,'unit':'mm'}, 'scope':scope}]
    for kind, value, unit in [('overall_dimension',length,'mm'), ('hole_count',count,'count'),
                              ('hole_diameter',diameter,'mm'), ('hole_through',1.,'count')]:
        result.append({'check_id':kind, 'kind':kind, 'comparison':{'op':'eq','value':value,'unit':unit},
                       'scope':{**scope, 'axis':[0,0,1] if kind.startswith('hole_') else [1,0,0]}})
    for check in result:
        check['required'] = True
    return result


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    program = root/'fixture.py'
    program.write_text(f'''
import FreeCAD as App, Part
root={str(root)!r}
for extra in [0,2]:
    doc=App.newDocument('Components')
    box=Part.makeBox(80+extra,55,32).cut(Part.makeBox(74+extra,49,29,App.Vector(3,3,3)))
    lid=Part.makeBox(80,55,3,App.Vector(90,0,0))
    for x,y in [(6,6),(74,6),(6,49),(74,49)]:
        box=box.cut(Part.makeCylinder(2.25,3,App.Vector(x,y,0)))
        lid=lid.cut(Part.makeCylinder(1.7,3,App.Vector(x+90,y,0)))
    box=box.cut(Part.makeCylinder(6.25,3,App.Vector(77+extra,27.5,16),App.Vector(1,0,0)))
    for name,shape in [('Box',box),('Lid',lid)]:
        feature=doc.addObject('PartDesign::Body',name).newObject('PartDesign::Feature',name+'Shape')
        feature.Shape=shape
        assert shape.isValid() and len(shape.Solids)==1
    doc.recompute()
    path=root+'/'+str(extra)
    doc.saveAs(path+'.FCStd'); App.closeDocument(doc.Name)
    restored=App.openDocument(path+'.FCStd'); restored.recompute()
    Part.export([restored.getObject('Box'),restored.getObject('Lid')],path+'.step')
    App.closeDocument(restored.Name)
print('COMPONENT_FIXTURES_SAVED')
''')
    process = subprocess.run(['/opt/freecad/bin/FreeCADCmd','-c',
        f'exec(compile(open({str(program)!r}).read(),{str(program)!r},"exec"))'],capture_output=True,text=True)
    assert process.returncode==0 and 'COMPONENT_FIXTURES_SAVED' in process.stdout and 'Traceback' not in process.stdout+process.stderr,process.stdout+process.stderr
    reports = {}
    for extra in (0,2):
        path = root/f'{extra}.step'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        shape = cq.importers.importStep(str(path)).val()
        for label,region,diameter in [('box',{'min_mm':[0,0,0],'max_mm':[80,55,32]},4.5),
                                      ('lid',{'min_mm':[90,0,0],'max_mm':[170,55,3]},3.4)]:
            rows = measure_checks(shape,checks(region,diameter,4.,80.),
                acceptance_version='engineering-acceptance.v2',comparison_policy_id='occt-confusion-v1')
            assert [r['outcome'] for r in rows] == (['failed','failed','passed','passed','passed']
                if extra and label=='box' else ['passed']*5),rows
            assert rows[2]['measured']==[4],rows
            assert all(not r['details']['component_selection']['clipped'] for r in rows)
            reports[f'{label}-{extra}']=rows
            # Exercise the actual exported-file gate and its frozen scope /
            # artifact evidence, not only the analytic measurement function.
            contract={'schema_version':'engineering-acceptance.v2',
                      'checks':checks(region,diameter,4.,80.), 'comparison_policy_id':'occt-confusion-v1'}
            context={'schema_version':'measurement-context.v1', 'base_revision_id':'source',
                     'checkpoint_manifest_id':'exported-components', 'artifact_sha256s':[digest]}
            report=validate_geometry_files([{'role':'final','format':'step','path':str(path)}],
                expected_solid_count=2, acceptance=contract, measurement_context=context)
            measured=report['acceptance']['evidence']
            assert [r['outcome'] for r in measured]==[r['outcome'] for r in rows],report
            for requested, item in zip(contract['checks'],measured):
                binding=item['details']['measurement_binding']
                verify_measurement_binding(binding,check=requested,
                    acceptance_digest=report['acceptance']['contract_sha256'],
                    context=context,artifact_sha256s=[digest])
                assert binding['proof']['selection']['component_selection']['region']==region
        for region in [{'min_mm':[-1,-1,-1],'max_mm':[180,60,40]},
                       {'min_mm':[200,0,0],'max_mm':[210,10,10]}]:
            row = measure_checks(shape, checks(region,4.5,4.,80.)[:1],
                acceptance_version='engineering-acceptance.v2',comparison_policy_id='occt-confusion-v1')[0]
            assert row['outcome']=='indeterminate' and row['details']['cad_source_repair_allowed'] is False,row
        assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
    print('CAD_COMPONENT_MEASUREMENT_CONTRACT='+json.dumps(reports))
