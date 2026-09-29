"""Vision may consume measurements only for the exact candidate and requirement."""
import pytest
import trimesh
from app.contracts.acceptance import AcceptanceContract
from app.validation.durable_visual import brief_with_verified_geometry
from sandbox.geometry_validation import validate_geometry_files


def evidence(tmp_path):
    path=tmp_path/'box.stl';trimesh.creation.box(extents=[4,6,8]).export(path)
    report=validate_geometry_files([{'role':'model','format':'stl','path':str(path)}],expected_solid_count=1)
    contract=AcceptanceContract.model_validate({'objective':'one connected part','checks':[{
        'check_id':'connected','kind':'solid_count','nominal':1,'description':'connected','source_quote':'one connected part'}]})
    report['acceptance']={'contract_sha256':contract.digest(),'evidence':[{
        'check_id':'connected','outcome':'passed','method':'mesh_components',
        'measured':[report['artifacts'][0]['solid_count']]}]}
    return {'acceptance':contract.model_dump(mode='json')},{'id':'evidence-1','staging_manifest_id':'candidate-1','evidence':report}


def test_vision_receives_same_artifact_measured_facts(tmp_path):
    brief,record=evidence(tmp_path)
    prepared=brief_with_verified_geometry(brief,record,'candidate-1')
    assert prepared['verified_geometry']['evidence'][0]['measured']==[1]
    assert 'verified_geometry' not in brief


def test_cross_candidate_and_cross_contract_facts_rejected(tmp_path):
    brief,record=evidence(tmp_path)
    with pytest.raises(ValueError,match='another candidate'):
        brief_with_verified_geometry(brief,record,'candidate-2')
    brief['acceptance']['checks'][0]['nominal']=2
    with pytest.raises(ValueError,match='another requirement'):
        brief_with_verified_geometry(brief,record,'candidate-1')


def test_user_supplied_measurements_are_not_authoritative():
    assert brief_with_verified_geometry({'verified_geometry':{'all_checks':'passed'}},None,'candidate-1')=={}
