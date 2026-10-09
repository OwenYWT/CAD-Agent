"""Real kernel regression for relation edits, topology, imports and delivery.

Run with FreeCADCmd in the locked sandbox, never with a simulated CAD kernel.
"""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, '/opt/cad-agent')
import FreeCAD as App
import Part
import Sketcher
import freecad_entry as runner
from freecad_state_projector import project_object
from freecad_engineering import EngineeringError, run_engineering
from freecad_constraint_relationships import native_args, relation_rows, satisfies
from freecad_scene import tessellate_scene, LOD
from freecad_release import run_release

INPUT=Path('/sandbox/input')
OUTPUT=Path('/sandbox/output')
RELATION='relation_'+'a'*32
SOURCE={'document_id':'product-regression','revision_id':'source-revision','state_version':1}


def plan_task(operations,base,**params):
    return {'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute',
        'params':{'plan':{'schema_version':'freecad-operation-plan.v1','document_name':'ProductRegression',
            'execution_mode':'final','operations':operations},'expected_revision_id':SOURCE['revision_id'],**params},
        'inputs':{'base':base}}


def export(identity,basename,objects=None):
    return {'op_id':identity,'action':'document.export','args':{'formats':['fcstd','step','stl'],'basename':basename,**({'objects':objects} if objects else {})}}


def make_fixture():
    doc=App.newDocument('ProductBaseline')
    body=doc.addObject('PartDesign::Body','Body')
    sketch=body.newObject('Sketcher::SketchObject','Sketch')
    points=[(0,0),(10,0),(10,8),(0,8)]
    for index,point in enumerate(points):
        end=points[(index+1)%4]
        sketch.addGeometry(Part.LineSegment(App.Vector(*point,0),App.Vector(*end,0)),False)
    for kind,index in [('Horizontal',0),('Vertical',1),('Horizontal',2),('Vertical',3)]:
        sketch.addConstraint(Sketcher.Constraint(kind,index))
    for index in range(4):
        sketch.addConstraint(Sketcher.Constraint('Coincident',index,2,(index+1)%4,1))
    sketch.addConstraint(Sketcher.Constraint('Coincident',0,1,-1,1))
    sketch.addConstraint(Sketcher.Constraint('Distance',0,10.0))
    sketch.addConstraint(Sketcher.Constraint('Distance',1,8.0))
    sketch.renameConstraint(4,RELATION)
    sketch.addProperty('App::PropertyStringList','CADAgentRelationOrigins','CAD Agent')
    sketch.CADAgentRelationOrigins=[RELATION]
    pad=body.newObject('PartDesign::Pad','Pad');pad.Profile=sketch;pad.Length=5
    other=doc.addObject('Part::Feature','Other');other.Shape=Part.makeBox(10,8,5)
    other.Placement.Base=App.Vector(30,0,0)
    cylinder=doc.addObject('Part::Feature','Cylinder');cylinder.Shape=Part.makeCylinder(3,5)
    cylinder.Placement.Base=App.Vector(60,0,0)
    runner._ledger(doc)
    doc.recompute()
    assert sketch.FullyConstrained and sketch.DoF==0 and abs(body.Shape.Volume-400)<1e-7
    fingerprint=project_object(sketch)['sketch_constraints_sha256']
    doc.saveAs(str(INPUT/'baseline.FCStd'))
    body.Shape.exportStep(str(INPUT/'baseline.step'))
    App.closeDocument(doc.Name)
    return fingerprint


def measure(mode,value,**extras):
    params={'kind':'native_measure','component_name':'Body','measurement':mode,'selectors':[],
        'expected_revision_id':SOURCE['revision_id'],**extras}
    result=run_engineering({'params':params,'inputs':{'base':'baseline.FCStd'}},INPUT,OUTPUT)
    report=json.loads(Path(result['files']['engineering_report']).read_text())
    assert abs(report['value']-value)<1e-6,report
    assert report['status']=='measured' and report['source_fcstd_sha256']==hashlib.sha256((INPUT/'baseline.FCStd').read_bytes()).hexdigest()
    return report


def selector(name,axis,extreme,**extra):
    return {'schema_version':'topology-selector.v1','backend':'freecad','revision_id':SOURCE['revision_id'],
        'object_name':name,'subelement_kind':'face','geometry':'planar','axis':axis,'extreme':extreme,'tolerance_mm':1e-5,**extra}


def main():
    circle_doc=App.newDocument('CircleCenterProof')
    circle=circle_doc.addObject('Sketcher::SketchObject','Circle')
    circle.addGeometry(Part.Circle(App.Vector(0,0,0),App.Vector(0,0,1),3),False)
    circle.addConstraint(Sketcher.Constraint('Coincident',0,3,-1,1))
    circle.addConstraint(Sketcher.Constraint('Radius',0,3.0))
    circle_doc.recompute()
    circle_state=project_object(circle)['inspection']
    circle_args=native_args(circle_state['constraints']['items'][0])
    assert circle_args['first']['point_position']==3 and circle.FullyConstrained
    assert all(satisfies(row,circle_state['geometry']['items']) for row in relation_rows(circle_args,circle_state['geometry']['items']))
    App.closeDocument(circle_doc.Name)
    fingerprint=make_fixture()
    original_hash=hashlib.sha256((INPUT/'baseline.FCStd').read_bytes()).hexdigest()
    replacement={'sketch':'Sketch','kind':'coincident','first':{'geometry_index':1,'point_position':1},
        'second':{'geometry_index':0,'point_position':2}}
    args={'sketch':'Sketch','expected_constraints_sha256':fingerprint,
        'changes':[{'action':'replace','logical_id':RELATION,'constraint':replacement}]}
    result=runner.run_task(plan_task([{'op_id':'replace-relation','action':'sketch.patch_relations','args':args},
        export('export-relation','relation')],'baseline.FCStd'))
    assert result['status']=='succeeded',result
    receipt=result['operations'][0]
    assert receipt['relationship_verified'] is True and receipt['parameter_probes']>=6,receipt
    doc=App.openDocument(str(OUTPUT/'relation.FCStd'))
    assert doc.Sketch.FullyConstrained and doc.Sketch.ConstraintCount==11
    assert abs(doc.Body.Shape.Volume-400)<1e-6
    doc.Sketch.setDatum(8,App.Units.Quantity('12 mm'))
    doc.recompute()
    assert doc.Sketch.FullyConstrained and abs(doc.Body.Shape.Volume-480)<1e-6
    App.closeDocument(doc.Name)
    # Unknown originals and loss of a necessary connection must fail without
    # producing a candidate. Repeat against the same immutable source.
    for changes in ([{'action':'delete','logical_id':RELATION}],
        [{'action':'delete','logical_id':'relation_'+'b'*32}]):
        try:
            runner.run_task(plan_task([{'op_id':'reject-relation','action':'sketch.patch_relations',
                'args':{**args,'changes':changes}},export('rejected-export','rejected')],'baseline.FCStd'))
        except runner.FreeCADRunnerError:
            assert not (OUTPUT/'rejected.FCStd').exists()
        else:raise AssertionError('protected relationship unexpectedly accepted')
    assert hashlib.sha256((INPUT/'baseline.FCStd').read_bytes()).hexdigest()==original_hash
    typed=runner.run_task(plan_task([
        {'op_id':'typed-sketch','action':'sketch.create','args':{'name':'TypedSketch'}},
        {'op_id':'typed-profile','action':'sketch.add_profile','args':{'sketch':'TypedSketch','geometry':{'kind':'rectangle','corner':{'x':0,'y':0},'width_mm':10,'height_mm':8}}},
        {'op_id':'typed-pad','action':'feature.pad','args':{'name':'TypedPad','profile':'TypedSketch','length_mm':5}},
        export('typed-export','typed')],None))
    (INPUT/'typed.FCStd').write_bytes(Path(typed['files']['fcstd']).read_bytes())
    doc=App.openDocument(str(INPUT/'typed.FCStd'))
    projected=project_object(doc.TypedSketch)
    relation=next(c for c in projected['inspection']['constraints']['items'] if c['type']=='Horizontal')
    assert relation['origin']=='typed_operation' and relation['logical_id'].startswith('relation_')
    App.closeDocument(doc.Name)
    typed_edit=runner.run_task(plan_task([{'op_id':'typed-replace','action':'sketch.patch_relations','args':{
        'sketch':'TypedSketch','expected_constraints_sha256':projected['sketch_constraints_sha256'],
        'changes':[{'action':'replace','logical_id':relation['logical_id'],'constraint':{'sketch':'TypedSketch','kind':'horizontal','first':{'geometry_index':relation['first']}}}]}},
        export('typed-replaced-export','typed_replaced')],'typed.FCStd'))
    assert typed_edit['operations'][0]['relationship_verified'] is True
    # API programs cannot manufacture trusted relation ownership on new or
    # changed native sketches, even if they copy our metadata property names.
    forged=runner.run_task(plan_task([{'op_id':'api-forge','action':'api.execute','args':{'source':
        "sketch=document.addObject('Sketcher::SketchObject','Forged')\n"
        "sketch.addGeometry(Part.LineSegment(App.Vector(0,0,0),App.Vector(10,0,0)),False)\n"
        "sketch.addConstraint(Sketcher.Constraint('Horizontal',0))\n"
        "sketch.renameConstraint(0,'relation_"+'c'*32+"')\n"
        "sketch.addProperty('App::PropertyStringList','CADAgentRelationOrigins')\n"
        "sketch.CADAgentRelationOrigins=['relation_"+'c'*32+"']\n"}},export('api-forged-export','api_forged',['Body'])],'typed.FCStd'))
    doc=App.openDocument(forged['files']['fcstd'])
    assert not project_object(doc.Forged)['inspection']['constraints']['items'][0].get('origin')
    assert project_object(doc.TypedSketch)['inspection']['constraints']['items'][0].get('origin')=='typed_operation'
    App.closeDocument(doc.Name)
    measure('volume',400)
    measure('solid_count',1)
    measure('component_clearance',20,other_component_name='Other')
    measure('intersection_volume',0,other_component_name='Other')
    measure('face_distance',20,selectors=[selector('Body','x','max'),selector('Other','x','min')])
    measure('circle_diameter',6,component_name='Cylinder',selectors=[selector('Cylinder','z','max',
        subelement_kind='edge',geometry='circular',radius_mm=3,center={'x':60,'y':0,'z':5})])
    for invalid in [
        {'component_name':'Body','measurement':'circle_diameter','selectors':[selector('Cylinder','z','max',
            subelement_kind='edge',geometry='circular',radius_mm=3)]},
        {'measurement':'face_distance','selectors':[selector('Body','x','min'),selector('Body','x','min',tolerance_mm=1e-4)]},
        {'measurement':'face_distance','selectors':[selector('Body','x','max',revision_id='other-revision'),selector('Other','x','min')]},
    ]:
        try:
            measure('volume',0,**invalid)
        except EngineeringError as error:
            assert error.code=='measurement_selection_invalid',error.code
        else:raise AssertionError('invalid measurement attribution must fail in the actual kernel')
    doc=App.openDocument(str(INPUT/'baseline.FCStd'))
    paths=tessellate_scene(doc,OUTPUT)
    scene=json.loads(Path(paths['scene']).read_text())
    assert len(scene['instances'])==3 and all(len(i['face_bindings'])>=2 for i in scene['instances'])
    for definition in scene['definitions'].values():
        for mesh in definition['lods'].values():
            assert sum(r['count'] for r in mesh['face_ranges'])==mesh['triangles']
            assert [r['start'] for r in mesh['face_ranges']]==[sum(x['count'] for x in mesh['face_ranges'][:i]) for i in range(len(mesh['face_ranges']))]
    App.closeDocument(doc.Name)
    for format in ['fcstd','step']:
        imported=runner.run_task(plan_task([{'op_id':'inspect-import','action':'document.inspect','args':{}},
            export('export-import','imported_'+format)],'baseline.'+('FCStd' if format=='fcstd' else 'step'),import_format=format))
        assert imported['status']=='succeeded',imported
        assert imported['source_baseline']=={'sha256':hashlib.sha256((INPUT/('baseline.'+('FCStd' if format=='fcstd' else 'step'))).read_bytes()).hexdigest(),
            'solid_count':3 if format=='fcstd' else 1}
        opened=App.openDocument(str(OUTPUT/('imported_'+format+'.FCStd')))
        assert opened.Body.Shape.isValid() if format=='fcstd' else opened.ImportedBase.Shape.isValid()
        if format=='fcstd':
            assert opened.Sketch.FullyConstrained and 'CADAgentRelationOrigins' not in opened.Sketch.PropertiesList
            assert len(opened.CADAgentLedger.OperationRecords)==2
        else:
            assert abs(opened.ImportedBase.Shape.Volume-400)<1e-6
        App.closeDocument(opened.Name)
    doc=App.openDocument(str(INPUT/'baseline.FCStd'))
    doc.addObject('App::FeaturePython','CustomProxy')
    doc.saveAs(str(INPUT/'unsupported.FCStd'));App.closeDocument(doc.Name)
    try:
        runner.run_task(plan_task([{'op_id':'inspect-unsupported','action':'document.inspect','args':{}},
            export('export-unsupported','unsupported')],'unsupported.FCStd',import_format='fcstd'))
    except runner.FreeCADRunnerError as error:
        assert error.code=='native_import_unsupported'
    else:raise AssertionError('custom Python objects must not be imported')
    for filename,format in [('broken.FCStd','fcstd'),('broken.step','step')]:
        (INPUT/filename).write_bytes(b'not a native CAD document')
        try:
            runner.run_task(plan_task([{'op_id':'invalid-import-inspect','action':'document.inspect','args':{}},
                export('invalid-import-export','invalid_import')],filename,import_format=format))
        except runner.FreeCADRunnerError as error:
            assert error.code=='native_import_invalid',error.code
        else:raise AssertionError('malformed native files must fail with an actionable error')
    assert not (OUTPUT/'invalid_import.FCStd').exists()
    release={'params':{'kind':'release_package','release_name':'Scoped product acceptance',
        'source':{**SOURCE,'fcstd_sha256':original_hash},'annotations':[],'engineering_artifacts':[],
        'options':{'component_names':['Body'],'mesh_precision':'fine','units':'mm'},
        'manufacturing_profile':{'process':'fdm','material':'PLA'},'requirements':{'objective':'10 x 8 x 5 mm'}},
        'inputs':{'base':'baseline.FCStd'}}
    released=run_release(release,INPUT,OUTPUT)
    with zipfile.ZipFile(released['files']['engineering_bundle']) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        bom=json.loads(archive.read('bom.json'))
        handoff=json.loads(archive.read('engineering-handoff.json'))
        assert bom['instance_count']==1 and bom['rows'][0]['kernel_name']=='Body'
        assert bom['inclusion_policy']=='selected_final_solid_components_including_hidden'
        assert manifest['mesh_settings']['linear_deflection_mm']==LOD['fine'][0]
        assert handoff['dimensions'][0]['dimensions_mm']==[10,8,5] and handoff['manufacturing_profile']['process']=='fdm'
        assert handoff['source']['fcstd_sha256']==original_hash
        for item in manifest['files']:
            data=archive.read(item['path']);assert hashlib.sha256(data).hexdigest()==item['sha256'] and len(data)==item['size_bytes']
    assert hashlib.sha256((INPUT/'baseline.FCStd').read_bytes()).hexdigest()==original_hash
    print('CAD_PRODUCT_ACCEPTANCE_CONTRACT='+json.dumps({'relationship_probes':receipt['parameter_probes'],
        'protected_rejections':2,'native_measurements':6,'import_formats':['fcstd','step'],'custom_python_rejected':True,
        'scene_topology_lods':3,'scoped_release_bom':1,'source_unchanged':True,'parameter_reopened_volume_mm3':480,
        'trusted_typed_relation_edit':True,'api_forged_relation_rejected':True,'malformed_imports_rejected':2,
        'measurement_scope_rejections':3,'native_circle_center_proof':True}))


main()
