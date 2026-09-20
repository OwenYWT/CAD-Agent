from copy import deepcopy
import json

import pytest

from app.freecad.operation_generator import FreeCADOperationGenerator
from tests.test_freecad_operation_generator import _client, _provenance, _valid_plan


def source_plan():
    plan=_valid_plan()
    plan['operations'][2:2]=[
        {'op_id':'radius', 'action':'sketch.add_constraint', 'args':{
            'sketch':'BaseSketch', 'kind':'radius', 'first':{'geometry_index':0}, 'value_mm':5}},
        {'op_id':'relation', 'action':'sketch.add_constraint', 'args':{
            'sketch':'BaseSketch', 'kind':'equal',
            'first':{'geometry_index':0},
            'second':{'geometry_index':0}}},
    ]
    return plan


async def repair(before, after, code='sketch_redundant_constraints', sketch='BaseSketch'):
    generator=FreeCADOperationGenerator(client=_client([after]),provenance_reader=_provenance)
    return await generator.repair(source_code=json.dumps(before),
        failure={'error_code':code,'details':{'object':sketch}},
        base_state=None,output_formats=('step','stl'))


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['dimension','dimension-delete','geometry','pad','export','other-sketch','missing-diagnosis'])
async def test_constraint_repair_cannot_rewrite_protected_intent(mutation):
    before=source_plan();after=deepcopy(before)
    sketch='BaseSketch'
    if mutation=='dimension': after['operations'][2]['args']['value_mm']=7
    elif mutation=='dimension-delete': del after['operations'][2]
    elif mutation=='geometry': after['operations'][1]['args']['geometry']['radius_mm']=7
    elif mutation=='pad': after['operations'][-2]['args']['length_mm']=8
    elif mutation=='export': after['operations'][-1]['args']['basename']='different'
    elif mutation=='other-sketch':
        sketch='OtherSketch';del after['operations'][3]
    else:
        sketch=None;del after['operations'][3]
    with pytest.raises(ValueError,match='constraint repair'):
        await repair(before,after,sketch=sketch)


@pytest.mark.asyncio
async def test_constraint_repair_can_remove_geometric_relation_without_resizing():
    before=source_plan();after=deepcopy(before);del after['operations'][3]
    result=await repair(before,after)
    assert next(op for op in result.operation_plan.operations if op.op_id=='radius').args['value_mm']==5


@pytest.mark.asyncio
async def test_underconstraint_repair_can_add_missing_dimension_but_not_remove_existing_relation():
    before=source_plan();after=deepcopy(before)
    after['operations'].insert(3, {'op_id':'center-x','action':'sketch.add_constraint',
        'args':{'sketch':'BaseSketch','kind':'distance_x','first':{'geometry_index':0,'point_position':3},'value_mm':5}})
    await repair(before,after,'sketch_under_constrained')
    del after['operations'][4]
    with pytest.raises(ValueError,match='constraint repair'):
        await repair(before,after,'sketch_under_constrained')
