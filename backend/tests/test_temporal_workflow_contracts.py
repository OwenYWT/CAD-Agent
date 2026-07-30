"""Pure contract checks for the durable MCAD workflow boundary."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.workflows.temporal import McadExecutionRequest


def test_execution_request_derives_real_output_contracts_from_mode():
    request = McadExecutionRequest(
        step_key="model",
        kind="mcad_model",
        operation="generate",
        mode="3d",
        source_code="result = cq.Workplane('XY').box(1, 1, 1)",
    )
    assert [(item.name, item.media_type) for item in request.outputs] == [
        ("step", "model/step"),
        ("stl", "model/stl"),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("step_key", "../model"),
        ("step_key", "model/result"),
        ("kind", "MCAD MODEL"),
        ("capability", "mcad local"),
        ("operation", "generate model"),
    ],
)
def test_execution_identifiers_cannot_escape_artifact_or_event_names(
    field,
    value,
):
    values = {
        "step_key": "model",
        "kind": "mcad_model",
        "capability": "mcad.local",
        "operation": "generate",
        "mode": "3d",
        "source_code": "result = cq.Workplane('XY').box(1, 1, 1)",
    }
    values[field] = value
    with pytest.raises(ValidationError):
        McadExecutionRequest(**values)
