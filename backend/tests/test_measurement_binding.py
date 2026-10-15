from copy import deepcopy
import pytest
from app.contracts.measurement_binding import bind_measurement_scope, verify_measurement_binding
from app.contracts.measurement_binding import rigid_transform, transform_scope, binding_digest
from app.contracts.geometry_request import geometry_request_digest


def bound():
    check = {'check_id':'gap', 'kind':'planar_gap', 'scope':{'frame':'bounds_center'}}
    context = {'schema_version':'measurement-context.v1', 'base_revision_id':'base',
               'checkpoint_manifest_id':'current', 'artifact_sha256s':['a'*64]}
    row = {'method':'occt_unique_planar_gap_and_empty_normal_prism', 'measured':[25.], 'issues':[],
           'details':{'binding':'unique_opposing_planar_faces'}}
    result = bind_measurement_scope(check, acceptance_digest='b'*64, context=context, artifact_sha256='a'*64,
        resolved_scope={'frame':'bounds_center'}, frame_origin=(13.,7.,0.), evidence=row)
    return check, context, result


def test_readonly_coordinate_binding_retains_request_and_artifact_identity():
    check, context, result = bound()
    verify_measurement_binding(result, check=check, acceptance_digest='b'*64, context=context, artifact_sha256s=['a'*64])
    assert result['frame_transform'][3] == 13 and result['resolved_scope']['frame'] == 'world'
    assert check['scope']['frame'] == 'bounds_center' and result['status'] == 'bound'


@pytest.mark.parametrize('field,value', [('checkpoint_manifest_id','old'), ('base_revision_id','old'),
                                      ('artifact_sha256s',['c'*64])])
def test_prior_checkpoint_or_wrong_artifact_never_reuses_binding(field,value):
    check, context, result = bound(); context[field] = value
    with pytest.raises(ValueError):
        verify_measurement_binding(result, check=check, acceptance_digest='b'*64, context=context, artifact_sha256s=['a'*64])


def test_changed_scope_or_unsigned_transform_is_rejected():
    check, context, result = bound()
    changed = deepcopy(result); changed['frame_transform'][3] += 1
    with pytest.raises(ValueError, match='digest'):
        verify_measurement_binding(changed, check=check, acceptance_digest='b'*64, context=context, artifact_sha256s=['a'*64])
    check['scope']['axis'] = [1,0,0]
    with pytest.raises(ValueError, match='frozen request'):
        verify_measurement_binding(result, check=check, acceptance_digest='b'*64, context=context, artifact_sha256s=['a'*64])


def test_binding_request_version_does_not_change_old_request_digests():
    args = {'expected_dimensions_mm':{},'dimension_tolerance':.05,'acceptance':None}
    assert geometry_request_digest(**args) == geometry_request_digest(**args, measurement_context=None)
    _, context, _ = bound()
    assert geometry_request_digest(**args) != geometry_request_digest(**args, measurement_context=context)


def test_local_region_keeps_exact_oriented_corners_and_explicit_axis():
    matrix=[0,-1,0,13,1,0,0,-7,0,0,1,11,0,0,0,1]
    scope={'frame':'selected_object','axis':[0,1,0],'region_min_mm':[0,0,0],'region_max_mm':[2,3,4]}
    resolved=transform_scope(scope,matrix)
    assert resolved['axis']==[-1,0,0] and resolved['region_kind']=='oriented_box'
    assert len(resolved['region_vertices_mm'])==8 and [10,-5,15] in resolved['region_vertices_mm']
    assert 'region_min_mm' not in resolved
    assert scope['frame']=='selected_object'
    matrix[0]=2
    with pytest.raises(ValueError,match='rigid'):rigid_transform(matrix)


@pytest.mark.parametrize('mutation',['native_digest','reference','containment','resolved_axis'])
def test_local_binding_rejects_rehashed_evidence_from_another_native_source(mutation):
    reference={'feature_id':'body','revision_id':'base','kernel_name':'Body',
        'type_id':'PartDesign::Body','parameter_state_sha256':'c'*64}
    check={'check_id':'height','kind':'overall_dimension',
        'scope':{'frame':'selected_object','object_ref':reference,'axis':[0,0,1]}}
    context={'schema_version':'measurement-context.v2','base_revision_id':'base',
        'checkpoint_manifest_id':'current','artifact_sha256s':['a'*64],
        'native_reference':{'sha256':'d'*64},'selected_objects':[reference]}
    native={'reference':reference,'frame_transform':[1,0,0,13,0,1,0,0,0,0,1,0,0,0,0,1],
        'contained_in_final_step':True,'native_artifact_sha256':'d'*64}
    result=bind_measurement_scope(check,acceptance_digest='b'*64,context=context,
        artifact_sha256='a'*64,resolved_scope=check['scope'],frame_origin=None,native_binding=native,
        evidence={'method':'occt_bounds','measured':[3.],'issues':[],'details':{}})
    args=dict(check=check,acceptance_digest='b'*64,context=context,artifact_sha256s=['a'*64])
    verify_measurement_binding(result,**args)
    if mutation=='native_digest':result['proof']['native_component']['native_artifact_sha256']='e'*64
    elif mutation=='reference':result['proof']['native_component']['reference']={**reference,'kernel_name':'Other'}
    elif mutation=='containment':result['proof']['native_component']['contained_in_final_step']=False
    else:result['resolved_scope']['axis']=[0,1,0]
    result['binding_digest']=binding_digest({k:v for k,v in result.items() if k!='binding_digest'})
    with pytest.raises(ValueError,match='coordinate equivalence'):
        verify_measurement_binding(result,**args)
