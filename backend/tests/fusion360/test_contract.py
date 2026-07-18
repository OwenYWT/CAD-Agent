import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.fusion360.contract import (
    ACTION_NAMES,
    CAD_ACTION_ADAPTER,
    ContextRequest,
    DimensionValue,
    VerifyRequest,
)
from app.fusion360.errors import ERROR_SPECS, FusionConnectorError


ROOT = Path(__file__).resolve().parents[3]


def parameter_action(**overrides):
    value = {
        "action": "cad.update_parameter",
        "target": {"document_id": "doc", "component_id": "component", "parameter_id": "parameter"},
        "value": {"amount": 3, "unit": "mm"},
    }
    value.update(overrides)
    return value


def test_all_normative_action_names_are_discriminated():
    assert len(ACTION_NAMES) == 11
    parsed = CAD_ACTION_ADAPTER.validate_python(parameter_action())
    assert parsed.action == "cad.update_parameter"


def test_dimension_expression_and_amount_are_mutually_exclusive():
    with pytest.raises(ValidationError):
        CAD_ACTION_ADAPTER.validate_python(parameter_action(value={"amount": 3, "unit": "mm", "expression": "3 mm"}))
    with pytest.raises(ValidationError):
        CAD_ACTION_ADAPTER.validate_python(parameter_action(value={"amount": 3}))


def test_dimension_mutual_exclusion_is_present_in_public_json_schema():
    schema = DimensionValue.model_json_schema()
    assert schema["oneOf"] == [
        {
            "required": ["expression"],
            "not": {"anyOf": [{"required": ["amount"]}, {"required": ["unit"]}]},
        },
        {"required": ["amount", "unit"], "not": {"required": ["expression"]}},
    ]


def test_unknown_and_arbitrary_code_fields_are_rejected():
    with pytest.raises(ValidationError):
        CAD_ACTION_ADAPTER.validate_python({**parameter_action(), "code": "import os"})
    with pytest.raises(ValidationError):
        CAD_ACTION_ADAPTER.validate_python({"action": "cad.run_python", "source": "pass"})


def test_export_options_are_format_specific_and_filename_is_confined():
    common = {"action": "cad.export", "target": {"document_id": "doc"}, "format": "dxf", "filename": "part.dxf"}
    with pytest.raises(ValidationError):
        CAD_ACTION_ADAPTER.validate_python(common)
    with pytest.raises(ValidationError):
        CAD_ACTION_ADAPTER.validate_python({**common, "filename": "../part.dxf", "options": {"sketch_id": "s"}})
    assert CAD_ACTION_ADAPTER.validate_python({**common, "options": {"sketch_id": "s"}}).format == "dxf"
    with pytest.raises(ValidationError):
        CAD_ACTION_ADAPTER.validate_python({
            "action": "cad.export", "target": {"document_id": "doc"},
            "format": "step", "filename": "part.stl",
        })
    assert CAD_ACTION_ADAPTER.validate_python({
        "action": "cad.export", "target": {"document_id": "doc"},
        "format": "step", "filename": "part.STP",
    }).filename == "part.STP"


def test_verify_reference_requirements_are_enforced():
    with pytest.raises(ValidationError):
        VerifyRequest.model_validate({"specification": {"document_id": "d", "checks": [{"check": "no_new_feature_errors"}]}})
    value = VerifyRequest.model_validate({
        "specification": {
            "document_id": "d",
            "baseline_request_id": "2144d1f8-bfe1-43da-b3c7-8b444bd77e66",
            "checks": [{"check": "no_new_feature_errors"}],
        }
    })
    assert value.specification.checks[0].check == "no_new_feature_errors"


def test_context_unknown_sections_and_extra_fields_fail():
    with pytest.raises(ValidationError):
        ContextRequest.model_validate({"query": {"sections": ["everything"]}})
    with pytest.raises(ValidationError):
        ContextRequest.model_validate({"query": {}, "source": "python"})


def test_error_table_has_exact_stable_behavior_for_security_sensitive_codes():
    expected = {
        "PROTOCOL_MISMATCH": ("validation", False, 426),
        "STALE_LEASE": ("conflict", False, 409),
        "OWNER_MISMATCH": ("not_found", False, 404),
        "QUEUE_FULL": ("rate_limit", True, 429),
        "CREDENTIAL_ROLE_MISMATCH": ("auth", False, 403),
        "ARTIFACT_QUOTA_EXCEEDED": ("artifact", True, 507),
    }
    for code, triple in expected.items():
        spec = ERROR_SPECS[code]
        assert (spec.category, spec.retryable, spec.http_status) == triple
        error = FusionConnectorError(code, "safe").to_model()
        assert (error.category, error.retryable) == triple[:2]


def test_all_error_specs_are_serializable_and_no_traceback_leaks():
    for code, spec in ERROR_SPECS.items():
        error = FusionConnectorError(code, "safe", details={"traceback": "secret", "reason": "ok"})
        assert error.http_status == spec.http_status
        assert error.to_model().model_dump(mode="json")["details"] == {"reason": "ok"}


def test_error_details_recursively_remove_credentials_and_validation_input():
    error = FusionConnectorError(
        "FUSION_API_ERROR",
        "safe",
        details={
            "nested": {"access_token": "leak", "password": "leak", "reason": "safe"},
            "items": [{"authorization": "leak", "code": "safe"}],
        },
    ).to_model()
    assert error.details == {"nested": {"reason": "safe"}, "items": [{"code": "safe"}]}

    with pytest.raises(ValidationError) as raised:
        CAD_ACTION_ADAPTER.validate_python({**parameter_action(), "connector_secret": "must-not-reflect"})
    from app.fusion360.errors import map_exception
    mapped = map_exception(raised.value).to_model().model_dump(mode="json")
    assert "must-not-reflect" not in json.dumps(mapped)


def test_generated_schemas_are_current_and_versioned():
    result = subprocess.run(
        [sys.executable, "scripts/fusion360/generate_schema.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    documents = list((ROOT / "schemas" / "fusion360").glob("*.schema.json"))
    assert len(documents) >= 10
    for path in documents:
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["x-contract-version"] == "1.0.0"
        assert schema["$id"].endswith("/1.0.0")
