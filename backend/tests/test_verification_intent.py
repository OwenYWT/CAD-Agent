"""A model disclaimer is not authority to add or remove a required condition."""
from copy import deepcopy

import pytest

from app.contracts.acceptance_v2 import AcceptanceContractV2, RequirementBindingError
from app.validation import verification_intent as intent
from app.validation.requirement_binding import bind_acceptance_proposal, validate_requirement_bindings
from tests.test_intake_proposal_authority import RECEIPTS, replay_inputs


@pytest.mark.parametrize('text', ['Create a plate 80x40x8 mm', 'plain cylinder diameter 40mm'])
def test_words_starting_with_pla_are_not_a_printing_process(text):
    sources = [{'source_input_id':'objective', 'source_kind':'user', 'text':text}]
    assert not intent.verification_intent(sources)['notices']


def test_saved_fdm_concept_reply_keeps_geometry_and_records_only_sourced_limitations():
    task = RECEIPTS[0]
    objective, sources = replay_inputs(task)
    for call in task['calls']:
        proposal = deepcopy(call['acceptance'])
        proposal['objective'] = objective
        bind_acceptance_proposal(objective, proposal, requirement_sources=sources)
        original_checks = deepcopy(proposal['checks'])
        intent.bind_verification_intent(proposal, sources)
        contract = AcceptanceContractV2.model_validate(proposal)
        assert not contract.verification_limits
        assert contract.model_dump(mode='json')['checks'] == AcceptanceContractV2.model_validate(
            {**proposal, 'checks':original_checks}).model_dump(mode='json')['checks']
        assert [n.category for n in contract.verification_notices] == ['manufacturing_process']
        assert contract.verification_notices[0].source_refs[0].source_quote == 'FDM PLA打印'
        assert validate_requirement_bindings(objective, contract, requirement_sources=sources)['status'] == 'bound'


@pytest.mark.parametrize('scope,blocked', [('cad_geometry', True), ('concept_geometry', False)])
def test_explicit_waterproof_performance_is_never_silently_removed(scope, blocked):
    sources = [{'source_input_id':'objective','source_kind':'user','text':'防水外壳，长90mm', 'execution_scope':scope}]
    result = intent.verification_intent(sources)
    assert bool(result['required_limits']) is blocked
    assert result['notices'][0]['category'] == 'water_protection'
    assert result['notices'][0]['source_refs'][0]['source_quote'] == '防水'


@pytest.mark.parametrize('limit', ['密封槽深度尚不能测量', '防水腔与安装孔之间的距离无法验证',
                                  '未知的必要几何关系无法核验', '防水性能和外壳长度无法验证',
                                  '防水等级及上盖装配结构无法通过实物试验确认', '防水性能和几何尺寸无法确认'])
def test_known_disclaimer_categories_cannot_erase_an_unsupported_geometric_requirement(limit):
    task = RECEIPTS[0]
    _, sources = replay_inputs(task)
    proposal = {'verification_limits':[limit]}
    intent.bind_verification_intent(proposal, sources)
    assert proposal['verification_limits'] == [limit]


def test_explicit_strength_requirement_is_not_replaced_by_an_informational_print_notice():
    sources = [{'source_input_id':'objective','source_kind':'user','text':'FDM打印，成品强度必须满足使用要求'}]
    proposal = {'verification_limits':['成品强度需通过实际打印验证']}
    intent.bind_verification_intent(proposal, sources)
    assert proposal['verification_limits']


@pytest.mark.parametrize('limit', ['Manufacturing performance is not certified.',
                                  'Printing performance has not been verified.',
                                  'Waterproof performance requires physical testing.'])
def test_physical_disclaimer_uses_saved_intent_without_blocking_geometric_scope(limit):
    sources = [{'source_input_id':'objective', 'source_kind':'user',
                'text':'cylinder radius 20mm, height 25mm. ' + limit}]
    proposal = {'verification_limits':[limit]}
    intent.bind_verification_intent(proposal, sources)
    assert not proposal['verification_limits']
    assert proposal['verification_notices'][0]['status'] == 'unverified'
    sources[0]['text'] = 'Waterproof performance is required. ' + limit
    intent.bind_verification_intent(proposal, sources)
    assert proposal['verification_limits']


@pytest.mark.parametrize('limit', ['Manufacturing performance is not certified and the hole path is unverified.',
                                  'Manufacturing performance is not certified. The groove cannot be measured.'])
def test_english_physical_disclaimer_cannot_remove_a_mixed_geometric_limit(limit):
    proposal = {'verification_limits':[limit]}
    intent.bind_verification_intent(proposal, [{'source_input_id':'objective', 'source_kind':'user',
        'text':'cylinder radius 20mm, height 25mm. Manufacturing performance is not certified.'}])
    assert proposal['verification_limits'] == [limit]


def test_omitted_provider_limit_cannot_certify_an_explicit_physical_requirement():
    sources = [{'source_input_id':'objective','source_kind':'user','text':'FDM打印，承重至少2kg'}]
    result = intent.verification_intent(sources)
    assert result['required_limits'] and result['unsupported']
    proposal = {'verification_limits':[]}
    intent.bind_verification_intent(proposal,sources)
    assert proposal['verification_limits']


@pytest.mark.parametrize('limit', [
    '制造性能（承载、材料、工艺可行性）未通过工程分析或实物试验验证',
    '制造性能、承载能力和材料强度不在几何验证范围内。',
    '制造性能、承载能力、材料性能未通过工程分析或实物试验验证，不在本次几何验收范围内。',
    '材料性能尚未验证',
])
def test_chinese_physical_disclaimers_preserve_source_intent_and_geometry(limit):
    sources = [{'source_input_id': 'objective', 'source_kind': 'user',
                'text': 'Evaluate geometry only; manufacturing performance is not certified.'}]
    proposal = {'checks': [{'check_id': 'unchanged-geometry'}], 'verification_limits': [limit]}
    intent.bind_verification_intent(proposal, sources)
    assert proposal['verification_limits'] == []
    assert proposal['checks'] == [{'check_id': 'unchanged-geometry'}]
    assert proposal['verification_notices'][0]['status'] == 'unverified'
    sources[0]['text'] = '材料强度必须符合承重要求。' + limit
    intent.bind_verification_intent(proposal, sources)
    assert proposal['verification_limits']


@pytest.mark.parametrize('limit', [
    '制造性能（承载、材料、孔径）未通过工程分析或实物试验验证',
    '材料性能和密封槽深度尚未验证',
    '制造性能不在几何验证范围内，孔与内腔的隔离尚未验证',
    '制造性能未通过实物测试验证。轴向尺寸无法测量。',
])
def test_chinese_disclaimers_cannot_absorb_unknown_geometric_limits(limit):
    proposal = {'verification_limits': [limit]}
    intent.bind_verification_intent(proposal, [{'source_input_id': 'objective', 'source_kind': 'user',
        'text': 'Evaluate geometry only; manufacturing performance is not certified.'}])
    assert proposal['verification_limits'] == [limit]


def test_sensor_name_is_not_a_requested_temperature_or_load_rating():
    for text in ('温度传感器外壳', '载荷传感器外壳'):
        sources = [{'source_input_id':'objective','source_kind':'user','text':text}]
        assert not intent.verification_intent(sources)['required_limits']


def test_source_notices_are_frozen_and_cannot_be_added_by_a_provider():
    task = RECEIPTS[0]
    objective, sources = replay_inputs(task)
    proposal = deepcopy(task['calls'][-1]['acceptance'])
    proposal['objective'] = objective
    bind_acceptance_proposal(objective, proposal, requirement_sources=sources)
    intent.bind_verification_intent(proposal, sources)
    proposal['verification_notices'][0]['source_refs'][0]['source_quote'] = '概念模型'
    with pytest.raises(RequirementBindingError, match='verification notices'):
        validate_requirement_bindings(objective, AcceptanceContractV2.model_validate(proposal), requirement_sources=sources)
