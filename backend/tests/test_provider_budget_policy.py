from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from app.contracts.provider_budget import ProviderBudgetError
from app.services.provider_budgets import freeze_policy, prepare_request, receipt_amount


def cost_contract():
    return {'provider': 'moonshot', 'model': 'kimi-k2.7-code', 'currency': 'CNY',
            'task_limit': '5', 'month_limit': '5', 'input_per_million': '6.5',
            'output_per_million': '27', 'input_bound': 262144, 'output_bound': 32768,
            'output_parameter': 'max_tokens', 'reasoning_included_in_output': True,
            'price_version': 'test-contract', 'bound_version': 'test-contract',
            'price_source': 'https://platform.kimi.com/docs/pricing/chat',
            'bound_source': 'https://platform.kimi.com/docs/guide/kimi-k2-7-code-quickstart',
            'valid_until': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}


def test_cost_bounds_must_be_proven_and_frozen():
    with pytest.raises(ProviderBudgetError, match='billing contract'):
        freeze_policy(mode='hard_cost', request_limit=8, month_request_limit=20)
    contract = cost_contract()
    policy = freeze_policy(mode='hard_cost', request_limit=8, month_request_limit=20, cost_contract=contract)
    contract['output_bound'] = 1
    assert policy['cost']['output_bound'] == 32768
    assert policy['reservation'] == '2.588672'
    params = prepare_request(policy, {'model': 'kimi-k2.7-code', 'messages': [],
                                     'max_tokens': 1}, provider='moonshot')
    assert params['max_tokens'] == 32768
    with pytest.raises(ProviderBudgetError):
        prepare_request(policy, {'model': 'different', 'messages': []}, provider='moonshot')
    with pytest.raises(ProviderBudgetError):
        prepare_request(policy, {'model': 'kimi-k2.7-code', 'messages': [],
                                'tools': [{'type': 'builtin_function'}]}, provider='moonshot')


@pytest.mark.parametrize('field,value', [('output_bound', True), ('input_per_million', 'NaN'),
    ('output_per_million', '-1'), ('reasoning_included_in_output', False),
    ('provider', 'unverified'), ('output_parameter', 'max_output_tokens'),
    ('valid_until', '2020-01-01T00:00:00Z')])
def test_unverifiable_contract_is_rejected(field, value):
    contract = cost_contract(); contract[field] = value
    with pytest.raises(ProviderBudgetError):
        freeze_policy(mode='hard_cost', request_limit=8, month_request_limit=20, cost_contract=contract)


def test_receipts_missing_usage_are_not_zero_and_decimal_is_exact():
    policy = freeze_policy(mode='hard_cost', request_limit=8, month_request_limit=20, cost_contract=cost_contract())
    assert receipt_amount(policy, {}) is None
    assert receipt_amount(policy, {'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}) == Decimal('0.0000335')
    assert receipt_amount(policy, {'usage': {'prompt_tokens': True, 'completion_tokens': 1}}) is None
    request_policy = freeze_policy(mode='requests', request_limit=8, month_request_limit=20)
    assert request_policy['reservation'] == '0' and request_policy['cost'] is None
    assert receipt_amount(request_policy, {'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}) is None


@pytest.mark.parametrize('option',['prompt_cache_options','max_completion_tokens','max_output_tokens'])
def test_hard_cost_rejects_unaudited_top_level_billing_options(option):
    policy=freeze_policy(mode='hard_cost',request_limit=8,month_request_limit=20,cost_contract=cost_contract())
    with pytest.raises(ProviderBudgetError,match='Unbounded provider option'):
        prepare_request(policy,{'model':'kimi-k2.7-code','messages':[],option:262144},provider='moonshot')
