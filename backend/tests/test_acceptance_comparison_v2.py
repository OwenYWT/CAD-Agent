import pytest
from pydantic import ValidationError

from app.contracts.acceptance_v2 import AcceptanceContractV2, decode_acceptance
from app.contracts.acceptance_comparison import compare_measurement
from app.contracts.acceptance import AcceptanceContract


def contract(comparison, kind='planar_gap', **extra):
    return {'schema_version': 'engineering-acceptance.v2', 'objective': '夹口至少 5 mm',
            'checks': [{'check_id': 'gap', 'kind': kind, 'description': '夹口',
                        'source_quote': '夹口至少 5 mm', 'binding_ref': 'r-0',
                        'comparison': comparison, **extra}]}


@pytest.mark.parametrize('comparison,values,passed', [
    ({'op': 'eq', 'value': 25., 'unit': 'mm'}, [25.], True),
    ({'op': 'eq', 'value': 25., 'unit': 'mm'}, [20.], False),
    ({'op': 'ge', 'lower': 5., 'unit': 'mm'}, [5., 6.], True),
    ({'op': 'ge', 'lower': 5., 'unit': 'mm'}, [6., 4.], False),
    ({'op': 'le', 'upper': 5., 'unit': 'mm'}, [4.], True),
    ({'op': 'range', 'lower': 4., 'upper': 6., 'unit': 'mm'}, [4., 6.], True),
    ({'op': 'range', 'lower': 4., 'upper': 6., 'unit': 'mm'}, [4., 7.], False),
    ({'op': 'eq', 'value': 25., 'unit': 'mm', 'tolerance_minus': .1,
      'tolerance_plus': .2, 'tolerance_source_refs': ['t-1']}, [24.9, 25.2], True),
])
def test_v2_relations_compare_every_measured_value(comparison, values, passed):
    parsed = AcceptanceContractV2.model_validate(contract(comparison))
    result = compare_measurement(parsed.checks[0].comparison.model_dump(mode='json'), values, 1e-7)
    assert result['outcome'] == ('passed' if passed else 'failed')
    assert result['comparison_policy_id'] == parsed.comparison_policy_id


@pytest.mark.parametrize('comparison', [
    {'op': 'ge', 'lower': 5., 'unit': 'mm', 'tolerance_plus': 5.},
    {'op': 'range', 'lower': 6., 'upper': 4., 'unit': 'mm'},
    {'op': 'eq', 'value': 25., 'unit': 'mm', 'tolerance_minus': 5.},
    {'op': 'eq', 'value': float('nan'), 'unit': 'mm'},
    {'op': 'gt', 'lower': 5., 'unit': 'mm'},
])
def test_invalid_or_unbound_comparison_is_rejected(comparison):
    with pytest.raises(ValidationError):
        AcceptanceContractV2.model_validate(contract(comparison))


def test_v1_roundtrip_does_not_gain_comparison_or_new_defaults():
    before = AcceptanceContract.model_validate({'objective': '夹口 25 mm', 'checks': [
        {'check_id': 'gap', 'kind': 'planar_gap', 'description': '夹口',
         'source_quote': '夹口 25 mm', 'nominal': 25.}]})
    encoded = before.model_dump_json()
    after = decode_acceptance(before.model_dump(mode='json'))
    assert after.model_dump_json() == encoded
    assert after.digest() == before.digest()


def test_counts_and_position_residuals_do_not_use_length_comparison_incorrectly():
    with pytest.raises(ValidationError):
        AcceptanceContractV2.model_validate(contract({'op': 'eq', 'value': 2.5, 'unit': 'count'}, 'hole_count'))
    with pytest.raises(ValidationError):
        AcceptanceContractV2.model_validate(contract({'op': 'ge', 'lower': 2., 'unit': 'mm'}, 'hole_position',
            scope={'axis': [0, 0, 1], 'centers_mm': [[0, 0, 0]]}))
    assert compare_measurement({'op': 'ge', 'lower': 5., 'unit': 'mm'}, [], 1e-7)['outcome'] == 'indeterminate'
    assert compare_measurement({'op': 'ge', 'lower': 5., 'unit': 'mm'}, [float('nan')], 1e-7)['outcome'] == 'indeterminate'
