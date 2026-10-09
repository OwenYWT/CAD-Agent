"""Pure boundaries complement the real kernel and durable API regressions."""
from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.dfm.models import validate_rule_thresholds, rule_process
from app.freecad.contracts import SketchPatchRelationsArgs
from app.freecad.engineering_contracts import NativeMeasurementTask
from app.freecad.release_contracts import ReleaseOptions,ReleaseTask
from app.freecad.sketch_relations import relation_certificate
from app.models.workflow_requests import NativeImportV1,OperationContextV1


def relation_base():
    return {'name':'Sketch','type_id':'Sketcher::SketchObject','sketch_constraints_sha256':'a'*64,
        'external_geometry':0,'expressions':{'total':0,'items':[]},
        'inspection':{'geometry':{'total':1,'items':[{'index':0,'type':'Part::GeomLineSegment',
            'start':[0,0,0],'end':[10,0,0],'construction':False}]},
            'constraints':{'total':1,'items':[{'index':0,'type':'Horizontal','first':0,'first_position':-2000,
                'second':-2000,'second_position':-2000,'value':0,'name':'relation_'+'b'*32,
                'logical_id':'relation_'+'b'*32,'origin':'user_relation'}]}}}


def replacement():
    return SketchPatchRelationsArgs(sketch='Sketch',expected_constraints_sha256='a'*64,changes=[{
        'action':'replace','logical_id':'relation_'+'b'*32,
        'constraint':{'sketch':'Sketch','kind':'horizontal','first':{'geometry_index':0}}}]).model_dump(mode='json')


def test_relation_proof_preserves_required_relationship_and_unknown_origin():
    sketch=relation_base()
    proof=relation_certificate(sketch,replacement())
    assert proof['sketches']['Sketch']['derivations']
    trusted=deepcopy(sketch);trusted['inspection']['constraints']['items'][0]['origin']='typed_operation'
    assert relation_certificate(trusted,replacement())['sketches']['Sketch']['derivations']
    for missing in ['origin','logical_id']:
        unsafe=deepcopy(sketch);unsafe['inspection']['constraints']['items'][0].pop(missing)
        with pytest.raises(ValueError,match='保护'):
            relation_certificate(unsafe,replacement())
    unsafe=replacement();unsafe['changes']=[{'action':'delete','logical_id':'relation_'+'b'*32}]
    with pytest.raises(ValueError,match='必要几何关系'):
        relation_certificate(sketch,unsafe)


def test_relationship_edit_protects_cross_object_expressions_external_and_stale_baselines():
    sketch=relation_base()
    with pytest.raises(ValueError,match='表达式'):
        relation_certificate(sketch,replacement(),[sketch,{'name':'Pad','expressions':{'total':1,'items':[{'property':'Length','expression':'Sketch.Constraints[0]'}]}}])
    for field,value in [('external_geometry',1),('sketch_constraints_sha256','c'*64)]:
        unsafe={**sketch,field:value}
        with pytest.raises(ValueError):relation_certificate(unsafe,replacement())
    with pytest.raises(ValueError,match='表达式'):
        relation_certificate(sketch,replacement(),[sketch,{'name':'Pad'}])


@pytest.mark.parametrize('patch',[
    {'constraint':{'sketch':'Sketch','kind':'radius','first':{'geometry_index':0},'value_mm':5}},
    {'constraint':{'sketch':'Other','kind':'horizontal','first':{'geometry_index':0}}},
    {'constraint':{'sketch':'Sketch','kind':'horizontal','first':{'geometry_index':0},'driving':False}},
])
def test_relation_input_cannot_change_dimensions_other_sketch_or_reference_mode(patch):
    data=replacement();data['changes'][0].update(patch)
    with pytest.raises(ValidationError):SketchPatchRelationsArgs.model_validate(data)


@pytest.mark.parametrize('minimum,maximum',[(float('nan'),10),(0,float('inf')),(11,10)])
def test_dfm_thresholds_reject_nonfinite_or_inverted_merged_values(minimum,maximum):
    with pytest.raises(ValueError):validate_rule_thresholds({'threshold_min':minimum,'threshold_max':maximum})
    validate_rule_thresholds({'threshold_min':0,'threshold_max':10})


def test_native_import_identity_is_frozen_and_storage_paths_are_server_derived():
    tenant,document=uuid4(),uuid4()
    source=NativeImportV1(format='fcstd',artifact_id=uuid4(),sha256='a'*64,size_bytes=12,filename='part.FCStd')
    context=OperationContextV1(rule='explicit_native_import',source_channel='rest',requested_operation='generate',
        resolved_operation='generate',submission_modeling_backend='freecad',base_revision_id=uuid4(),
        base_source_kind='native_import_artifact',base_source_id=source.artifact_id,base_source_sha256=source.sha256,native_import=source)
    assert context.native_import==source
    assert source.object_key(tenant,document)==f'tenants/{tenant}/document-imports/{document}/'+'a'*64+'.fcstd'
    data=context.model_dump(mode='json');data['base_source_sha256']='b'*64
    with pytest.raises(ValidationError):OperationContextV1.model_validate(data)
    for size in [0,True,64*1024*1024+1]:
        with pytest.raises(ValidationError):NativeImportV1.model_validate({**source.model_dump(),'size_bytes':size})


def test_measurement_requires_correct_topology_count_and_distinct_actual_components():
    assert NativeMeasurementTask(component_name='Body',measurement='volume').selectors==()
    assert NativeMeasurementTask(component_name='Body',measurement='component_clearance',other_component_name='Other').other_component_name=='Other'
    for body in [{'component_name':'Body','measurement':'face_distance'},
        {'component_name':'Body','measurement':'circle_diameter'},
        {'component_name':'Body','measurement':'intersection_volume','other_component_name':'Body'},
        {'component_name':'Body','measurement':'solid_count','other_component_name':'Other'}]:
        with pytest.raises(ValidationError):NativeMeasurementTask.model_validate(body)
    selector={'schema_version':'topology-selector.v1','backend':'freecad','revision_id':str(uuid4()),
        'object_name':'Other','subelement_kind':'face','geometry':'planar','axis':'x','extreme':'min','tolerance_mm':1e-5}
    with pytest.raises(ValidationError,match='当前对象'):
        NativeMeasurementTask(component_name='Body',measurement='face_distance',selectors=[selector,selector])
    with pytest.raises(ValidationError,match='不同的子元素'):
        NativeMeasurementTask(component_name='Other',measurement='face_distance',selectors=[selector,selector])


def test_circle_center_relationship_retains_native_point_position():
    from app.freecad.constraint_relationships import native_args, relation_rows, satisfies
    geometry=[{'index':0,'type':'Part::GeomCircle','center':[0,0,0],'radius_mm':3}]
    args=native_args({'type':'Coincident','first':0,'first_position':3,
        'second':-1,'second_position':1,'value':0})
    assert args['first']['point_position']==3
    rows=relation_rows(args,geometry)
    assert rows and all(satisfies(row,geometry) for row in rows)


def test_release_scope_precision_units_and_legacy_default_identity():
    for options in [{'component_names':['Body','Body']},{'mesh_precision':'unbounded'},{'units':'inch'}]:
        with pytest.raises(ValidationError):ReleaseOptions.model_validate(options)
    source={'document_id':uuid4(),'project_id':uuid4(),'revision_id':uuid4(),'state_version':1,
        'fcstd_artifact_id':uuid4(),'fcstd_sha256':'a'*64}
    task=ReleaseTask(release_name='Source-bound release',source=source)
    assert 'options' not in task.model_dump(mode='json')
    scoped=task.model_copy(update={'options':ReleaseOptions(component_names=['Body'],mesh_precision='fine')})
    assert scoped.model_dump(mode='json')['options']=={'component_names':['Body'],'mesh_precision':'fine','units':'mm'}


def test_manufacturing_profiles_select_canonical_rules_without_assuming_unknown_processes():
    for profile,rules in [('fdm','FDM'),('FDM','FDM'),('cnc','CNC'),('sla','SLA'),('laser_cut','sheet_metal')]:
        assert rule_process(profile)==rules
    assert rule_process('generic')=='generic'
    assert rule_process('custom-process')=='custom-process'
    assert rule_process(None) is None
