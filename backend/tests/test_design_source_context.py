"""Product wording and external dimensions must retain distinct authorities."""
from copy import deepcopy

import pytest

from app.contracts.acceptance_v2 import AcceptanceContractV2, RequirementBindingError
from app.validation.requirement_binding import (
    extract_requirements, extract_source_requirements, validate_requirement_bindings,
)


HOOK = '设计一个可夹在 25 mm 桌板上的耳机挂钩'


def concept_sources(text=HOOK):
    return [{'source_input_id': 'objective', 'source_kind': 'user', 'text': text,
             'execution_scope': 'concept_geometry', 'acceptance_domain': 'solid'}]


def contract_for(text, extraction):
    return AcceptanceContractV2.model_validate({
        'objective': text,
        'checks': [{'check_id': row['requirement_id'], 'binding_ref': row['requirement_id'],
                    'source_kind': row['source_kind'], 'source_quote': row['source_quote'],
                    'kind': row['kind'], 'description': row['property'],
                    'comparison': row['comparison'], 'scope': row['scope']}
                   for row in extraction['requirements']],
        **({'source_context': extraction['source_context']} if extraction.get('source_context') else {}),
    })


@pytest.mark.parametrize('text', [
    '设计一个长80mm、宽40mm、厚8mm的板',
    '创建一个 100×60×3 mm 的板',
    '请帮我设计一个板，长80mm、宽40mm、厚8mm',
    'Create a plate 80x40x8 mm',
    '创建1个长80mm、宽40mm、厚8mm的板，单位：mm',
    '设计一个带2个孔的板，长80mm，孔径6mm',
])
def test_one_design_request_is_not_a_one_solid_requirement(text):
    result = extract_requirements(text)
    assert not result['unsupported'], result
    assert not any(row['kind'] == 'solid_count' for row in result['requirements'])
    requests = result['source_context']['design_requests']
    assert len(requests) == 1 and requests[0]['count'] == 1
    assert requests[0]['delivery_unit'] == 'cad_document'
    assert validate_requirement_bindings(text, contract_for(text, result))['source_context'] == result['source_context']


def test_design_article_does_not_discharge_a_hole_or_unknown_feature_count():
    supported = extract_requirements('设计一个板，长80mm，4个孔，孔径6mm')
    assert not supported['unsupported'], supported
    assert [r['comparison']['value'] for r in supported['requirements'] if r['kind'] == 'hole_count'] == [4.]
    for text in ('设计一个板，长80mm，两个特殊加强筋', '设计两个板，长80mm'):
        assert extract_requirements(text)['unsupported'], text
    solids = extract_requirements('创建两个独立实体')
    assert not solids['unsupported']
    assert solids['requirements'][0]['comparison']['value'] == 2.


@pytest.mark.parametrize('text', [
    '设计一个板上的孔，孔径6mm',
    '设计一个板上的加强筋，板长80mm',
    '设计一个加强筋放在板上，板长80mm',
    '设计一个板状加强筋，长80mm',
    'Design one rib on a plate, length 80mm',
], ids=['hole-in-plate', 'rib-on-plate', 'rib-at-plate', 'plate-shaped-rib', 'english-rib'])
def test_delivery_count_cannot_use_a_modifier_or_location_as_the_requested_product(text):
    result = extract_requirements(text)
    assert not result.get('source_context', {}).get('design_requests'), result
    assert any(r['reason'] == 'count_property_unbound' for r in result['unsupported']), result


@pytest.mark.parametrize('text', [
    HOOK,
    '制作一个夹在2.5厘米厚的桌板上的挂钩',
    '设计一个耳机挂钩，夹在厚度为25毫米的桌板上',
    'Design a headphone hook that clamps to a 25 mm desktop',
])
def test_plate_is_an_external_input_and_clearance_is_a_derived_geometry_check(text):
    sources = concept_sources(text)
    result = extract_source_requirements(text, requirement_sources=sources)
    assert not result['unsupported'], result
    assert len(result['requirements']) == 1
    row = result['requirements'][0]
    assert row['kind'] == 'planar_gap'
    assert row['comparison'] == {'op': 'ge', 'lower': 25., 'unit': 'mm'}
    assert row['scope'] == {}
    context = result['source_context']
    assert context['execution_scope'] == 'concept_geometry'
    assert context['unverified_aspects'] == ['physical_fit', 'clamping_force']
    reference = context['external_references'][0]
    assert reference['property'] == 'plate_thickness'
    assert reference['comparison'] == {'op': 'eq', 'value': 25., 'unit': 'mm'}
    assert row['derivation']['source_refs'] == [reference['reference_id']]
    contract = contract_for(text, result)
    binding = validate_requirement_bindings(text, contract, requirement_sources=sources)
    assert binding['source_context'] == context


def test_external_fit_is_not_silently_accepted_without_concept_confirmation():
    result = extract_requirements(HOOK)
    assert any(item['reason'] == 'external_fit_confirmation_required' for item in result['unsupported'])


def test_concept_confirmation_cannot_hide_other_unbound_requirements():
    for suffix in ('，两个支脚', '，孔径大于6mm', '，圆角半径2mm'):
        text = HOOK + suffix
        assert extract_source_requirements(text, requirement_sources=concept_sources(text))['unsupported']


def test_external_dimension_and_derived_predicate_cannot_be_changed_or_omitted():
    result = extract_source_requirements(HOOK, requirement_sources=concept_sources())
    assert not result['unsupported'], result
    contract = contract_for(HOOK, result)
    for mutate in ('context', 'clearance', 'relation', 'missing_context'):
        raw = deepcopy(contract.model_dump(mode='json'))
        if mutate == 'context':
            raw['source_context']['external_references'][0]['comparison']['value'] = 20.
        elif mutate == 'clearance':
            raw['checks'][0]['comparison']['lower'] = 20.
        elif mutate == 'relation':
            raw['checks'][0]['comparison'] = {'op': 'eq', 'value': 25., 'unit': 'mm'}
        else:
            raw.pop('source_context')
        with pytest.raises(RequirementBindingError):
            validate_requirement_bindings(HOOK, AcceptanceContractV2.model_validate(raw), requirement_sources=concept_sources())


def test_no_context_preserves_the_existing_v2_wire_and_digest():
    result = extract_requirements('夹口25mm')
    contract = contract_for('夹口25mm', result)
    assert 'source_context' not in contract.model_dump(mode='json')
    # Computed by the unchanged contract class from main 0680fbf1.
    assert contract.digest() == 'da9b8a05f446678ad3632893bd2234e19ce427c86a137cdfba6607016010e0ea'


@pytest.mark.parametrize('operation', ['generate', 'modify'])
@pytest.mark.asyncio
async def test_unbindable_solid_input_fails_before_any_model_dispatch(operation):
    from app.agent.planner import Planner
    class NoDispatchPlanner(Planner):
        @property
        def client(self):
            pytest.fail('a deterministic source error must be found before model dispatch')
    text = '设计一个板，长80mm，两个特殊加强筋'
    options = dict(require_acceptance=True, acceptance_version='engineering-acceptance.v2',
                   requirement_sources=concept_sources(text))
    planner = NoDispatchPlanner()
    with pytest.raises(RequirementBindingError, match='两个特殊加强筋'):
        if operation == 'generate':
            await planner.plan_new([{'role': 'user', 'content': text}], **options)
        else:
            await planner.plan_modification([{'role': 'user', 'content': text}], '{}', **options)


def test_saved_consent_and_output_domain_reach_the_source_binder():
    from types import SimpleNamespace
    from app.domain.requirement_basis import RequirementBasisV1
    from app.workflows.handlers.planning import requirements_objective, requirements_sources
    basis = RequirementBasisV1(target=HOOK, design_scope='physical_fit', dimensions=HOOK,
                               concept_acknowledged=True)
    request = SimpleNamespace(objective=HOOK, output_formats=('step', 'stl'),
                              operation_context=SimpleNamespace(requirement_basis=basis))
    sources = requirements_sources(request)
    assert len(sources) == 1 and sources[0]['execution_scope'] == 'concept_geometry'
    assert sources[0]['acceptance_domain'] == 'solid'
    result = extract_source_requirements(requirements_objective(request), requirement_sources=sources)
    assert not result['unsupported'], result
    request.output_formats = ('dxf', 'svg')
    assert requirements_sources(request)[0]['acceptance_domain'] == 'profile'


def test_external_material_input_does_not_masquerade_as_hook_material_thickness():
    from app.validation.acceptance_preflight import validate_new_acceptance
    text = '设计一个耳机挂钩，夹在板厚25mm的桌板上'
    result = extract_source_requirements(text, requirement_sources=concept_sources(text))
    assert not result['unsupported'], result
    contract = contract_for(text, result)
    validate_new_acceptance(contract)
    assert validate_requirement_bindings(text, contract, requirement_sources=concept_sources(text))['status'] == 'bound'


def test_workflow_error_keeps_one_code_and_the_original_diagnostic():
    from app.workflows.errors import planning_error
    error = planning_error(RequirementBindingError('engineering_verification_unavailable', '孔中心距尚不能核验'))
    assert error.message == '孔中心距尚不能核验'
    assert error.type == 'engineering_verification_unavailable' and error.non_retryable


@pytest.mark.parametrize('text', [
    '设计一个可夹在桌板（厚25mm）上的耳机挂钩',
    '设计一个可夹在厚25mm的桌板上的耳机挂钩',
    '设计一个用于厚度25mm桌板的夹具',
    'Design a hook for a desktop of thickness 25mm',
])
def test_unrecognized_external_word_order_never_becomes_the_generated_part_thickness(text):
    result = extract_source_requirements(text, requirement_sources=concept_sources(text))
    assert result['unsupported'] or all(r['kind'] != 'overall_dimension' for r in result['requirements']), result


@pytest.mark.parametrize('quantity,minimum', [('2.5厘米', 25), ('0.025m', 25),
    ('20至25mm', 25), ('25±0.2mm', 25.2), ('不超过25mm', 25)])
def test_external_unit_range_tolerance_and_bound_retain_the_input(quantity, minimum):
    text = '设计一个夹在' + quantity + '桌板上的挂钩'
    result = extract_source_requirements(text, requirement_sources=concept_sources(text))
    assert not result['unsupported'], result
    assert result['requirements'][0]['comparison'] == {'op': 'ge', 'lower': minimum, 'unit': 'mm'}
    assert validate_requirement_bindings(text, contract_for(text, result), requirement_sources=concept_sources(text))['status'] == 'bound'


@pytest.mark.parametrize('quantity', ['至少25mm', '大于25mm', '0mm', '-25mm', '25mm³'])
def test_external_unbounded_strict_or_nonlength_input_is_not_silently_reinterpreted(quantity):
    text = '设计一个夹在' + quantity + '桌板上的挂钩'
    assert extract_source_requirements(text, requirement_sources=concept_sources(text))['unsupported']
