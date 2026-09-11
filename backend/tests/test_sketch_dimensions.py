"""Typed edit boundaries before the real Sketcher solver is invoked."""
import pytest
from pydantic import ValidationError

from app.freecad.contracts import SketchSetConstraintArgs
from app.freecad.state_contract import ParameterStateError,compile_parameter_operation_plan
from app.workflows.temporal import FreeCADStructuredModificationV1
from app.services.feature_leases import operation_targets,related


def test_signed_cartesian_dimensions_and_positive_sizes():
    for kind,value in [('DistanceX',0),('DistanceY',-5),('Radius',7),('Diameter',14),('Distance',2)]:
        assert SketchSetConstraintArgs(sketch='Sketch',constraint_index=0,expected_type=kind,value_mm=value).value_mm==value
    for kind,value in [('Radius',0),('Diameter',-1),('Distance',float('inf')),('DistanceX',float('nan'))]:
        with pytest.raises(ValidationError):
            SketchSetConstraintArgs(sketch='Sketch',constraint_index=0,expected_type=kind,value_mm=value)


def test_compiler_resolves_recorded_constraint_and_rejects_wrong_index_type_or_reference():
    state={'schema_version':'freecad-state.v2','document':'Model','object_count':1,'root_objects':['Sketch'],
        'parameters':[],'objects':[{'name':'Sketch','type_id':'Sketcher::SketchObject',
            'inspection':{'constraints':{'items':[{'index':2,'type':'Radius','driving':True}]}}}]}
    def compile(index=2,kind='Radius'):
        modification=FreeCADStructuredModificationV1(expected_state_sha256='a'*64,native_edits=[{
            'action':'sketch.set_constraint','args':{'sketch':'Sketch','constraint_index':index,'expected_type':kind,'value_mm':7}}])
        return compile_parameter_operation_plan(state,modification.model_dump(mode='json'),output_formats=('step','stl'))
    assert compile().operations[0].action=='sketch.set_constraint'
    with pytest.raises(ParameterStateError,match='完整记录'):
        compile(99)
    with pytest.raises(ParameterStateError,match='类型'):
        compile(kind='Diameter')
    state['objects'][0]['inspection']['constraints']['items'][0]['driving']=False
    with pytest.raises(ParameterStateError,match='测量'):
        compile()


def test_sketch_dimension_lease_scope_includes_dependents_but_not_independent_bodies():
    features=[{'id':'sketch','kernel_name':'SketchA','dependencies':[]},
        {'id':'pad','kernel_name':'PadA','dependencies':['sketch']},
        {'id':'other','kernel_name':'PadB','dependencies':[]}]
    payload={'structured_modification':{'native_edits':[{'action':'sketch.set_constraint','args':{'sketch':'SketchA'}}]}}
    assert operation_targets(features,payload)=={'sketch'}
    assert related(features,'pad','sketch') and not related(features,'other','sketch')
    payload['structured_modification']['native_edits'].append({'action':'assembly.instance','args':{'object':'Link'}})
    assert operation_targets(features,payload) is None
