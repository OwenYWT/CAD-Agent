"""Reject engineering requests whose physical meaning cannot be established."""
import pytest
from pydantic import ValidationError
from app.freecad.engineering_contracts import LinearStaticTask, ContourMillingTask, ENGINEERING_TASK


def task(**changes):
    return LinearStaticTask.model_validate({'component_name':'Body', 'material':{'name':'Steel','young_modulus_mpa':210000,'poisson_ratio':0.3},
        'mesh_size_mm':4, 'fixed_face':{'axis':'z','side':'min'},'loaded_face':{'axis':'z','side':'max'},'force_n':[0,0,1000], **changes})


@pytest.mark.parametrize('changes', [
    {'force_n':[0,0,0]}, {'force_n':[0,float('nan'),1]}, {'force_n':[0,float('inf'),1]},
    {'loaded_face':{'axis':'z','side':'min'}}, {'fixed_face':{'axis':'xyz','side':'min'}},
    {'material':{'name':'unknown'}}, {'material':{'name':'','young_modulus_mpa':1,'poisson_ratio':0.3}},
    {'material':{'name':'unstable','young_modulus_mpa':1,'poisson_ratio':0.5}},
    {'material':{'name':'zero stiffness','young_modulus_mpa':0,'poisson_ratio':0.3}},
    {'mesh_size_mm':0}, {'component_name':'../Solid'}, {'yield_strength_mpa':400},
])
def test_undefined_or_unstable_physics_are_rejected(changes):
    with pytest.raises(ValidationError):
        task(**changes)


def test_signed_load_and_auxetic_material_remain_explicit():
    actual=task(force_n=[-100,0,0],material={'name':'Specified auxetic material','young_modulus_mpa':100,'poisson_ratio':-0.3})
    assert actual.force_n==(-100,0,0) and actual.material.poisson_ratio==-0.3


CAM={'kind':'contour_milling','component_name':'Body','tool':{'name':'6 mm end mill','diameter_mm':6,'cutting_length_mm':12},
     'postprocessor':'grbl_1_1','work_origin_mm':[5,-2,1],'stepdown_mm':3,'feed_mm_min':400,'plunge_mm_min':100,
     'spindle_rpm':12000,'safe_height_mm':5,'stock_margin_mm':5,'radial_allowance_mm':0,'chord_tolerance_mm':0.01}


@pytest.mark.parametrize('changes',[{'postprocessor':'generic'},{'postprocessor':'fanuc'}, {'stepdown_mm':13},
    {'feed_mm_min':0},{'spindle_rpm':100.5},{'work_origin_mm':[0,0,float('nan')]},{'chord_tolerance_mm':0.0001},
    {'tool':{'name':'unknown','diameter_mm':6}},{'stock_margin_mm':-1}])
def test_unsafe_or_undefined_cam_setup_is_rejected(changes):
    with pytest.raises(ValidationError):
        ENGINEERING_TASK.validate_python({**CAM,**changes})


def test_cam_dialect_tool_and_coordinate_contract_is_preserved():
    parsed=ENGINEERING_TASK.validate_python(CAM)
    assert isinstance(parsed,ContourMillingTask) and parsed.work_origin_mm==(5,-2,1)
    assert parsed.tool.diameter_mm==6 and parsed.postprocessor=='grbl_1_1'
