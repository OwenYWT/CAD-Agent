from copy import deepcopy
import json
import math

import pytest

from benchmark.native_oracle import cases, compare, compile_plan, expected, freeze, read_frozen, validate_case
from app.freecad.contracts import FreeCADOperationPlan


def test_generated_recipes_are_real_typed_plans_with_independent_expected_values():
    for case in cases(range(32)):
        FreeCADOperationPlan.model_validate(compile_plan(case))
        oracle = expected(case)
        assert oracle['volume_mm3'] > 0 and oracle['area_mm2'] > 0


def test_blind_and_through_reference_material_and_surface_are_distinct():
    indexed = {case['case_id']:case for case in cases()}
    through,blind = (expected(indexed[name]) for name in ('offset-through','blind'))
    assert blind['volume_mm3']-through['volume_mm3'] == pytest.approx(math.pi*9*6)
    assert through['area_mm2']-blind['area_mm2'] == pytest.approx(2*math.pi*3*6-2*math.pi*9)
    assert through['holes'][0]['bottom_mm'][2] == 0
    assert blind['holes'][0]['bottom_mm'][2] == 6


def test_metamorphic_scale_translation_and_rotation_keep_the_expected_invariants():
    indexed = {case['case_id']:expected(case) for case in cases()}
    original = indexed['offset-through']
    for name in ('translated','rotated'):
        assert indexed[name]['volume_mm3'] == pytest.approx(original['volume_mm3'])
        assert indexed[name]['area_mm2'] == pytest.approx(original['area_mm2'])
    assert indexed['scaled']['volume_mm3'] == pytest.approx(original['volume_mm3']*1.7**3)
    assert indexed['scaled']['area_mm2'] == pytest.approx(original['area_mm2']*1.7**2)
    assert indexed['translated']['centroid_mm'] == pytest.approx([v+d for v,d in zip(original['centroid_mm'],[-27,19,-8])])


@pytest.mark.parametrize('field,value',[('radius',float('nan')),('x',1),('depth',13),('through',False)])
def test_analytic_domain_does_not_extrapolate_invalid_recipes(field,value):
    case = cases()[0];case['holes'][0][field] = value
    with pytest.raises(ValueError):validate_case(case)


def test_replay_uses_frozen_operations_not_the_current_compiler(tmp_path,monkeypatch):
    from benchmark import native_oracle
    case = cases()[0];original = compile_plan(case)
    freeze(case,tmp_path/'case',original)
    monkeypatch.setattr(native_oracle,'compile_plan',lambda _: (_ for _ in ()).throw(AssertionError('recompiled')))
    assert read_frozen(tmp_path/'case')[1] == original
    path = tmp_path/'case'/'operations.json'
    changed = json.loads(path.read_text());changed['operations'][2]['args']['length_mm'] += 1
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='operations bytes changed'):read_frozen(tmp_path/'case')


def test_missing_nan_or_boolean_measurements_cannot_pass():
    oracle = expected(cases()[1])  # Plain plate, no hole measurements involved.
    actual = {'valid':True,'solid_count':1,'volume_mm3':oracle['volume_mm3'],
        'area_mm2':oracle['area_mm2'],'centroid_mm':oracle['centroid_mm'],
        'bounds_min':oracle['bounds_mm']['min'],'bounds_max':oracle['bounds_mm']['max'],'holes':[]}
    assert not compare(oracle,actual)
    for key in ('solid_count','volume_mm3','area_mm2'):
        for value in (None,True,float('nan'),float('inf')):
            changed = deepcopy(actual);changed[key] = value
            assert key in compare(oracle,changed)
    missing = deepcopy(actual);del missing['bounds_max']
    assert 'bounds_max.0' in compare(oracle,missing)
