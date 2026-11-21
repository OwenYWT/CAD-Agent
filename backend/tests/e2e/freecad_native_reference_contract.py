"""Real native references: retain one hole's identity through edits and reloads."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from copy import deepcopy

sys.path.insert(0, '/opt/cad-agent')
import FreeCAD as App
import Part
import freecad_entry as runner
from freecad_native_references import NativeReferences, NativeReferenceError
from freecad_native_reference_contracts import verify_transition


def plate_plan(*, width=40, depth=30, centers=((10,15),(30,15))):
    operations = []
    def add(action, **args):
        operations.append({'op_id': f'create-{len(operations)}', 'action': action, 'args': args})
    add('sketch.create', name='Profile')
    add('sketch.add_profile', sketch='Profile', geometry={'kind': 'rectangle',
        'corner': {'x': 0, 'y': 0}, 'width_mm': width, 'height_mm': depth})
    add('feature.pad', name='Pad', profile='Profile', length_mm=12)
    add('sketch.create', name='Holes', offset_mm=12)
    for x,y in centers:
        add('sketch.add_profile', sketch='Holes', geometry={'kind': 'circle',
            'center': {'x': x, 'y': y}, 'radius_mm': 3})
    add('feature.hole', name='Hole', profile='Holes', diameter_mm=6, through_all=True)
    add('document.export', formats=['fcstd', 'step'], basename='model')
    return {'schema_version': 'freecad-operation-plan.v1', 'document_name': 'References', 'operations': operations}


def run(root, name, plan, base=None, *, catalog_override=None):
    runner.INPUT_ROOT = root / name / 'input'; runner.INPUT_ROOT.mkdir(parents=True)
    runner.OUTPUT_ROOT = root / name / 'output'; runner.OUTPUT_ROOT.mkdir()
    params = {'plan': plan, 'expected_revision_id': '11111111-1111-4111-8111-111111111111'}
    inputs = {}
    if base:
        shutil.copyfile(base['files']['fcstd'], runner.INPUT_ROOT / 'base.FCStd')
        state = json.loads(Path(base['files']['state']).read_text())
        params['native_topology'] = catalog_override if catalog_override is not None else state['native_topology']
        inputs['base'] = 'base.FCStd'
    return runner.run_task({'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
        'operation': 'execute', 'inputs': inputs, 'params': params})


def main(root):
    original = run(root, 'original', plate_plan())
    state = json.loads(Path(original['files']['state']).read_text())
    catalog = state['native_topology']
    owner = catalog['objects']['Hole']
    chosen = [r for r in owner['references'] if r['subelement_kind'] == 'edge'
              and r['geometry_type'] == 'Part::GeomCircle'
              and all(abs(a-b) < 1e-7 for a,b in zip(r['center_mm'], [10,15,12]))]
    assert len(chosen) == 1, chosen
    reference = chosen[0]
    selector = {'schema_version': 'topology-selector.v2', 'backend': 'freecad',
        'revision_id': '11111111-1111-4111-8111-111111111111', 'object_name': 'Hole',
        'subelement_kind': 'edge', 'reference_id': reference['reference_id']}
    plan = {'schema_version': 'freecad-operation-plan.v1', 'document_name': 'References', 'operations': [
        {'op_id': 'resize', 'action': 'property.set', 'args': {'object': 'Hole', 'property': 'Diameter', 'value': 8}},
        {'op_id': 'one-rim', 'action': 'feature.chamfer', 'args': {'name': 'Chamfer', 'target': 'Hole',
            'size_mm': .5, 'use_all_edges': False, 'selector': selector}},
        {'op_id': 'export-edit', 'action': 'document.export', 'args': {'formats': ['fcstd','step'], 'basename':'model'}},
    ]}
    edited = run(root, 'resized', plan, original)
    doc = App.openDocument(edited['files']['fcstd'])
    try:
        shapes = [doc.Body.Tip.Shape, Part.read(edited['files']['step'])]
        for shape in shapes:
            assert shape.isValid() and len(shape.Solids) == 1
            cones = [f for f in shape.Faces if f.Surface.TypeId == 'Part::GeomCone']
            assert len(cones) == 1
            assert abs(cones[0].Surface.Center.x-10) < 1e-7
            assert abs(cones[0].Surface.Center.y-15) < 1e-7
        assert shapes[0].cut(shapes[1]).Volume + shapes[1].cut(shapes[0]).Volume < 1e-6
    finally:
        App.closeDocument(doc.Name)
    after = json.loads(Path(edited['files']['state']).read_text())['native_topology']
    verify_transition(catalog,plate_plan())
    verify_transition(after,plan,catalog)
    assert after['objects']['Hole']['incarnation'] == owner['incarnation']
    assert reference['reference_id'] in [r['reference_id'] for r in after['objects']['Hole']['references']]
    assert catalog['fcstd_sha256'] == hashlib.sha256(Path(original['files']['fcstd']).read_bytes()).hexdigest()
    # The native dependent Chamfer follows the original hole after another save,
    # reopen and center edit, rather than snapping to the same-radius neighbour.
    hole_state = next(o for o in json.loads(Path(edited['files']['state']).read_text())['objects'] if o['name']=='Holes')
    constraint = next(c for c in hole_state['inspection']['constraints']['items']
                      if c['type']=='DistanceX' and abs(c['value']-10)<1e-7)
    move={'schema_version':'freecad-operation-plan.v1','document_name':'References','operations':[
        {'op_id':'move-center','action':'sketch.set_constraint','args':{'sketch':'Holes','constraint_index':constraint['index'],
            'expected_type':'DistanceX','value_mm':13}},
        {'op_id':'export-moved','action':'document.export','args':{'formats':['fcstd','step'],'basename':'model'}}]}
    moved=run(root,'moved',move,edited)
    moved_doc=App.openDocument(moved['files']['fcstd'])
    try:
        for shape in (moved_doc.Body.Tip.Shape,Part.read(moved['files']['step'])):
            cones=[f for f in shape.Faces if f.Surface.TypeId=='Part::GeomCone']
            assert shape.isValid() and len(cones)==1 and abs(cones[0].Surface.Center.x-13)<1e-7
    finally:App.closeDocument(moved_doc.Name)
    rejected=[]
    for name, mutate, code in (
        ('unknown-id',lambda s:s.update(reference_id='0'*64),'topology_reference_unverified'),
        ('wrong-owner',lambda s:s.update(object_name='Pad'),'topology_reference_unverified'),
        ('stale-revision',lambda s:s.update(revision_id='22222222-2222-4222-8222-222222222222'),'topology_reference_stale'),
    ):
        invalid=deepcopy(plan);mutate(invalid['operations'][1]['args']['selector'])
        try:run(root,name,invalid,original)
        except runner.FreeCADRunnerError as error:assert error.code==code,(name,error.code)
        else:raise AssertionError(name+' was accepted')
        assert not (root/name/'output'/'model.FCStd').exists()
        rejected.append(name)
    wrong_file=deepcopy(catalog);wrong_file['fcstd_sha256']='0'*64
    try:run(root,'wrong-file',plan,original,catalog_override=wrong_file)
    except runner.FreeCADRunnerError as error:assert error.code=='topology_reference_catalog_invalid'
    else:raise AssertionError('another source FCStd was accepted')
    rejected.append('wrong-file')
    for name in ('deleted','same-name-recreated','mapping-lost'):
        doc=App.openDocument(original['files']['fcstd'])
        tracker=NativeReferences(doc,catalog,fcstd_sha256=catalog['fcstd_sha256'])
        try:
            if name=='mapping-lost':doc.Hole.Shape=Part.makeBox(40,30,12)
            else:
                doc.removeObject('Hole')
                if name=='same-name-recreated':doc.Body.newObject('PartDesign::Hole','Hole')
            try:tracker.resolve(doc,selector,selector['revision_id'])
            except NativeReferenceError as error:
                assert error.code in {'topology_owner_recreated','topology_reference_missing','topology_mapping_unavailable'}
            else:raise AssertionError(name+' silently rebound')
            rejected.append(name)
        finally:App.closeDocument(doc.Name)
    # Complete enumeration is independent of the UI's 64-element summary.
    large=run(root,'many-holes',plate_plan(width=120,depth=100,
        centers=tuple((x,y) for x in (10,30,50,70,90) for y in (10,30,50,70,90))))
    many=json.loads(Path(large['files']['state']).read_text())['native_topology']
    many_doc=App.openDocument(large['files']['fcstd'])
    try:
        assert len(many_doc.Hole.Shape.Edges)>64
        rows=[r for r in many['objects']['Hole']['references'] if r['subelement_kind']=='edge'
              and int(many_doc.Hole.Shape.ElementMap[r['mapped_name']][4:])>64]
        assert rows
        target={**selector,'reference_id':rows[-1]['reference_id']}
        NativeReferences(many_doc,many,fcstd_sha256=many['fcstd_sha256']).resolve(many_doc,target,selector['revision_id'])
    finally:App.closeDocument(many_doc.Name)
    # A forged property inside an imported FCStd is never creation authority.
    imported_path = root/'forged.FCStd'
    imported_doc = App.openDocument(original['files']['fcstd'])
    imported_doc.Hole.addProperty('App::PropertyString', 'AgentNativeTopology')
    imported_doc.Hole.AgentNativeTopology = json.dumps(catalog)
    imported_doc.saveAs(str(imported_path))
    App.closeDocument(imported_doc.Name)
    runner.INPUT_ROOT = root/'import'/'input'; runner.INPUT_ROOT.mkdir(parents=True)
    runner.OUTPUT_ROOT = root/'import'/'output'; runner.OUTPUT_ROOT.mkdir()
    shutil.copyfile(imported_path, runner.INPUT_ROOT/'base.FCStd')
    imported = runner.run_task({'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute',
        'inputs':{'base':'base.FCStd'},'params':{'import_format':'fcstd','plan':{
            'schema_version':'freecad-operation-plan.v1','document_name':'Import','operations':[
                {'op_id':'inspect','action':'document.inspect','args':{}},
                {'op_id':'export','action':'document.export','args':{'formats':['fcstd','step'],'basename':'model'}}]}}})
    imported_catalog = json.loads(Path(imported['files']['state']).read_text())['native_topology']
    assert imported_catalog['objects'] == {} and imported_catalog['uses'] == []
    api_plan = {'schema_version':'freecad-operation-plan.v1','document_name':'API','operations':[
        {'op_id':'program','action':'api.execute','args':{'source':'document.Hole.Diameter=9\ndocument.recompute()'}},
        {'op_id':'export','action':'document.export','args':{'objects':['Body'],'formats':['fcstd','step'],'basename':'model'}}]}
    api = run(root, 'api-edit', api_plan, edited)
    api_catalog = json.loads(Path(api['files']['state']).read_text())['native_topology']
    assert api_catalog['objects'] == {} and api_catalog['uses'] == []
    verify_transition(api_catalog, api_plan, after)
    # Inspect the real dependent link, not just the continued existence of its owner.
    dependent_doc = App.openDocument(edited['files']['fcstd'])
    try:
        tracker = NativeReferences(dependent_doc, after, fcstd_sha256=after['fcstd_sha256'])
        tracker.verify_uses(dependent_doc, selector['revision_id'])
        neighbor = next(r for r in after['objects']['Hole']['references'] if r['subelement_kind']=='edge'
            and r['geometry_type']=='Part::GeomCircle' and abs(r['center_mm'][0]-30)<1e-7 and abs(r['center_mm'][2]-12)<1e-7)
        wrong = tracker.resolve(dependent_doc, {**selector,'reference_id':neighbor['reference_id']}, selector['revision_id'])
        dependent_doc.Chamfer.Base = (dependent_doc.Hole, [wrong['subelement_name']])
        dependent_doc.recompute()
        assert dependent_doc.Chamfer.Shape.isValid()
        try:tracker.verify_uses(dependent_doc, selector['revision_id'])
        except NativeReferenceError as error:assert error.code=='topology_dependency_changed'
        else:raise AssertionError('dependent dress-up silently changed its source edge')
        rejected.append('dependent-retargeted')
    finally:App.closeDocument(dependent_doc.Name)
    # Exercise both native cut types and both dress-ups on the symmetric pair.
    variants = []
    for cut_type, dressup in (('hole','fillet'), ('pocket','fillet'), ('pocket','chamfer')):
        name = cut_type+'-'+dressup
        raw = plate_plan()
        if cut_type=='pocket':
            cut = next(op for op in raw['operations'] if op['action']=='feature.hole')
            cut['action']='feature.pocket';cut['args'].pop('diameter_mm')
        base = run(root, name+'-base', raw)
        source_state = json.loads(Path(base['files']['state']).read_text())
        source_catalog = source_state['native_topology']
        rim = next(r for r in source_catalog['objects']['Hole']['references'] if r['subelement_kind']=='edge'
            and r['geometry_type']=='Part::GeomCircle' and all(abs(a-b)<1e-7 for a,b in zip(r['center_mm'],[10,15,12])))
        edit = deepcopy(plan)
        if cut_type=='pocket':
            sketch = next(o for o in source_state['objects'] if o['name']=='Holes')
            radius = next(c for c in sketch['inspection']['constraints']['items'] if c['first']==0 and c['type']=='Radius')
            edit['operations'][0]={'op_id':'resize','action':'sketch.set_constraint','args':{
                'sketch':'Holes','constraint_index':radius['index'],'expected_type':'Radius','value_mm':4}}
        edge = edit['operations'][1]
        edge['action']='feature.'+dressup
        edge['args'].update(name='Dressup',selector={**selector,'reference_id':rim['reference_id']})
        if dressup=='fillet':edge['args']['radius_mm']=edge['args'].pop('size_mm')
        result=run(root,name,edit,base)
        output_catalog=json.loads(Path(result['files']['state']).read_text())['native_topology']
        verify_transition(output_catalog,edit,source_catalog)
        document=App.openDocument(result['files']['fcstd'])
        try:
            NativeReferences(document,output_catalog,fcstd_sha256=output_catalog['fcstd_sha256']).verify_uses(document,selector['revision_id'])
            shapes=[document.Body.Tip.Shape,Part.read(result['files']['step'])]
            for shape in shapes:
                assert shape.isValid() and not shape.check(True)
                dressed=[f for f in shape.Faces if f.Surface.TypeId==('Part::GeomCone' if dressup=='chamfer' else 'Part::GeomToroid')]
                assert len(dressed)==1 and abs(dressed[0].Surface.Center.x-10)<1e-7 and abs(dressed[0].Surface.Center.y-15)<1e-7
            assert shapes[0].cut(shapes[1]).Volume+shapes[1].cut(shapes[0]).Volume<1e-6
        finally:App.closeDocument(document.Name)
        variants.append(name)
    assert catalog['fcstd_sha256']==hashlib.sha256(Path(original['files']['fcstd']).read_bytes()).hexdigest()
    print('CAD_NATIVE_REFERENCE_CONTRACT=' + json.dumps({'passed': True, 'same_hole_after_resize': True,
        'same_hole_after_move_and_reload':True,'beyond_64_elements':True,'rejected':rejected,
        'native_and_step_reopened': True, 'import_cannot_forge_origin':True, 'api_clears_origin':True,
        'cut_dressup_variants':variants, 'original_sha256': catalog['fcstd_sha256']}))


if __name__ == '__main__':
    path = os.environ.get('CAD_NATIVE_REFERENCE_EVIDENCE_DIR')
    if path:
        main(Path(path))
    else:
        with tempfile.TemporaryDirectory() as directory:
            main(Path(directory))
