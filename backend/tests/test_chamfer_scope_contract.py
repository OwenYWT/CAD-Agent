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


@pytest.mark.parametrize('text,expected', [
    ('Chamfer only outer edges, not hole mouths.', 'outer'),
    ('Chamfer outer edges but do not chamfer hole mouths.', 'outer'),
    ('Do not chamfer hole mouths; chamfer external edges.', 'outer'),
    ('Chamfer outer edges without touching hole rims.', 'outer'),
    ('Chamfer outer edges only, excluding hole mouths.', 'outer'),
    ('Keep hole mouths unchanged; chamfer outer edges.', 'outer'),
    ('Chamfer the outer edge by 1 mm; leave the hole mouth unchamfered.', 'outer'),
    ('Chamfer outer edges. Hole mouths must not be chamfered.', 'outer'),
    ('仅外边倒角，孔口不要倒角。', 'outer'),
    ('所有外边缘添加 1 mm 倒角，不倒孔口。', 'outer'),
    ('Chamfer only the outer edge while keeping the hole openings unchanged.', 'outer'),
    ('孔口不处理；外部棱倒角。', 'outer'),
    ('外边倒角，不要对孔口倒角。', 'outer'),
    ('外边倒角，孔口维持原样。', 'outer'),
    ('Chamfer hole mouths; leave outer edges unchanged.', 'hole_mouths'),
    ('仅孔口倒角，外边保持不变。', 'hole_mouths'),
    ('Do not chamfer hole mouths or outer edges.', None),
    ('Leave outer edges and hole mouths unchanged.', None),
])
def test_treatment_and_protection_have_distinct_polarity(text, expected):
    assert requested_edge_scope(text) == expected


@pytest.mark.parametrize('text', [
    'Chamfer all edges except hole mouths.',
    'Chamfer outer edges and hole mouths.',
    'Chamfer outer edges; leave outer edges unchanged.',
    'Not only outer edges but also hole mouths.',
    'Chamfer outer edges; do not leave hole mouths unchanged.',
])
def test_conflicts_and_unsupported_exclusions_need_clarification(text):
    with pytest.raises(ValueError, match='ambiguous'):
        requested_edge_scope(text)


@pytest.mark.parametrize('objective,requirements', [
    ('Do not chamfer hole mouths.', {'new_features': ['chamfer hole mouths']}),
    ('Keep all edges unchanged.', {'new_features': ['chamfer outer edges']}),
    ('Chamfer all edges.', {'constraints': ['孔口保持不变']}),
])
def test_protected_scope_survives_separate_requirement_fields(objective, requirements):
    with pytest.raises(ValueError, match='protected'):
        validate_chamfer_intent(plan(use_all_edges=False,edge_scope='all'), requirements, objective)


@pytest.mark.parametrize('args', [{'use_all_edges':True}, {'use_all_edges':False,'edge_scope':'all'}, {'use_all_edges':False,'edge_scope':'hole_mouths'}])
def test_protecting_holes_never_relaxes_the_operation_scope(args):
    with pytest.raises(ValueError, match='preserve requested'):
        validate_chamfer_intent(plan(**args), {'new_features':['chamfer outer edges'],
             'constraints':['孔口保持不变']}, 'Chamfer only outer edges, not hole mouths.')
