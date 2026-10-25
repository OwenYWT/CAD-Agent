"""Source -> bound contract -> saved FCStd -> STEP -> independent gate."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile

from geometry_validation import validate_geometry_files


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    program = root / 'geometry.py'
    program.write_text(f'''
import FreeCAD as App, Part, json, math
root={str(root)!r}
for name in ['one_hole','components','oversize','wrong_lid',
             'distributed_correct','distributed_box_missing','distributed_lid_missing',
             'distributed_box_three','distributed_lid_three',
             'distributed_box_wrong_diameter','distributed_lid_wrong_diameter',
             'mixed_correct','mixed_same_diameter','mixed_missing','mixed_extra',
             'mixed_unmatched_extra','mixed_wrong_count','mixed_wrong_diameter','mixed_blind','mixed_zero',
             'mixed_extra_conical','mixed_extra_square','mixed_extra_spline','mixed_extra_conical_blind',
             'mixed_extra_side_square',
             'mixed_extra_analytic_blind_micro','mixed_extra_analytic_blind_small','mixed_extra_analytic_blind_large',
             'mixed_extra_nurbs_blind_micro','mixed_extra_nurbs_blind_small','mixed_extra_nurbs_blind_large',
             'mixed_extra_nurbs_blind_split4','mixed_extra_nurbs_blind_split8',
             'mixed_extra_sphere_blind','mixed_four_blind',
             'grouped_components_correct','grouped_components_box_wrong','grouped_components_lid_wrong']:
    doc=App.newDocument('SourceObligations')
    if name=='one_hole':
        shapes=[Part.makeBox(80,40,8).cut(Part.makeCylinder(3,8,App.Vector(40,20,0)))]
    elif name.startswith('mixed_'):
        diameters={{'mixed_correct':[6,6,4,4], 'mixed_same_diameter':[6,6,6,6], 'mixed_missing':[6,4],
                    'mixed_extra':[6,6,4,4,6], 'mixed_unmatched_extra':[6,6,4,4,3],
                    'mixed_wrong_count':[6,6,6,4], 'mixed_wrong_diameter':[6,6,4,5],
                    'mixed_blind':[6,6,4,4], 'mixed_zero':[]}}.get(name, [6,6,4,4])
        plate=Part.makeBox(80,40,8)
        for index,diameter in enumerate(diameters):
            depth=4 if name=='mixed_four_blind' or (name=='mixed_blind' and index==2) else 8
            plate=plate.cut(Part.makeCylinder(diameter/2,depth,App.Vector(10+index*14,20,0)))
        if name in ('mixed_extra_conical','mixed_extra_conical_blind'):
            plate=plate.cut(Part.makeCone(2,3,8 if name=='mixed_extra_conical' else 4,App.Vector(68,20,0)))
        elif name=='mixed_extra_square':
            plate=plate.cut(Part.makeBox(4,4,8,App.Vector(66,18,0)))
        elif name=='mixed_extra_side_square':
            plate=plate.cut(Part.makeBox(4,40,4,App.Vector(66,0,2)))
        elif name=='mixed_extra_spline':
            curve=Part.BSplineCurve()
            curve.interpolate(Points=[App.Vector(68+3*math.cos(i*math.pi/4),20+2*math.sin(i*math.pi/4),0)
                                      for i in range(8)],PeriodicFlag=True)
            plate=plate.cut(Part.Face(Part.Wire(curve.toShape())).extrude(App.Vector(0,0,8)))
        elif '_blind_' in name:
            size=name.rsplit('_',1)[1]
            radius=.01 if size.startswith('split') else {{'micro':.01,'small':.1,'large':1.5}}[size]
            segments=int(size[-1]) if size.startswith('split') else 1
            for index in range(segments):
                tool=Part.makeCylinder(radius,4,App.Vector(68,20,4),App.Vector(0,0,1),360/segments)
                tool.rotate(App.Vector(68,20,4),App.Vector(0,0,1),index*360/segments)
                if '_nurbs_' in name: tool=tool.toNurbs()
                plate=plate.cut(tool)
        elif name=='mixed_extra_sphere_blind':
            plate=plate.cut(Part.makeSphere(.01,App.Vector(68,20,8)))
        shapes=[plate]
    else:
        extra=2 if name=='oversize' else 0
        box=Part.makeBox(80+extra,55,32).cut(Part.makeBox(74+extra,49,29,App.Vector(3,3,3)))
        lid=Part.makeBox(80,55,3,App.Vector(90,0,0))
        for index,(x,y) in enumerate([(6,6),(74,6),(6,49),(74,49)]):
            box_count=0 if name=='distributed_box_missing' else 3 if name=='distributed_box_three' else 4
            lid_count=0 if name=='distributed_lid_missing' else 3 if name=='distributed_lid_three' else 4
            box_radius=2.5 if name=='distributed_box_wrong_diameter' else 3 if name.startswith('distributed') else 2.25
            lid_radius=2.5 if name=='distributed_lid_wrong_diameter' else 3 if name.startswith('distributed') else 2 if name=='wrong_lid' else 1.7
            if name.startswith('grouped_components'):
                box_radius=3 if index<2 or (index==2 and name=='grouped_components_box_wrong') else 2
                lid_radius=3 if index<2 or (index==2 and name=='grouped_components_lid_wrong') else 2
            if index<box_count: box=box.cut(Part.makeCylinder(box_radius,3,App.Vector(x,y,0)))
            if index<lid_count: lid=lid.cut(Part.makeCylinder(lid_radius,3,App.Vector(x+90,y,0)))
        shapes=[box,lid]
    for index,shape in enumerate(shapes):
        assert shape.isValid() and len(shape.Solids)==1
        doc.addObject('Part::Feature','Solid'+str(index)).Shape=shape
    doc.recompute(); path=root+'/'+name
    doc.saveAs(path+'.FCStd'); App.closeDocument(doc.Name)
    restored=App.openDocument(path+'.FCStd'); restored.recompute()
    assert all(o.Shape.isValid() for o in restored.Objects)
    Part.export(restored.Objects,path+'.step'); App.closeDocument(restored.Name)
    if name.startswith('mixed_extra_') and name!='mixed_extra_conical_blind':
        # Independent opening oracle on both saved formats, not the detector.
        native=App.openDocument(path+'.FCStd')
        for candidate in [native.Solid0.Shape,Part.read(path+'.step')]:
            assert candidate.isValid() and len(candidate.Solids)==1
            if '_split' in name: candidate.check(True)
            for z in (0,8):
                face=next(f for f in candidate.Faces if isinstance(f.Surface,Part.Plane)
                          and abs(f.CenterOfMass.z-z)<1e-7 and abs(abs(f.normalAt(0,0).z)-1)<1e-7)
                count=4 if name=='mixed_extra_side_square' or ('blind' in name and z==0) else 5
                assert len(face.Wires)-1==count, (name,z,len(face.Wires))
            if name=='mixed_extra_side_square':
                for y in (0,40):
                    face=next(f for f in candidate.Faces if isinstance(f.Surface,Part.Plane)
                              and abs(f.CenterOfMass.y-y)<1e-7 and abs(abs(f.normalAt(0,0).y)-1)<1e-7)
                    assert len(face.Wires)-1==1
        App.closeDocument(native.Name)
# Representation metamorphism: equivalent source solids must never disagree
# on permission to pass. Independent Boolean and opening oracles use both files.
for size in ('micro','small','large'):
    a=Part.read(root+'/mixed_extra_analytic_blind_'+size+'.step')
    b=Part.read(root+'/mixed_extra_nurbs_blind_'+size+'.step')
    assert abs(a.cut(b).Volume)<1e-7 and abs(b.cut(a).Volume)<1e-7
for segments in (4,8):
    a=Part.read(root+'/mixed_extra_analytic_blind_micro.step')
    b=Part.read(root+'/mixed_extra_nurbs_blind_split'+str(segments)+'.step')
    assert abs(a.cut(b).Volume)<1e-7 and abs(b.cut(a).Volume)<1e-7
# Independent readback oracle; no expected-radius filter selects the geometry.
doc=App.newDocument('MixedGroups')
shape=Part.makeBox(80,40,8)
for x,radius in [(12,3),(30,3),(48,2),(66,2)]:
    shape=shape.cut(Part.makeCylinder(radius,8,App.Vector(x,20,0)))
doc.addObject('Part::Feature','Plate').Shape=shape
doc.recompute(); doc.saveAs(root+'/mixed-groups.FCStd'); App.closeDocument(doc.Name)
restored=App.openDocument(root+'/mixed-groups.FCStd')
Part.export(restored.Objects,root+'/mixed-groups.step')
readback=Part.read(root+'/mixed-groups.step')
observations=[]
for candidate in [restored.Plate.Shape,readback]:
    diameters=sorted(round(2*face.Surface.Radius,6) for face in candidate.Faces
                     if isinstance(face.Surface,Part.Cylinder))
    assert candidate.isValid() and len(candidate.Solids)==1
    assert diameters==[4,4,6,6], diameters
    assert abs(candidate.Volume-(80*40*8-math.pi*(9+9+4+4)*8))<1e-6
    observations.append(dict(diameters_mm=diameters,volume_mm3=candidate.Volume,solids=1))
App.closeDocument(restored.Name)
open(root+'/mixed-groups-observations.json','w').write(json.dumps(observations))
print('SOURCE_OBLIGATION_MODELS_SAVED')
''')
    run = subprocess.run(['/opt/freecad/bin/FreeCADCmd', '-c',
        f'exec(compile(open({str(program)!r}).read(),{str(program)!r},"exec"))'], capture_output=True, text=True)
    assert run.returncode == 0 and 'SOURCE_OBLIGATION_MODELS_SAVED' in run.stdout and 'Traceback' not in run.stdout + run.stderr, run.stdout + run.stderr
    fixtures = json.loads(Path('/sandbox/input/source-obligations.json').read_text())
    reports = []
    for case in fixtures['cases']:
        path = root / (case['model'] + '.step')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        contract_digest = hashlib.sha256(json.dumps(case['contract'], sort_keys=True).encode()).hexdigest()
        context = {'schema_version': 'measurement-context.v1', 'base_revision_id': 'source-obligations',
                   'checkpoint_manifest_id': case['model'], 'artifact_sha256s': [digest]}
        report = validate_geometry_files([{'role': 'final', 'format': 'step', 'path': str(path)}],
            expected_solid_count=1 if case['model'] == 'one_hole' or case['model'].startswith('mixed_') else 2,
            acceptance=case['contract'], measurement_context=context)
        assert report['outcome'] == case['expected'], (case, report)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        assert hashlib.sha256(json.dumps(case['contract'], sort_keys=True).encode()).hexdigest() == contract_digest
        reports.append({'objective': case['contract']['objective'], 'model': case['model'],
                        'outcome': report['outcome'], 'evidence': report['acceptance']['evidence'],
                        'report': report, 'measurement_context': context})
    # Topology is independent of world axes and surface analytic type. Recheck
    # the same saved models in a non-axis-aligned frame without extent checks.
    import cadquery as cq
    from feature_verification import measure_checks
    from acceptance_comparison import COMPARISON_POLICY_ID
    contract = next(c['contract'] for c in fixtures['cases'] if c['model']=='mixed_correct')
    groups = [c for c in contract['checks'] if c.get('hole_group')]
    rotated = {}
    for name in ('mixed_correct', 'mixed_extra_conical', 'mixed_extra_square', 'mixed_extra_spline',
                 'mixed_extra_side_square', 'mixed_extra_nurbs_blind_micro', 'mixed_extra_sphere_blind',
                 'mixed_extra_nurbs_blind_split4', 'mixed_extra_nurbs_blind_split8'):
        shape = cq.importers.importStep(str(root/(name+'.step'))).val()
        moved = shape.rotate((0,0,0),(1,2,3),37).translate((13,-9,17))
        rows = measure_checks(moved, groups, acceptance_version='engineering-acceptance.v2',
                              comparison_policy_id=COMPARISON_POLICY_ID)
        expected = 'passed' if name=='mixed_correct' else 'indeterminate'
        assert rows and all(r['outcome']==expected for r in rows), (name,rows)
        rotated[name] = rows
    if os.environ.get('CAD_SOURCE_EVIDENCE_DIR'):
        shutil.copytree(root, Path(os.environ['CAD_SOURCE_EVIDENCE_DIR']) / 'saved-models')
    print('CAD_SOURCE_OBLIGATION_CONTRACT=' + json.dumps({'cases': reports,
        'rotated_inventory_evidence': rotated,
        'unscoped_multigroup_readback': json.loads((root / 'mixed-groups-observations.json').read_text()),
        'rejected_before_dispatch': fixtures['rejected_before_dispatch']}, ensure_ascii=False))
