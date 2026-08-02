from uuid import uuid4

from app.workflows.temporal import (
    McadCheckRequest,
    temporal_check_workflow_id,
)


# Regression: QA ISSUE-005 — /api/analyze was not a durable Temporal workflow
def test_durable_check_contract_is_explicit_and_has_a_distinct_temporal_id():
    workflow_id = uuid4()
    request = McadCheckRequest(
        workflow_run_id=workflow_id,
        source_workflow_run_id=uuid4(),
        source_revision_id=uuid4(),
        tenant_id=uuid4(),
        project_id=uuid4(),
        principal_id=uuid4(),
        code="result = 1",
        description="工程检查",
        process="cnc_milling",
        material="aluminum",
    )

    assert request.temporal_payload()["source_revision_id"] == str(
        request.source_revision_id
    )
    assert temporal_check_workflow_id(workflow_id) == (
        f"mcad-check-workflow-{workflow_id}"
    )
