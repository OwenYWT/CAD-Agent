"""Numerical CAD comparison, shared verbatim with the isolated verifier.

Numerical precision is measured by the kernel, never a model-provided percentage.
This is geometric consistency, not a manufacturing/strict physical guarantee.
"""
import math

COMPARISON_POLICY_ID = 'occt-confusion-v1'


def compare_measurement(comparison: dict, measured, numerical_tolerance: float,
                        *, policy_id: str = COMPARISON_POLICY_ID) -> dict:
    if policy_id != COMPARISON_POLICY_ID:
        raise ValueError('unsupported numerical comparison policy')
    if not math.isfinite(numerical_tolerance) or numerical_tolerance < 0:
        raise ValueError('invalid kernel numerical tolerance')
    epsilon = 0.0 if comparison['unit'] == 'count' else numerical_tolerance
    op = comparison['op']
    minus = comparison.get('tolerance_minus', 0) or 0
    plus = comparison.get('tolerance_plus', 0) or 0
    if op == 'eq':
        lower, upper = comparison['value'] - minus, comparison['value'] + plus
    elif op == 'ge':
        lower, upper = comparison['lower'], None
    elif op == 'le':
        lower, upper = None, comparison['upper']
    elif op == 'range':
        lower, upper = comparison['lower'], comparison['upper']
    else:
        raise ValueError('unsupported comparison relation')
    outcome = 'indeterminate'
    if measured and all(math.isfinite(v) for v in measured):
        outcome = 'passed' if all(
            (lower is None or v >= lower - epsilon) and
            (upper is None or v <= upper + epsilon) for v in measured) else 'failed'
    return {'outcome': outcome, 'comparison_policy_id': policy_id,
            'verification_scope': 'cad_geometry', 'comparison': comparison,
            'numerical_tolerance': epsilon,
            'engineering_tolerance': {'minus': minus, 'plus': plus},
            'evaluated_values': len(measured), 'all_values_required': True}


def compare_feature_measurement(check: dict, measured, numerical_tolerance: float,
                                *, feature_count=None, policy_id=COMPARISON_POLICY_ID) -> dict:
    """Keep observed absence distinct from a missing/failed measurement.

    The verifier supplies cardinality only after complete feature enumeration
    in the bound scope. No zero diameter/depth is fabricated for an absent
    feature. Both the isolated worker and the receiver apply this same rule.
    """
    result = compare_measurement(check['comparison'], measured, numerical_tolerance, policy_id=policy_id)
    if feature_count is None:
        return result
    if (type(feature_count) is not int or feature_count < 0
            or check['kind'] not in {'hole_diameter', 'hole_depth', 'hole_through', 'hole_position'}):
        raise ValueError('invalid measured feature cardinality')
    result['feature_count'] = feature_count
    if check['kind'] == 'hole_position':
        expected = len(check['scope']['centers_mm'])
        if feature_count != expected:
            result.update(outcome='failed', feature_failure='position_count_mismatch')
    else:
        if measured and len(measured) != feature_count:
            raise ValueError('feature cardinality differs from measured values')
        if feature_count == 0:
            result.update(outcome='failed', feature_failure='required_feature_absent')
    return result
