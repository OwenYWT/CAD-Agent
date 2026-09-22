import pytest
from app.freecad.contracts import FreeCADOperation, FreeCADOperationPlan
from app.freecad.edge_intent import requested_edge_scope, validate_chamfer_intent, validate_chamfer_repair


def plan(**args):
    return FreeCADOperationPlan(operations=[
        {'op_id':'chamfer','action':'feature.chamfer','args':{
            'name':'Treatment','target':'Hole','size_mm':1, **args}},
        {'op_id':'export','action':'document.export','args':{'formats':['fcstd','step']}}
    ])


@pytest.mark.parametrize('text,scope',[
    ('chamfer:size=1,edges=all_outer','outer'),('1 mm chamfer on all outer edges','outer'),
    ('全部外边倒角1mm','outer'),('孔口倒角','hole_mouths'),('chamfer hole rims','hole_mouths'),
    ('chamfer all edges','all'),('chamfer:size=1,edges=all','all'),('1mm chamfer',None)])
def test_scope_is_not_inferred_from_chamfer_alone(text,scope):
    assert requested_edge_scope(text) == scope


def test_legacy_args_keep_identical_wire_shape():
    original={'name':'Treatment','target':'Hole','size_mm':1.0,'use_all_edges':True,'selector':None}
    assert FreeCADOperation(op_id='old',action='feature.chamfer',args=original).args == original


@pytest.mark.parametrize('args',[
    {'use_all_edges':True}, {'use_all_edges':False,'edge_scope':'all'},
    {'use_all_edges':False,'edge_scope':'hole_mouths'}])
def test_generator_cannot_expand_outer_scope(args):
    with pytest.raises(ValueError,match='preserve requested'):
        validate_chamfer_intent(plan(**args), {'new_features':['1 mm chamfer on outer edges']})


def test_missing_or_conflicting_scope_requires_confirmation():
    with pytest.raises(ValueError,match='scope is missing'):
        validate_chamfer_intent(plan(use_all_edges=True),{'new_features':['1 mm chamfer']})
    with pytest.raises(ValueError,match='conflicts'):
        validate_chamfer_intent(plan(use_all_edges=False,edge_scope='all'),
                               {'new_features':['chamfer all edges']},'chamfer outer edges')
    with pytest.raises(ValueError,match='ambiguous'):
        requested_edge_scope('outer edges and hole mouths')


@pytest.mark.parametrize('args',[
    {'use_all_edges':True}, {'use_all_edges':False,'edge_scope':'hole_mouths'},
    {'use_all_edges':False,'edge_scope':'outer','size_mm':2},
    {'use_all_edges':False,'edge_scope':'outer','target':'Other'}])
def test_repair_preserves_scope_size_and_target(args):
    before=plan(use_all_edges=False,edge_scope='outer')
    with pytest.raises(ValueError,match='repair cannot'):
        validate_chamfer_repair(before,plan(**args))
