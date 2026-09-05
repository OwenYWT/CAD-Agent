"""Pure contract checks for the durable MCAD workflow boundary."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.workflows.temporal import (
    McadAgentWorkflowV2Request,
    McadExecutionRequest,
    McadSourcePreparationRequest,
    McadWorkflowRequest,
)
from uuid import uuid4


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


def test_workflow_accepts_source_preparation_instead_of_prebuilt_code():
    request = McadWorkflowRequest(
        workflow_run_id=uuid4(),
        tenant_id=uuid4(),
        project_id=uuid4(),
        principal_id=uuid4(),
        branch_id=uuid4(),
        expected_base_revision_id=uuid4(),
        objective="创建支架",
        preparation=McadSourcePreparationRequest(
            operation="generate",
            prompt="创建 20 x 10 x 4 mm 支架",
        ),
        require_confirmation=False,
        commit_after_confirmation=False,
    )
    assert request.primary is None
    assert request.preparation is not None


def test_workflow_rejects_missing_or_duplicate_execution_source():
    base = {
        "workflow_run_id": uuid4(),
        "tenant_id": uuid4(),
        "project_id": uuid4(),
        "principal_id": uuid4(),
        "branch_id": uuid4(),
        "expected_base_revision_id": uuid4(),
        "objective": "创建支架",
        "require_confirmation": False,
        "commit_after_confirmation": False,
    }
    with pytest.raises(ValidationError, match="exactly one"):
        McadWorkflowRequest(**base)
    with pytest.raises(ValidationError, match="exactly one"):
        McadWorkflowRequest(
            **base,
            primary=McadExecutionRequest(
                step_key="model",
                kind="mcad_model",
                operation="generate",
                source_code="result = box(1, 1, 1)",
            ),
            preparation=McadSourcePreparationRequest(
                operation="generate",
                prompt="创建支架",
            ),
        )


def test_freecad_modify_uses_revision_state_without_existing_code():
    request = McadAgentWorkflowV2Request(
        workflow_run_id=uuid4(),
        tenant_id=uuid4(),
        project_id=uuid4(),
        principal_id=uuid4(),
        branch_id=uuid4(),
        expected_base_revision_id=uuid4(),
        operation="modify",
        modeling_backend="freecad",
        objective="Set Pad Length to 20 mm",
        output_formats=("step", "stl"),
    )

    assert request.existing_code is None
    assert request.modeling_backend == "freecad"
