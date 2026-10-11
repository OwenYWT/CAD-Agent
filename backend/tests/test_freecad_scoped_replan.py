"""Repair proposals must not reproduce or rewrite protected transaction source."""
from copy import deepcopy

import pytest

from app.freecad import profile_replan
from app.freecad.contracts import FreeCADOperationPlan, SketchAddConstraintArgs
from app.freecad.failure_snapshot import digest
from tests.test_freecad_profile_replan import context
from tests.e2e.profile_replan_fixture import proposal


def local_proposal(ctx):
    return {
        'plan_hash': digest(ctx['source_plan']),
        'checkpoint_hash': ctx['checkpoint_hash'],
        'diagnostic_hash': digest(ctx['snapshot']),
        'sketch': ctx['sketch'],
        'operations': [op for op in proposal()['operations']
                       if op['args'].get('sketch') == ctx['sketch']],
    }


def test_profile_patch_compiles_protected_operations_and_export_from_baseline():
    ctx = context()
    patch = local_proposal(ctx)
    result = profile_replan.compile_patch(patch, ctx)
    expected = FreeCADOperationPlan.model_validate(proposal())
    assert result == expected
    assert profile_replan.compile_patch(deepcopy(patch), ctx) == result
    assert result.operations[-1].action == 'document.export'
    assert digest(ctx['source_plan']) == patch['plan_hash']


@pytest.mark.parametrize('field', ['plan_hash', 'checkpoint_hash', 'diagnostic_hash', 'sketch'])
def test_profile_patch_rejects_stale_or_wrong_baseline_with_field_evidence(field):
    ctx = context()
    patch = local_proposal(ctx)
    patch[field] = 'Other' if field == 'sketch' else '0' * 64
    with pytest.raises(ValueError) as error:
        profile_replan.compile_patch(patch, ctx)
    assert error.value.differences[0]['field'] == field


@pytest.mark.parametrize('mutation', ['export', 'frame', 'other-sketch', 'protected-id'])
def test_profile_patch_cannot_smuggle_changes_to_unrelated_operations(mutation):
    ctx = context()
    patch = local_proposal(ctx)
    if mutation in {'export', 'frame'}:
        patch['operations'].append(deepcopy(ctx['source_plan']['operations'][-1 if mutation == 'export' else 0]))
    elif mutation == 'other-sketch':
        patch['operations'][0]['args']['sketch'] = 'Another'
    else:
        patch['operations'][0]['op_id'] = ctx['source_plan']['operations'][-1]['op_id']
    with pytest.raises(ValueError):
        profile_replan.compile_patch(patch, ctx)


def test_rejected_full_profile_plan_identifies_changed_operation_and_field():
    ctx = context()
    bad = proposal()
    bad['operations'][-2]['args']['length_mm'] += 1
    with pytest.raises(ValueError) as error:
        profile_replan.validate_proposal(FreeCADOperationPlan.model_validate(bad), ctx)
    assert any(d['operation_id'] == bad['operations'][-2]['op_id']
               and d['field'] == 'args.length_mm' for d in error.value.differences)


@pytest.mark.parametrize('datum,axis', [('horizontal_axis', 'y'), ('vertical_axis', 'x')])
def test_datum_axis_is_a_proven_relation_instead_of_an_editable_zero_offset(datum, axis):
    from app.freecad.constraint_relationships import native_args, relation_rows, satisfies
    args = SketchAddConstraintArgs.model_validate({
        'sketch': 'Section', 'kind': 'point_on_object',
        'first': {'geometry_index': 0, 'point_position': 1}, 'second': {'datum': datum}})
    geometry = [{'index': 0, 'type': 'Part::GeomLineSegment',
                 'start': [0, 0, 0], 'end': [0, 10, 0]}]
    rows = relation_rows(args.model_dump(), geometry)
    assert len(rows) == 1 and satisfies(rows[0], geometry)
    assert set(rows[0]['terms']) == {f'0:s{axis}'}
    observed = native_args({'type': 'PointOnObject', 'first': 0, 'first_position': 1,
        'second': -1 if datum == 'horizontal_axis' else -2, 'second_position': 0, 'value': 0})
    assert observed['second'] == {'datum': datum}
    assert observed['value_mm'] is None


@pytest.mark.parametrize('kind,second', [('DistanceX', 1), ('DistanceY', -1), ('Distance', -1), ('Distance', -2000)])
def test_native_two_point_distances_are_not_reinterpreted_as_absolute_anchors(kind, second):
    from app.freecad.constraint_relationships import native_args
    assert native_args({'type': kind, 'first': 0, 'first_position': 1,
                        'second': second, 'second_position': 2, 'value': 5}) is None


def test_real_crossed_profile_compiles_without_copying_frame_or_clamp():
    from tests.e2e.scoped_repair_fixture import prepared
    data = prepared()
    before = FreeCADOperationPlan.model_validate(data['original'])
    after = FreeCADOperationPlan.model_validate(data['datum_anchor']['plan'])
    fixed = lambda plan: [op for op in plan.operations if op.args.get('sketch') != 'HookSketch']
    assert fixed(before) == fixed(after)
    assert data['datum_anchor']['profile_replan']['acceptance_hash'] == digest(data['acceptance'])
