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
