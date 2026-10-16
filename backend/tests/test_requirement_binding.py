import pytest

from app.validation.requirement_binding import extract_requirements, validate_requirement_bindings, RequirementBindingError
from app.contracts.acceptance_v2 import AcceptanceContractV2


def parsed_contract(text, *, comparison=None, kind=None, source_kind='user', verified_state=None):
    records = extract_requirements(text, verified_state=verified_state)['requirements']
    checks = []
    for record in records:
        checks.append({'check_id': record['requirement_id'], 'binding_ref': record['requirement_id'],
            'kind': kind or record['kind'], 'description': record['property'],
            'source_quote': record['source_quote'], 'source_kind': source_kind,
            'comparison': comparison or record['comparison'], 'scope': record.get('scope', {})})
    return AcceptanceContractV2.model_validate({'objective': text, 'checks': checks})


@pytest.mark.parametrize('text,op,value,unit', [
    ('夹口 25 mm', 'eq', 25., 'mm'),
    ('夹口 2.5 cm', 'eq', 25., 'mm'),
    ('壁厚至少 5 mm', 'ge', 5., 'mm'),
    ('间隙不超过 5 mm', 'le', 5., 'mm'),
    ('体积 2 cm³', 'eq', 2000., 'mm3'),
    ('两个独立实体', 'eq', 2., 'count'),
    ('hole diameter 6 mm', 'eq', 6., 'mm'),
])
def test_property_quantity_relation_and_unit_are_independently_extracted(text, op, value, unit):
    result = extract_requirements(text)
    assert not result['unsupported']
    assert len(result['requirements']) == 1
    comparison = result['requirements'][0]['comparison']
    assert comparison['op'] == op and comparison['unit'] == unit
    assert comparison[{'eq': 'value', 'ge': 'lower', 'le': 'upper'}[op]] == value
    report = validate_requirement_bindings(text, parsed_contract(text))
    assert report['status'] == 'bound' and report['bindings'][0]['status'] == 'bound'


@pytest.mark.parametrize('comparison,code', [
    ({'op': 'eq', 'value': 20., 'unit': 'mm', 'tolerance_minus': 5.,
      'tolerance_plus': 5., 'tolerance_source_refs': ['invented']}, 'requirement_binding_mismatch'),
    ({'op': 'eq', 'value': 25., 'unit': 'mm', 'tolerance_minus': 5.,
      'tolerance_plus': 5., 'tolerance_source_refs': ['invented']}, 'requirement_tolerance_unbound'),
    ({'op': 'ge', 'lower': 25., 'unit': 'mm'}, 'requirement_comparison_mismatch'),
])
def test_wrong_value_relation_and_tolerance_rejected_before_execution(comparison, code):
    with pytest.raises(RequirementBindingError) as error:
        validate_requirement_bindings('夹口 25 mm', parsed_contract('夹口 25 mm', comparison=comparison))
    assert error.value.code == code


@pytest.mark.parametrize('text,minus,plus', [('夹口 25±0.2 mm', .2, .2), ('夹口 25 +0.2/-0.1 mm', .1, .2)])
def test_user_authorized_tolerance_keeps_asymmetry_and_source(text, minus, plus):
    parsed = parsed_contract(text)
    assert parsed.checks[0].comparison.tolerance_minus == minus
    assert parsed.checks[0].comparison.tolerance_plus == plus
    assert validate_requirement_bindings(text, parsed)['status'] == 'bound'


def test_closed_range_and_strict_boundary_are_not_conflated():
    text = '夹口 20 至 25 mm'
    record = extract_requirements(text)['requirements'][0]
    assert record['comparison'] == {'op': 'range', 'lower': 20., 'upper': 25., 'unit': 'mm'}
    assert extract_requirements('壁厚大于 5 mm')['unsupported']


@pytest.mark.parametrize('text,kind,op,value', [
    ('至少两个独立实体', 'solid_count', 'ge', 2),
    ('at most two holes', 'hole_count', 'le', 2),
    ('孔径改为 8 mm', 'hole_diameter', 'eq', 8),
    ('minimum wall thickness 5 mm', 'wall_thickness', 'ge', 5),
])
def test_prefix_relations_and_modification_values(text, kind, op, value):
    extracted = extract_requirements(text)
    assert not extracted['unsupported']
    assert extracted['requirements'][0]['kind'] == kind
    comparison = extracted['requirements'][0]['comparison']
    assert comparison['op'] == op
    assert comparison[{'eq':'value','ge':'lower','le':'upper'}[op]] == value


@pytest.mark.parametrize('text', ['不要直径6 mm的孔', '壁厚5 mm，保持孔径不变',
    '至少长方体20×30×40 mm', '壁厚5 mm，圆角半径2 mm', '壁厚5 mm，所有孔均匀分布'])
def test_unsupported_negation_preservation_or_coverage_is_never_silently_bound(text):
    assert extract_requirements(text)['unsupported']


def test_check_coverage_and_confirmed_source_cannot_be_invented():
    text = '孔径 6 mm，孔深 8 mm，2个孔'
    parsed = parsed_contract(text)
    assert len(parsed.checks) == 3
    with pytest.raises(RequirementBindingError, match='coverage'):
        validate_requirement_bindings(text, parsed.model_copy(update={'checks': parsed.checks[:-1]}))
    with pytest.raises(RequirementBindingError, match='confirmed'):
        validate_requirement_bindings(text, parsed_contract(text, source_kind='confirmed'))
    assert extract_requirements('型号 M6')['requirements'] == []


def test_equal_numbers_do_not_authorize_swapping_properties_or_selection():
    text = '孔径 6 mm，孔深 6 mm'
    parsed = parsed_contract(text)
    changed = parsed.checks[0].model_copy(update={'kind': 'hole_depth'})
    with pytest.raises(RequirementBindingError, match='property'):
        validate_requirement_bindings(text, parsed.model_copy(update={'checks': (changed, parsed.checks[1])}))


def test_unsupported_numeric_geometry_is_never_reported_as_fully_bound():
    result = extract_requirements('沿螺旋方向均匀间距 5 mm')
    assert result['unsupported']


@pytest.mark.parametrize('objective',[
    '长度100mm，倾角45°','长度100mm，旋转0.5 rad','长度100mm，四个支脚',
    '长度100mm，3个槽','length100mm, 4 support legs',
])
def test_unsupported_angle_or_feature_count_is_not_lost_beside_supported_dimensions(objective):
    result=extract_requirements(objective)
    assert result['requirements'] and result['unsupported']


def baseline():
    return {'revision_id':'current-revision', 'verified_acceptance':{
        'revision_id':'current-revision', 'evidence_id':'sealed-evidence', 'evidence_sha256':'a'*64,
        'acceptance_digest':'b'*64, 'artifact_sha256s':['c'*64],
        'measurements':[{'kind':'hole_diameter', 'scope':{}, 'check_id':'hole-shaft', 'values':[6.]}]}}


def test_preservation_binds_measured_value_of_exact_revision_without_reusing_tolerance():
    text, state = '长度改为 80 mm，保持孔径不变', baseline()
    contract = parsed_contract(text, verified_state=state)
    result = validate_requirement_bindings(text, contract, state)
    protected = next(b for b in result['bindings'] if b['property']=='hole_diameter')
    assert protected['comparison'] == {'op':'eq','value':6.,'unit':'mm'}
    assert protected['derivation']['evidence_sha256']=='a'*64
    # A wrong baseline is not repaired by trusting its feature name.
    state['revision_id']='other'
    with pytest.raises(RequirementBindingError, match='preservation_baseline_required'):
        validate_requirement_bindings(text, contract, state)


@pytest.mark.parametrize('values', [[6.,8.], [], [float('nan')]])
def test_ambiguous_or_unmeasured_preservation_is_unavailable(values):
    state = baseline()
    state['verified_acceptance']['measurements'][0]['values']=values
    assert extract_requirements('保持孔径不变', verified_state=state)['unsupported']


def test_selected_object_baseline_cannot_authorize_whole_model_preservation():
    state=baseline()
    state['verified_acceptance']['measurements'][0]['scope']={'frame':'selected_object',
        'object_ref':{'feature_id':'one-of-two-bodies','revision_id':'previous-base'}}
    result=extract_requirements('保持孔径不变',verified_state=state)
    assert result['unsupported'] and not result['requirements']


def test_hole_section_semantics_cannot_be_changed_by_planner():
    contract = parsed_contract('孔径6 mm')
    changed = contract.checks[0].model_copy(update={'scope':contract.checks[0].scope.model_copy(update={'hole_diameter_mode':'largest_section'})})
    with pytest.raises(RequirementBindingError, match='scope'):
        validate_requirement_bindings('孔径6 mm', contract.model_copy(update={'checks':(changed,)}))


def test_saved_requirement_basis_is_bound_separately_from_server_instructions():
    from types import SimpleNamespace
    from app.domain.requirement_basis import RequirementBasisV1
    from app.workflows.handlers.planning import requirements_objective, requirements_sources
    from app.validation.requirement_binding import extract_source_requirements
    request = SimpleNamespace(objective='设计夹具', operation_context=SimpleNamespace(requirement_basis=RequirementBasisV1(
        target='工件', design_scope='geometry', source_kind='user_specification',
        source_reference='用户确认', dimensions='夹口25±0.2 mm')))
    objective, sources = requirements_objective(request), requirements_sources(request)
    extracted = extract_source_requirements(objective, requirement_sources=sources)
    assert not extracted['unsupported'] and len(extracted['requirements'])==1
    record=extracted['requirements'][0]
    check={'check_id':'confirmed-gap', 'binding_ref':record['requirement_id'], 'kind':record['kind'],
           'source_kind':'confirmed', 'source_quote':record['source_quote'], 'description':'夹口',
           'comparison':record['comparison']}
    contract=AcceptanceContractV2.model_validate({'objective':objective, 'checks':[check]})
    assert validate_requirement_bindings(objective, contract, requirement_sources=sources)['status']=='bound'


def test_sized_centered_hole_phrase_retains_separate_count_diameter_and_depth():
    text='Create a 60x40x8 mm plate with one centered 6 mm through hole.'
    extracted=extract_requirements(text)
    assert not extracted['unsupported']
    records={r['property']:r for r in extracted['requirements']}
    assert len(records)==8
    assert records['hole_count']['comparison']=={'op':'eq','value':1.,'unit':'count'}
    assert records['hole_diameter']['comparison']=={'op':'eq','value':6.,'unit':'mm'}
    assert records['hole_depth']['comparison']=={'op':'eq','value':8.,'unit':'mm'}
    assert validate_requirement_bindings(text,parsed_contract(text))['status']=='bound'


@pytest.mark.parametrize('text', ['100 mm long, 60 mm wide, 10 mm thick plate',
                                 '长度100mm宽度60mm厚度10mm的板'])
def test_dimension_word_order_does_not_create_extra_obligations(text):
    result = extract_requirements(text)
    assert not result['unsupported']
    assert {r['property']:r['comparison']['value'] for r in result['requirements']} == {
        'length':100., 'width':60., 'thickness':10.}
    assert len(result['requirements']) == 3


def test_explicit_world_axis_is_bound_and_cannot_be_deleted():
    text = '沿世界 Y 轴夹口25 mm'
    contract = parsed_contract(text)
    assert contract.checks[0].scope.axis == (0,1,0)
    assert validate_requirement_bindings(text,contract)['status']=='bound'
    changed = contract.checks[0].model_copy(update={'scope':contract.checks[0].scope.model_copy(update={'axis':None})})
    with pytest.raises(RequirementBindingError, match='scope'):
        validate_requirement_bindings(text,contract.model_copy(update={'checks':(changed,)}))


@pytest.mark.parametrize('text', ['沿局部Y轴夹口25 mm', '沿世界Y轴，夹口25 mm，孔径6 mm'])
def test_unproven_frame_or_ambiguous_axis_never_silently_becomes_relative(text):
    assert extract_requirements(text)['unsupported']


def test_frozen_live_trajectory_has_independent_complete_geometry_obligations():
    import json
    from pathlib import Path
    cases=json.loads((Path(__file__).resolve().parents[2]/'docs/architecture/ci-live-cases.json').read_text())['mainline_cases']
    for case in cases:
        result=extract_requirements(case['objective'])
        assert not result['unsupported'],result
        records={r['property']:r for r in result['requirements']}
        assert set(records)=={'length','width','thickness','hole_count','hole_diameter','hole_position','hole_depth','hole_through'}
        for name in ('length','width','thickness','hole_count','hole_diameter'):
            assert records[name]['comparison']['value']==case['expected'][name]
        assert records['hole_depth']['comparison']['value']==case['expected']['thickness']
        assert validate_requirement_bindings(case['objective'],parsed_contract(case['objective']))['status']=='bound'


def test_local_axis_uses_only_frozen_confirmed_body_identity():
    state={'revision_id':'base', 'selection_context':{'revision_id':'base','parameter_state_sha256':'a'*64,
        'features':[{'feature_id':'confirmed-body','kernel_name':'Body','type':'PartDesign::Body'}]}}
    objective='所选对象沿局部Y轴夹口25 mm'
    contract=parsed_contract(objective,verified_state=state)
    assert validate_requirement_bindings(objective,contract,state)['status']=='bound'
    scope=contract.checks[0].scope
    assert scope.frame=='selected_object' and scope.object_ref.feature_id=='confirmed-body'
    changed=scope.model_copy(update={'object_ref':scope.object_ref.model_copy(update={'kernel_name':'OtherBody'})})
    check=contract.checks[0].model_copy(update={'scope':changed})
    with pytest.raises(RequirementBindingError,match='scope'):
        validate_requirement_bindings(objective,contract.model_copy(update={'checks':(check,)}),state)
    state['selection_context']['revision_id']='stale'
    assert extract_requirements(objective,verified_state=state)['unsupported']
@pytest.mark.parametrize('objective',[
    '将所选对象高度改为3mm','将所选对象沿世界Z轴高度改为3mm',
    '将高度改为3mm，所选对象','沿局部Z轴与世界Z轴高度3mm',
])
def test_selected_or_conflicting_coordinate_scope_cannot_become_global(objective):
    from app.validation.requirement_binding import extract_requirements
    assert extract_requirements(objective)['unsupported']


@pytest.mark.parametrize('objective', [
    '一个孔径6 mm的通孔', '一个孔径6 mm的贯穿孔',
    'one 6 mm through hole', 'a 6 mm through-hole', 'one 6 mm throughhole', '一个贯穿孔', 'one through-hole',
    'Create a 60x40x12 mm plate with one 6 mm through hole.',
])
def test_through_hole_is_a_separate_required_predicate_without_inferred_thickness(objective):
    extracted = extract_requirements(objective)
    assert not extracted['unsupported']
    form = [r for r in extracted['requirements'] if r['kind'] == 'hole_through']
    assert len(form) == 1
    assert form[0]['comparison'] == {'op': 'eq', 'value': 1., 'unit': 'count'}
    assert any(r['kind'] == 'hole_count' and r['comparison'] == {'op':'eq','value':1.,'unit':'count'}
               for r in extracted['requirements'])
    contract = parsed_contract(objective)
    assert validate_requirement_bindings(objective, contract)['status'] == 'bound'
    missing = contract.model_copy(update={'checks': tuple(c for c in contract.checks if c.kind != 'hole_through')})
    with pytest.raises(RequirementBindingError, match='coverage'):
        validate_requirement_bindings(objective, missing)


@pytest.mark.parametrize('objective', [
    'greater than 6 mm diameter hole', 'less than 6 mm diameter hole',
    'greater than diameter 6 mm hole', 'diameter less than 6 mm hole',
    '直径大于6 mm的孔', '小于直径6 mm的孔',
    '> 6 mm diameter hole', '< 6 mm hole',
    'greater than hole diameter 6 mm', '孔直径小于6 mm',
])
def test_every_diameter_word_order_preserves_strict_comparison_rejection(objective):
    extracted = extract_requirements(objective)
    assert any(r['reason'] == 'strict_boundary_unsupported' for r in extracted['unsupported'])
    assert not any(r['kind'] == 'hole_diameter' for r in extracted['requirements'])


@pytest.mark.parametrize('objective,comparison', [
    ('at least 6 mm diameter hole', {'op': 'ge', 'lower': 6., 'unit': 'mm'}),
    ('at most 6 mm diameter hole', {'op': 'le', 'upper': 6., 'unit': 'mm'}),
    ('diameter no less than 6 mm hole', {'op': 'ge', 'lower': 6., 'unit': 'mm'}),
    ('直径不超过6 mm的孔', {'op': 'le', 'upper': 6., 'unit': 'mm'}),
    ('6 to 8 mm diameter hole', {'op': 'range', 'lower': 6., 'upper': 8., 'unit': 'mm'}),
    ('直径6至8 mm的孔', {'op': 'range', 'lower': 6., 'upper': 8., 'unit': 'mm'}),
])
def test_diameter_shortcuts_retain_supported_bounds(objective, comparison):
    extracted = extract_requirements(objective)
    assert not extracted['unsupported']
    assert [r['comparison'] for r in extracted['requirements'] if r['kind'] == 'hole_diameter'] == [comparison]
    assert validate_requirement_bindings(objective, parsed_contract(objective))['status'] == 'bound'


def test_diameter_shortcut_tolerance_retains_authority():
    objective = '直径6±0.1 mm的孔'
    contract = parsed_contract(objective)
    assert contract.checks[0].comparison.tolerance_minus == .1
    assert contract.checks[0].comparison.tolerance_source_refs
    assert validate_requirement_bindings(objective, contract)['status'] == 'bound'


@pytest.mark.parametrize('objective', [
    'greater than approximately 6 mm diameter hole',
    'less than approximately 6 mm diameter hole',
    'more than 6 mm diameter hole',
    'at least approximately 6 mm diameter hole',
])
def test_unbound_comparative_words_cannot_be_dropped_by_a_shorter_quantity_match(objective):
    assert extract_requirements(objective)['unsupported']
    proposed = AcceptanceContractV2.model_validate({'objective':objective, 'checks':[
        {'check_id':'diameter', 'binding_ref':'unproven-shortcut', 'kind':'hole_diameter',
         'description':'hole diameter', 'source_quote':objective, 'comparison':{'op':'eq','value':6.,'unit':'mm'}}]})
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable'):
        validate_requirement_bindings(objective, proposed)


@pytest.mark.parametrize('objective', [
    'a non-through hole with diameter 6 mm',
    'a non-through 6 mm hole',
    'a 6 mm diameter hole, not through',
    'no through hole with diameter 6 mm',
], ids=['non-through', 'non-through-sized', 'not-through', 'no-through'])
def test_negated_through_requirement_cannot_be_ignored_or_reversed(objective):
    assert any(r['reason'] == 'negative_requirement_unbound'
               for r in extract_requirements(objective)['unsupported'])
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable'):
        validate_requirement_bindings(objective, parsed_contract(objective))
