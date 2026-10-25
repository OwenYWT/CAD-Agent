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


def test_component_selection_is_explicit_and_preserves_old_contract_wire():
    from app.contracts.acceptance_v2 import MeasurementScopeV2
    original = AcceptanceContractV2.model_validate(contract({'op':'eq','value':5.,'unit':'mm'}))
    assert 'component_region' not in original.model_dump(mode='json')['checks'][0]['scope']
    assert AcceptanceContractV2.model_validate(original.model_dump(mode='json')).digest()==original.digest()
    region = {'min_mm':[0,0,0], 'max_mm':[80,55,32]}
    assert MeasurementScopeV2(component_region=region).component_region.max_mm==(80.,55.,32.)
    for invalid in ({'frame':'bounds_center','component_region':region},
                    {'component_region':{'min_mm':[0,0,0],'max_mm':[0,55,32]}}):
        with pytest.raises(ValidationError):
            MeasurementScopeV2.model_validate(invalid)
    with pytest.raises(ValidationError,match='bounding_box'):
        AcceptanceContractV2.model_validate(contract({'op':'eq','value':0.,'unit':'mm'},'bounding_box'))


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


@pytest.mark.parametrize('comparison', [
    {'op':'eq','value':0.,'unit':'count'}, {'op':'ge','lower':1.,'unit':'count'},
    {'op':'eq','value':1.,'unit':'mm'},
    {'op':'eq','value':1.,'unit':'count','tolerance_minus':1.,'tolerance_source_refs':['invented']},
])
def test_through_hole_topology_cannot_be_weakened_by_a_numeric_bound_or_tolerance(comparison):
    with pytest.raises(ValidationError):
        AcceptanceContractV2.model_validate(contract(comparison, 'hole_through'))


@pytest.mark.parametrize('kind', ['hole_diameter', 'hole_depth', 'hole_through', 'hole_position'])
def test_missing_features_are_explicit_failures_without_invented_measurements(kind):
    from types import SimpleNamespace
    from app.contracts.acceptance_comparison import compare_feature_measurement
    from app.contracts.acceptance import AcceptanceMeasurements, MeasurementEvidence
    from app.validation.durable_geometry import verify_geometry_evidence
    from app.contracts.measurement_binding import bind_measurement_scope
    comparison = {'op': 'eq', 'value': 1. if kind == 'hole_through' else 0. if kind == 'hole_position' else 6.,
                  'unit': 'count' if kind == 'hole_through' else 'mm'}
    extra = {'scope': {'axis': [0, 0, 1], 'centers_mm': [[0, 0, 0]]}} if kind == 'hole_position' else {}
    acceptance = AcceptanceContractV2.model_validate(contract(comparison, kind, **extra))
    check = acceptance.checks[0].model_dump(mode='json')
    result = compare_feature_measurement(check, [], 1e-7, feature_count=0)
    assert result['outcome'] == 'failed' and result['evaluated_values'] == 0
    assert result['feature_count'] == 0
    assert compare_feature_measurement(check, [], 1e-7)['outcome'] == 'indeterminate'
    from tests.test_hole_populations import inventory
    from app.contracts.hole_populations import digest
    empty = inventory([])
    result.update(hole_inventory=empty, hole_inventory_sha256=digest(empty), numerical_tolerance_mm=1e-7)
    evidence = MeasurementEvidence(check_id=check['check_id'], outcome=result.pop('outcome'),
        method='native-feature-enumeration', measured=(), details=result)
    context = {'schema_version':'measurement-context.v1', 'artifact_sha256s':['b'*64],
               'base_revision_id':'base', 'checkpoint_manifest_id':'checkpoint'}
    binding = bind_measurement_scope(check, acceptance_digest=acceptance.digest(), context=context,
        artifact_sha256='b'*64, resolved_scope=check['scope'], frame_origin=[0,0,0],
        evidence=evidence.model_dump(mode='json'))
    assert binding['status'] == 'bound'
    evidence = evidence.model_copy(update={'details': {**result, 'measurement_binding': binding}})
    report = SimpleNamespace(request_sha256='a'*64, acceptance_contract=acceptance, outcome='failed',
        artifacts=[SimpleNamespace(sha256='b'*64, format='step')],
        acceptance=AcceptanceMeasurements(contract_sha256=acceptance.digest(), evidence=(evidence,)))
    verify_geometry_evidence(report, request_sha256='a'*64, acceptance=acceptance, expected_solid_count=None,
                             measurement_context=context)
    report.acceptance = report.acceptance.model_copy(update={'evidence': (evidence.model_copy(update={'outcome': 'passed'}),)})
    with pytest.raises(ValueError, match='outcome'):
        verify_geometry_evidence(report, request_sha256='a'*64, acceptance=acceptance, expected_solid_count=None,
                                 measurement_context=context)


def test_feature_cardinality_cannot_disagree_with_measurements_or_be_invented():
    from app.contracts.acceptance_comparison import compare_feature_measurement
    check = AcceptanceContractV2.model_validate(contract({'op':'eq','value':6.,'unit':'mm'}, 'hole_diameter')).checks[0].model_dump(mode='json')
    for count in (-1, True, 1.5):
        with pytest.raises(ValueError):
            compare_feature_measurement(check, [], 1e-7, feature_count=count)
    with pytest.raises(ValueError):
        compare_feature_measurement(check, [6.], 1e-7, feature_count=0)
    assert compare_feature_measurement(check, [6., 5.], 1e-7, feature_count=2)['outcome'] == 'failed'
