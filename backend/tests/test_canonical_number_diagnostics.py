import pytest
import rfc8785

from app.execution.canonical import canonical_json_bytes


def test_unsafe_integer_reports_field_without_rounding_it():
    value = 2**53 + 1
    with pytest.raises(rfc8785.IntegerDomainError) as error:
        canonical_json_bytes({'payload': {'measurements': [value]}})
    assert '$.payload.measurements[0]' in str(error.value)
    assert str(value) in str(error.value)


def test_large_float_and_safe_integer_keep_existing_canonical_representation():
    assert canonical_json_bytes({'large': 4e100, 'integer': 2**53-1}) == rfc8785.dumps(
        {'large': 4e100, 'integer': 2**53-1})
