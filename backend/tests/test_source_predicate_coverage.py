"""Semantic expectations are authored separately from the source extractor.

Equivalent wording must retain the same engineering obligations. An unknown
predicate must remain visible even if a dimension in its clause can be parsed.
"""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.contracts.acceptance_v2 import AcceptanceContractV2, RequirementBindingError
from app.domain.requirement_basis import RequirementBasisV1
from app.validation.requirement_binding import (
    bind_acceptance_proposal, extract_source_requirements, require_bound_sources, validate_requirement_bindings,
)
from app.validation.source_obligations import inventory, resolve_inventory
from app.workflows.handlers.planning import requirements_objective, requirements_sources


def proposal_for(text, result, *, sources=None):
    proposal = {'objective': text, 'checks': [
        {'check_id': str(i), 'binding_ref': r['requirement_id'], 'kind': r['kind'],
         'description': r['property'], 'source_quote': r['source_quote'],
         'source_kind': r['source_kind'], 'comparison': r['comparison'], 'scope': r['scope'],
         **({'hole_group': r['hole_group']} if r.get('hole_group') else {})}
        for i, r in enumerate(result['requirements'])]}
    bind_acceptance_proposal(text, proposal, requirement_sources=sources)
    return AcceptanceContractV2.model_validate(proposal)


@pytest.mark.parametrize('phrase', [
    '孔须打通', '孔需打通', '孔要打通', '孔要穿过整块板',
    '孔必须打通', '孔务必打通', '孔需要打通', '孔应打通', '孔应当打通',
    '孔应该打通', '孔打通', '孔打穿', '孔贯通', '孔贯穿', '孔穿透',
    '孔应为通孔', '孔须贯穿整块板', '孔需穿透板厚',
])
@pytest.mark.parametrize('dimensions,diameter', [('40×30×12', 6.), ('70×45×10', 4.)])
def test_f01_predicate_clauses_freeze_throughness_independently_of_wording(phrase, dimensions, diameter):
    text = f'板{dimensions}mm，孔径{diameter:g}mm，{phrase}。'
    result = extract_source_requirements(text)
    assert not result['unsupported'], result
    expected = [('overall_dimension', float(v)) for v in dimensions.split('×')]
    expected += [('hole_diameter', diameter), ('hole_through', 1.),
                 ('hole_depth', float(dimensions.split('×')[-1]))]
    assert Counter((r['kind'], r['comparison'].get('value')) for r in result['requirements']) == Counter(expected)
    contract = proposal_for(text, result)
    assert validate_requirement_bindings(text, contract)['status'] == 'bound'
    omitted = contract.model_copy(update={'checks': tuple(c for c in contract.checks if c.kind != 'hole_through')})
    with pytest.raises(RequirementBindingError, match='coverage'):
        validate_requirement_bindings(text, omitted)


@pytest.mark.parametrize('clause', [
    '孔须钻透', '孔需连到指定位置', '孔要按未知规则处理',
    '孔钻到另一个零件里', '孔形成曲折路径',
    '槽延伸到指定位置', '腔呈未知轮廓', '板形成未定义轮廓',
    '须按未知规则处理', '需遵守未定义工程条件', '要符合未知条件',
    '孔径6mm且须按未知规则处理', '孔径6mm并延伸到别的零件',
    '孔须打通另一块板', '孔要穿过未知对象', '孔要穿过整块板后进入上盖',
    '孔须沿曲折路径打通', '孔不要打通', '孔无需打通', '孔未打通',
    '孔非打通状态', '孔如果需要则打通', '孔应为通孔且沿未知路径延伸',
    'holes must follow an unspecified path', 'hole opens into another component',
    'slots extend into an unspecified region',
])
def test_f01_unknown_predicates_remain_visible_even_without_known_action_words(clause):
    text = f'板40×30×12mm，孔径6mm，{clause}。'
    result = extract_source_requirements(text)
    assert result['unsupported'], result
    assert any(o['status'] == 'unresolved' for o in result['obligations'])
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable') as error:
        require_bound_sources(text)
    assert clause in str(error.value)
    with pytest.raises(RequirementBindingError):
        validate_requirement_bindings(text, proposal_for(text, result))


def test_f01_a_scalar_before_an_unknown_predicate_does_not_cover_the_sentence():
    text = '孔径6mm并按未知规则处理'
    result = extract_source_requirements(text)
    assert ('hole_diameter', 6.) in {(r['kind'], r['comparison'].get('value')) for r in result['requirements']}
    assert result['unsupported']
    assert any(o['source_quote'] == text and o['status'] == 'unresolved' for o in result['obligations'])


@pytest.mark.parametrize('clause', [
    '请让孔钻透', '1、孔钻透', '安装孔钻透', '这些孔钻透',
    '四个孔钻到另一个零件里', 'four holes follow an unspecified path',
    'a hole opens into another component',
])
def test_f01_feature_predicates_cannot_hide_behind_sentence_prefixes(clause):
    text = f'板40×30×12mm，孔径6mm，{clause}。'
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable') as error:
        require_bound_sources(text)
    assert clause in str(error.value)


@pytest.mark.parametrize('clause', [
    '须按未知方式加工孔径6mm', '需按未知方式加工板',
    '要沿未知路径加工孔径6mm', '1、须按未知方式加工孔径6mm',
])
def test_f01_a_recognized_subject_cannot_hide_an_earlier_required_predicate(clause):
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable') as error:
        require_bound_sources(f'板40×30×12mm，{clause}。')
    assert clause in str(error.value)


def test_f01_directive_inventory_has_unique_original_source_identities():
    result = extract_source_requirements('必须孔径6mm')
    assert not result['unsupported']
    identities = [o['obligation_id'] for o in result['obligations']]
    assert len(identities) == len(set(identities))


@pytest.mark.parametrize('clause', [
    '两端开口的圆孔直径6mm', '两端均开口的圆孔直径6mm',
    '双端开口的孔直径6mm', '请创建一个两端开口的圆孔直径6mm',
])
def test_f02_nominal_passage_qualifiers_are_frozen_before_the_subject(clause):
    text = f'板40×30×12mm，{clause}。'
    result = extract_source_requirements(text)
    assert not result['unsupported'], result
    expected = [
        ('overall_dimension', 40.), ('overall_dimension', 30.), ('overall_dimension', 12.),
        ('hole_diameter', 6.), ('hole_through', 1.), ('hole_depth', 12.)]
    if '一个' in clause:
        expected.append(('hole_count', 1.))
    assert Counter((r['kind'], r['comparison'].get('value')) for r in result['requirements']) == Counter(expected)
    contract = proposal_for(text, result)
    assert validate_requirement_bindings(text, contract)['status'] == 'bound'
    omitted = contract.model_copy(update={'checks': tuple(c for c in contract.checks if c.kind != 'hole_through')})
    with pytest.raises(RequirementBindingError):
        validate_requirement_bindings(text, omitted)


@pytest.mark.parametrize('clause', [
    '一端封闭的圆孔直径6mm', '沿未知路径的圆孔直径6mm',
    '带未定义轮廓的槽宽度6mm', '经过另一零件的通孔直径6mm',
    '两端开口且沿曲折路径的圆孔直径6mm', '未知结构圆孔直径6mm',
    '请创建一个沿未知路径的圆孔直径6mm',
    '1、沿未知路径的圆孔直径6mm', '两端开口的板厚度12mm',
    'sealed at one end hole diameter 6mm', 'unknown-profile slot width 6mm',
    'please create a curved-path through hole diameter 6mm',
])
def test_f02_unlowered_nominal_qualifiers_cannot_hide_before_a_known_subject(clause):
    text = f'板40×30×12mm，{clause}。'
    result = extract_source_requirements(text)
    assert result['unsupported'], result
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable') as error:
        require_bound_sources(text)
    assert clause in str(error.value)


@pytest.mark.parametrize('clause', [
    '请创建一个通孔直径6mm', '四个直径6mm的通孔',
    'please create a through hole diameter 6mm', '10mm厚的板',
])
def test_f02_supported_request_prefixes_keep_their_measured_meaning(clause):
    result = extract_source_requirements(clause)
    assert not result['unsupported'], result
    assert validate_requirement_bindings(clause, proposal_for(clause, result))['status'] == 'bound'


def test_f01_an_unspecified_plate_target_cannot_choose_between_named_components():
    text = ('板坐标范围为 X=0 至 40 mm、Y=0 至 30 mm、Z=0 至 12 mm。'
            '盖板坐标范围为 X=50 至 90 mm、Y=0 至 30 mm、Z=0 至 12 mm。'
            '盖板孔径6mm，孔要穿过整块板。')
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable'):
        require_bound_sources(text)


def test_f01_an_external_plate_does_not_authorize_a_passage_on_the_generated_product():
    text = '设计一个夹在12mm桌板上的挂钩，孔径6mm，孔要穿过整块板。'
    sources = [{'source_input_id': 'objective', 'source_kind': 'user', 'text': text,
                'execution_scope': 'concept_geometry'}]
    with pytest.raises(RequirementBindingError, match='engineering_verification_unavailable'):
        require_bound_sources(text, requirement_sources=sources)


@pytest.mark.parametrize('phrase', [
    '通孔', '贯穿孔', '贯穿圆孔', '贯穿圆形孔', '穿透圆孔', '贯穿的圆孔',
    'through hole', 'through-hole', 'through circular hole', 'through round hole',
    'thru hole', 'thru-hole',
])
@pytest.mark.parametrize('diameter', [2.5, 4.6, 12.0])
def test_through_predicate_survives_wording_and_dimension_changes(phrase, diameter):
    text = f'{phrase}，孔径{diameter}mm'
    result = extract_source_requirements(text)
    # This set is the independently stated meaning, not the parser's own list.
    assert not result['unsupported'], result
    assert Counter(r['kind'] for r in result['requirements']) == Counter(
        ['hole_diameter', 'hole_through'])
    through = next(r for r in result['requirements'] if r['kind'] == 'hole_through')
    assert through['comparison'] == {'op': 'eq', 'value': 1., 'unit': 'count'}
    contract = proposal_for(text, result)
    assert validate_requirement_bindings(text, contract)['status'] == 'bound'
    omitted = contract.model_copy(update={'checks': tuple(c for c in contract.checks if c.kind != 'hole_through')})
    with pytest.raises(RequirementBindingError, match='coverage'):
        validate_requirement_bindings(text, omitted)


@pytest.mark.parametrize('text', [
    '四个直径4.6mm的贯穿圆孔', '四个直径4.6mm的穿透圆形孔',
    '4个贯穿圆孔，孔径4.6mm', '4个穿透圆孔，孔径4.6mm',
    'four 4.6mm diameter through circular holes', 'four 4.6mm diameter thru holes',
])
def test_count_diameter_and_throughness_are_separate_obligations(text):
    result = extract_source_requirements(text)
    assert not result['unsupported'], result
    actual = {(r['kind'], r['comparison']['value']) for r in result['requirements']}
    assert actual == {('hole_count', 4.), ('hole_diameter', 4.6), ('hole_through', 1.)}
    assert validate_requirement_bindings(text, proposal_for(text, result))['status'] == 'bound'


@pytest.mark.parametrize('phrase', ['贯穿圆孔', 'through circular hole', 'thru round hole', '4个贯穿圆孔'])
@pytest.mark.parametrize('separator', [', ', '，', '\n'])
def test_an_unqualified_diameter_retains_the_immediately_declared_hole_subject(phrase, separator):
    text = phrase + separator + 'diameter 4.6mm'
    result = extract_source_requirements(text)
    assert not result['unsupported'], result
    assert ('hole_diameter', 4.6) in {(r['kind'], r['comparison']['value']) for r in result['requirements']}
    assert validate_requirement_bindings(text, proposal_for(text, result))['status'] == 'bound'


@pytest.mark.parametrize('text', [
    'through circular hole, flange diameter 4.6mm',
    'through circular hole, another feature, diameter 4.6mm',
    'plate with a through circular hole, diameter 4.6mm',
    '贯穿圆孔，安装耳，直径4.6mm',
])
def test_an_intervening_or_ambiguous_subject_cannot_borrow_hole_dimensions(text):
    result = extract_source_requirements(text)
    assert result['unsupported'], result
    with pytest.raises(RequirementBindingError):
        validate_requirement_bindings(text, proposal_for(text, result))


def test_an_explicit_material_subject_keeps_its_own_dimension():
    result = extract_source_requirements('through circular hole, cylinder diameter 4.6mm')
    assert ('cylinder_diameter', 4.6) in {(r['kind'], r['comparison']['value']) for r in result['requirements']}
    assert not any(r['kind'] == 'hole_diameter' for r in result['requirements'])


@pytest.mark.parametrize('text', [
    '孔径6mm，孔沿曲折路径贯穿', '孔径6mm，孔打穿指定的两个区域',
    '孔径6mm，孔穿透未知对象', '孔径6mm，孔贯通到另一个腔体',
    'diameter 6mm hole passing through the selected cavity',
    'diameter 6mm hole penetrates another component',
    '孔径6mm，贯穿所有隔板', '孔径6mm，打穿全部厚度',
    'through\ncircular hole, diameter 6mm', 'hole, diameter 6mm\nthrough',
])
def test_unlowered_passage_requirement_cannot_disappear(text):
    result = extract_source_requirements(text)
    assert result['unsupported'], result
    assert any(o['category'] == 'feature_semantic' and o['status'] == 'unresolved'
               for o in result['obligations'])
    with pytest.raises(RequirementBindingError):
        validate_requirement_bindings(text, proposal_for(text, result))


@pytest.mark.parametrize('kind', ['hole_diameter', 'hole_count', 'hole_depth', 'overall_dimension'])
def test_another_property_cannot_discharge_the_passage_inventory(kind):
    text = '贯穿圆孔直径4.6mm'
    obligations = inventory(text, source_input_id='objective')
    rows = [{'requirement_id': 'unrelated', 'kind': kind, 'source_span': [0, len(text)],
             'comparison': {'op': 'eq', 'value': 1., 'unit': 'count'}}]
    unsupported = []
    resolve_inventory(obligations, rows, [], unsupported)
    assert any(o['category'] == 'feature_semantic' and o['status'] == 'unresolved' for o in obligations)
    assert unsupported


@pytest.mark.parametrize('comparison', [
    {'op': 'eq', 'value': 0., 'unit': 'count'},
    {'op': 'ge', 'lower': 1., 'unit': 'count'},
    {'op': 'eq', 'value': 1., 'unit': 'mm'},
])
def test_passage_inventory_requires_the_exact_geometric_predicate(comparison):
    text = '贯穿圆孔直径4.6mm'
    obligations = inventory(text, source_input_id='objective')
    rows = [{'requirement_id': 'invalid', 'kind': 'hole_through', 'source_span': [0, len(text)],
             'comparison': comparison}]
    unsupported = []
    resolve_inventory(obligations, rows, [], unsupported)
    assert any(o['category'] == 'feature_semantic' and o['status'] == 'unresolved' for o in obligations)
    assert unsupported


@pytest.mark.parametrize('text', [
    '直径6mm的孔不要贯穿', '直径6mm的非贯穿圆孔', '孔径6mm，避免打穿',
    'diameter 6mm hole, not through', 'diameter 6mm non-through hole',
    'diameter 6mm hole, do not penetrate',
])
def test_negative_passage_requirements_are_not_positive_permissions(text):
    result = extract_source_requirements(text)
    assert result['unsupported']
    with pytest.raises(RequirementBindingError):
        validate_requirement_bindings(text, proposal_for(text, result))


def test_feedback_source_is_distinct_from_historical_task_input():
    fixture = json.loads((Path(__file__).parent/'fixtures/intake_1030_feedback.json').read_text())
    case = next(c for c in fixture['cases'] if c['feedback_number'] == 3)
    assert case['dimensions'] == '外壳总体长度：80 mm\n外壳总体宽度：55 mm\n外壳总体高度：32 mm\n贯穿圆孔直径：4.5 mm'
    basis = RequirementBasisV1(target=case['objective'], dimensions=case['dimensions'],
        source_kind='user_specification', source_reference='用户反馈中的设计尺寸', design_scope='geometry')
    request = SimpleNamespace(objective=case['objective'], output_formats=['step', 'stl'],
                              operation_context=SimpleNamespace(requirement_basis=basis))
    text, sources = requirements_objective(request), requirements_sources(request)
    result = extract_source_requirements(text, requirement_sources=sources)
    assert not result['unsupported'], result
    assert Counter((r['kind'], r['comparison']['value']) for r in result['requirements']) == Counter([
        ('overall_dimension', 80.), ('overall_dimension', 55.), ('overall_dimension', 32.),
        ('hole_diameter', 4.5), ('hole_through', 1.),
    ])
    contract = proposal_for(text, result, sources=sources)
    assert validate_requirement_bindings(text, contract, requirement_sources=sources)['status'] == 'bound'
    historical = json.loads((Path(__file__).parent/'fixtures/intake_1030_receipts.json').read_text())
    saved = next(c for c in historical['tasks'] if c['task_id'] == case['reported_task_id'])
    assert saved['requirement_basis']['dimensions'] != case['dimensions']
    forged = deepcopy(contract.model_dump(mode='json'))
    forged['checks'] = [c for c in forged['checks'] if c['kind'] != 'hole_through']
    with pytest.raises(RequirementBindingError, match='coverage'):
        validate_requirement_bindings(text, AcceptanceContractV2.model_validate(forged), requirement_sources=sources)


@pytest.mark.parametrize('number,quantities', [
    (2, [('overall_dimension', 90.), ('overall_dimension', 60.), ('overall_dimension', 35.)]),
    (4, [('overall_dimension', 90.), ('overall_dimension', 60.), ('overall_dimension', 35.)] * 2),
    (5, [('cylinder_diameter', 40.), ('cylinder_height', 25.)] * 2),
    (6, [('cylinder_radius', 20.), ('cylinder_height', 25.)] * 2),
])
def test_original_feedback_keeps_independently_stated_dimensions(number, quantities):
    fixture = json.loads((Path(__file__).parent/'fixtures/intake_1030_feedback.json').read_text())
    case = next(c for c in fixture['cases'] if c['feedback_number'] == number)
    basis = RequirementBasisV1(target=case['objective'], dimensions=case['dimensions'],
        source_kind='user_specification', source_reference='用户反馈中的设计尺寸', design_scope='geometry')
    request = SimpleNamespace(objective=case['objective'], output_formats=['step', 'stl'],
                              operation_context=SimpleNamespace(requirement_basis=basis))
    text, sources = requirements_objective(request), requirements_sources(request)
    result = extract_source_requirements(text, requirement_sources=sources)
    assert not result['unsupported'], result
    assert Counter((r['kind'], r['comparison']['value']) for r in result['requirements']) == Counter(quantities)
    contract = proposal_for(text, result, sources=sources)
    assert validate_requirement_bindings(text, contract, requirement_sources=sources)['status'] == 'bound'


def test_original_complete_enclosure_is_not_authorized_by_the_simpler_cases():
    fixture = json.loads((Path(__file__).parent/'fixtures/intake_1030_feedback.json').read_text())
    case = next(c for c in fixture['cases'] if c['feedback_number'] == 1)
    text = case['objective'] + '\n' + case['dimensions']
    result = extract_source_requirements(text)
    reasons = {u['reason'] for u in result['unsupported']}
    assert {'negative_requirement_unbound', 'component_scope_unbound',
            'feature_position_scope_unbound', 'physical_performance_not_verified'} <= reasons
    with pytest.raises(RequirementBindingError):
        validate_requirement_bindings(text, proposal_for(text, result))
